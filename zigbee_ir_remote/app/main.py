"""Zigbee IR Remote - Home Assistant app backend.

Talks to Home Assistant over its websocket API (via the Supervisor proxy when
running as an app) to:
  * discover Zigbee2MQTT IR blasters (devices exposing `learned_ir_code`)
  * put a blaster into learning mode and capture the learned code
  * send codes (`ir_code_to_send`) through the `mqtt.publish` service
  * optionally publish every control as an MQTT discovery button entity, or, for
    "config" devices (ACs whose remote sends the full state on every press),
    one select entity listing the device's learned configs
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web

LOG = logging.getLogger("irremote")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).parent / "static"
PORT = int(os.environ.get("PORT", "8099"))

DEFAULT_OPTIONS = {
    "z2m_base_topic": "zigbee2mqtt",
    "learn_timeout": 30,
    "expose_buttons": True,
    "discovery_prefix": "homeassistant",
}
INVALID_STATES = {None, "", "unknown", "unavailable", "None"}
# Messages arriving this soon after we enable learning are the blaster's
# acknowledgement (still carrying the previous code), never a fresh capture.
ACK_WINDOW = 2.0
# "buttons": every control is its own HA button.
# "configs": every control is a full AC state; HA gets one select to choose between them.
DEVICE_KINDS = ("buttons", "configs")


def load_options():
    opts = dict(DEFAULT_OPTIONS)
    path = DATA_DIR / "options.json"
    if path.exists():
        try:
            opts.update(json.loads(path.read_text()))
        except ValueError:
            LOG.exception("Could not parse %s", path)
    opts["z2m_base_topic"] = opts["z2m_base_topic"].strip("/")
    opts["discovery_prefix"] = opts["discovery_prefix"].strip("/")
    return opts


def new_id():
    return secrets.token_hex(4)


def slugify(text):
    text = unicodedata.normalize("NFKD", str(text).replace("ı", "i")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "ir"


class HAError(Exception):
    pass


class HAClient:
    """Minimal Home Assistant websocket client with reconnect + subscriptions."""

    def __init__(self, ws_url, token):
        self.ws_url = ws_url
        self.token = token
        self.connected = asyncio.Event()
        self._ws = None
        self._id = 0
        self._pending = {}
        self._subs = {}
        self.last_error = None

    async def run(self):
        async with ClientSession() as session:
            while True:
                try:
                    async with session.ws_connect(self.ws_url, heartbeat=30, max_msg_size=0) as ws:
                        await ws.receive_json()  # auth_required
                        await ws.send_json({"type": "auth", "access_token": self.token})
                        msg = await ws.receive_json()
                        if msg.get("type") != "auth_ok":
                            raise HAError(f"Authentication failed: {msg}")
                        self._ws = ws
                        self.last_error = None
                        self.connected.set()
                        LOG.info("Connected to Home Assistant (%s)", msg.get("ha_version"))
                        async for m in ws:
                            if m.type == WSMsgType.TEXT:
                                self._dispatch(json.loads(m.data))
                            elif m.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                                break
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - keep reconnecting
                    self.last_error = str(err)
                    LOG.warning("Home Assistant connection error: %s", err)
                finally:
                    self._ws = None
                    self.connected.clear()
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.set_exception(HAError("Disconnected from Home Assistant"))
                    self._pending.clear()
                    subs, self._subs = self._subs, {}
                    for cb in subs.values():
                        cb(None)
                await asyncio.sleep(5)

    def _dispatch(self, msg):
        if isinstance(msg, list):
            for m in msg:
                self._dispatch(m)
            return
        mid = msg.get("id")
        if msg.get("type") == "result":
            fut = self._pending.pop(mid, None)
            if fut and not fut.done():
                if msg.get("success"):
                    fut.set_result(msg.get("result"))
                else:
                    err = msg.get("error") or {}
                    fut.set_exception(HAError(err.get("message") or str(err)))
        elif msg.get("type") == "event":
            cb = self._subs.get(mid)
            if cb:
                cb(msg.get("event"))

    async def call(self, payload, callback=None, timeout=15):
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
        except asyncio.TimeoutError:
            raise HAError(f"Not connected to Home Assistant ({self.last_error or 'connecting'})") from None
        self._id += 1
        mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        if callback:
            self._subs[mid] = callback
        try:
            await self._ws.send_json({**payload, "id": mid})
            await asyncio.wait_for(fut, timeout)
        except BaseException:
            self._pending.pop(mid, None)
            self._subs.pop(mid, None)
            raise
        return mid if callback else fut.result()

    async def unsubscribe(self, sub_id):
        if self._subs.pop(sub_id, None) is None:
            return
        try:
            await self.call({"type": "unsubscribe_events", "subscription": sub_id}, timeout=5)
        except Exception:  # noqa: BLE001 - best effort
            pass

    async def mqtt_publish(self, topic, payload, retain=False):
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        await self.call({
            "type": "call_service",
            "domain": "mqtt",
            "service": "publish",
            "service_data": {"topic": topic, "payload": payload, "retain": retain},
        })


class Store:
    """JSON-file persistence for devices, controls and blaster overrides."""

    def __init__(self, path):
        self.path = path
        self.data = {"devices": [], "blasters": {}, "published": []}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except ValueError:
                LOG.exception("Could not parse %s, starting empty", path)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    def device(self, dev_id):
        for d in self.data["devices"]:
            if d["id"] == dev_id:
                return d
        raise web.HTTPNotFound(text="Device not found")

    def control(self, dev_id, ctl_id):
        dev = self.device(dev_id)
        for c in dev["controls"]:
            if c["id"] == ctl_id:
                return dev, c
        raise web.HTTPNotFound(text="Control not found")


class LearnCancelled(Exception):
    pass


class IRRemote:
    def __init__(self, ha, store, options):
        self.ha = ha
        self.store = store
        self.opts = options
        self._blaster_cache = (0.0, [])
        self.learning = {}  # blaster_id -> {"cancel": Event, "device": .., "control": ..}
        self._sync_lock = asyncio.Lock()

    # ---------- blasters ----------
    async def blasters(self, force=False):
        ts, cached = self._blaster_cache
        if not force and time.monotonic() - ts < 10:
            return cached
        ents, devs = await asyncio.gather(
            self.ha.call({"type": "config/entity_registry/list"}),
            self.ha.call({"type": "config/device_registry/list"}),
        )
        devmap = {d["id"]: d for d in devs}
        found = {}
        for ent in ents:
            uid = ent.get("unique_id") or ""
            eid = ent["entity_id"]
            if "learned_ir_code" not in uid and "learned_ir_code" not in eid:
                continue
            did = ent.get("device_id")
            if not did or did in found:
                continue
            dev = devmap.get(did, {})
            z2m_name = dev.get("name") or eid.split(".", 1)[1].rsplit("_learned_ir_code", 1)[0]
            found[did] = {
                "id": did,
                "name": dev.get("name_by_user") or z2m_name,
                "z2m_name": z2m_name,
                "model": " ".join(filter(None, [dev.get("manufacturer"), dev.get("model")])),
                "learned_entity": eid,
                "manual": False,
            }
        overrides = self.store.data["blasters"]
        result = []
        for bid, b in found.items():
            ov = overrides.get(bid, {})
            b["default_topic"] = f"{self.opts['z2m_base_topic']}/{b['z2m_name']}"
            b["topic"] = ov.get("topic") or b["default_topic"]
            result.append(b)
        for bid, ov in overrides.items():
            if ov.get("manual"):
                result.append({
                    "id": bid, "name": ov["name"], "z2m_name": None, "model": "Manual",
                    "learned_entity": None, "manual": True,
                    "topic": ov["topic"], "default_topic": ov["topic"],
                })
        result.sort(key=lambda b: b["name"].lower())
        self._blaster_cache = (time.monotonic(), result)
        return result

    async def blaster(self, blaster_id):
        for b in await self.blasters():
            if b["id"] == blaster_id:
                return b
        for b in await self.blasters(force=True):
            if b["id"] == blaster_id:
                return b
        raise web.HTTPNotFound(text="IR blaster not found in Home Assistant")

    # ---------- IR ----------
    async def send(self, blaster, code):
        await self.ha.mqtt_publish(f"{blaster['topic']}/set", {"ir_code_to_send": code})

    async def learn(self, blaster, device, control):
        bid = blaster["id"]
        if bid in self.learning:
            raise web.HTTPConflict(text="This blaster is already learning")
        cancel = asyncio.Event()
        self.learning[bid] = {"cancel": cancel, "device": device["id"], "control": control["id"]}
        queue = asyncio.Queue()
        subs = []

        def on_mqtt(ev):
            if ev is None:
                queue.put_nowait(("disconnect", None))
                return
            try:
                data = json.loads(ev.get("payload") or "")
            except ValueError:
                return
            if isinstance(data, dict):
                queue.put_nowait(("mqtt", data))

        def on_entity(ev):
            if ev is None:
                queue.put_nowait(("disconnect", None))
                return
            for key in ("a", "c"):
                for st in (ev.get(key) or {}).values():
                    if key == "c":
                        st = st.get("+") or {}
                    queue.put_nowait(("initial" if key == "a" else "state", st))

        stale = set()
        success = False
        try:
            subs.append(await self.ha.call({"type": "mqtt/subscribe", "topic": blaster["topic"]}, on_mqtt))
            if blaster.get("learned_entity"):
                subs.append(await self.ha.call(
                    {"type": "subscribe_entities", "entity_ids": [blaster["learned_entity"]]}, on_entity))
            # Let the initial entity snapshot land so we know the previous code.
            await asyncio.sleep(0.2)
            while not queue.empty():
                kind, st = queue.get_nowait()
                if kind == "initial":
                    for val in (st.get("s"), (st.get("a") or {}).get("learned_ir_code")):
                        if val not in INVALID_STATES:
                            stale.add(val)

            await self.ha.mqtt_publish(f"{blaster['topic']}/set", {"learn_ir_code": "ON"})
            started = time.monotonic()
            deadline = started + self.opts["learn_timeout"]
            seen_on = False
            LOG.info("Learning on %s for %s / %s", blaster["name"], device["name"], control["name"])

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                get = asyncio.ensure_future(queue.get())
                stop = asyncio.ensure_future(cancel.wait())
                done, _ = await asyncio.wait({get, stop}, timeout=remaining,
                                             return_when=asyncio.FIRST_COMPLETED)
                for t in (get, stop):
                    if t not in done:
                        t.cancel()
                if stop in done:
                    raise LearnCancelled
                if get not in done:
                    raise asyncio.TimeoutError
                kind, payload = get.result()
                in_ack = time.monotonic() - started < ACK_WINDOW

                if kind == "disconnect":
                    raise HAError("Lost connection to Home Assistant while learning")
                if kind == "mqtt":
                    code = payload.get("learned_ir_code")
                    mode = payload.get("learn_ir_code")
                    if mode == "ON" and not seen_on:
                        seen_on = True
                        if code:
                            stale.add(code)
                        continue
                    if not code:
                        continue
                    if in_ack and not seen_on:
                        stale.add(code)
                        continue
                    # New code, or the same code re-learned (blaster flipped learning back OFF)
                    if code not in stale or (seen_on and mode == "OFF" and not in_ack):
                        success = True
                        return code
                elif kind == "state":
                    candidates = [payload.get("s"), (payload.get("a") or {}).get("learned_ir_code")]
                    for code in candidates:
                        if code not in INVALID_STATES and code not in stale and not in_ack:
                            success = True
                            return code
        finally:
            self.learning.pop(bid, None)
            for sid in subs:
                await self.ha.unsubscribe(sid)
            if not success:
                try:
                    await self.ha.mqtt_publish(f"{blaster['topic']}/set", {"learn_ir_code": "OFF"})
                except Exception:  # noqa: BLE001
                    pass

    # ---------- HA button entities ----------
    async def sync_buttons(self):
        """Publish (or remove) MQTT discovery configs so each control is an HA button."""
        async with self._sync_lock:
            await self._sync_buttons()

    async def _sync_buttons(self):
        prefix = self.opts["discovery_prefix"]
        wanted = {}
        if self.opts["expose_buttons"]:
            try:
                blasters = {b["id"]: b for b in await self.blasters()}
            except HAError:
                return
            for dev in self.store.data["devices"]:
                blaster = blasters.get(dev["blaster_id"])
                if not blaster:
                    continue
                device_info = {
                    "identifiers": [f"irremote_{dev['id']}"],
                    "name": dev["name"],
                    "manufacturer": "Zigbee IR Remote",
                    "model": f"IR device via {blaster['name']}",
                }
                if dev.get("kind") == "configs":
                    select = self._select_config(dev, blaster, device_info)
                    if select:
                        wanted[f"{prefix}/select/irremote_{dev['id']}/config/config"] = select
                    continue
                for ctl in dev["controls"]:
                    if not ctl.get("code"):
                        continue
                    topic = f"{prefix}/button/irremote_{dev['id']}/{ctl['id']}/config"
                    wanted[topic] = {
                        "name": ctl["name"],
                        "unique_id": f"irremote_{dev['id']}_{ctl['id']}",
                        "default_entity_id": f"button.{slugify(dev['name'])}_{slugify(ctl['name'])}",
                        "command_topic": f"{blaster['topic']}/set",
                        "payload_press": json.dumps({"ir_code_to_send": ctl["code"]}),
                        "icon": ctl.get("icon") or "mdi:remote",
                        "device": device_info,
                    }
        published = set(self.store.data.get("published", []))
        signature = self.store.data.get("published_sig", {})
        try:
            for topic in published - wanted.keys():
                await self.ha.mqtt_publish(topic, "", retain=True)
                signature.pop(topic, None)
            for topic, cfg in wanted.items():
                body = json.dumps(cfg, sort_keys=True)
                if signature.get(topic) != body:
                    await self.ha.mqtt_publish(topic, body, retain=True)
                    signature[topic] = body
        except HAError as err:
            LOG.warning("Could not sync HA buttons: %s", err)
            return
        self.store.data["published"] = sorted(wanted)
        self.store.data["published_sig"] = signature
        self.store.save()

    @staticmethod
    def _select_config(dev, blaster, device_info):
        """One select entity whose options are the learned configs.

        The option -> IR code table lives in the command template, so HA sends the code
        straight to the blaster and it keeps working even when this app is stopped.
        """
        codes = {}
        for ctl in dev["controls"]:
            if ctl.get("code") and ctl["name"] not in codes:
                codes[ctl["name"]] = ctl["code"]
        if not codes:
            return None
        # A JSON object is also a valid Jinja dict literal (\uXXXX escapes included).
        template = ("{% set codes = " + json.dumps(codes) + " %}"
                    "{{ {'ir_code_to_send': codes[value]} | to_json }}")
        return {
            "name": None,  # entity takes the device name, e.g. "Bedroom AC"
            "unique_id": f"irremote_{dev['id']}_config",
            "default_entity_id": f"select.{slugify(dev['name'])}",
            "command_topic": f"{blaster['topic']}/set",
            "command_template": template,
            "options": list(codes),
            "optimistic": True,
            "icon": dev.get("icon") or "mdi:air-conditioner",
            "device": device_info,
        }


# ---------------------------------------------------------------- HTTP API

def ha_errors(handler):
    async def wrapper(request):
        try:
            return await handler(request)
        except HAError as err:
            return web.json_response({"error": str(err)}, status=502)
    return wrapper


def make_app(remote: IRRemote):
    store = remote.store
    routes = web.RouteTableDef()

    async def body(request):
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid JSON") from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object")
        return data

    def changed():
        store.save()
        asyncio.ensure_future(remote.sync_buttons())

    def clean_name(value, what="Name"):
        value = (value or "").strip()
        if not value:
            raise web.HTTPBadRequest(text=f"{what} is required")
        return value[:80]

    def clean_kind(value):
        if value not in DEVICE_KINDS:
            raise web.HTTPBadRequest(text=f"Device type must be one of {', '.join(DEVICE_KINDS)}")
        return value

    def check_unique(dev, name, ctl_id=None):
        """Config names are the HA select's options, so they must be unique per device."""
        if dev.get("kind") != "configs":
            return
        if any(c["name"] == name and c["id"] != ctl_id for c in dev["controls"]):
            raise web.HTTPConflict(text=f"There is already a config named “{name}”")

    @routes.get("/")
    async def index(_):
        return web.FileResponse(STATIC_DIR / "index.html")

    @routes.get("/api/state")
    @ha_errors
    async def state(request):
        force = request.query.get("refresh") == "1"
        blasters, error = [], None
        try:
            blasters = await remote.blasters(force=force)
        except HAError as err:
            error = str(err)
        return web.json_response({
            "connected": remote.ha.connected.is_set(),
            "error": error,
            "blasters": blasters,
            "devices": store.data["devices"],
            "learning": {k: {"device": v["device"], "control": v["control"]}
                         for k, v in remote.learning.items()},
            "options": {k: remote.opts[k] for k in ("learn_timeout", "expose_buttons", "z2m_base_topic")},
        })

    # --- blasters
    @routes.post("/api/blasters")
    async def add_blaster(request):
        data = await body(request)
        bid = "manual_" + new_id()
        store.data["blasters"][bid] = {
            "manual": True,
            "name": clean_name(data.get("name")),
            "topic": clean_name(data.get("topic"), "Topic").strip("/"),
        }
        remote._blaster_cache = (0.0, [])
        changed()
        return web.json_response({"id": bid})

    @routes.put("/api/blasters/{bid}")
    async def edit_blaster(request):
        data = await body(request)
        bid = request.match_info["bid"]
        ov = store.data["blasters"].setdefault(bid, {})
        if "topic" in data:
            topic = (data["topic"] or "").strip().strip("/")
            if topic:
                ov["topic"] = topic
            elif not ov.get("manual"):
                ov.pop("topic", None)
        if ov.get("manual") and data.get("name"):
            ov["name"] = clean_name(data["name"])
        if not ov:
            store.data["blasters"].pop(bid)
        remote._blaster_cache = (0.0, [])
        changed()
        return web.json_response({"ok": True})

    @routes.delete("/api/blasters/{bid}")
    async def delete_blaster(request):
        store.data["blasters"].pop(request.match_info["bid"], None)
        remote._blaster_cache = (0.0, [])
        changed()
        return web.json_response({"ok": True})

    # --- devices
    @routes.post("/api/devices")
    async def add_device(request):
        data = await body(request)
        kind = clean_kind(data.get("kind") or "buttons")
        names = []
        for n in data.get("controls", []):
            if isinstance(n, str) and n.strip() and n.strip()[:80] not in names:
                names.append(n.strip()[:80])
        dev = {
            "id": new_id(),
            "name": clean_name(data.get("name")),
            "blaster_id": clean_name(data.get("blaster_id"), "Blaster"),
            "kind": kind,
            "icon": data.get("icon") or ("mdi:air-conditioner" if kind == "configs" else "mdi:remote"),
            "controls": [{"id": new_id(), "name": n, "code": None} for n in names],
        }
        store.data["devices"].append(dev)
        changed()
        return web.json_response(dev)

    @routes.put("/api/devices/{did}")
    async def edit_device(request):
        data = await body(request)
        dev = store.device(request.match_info["did"])
        if "name" in data:
            dev["name"] = clean_name(data["name"])
        if data.get("blaster_id"):
            dev["blaster_id"] = data["blaster_id"]
        if "icon" in data:
            dev["icon"] = data["icon"] or "mdi:remote"
        if data.get("kind") and data["kind"] != dev.get("kind", "buttons"):
            kind = clean_kind(data["kind"])
            names = [c["name"] for c in dev["controls"]]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if kind == "configs" and dupes:
                raise web.HTTPConflict(text="Config names must be unique; rename first: " + ", ".join(dupes))
            dev["kind"] = kind
            if dev.get("icon") in ("mdi:remote", "mdi:air-conditioner"):
                dev["icon"] = "mdi:air-conditioner" if kind == "configs" else "mdi:remote"
        if isinstance(data.get("order"), list):
            pos = {cid: i for i, cid in enumerate(data["order"])}
            dev["controls"].sort(key=lambda c: pos.get(c["id"], len(pos)))
        changed()
        return web.json_response(dev)

    @routes.delete("/api/devices/{did}")
    async def delete_device(request):
        dev = store.device(request.match_info["did"])
        store.data["devices"].remove(dev)
        changed()
        return web.json_response({"ok": True})

    # --- controls
    @routes.post("/api/devices/{did}/controls")
    async def add_control(request):
        data = await body(request)
        dev = store.device(request.match_info["did"])
        ctl = {"id": new_id(), "name": clean_name(data.get("name")), "code": (data.get("code") or None)}
        check_unique(dev, ctl["name"])
        dev["controls"].append(ctl)
        changed()
        return web.json_response(ctl)

    @routes.put("/api/devices/{did}/controls/{cid}")
    async def edit_control(request):
        data = await body(request)
        dev, ctl = store.control(request.match_info["did"], request.match_info["cid"])
        if "name" in data:
            name = clean_name(data["name"])
            check_unique(dev, name, ctl["id"])
            ctl["name"] = name
        if "code" in data:
            ctl["code"] = (data["code"] or "").strip() or None
        if "icon" in data:
            ctl["icon"] = data["icon"] or None
        changed()
        return web.json_response(ctl)

    @routes.delete("/api/devices/{did}/controls/{cid}")
    async def delete_control(request):
        dev, ctl = store.control(request.match_info["did"], request.match_info["cid"])
        dev["controls"].remove(ctl)
        changed()
        return web.json_response({"ok": True})

    @routes.post("/api/devices/{did}/controls/{cid}/learn")
    @ha_errors
    async def learn(request):
        dev, ctl = store.control(request.match_info["did"], request.match_info["cid"])
        blaster = await remote.blaster(dev["blaster_id"])
        try:
            code = await remote.learn(blaster, dev, ctl)
        except asyncio.TimeoutError:
            return web.json_response({"error": "No IR signal received. Point the remote at the "
                                      "blaster from close range and try again."}, status=408)
        except LearnCancelled:
            return web.json_response({"cancelled": True})
        # Control may have been deleted/edited while waiting; re-resolve it.
        _, ctl = store.control(dev["id"], ctl["id"])
        ctl["code"] = code
        ctl["learned_at"] = int(time.time())
        changed()
        LOG.info("Learned %s / %s (%d chars)", dev["name"], ctl["name"], len(code))
        return web.json_response(ctl)

    @routes.post("/api/learn/cancel")
    async def cancel_learn(request):
        data = await body(request)
        for bid, info in remote.learning.items():
            if not data.get("blaster_id") or data["blaster_id"] == bid:
                info["cancel"].set()
        return web.json_response({"ok": True})

    @routes.post("/api/devices/{did}/controls/{cid}/send")
    @ha_errors
    async def send_control(request):
        dev, ctl = store.control(request.match_info["did"], request.match_info["cid"])
        if not ctl.get("code"):
            raise web.HTTPBadRequest(text="This control has no IR code yet")
        await remote.send(await remote.blaster(dev["blaster_id"]), ctl["code"])
        return web.json_response({"ok": True})

    # --- backup
    @routes.get("/api/export")
    async def export(_):
        return web.json_response(
            {"version": 1, "devices": store.data["devices"]},
            headers={"Content-Disposition": 'attachment; filename="ir-remotes.json"'},
            dumps=lambda d: json.dumps(d, indent=2),
        )

    @routes.post("/api/import")
    async def import_(request):
        data = await body(request)
        devices = data.get("devices")
        if not isinstance(devices, list):
            raise web.HTTPBadRequest(text="File does not contain any devices")
        existing = {d["id"] for d in store.data["devices"]}
        count = 0
        for dev in devices:
            if not isinstance(dev, dict) or not dev.get("name"):
                continue
            imported = {
                "id": dev["id"] if dev.get("id") and dev["id"] not in existing else new_id(),
                "name": str(dev["name"])[:80],
                "blaster_id": data.get("blaster_id") or dev.get("blaster_id") or "",
                "kind": dev.get("kind") if dev.get("kind") in DEVICE_KINDS else "buttons",
                "icon": dev.get("icon") or "mdi:remote",
                "controls": [
                    {"id": new_id(), "name": str(c.get("name"))[:80], "code": c.get("code") or None}
                    for c in dev.get("controls", []) if isinstance(c, dict) and c.get("name")
                ],
            }
            store.data["devices"].append(imported)
            existing.add(imported["id"])
            count += 1
        changed()
        return web.json_response({"imported": count})

    app = web.Application(client_max_size=10 * 1024 ** 2)
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC_DIR)
    return app


def supervisor_token():
    """The Supervisor token; s6-overlay keeps it out of our env, so also check its env dir."""
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
        path = Path("/run/s6/container_environment") / name
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
    return None


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    options = load_options()

    token = supervisor_token()
    if token:
        ws_url = "ws://supervisor/core/websocket"
    else:  # standalone / development: HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived>
        token = os.environ.get("HA_TOKEN")
        if not token:
            raise SystemExit("No Supervisor token found. Make sure 'homeassistant_api: true' is set, "
                             "or set HA_URL and HA_TOKEN when running outside Home Assistant.")
        ws_url = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
        ws_url = ws_url.replace("http", "ws", 1) + "/api/websocket"

    ha = HAClient(ws_url, token)
    remote = IRRemote(ha, Store(DATA_DIR / "remotes.json"), options)
    ha_task = asyncio.create_task(ha.run())

    async def initial_sync():
        await ha.connected.wait()
        await remote.sync_buttons()
    asyncio.create_task(initial_sync())

    runner = web.AppRunner(make_app(remote))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    LOG.info("Web UI listening on port %d", PORT)
    try:
        await ha_task
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
