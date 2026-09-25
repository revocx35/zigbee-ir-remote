"""Zigbee IR Remote - Home Assistant app backend.

Talks to Home Assistant over its websocket API (via the Supervisor proxy when
running as an app) to:
  * discover Zigbee2MQTT IR blasters (devices exposing `learned_ir_code`)
  * put a blaster into learning mode and capture the learned code
  * send codes (`ir_code_to_send`) through the `mqtt.publish` service
  * optionally publish every control as an MQTT discovery button entity, or, for
    "config" devices (ACs whose remote sends the full state on every press),
    one select entity listing the device's learned configs
  * run "climate regions": keep a room at a target temperature/humidity by stepping its
    ACs (HA climate entities or IR devices) through increasingly aggressive settings
"""

import asyncio
import json
import logging
import math
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
# Recent Zigbee2MQTT stamps each capture (learned_ir_timings.timestamp); allow this much
# clock difference between it and us when deciding whether a stamp is from this session.
CLOCK_SKEW = 5.0
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
        self.data = {"devices": [], "blasters": {}, "published": [], "regions": []}
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


class LearnTimeout(Exception):
    """No new code arrived in time; the message says what the blaster did send."""


class IRRemote:
    def __init__(self, ha, store, options):
        self.ha = ha
        self.store = store
        self.opts = options
        self._blaster_cache = (0.0, [])
        self.learning = {}  # blaster_id -> {"cancel": Event, "device": .., "control": ..}
        self.climate = None  # set in main()
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

    async def send_control(self, dev, ctl):
        """Send a control's code; for config devices also show it as the select's state."""
        if not ctl.get("code"):
            raise HAError(f"“{ctl['name']}” has no learned IR code")
        await self.send(await self.blaster(dev["blaster_id"]), ctl["code"])
        if dev.get("kind") == "configs":
            await self.ha.mqtt_publish(f"irremote/device/{dev['id']}/state", ctl["name"])

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
        heard = 0  # MQTT messages from the blaster during this session
        repeated = False  # it reported a code, but only the one it already had
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
            started, started_at = time.monotonic(), time.time()
            deadline = started + self.opts["learn_timeout"]
            seen_on = False
            LOG.info("Learning on %s for %s / %s", blaster["name"], device["name"], control["name"])

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LearnTimeout(self._learn_failure(blaster, heard, repeated))
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
                    raise LearnTimeout(self._learn_failure(blaster, heard, repeated))
                kind, payload = get.result()
                in_ack = time.monotonic() - started < ACK_WINDOW

                if kind == "disconnect":
                    raise HAError("Lost connection to Home Assistant while learning")
                if kind == "mqtt":
                    heard += 1
                    code = payload.get("learned_ir_code")
                    mode = payload.get("learn_ir_code")
                    timings = payload.get("learned_ir_timings")
                    stamp = timings.get("timestamp") if isinstance(timings, dict) else None
                    LOG.info("Learning: message from %s (code: %s, capture time: %s)", blaster["topic"],
                             f"{len(code)} chars" if code else "none", "yes" if stamp else "no")
                    # A capture stamped after learning started is new, even if its code is identical
                    # to the previously learned one (e.g. re-learning the same button).
                    if code and isinstance(stamp, (int, float)) and stamp / 1000 >= started_at - CLOCK_SKEW:
                        success = True
                        return code
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
                    repeated = True
                elif kind == "state":
                    candidates = [payload.get("s"), (payload.get("a") or {}).get("learned_ir_code")]
                    for code in candidates:
                        if code not in INVALID_STATES and code not in stale and not in_ack:
                            success = True
                            return code
        finally:
            # Nothing is sent to stop learning: Zigbee2MQTT treats any learn_ir_code value,
            # "OFF" included, as "start learning", which would swallow the next button press.
            # The blaster leaves learning mode by itself.
            self.learning.pop(bid, None)
            for sid in subs:
                await self.ha.unsubscribe(sid)
            if not success:
                LOG.info("Learning on %s ended without a new code (%d messages received)", blaster["name"], heard)

    @staticmethod
    def _learn_failure(blaster, heard, repeated):
        if repeated:
            return ("The blaster only reported the code it had already learned, so nothing new was saved. "
                    "Press the button again, or update Zigbee2MQTT: recent versions let the app recognise "
                    "a button whose code is identical to the last one learned.")
        if not heard:
            return (f"Nothing arrived from the blaster on “{blaster['topic']}”. If its LED didn't light up "
                    "when learning started, the topic is probably wrong: check it in Settings (⚙). Otherwise "
                    "hold the remote 5–20 cm from the blaster and press the button again.")
        return "No IR signal received. Point the remote at the blaster from close range and try again."

    # ---------- HA entities ----------
    async def sync_entities(self):
        """Publish (or remove) MQTT discovery configs: buttons/selects for devices, climate regions."""
        async with self._sync_lock:
            await self._sync_entities()

    async def _sync_entities(self):
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
        if self.climate:
            wanted.update(self.climate.discovery(prefix))
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
            LOG.warning("Could not sync HA entities: %s", err)
            return
        self.store.data["published"] = sorted(wanted)
        self.store.data["published_sig"] = signature
        self.store.save()
        if self.climate:
            try:
                await self.climate.assign_areas()
            except HAError as err:
                LOG.warning("Could not assign climate regions to areas: %s", err)

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
            # Optimistic for choices made in HA; the state topic reports sends from this app.
            "optimistic": True,
            "state_topic": f"irremote/device/{dev['id']}/state",
            "icon": dev.get("icon") or "mdi:air-conditioner",
            "device": device_info,
        }


# ---------------------------------------------------------------- climate regions

CLIMATE_TOPIC = "irremote/climate"
TICK_SECONDS = 5
RETRY_SECONDS = 60
TEMP_DIRS = ("heat", "cool")
HUMIDITY_DIRS = ("dry", "humidify")
VERBS = {"heat": "Heating", "cool": "Cooling", "dry": "Dehumidifying", "humidify": "Humidifying"}
HUMIDITY_RANGE = (20, 80)
# field: (default, min, max). target_temp's range depends on HA's unit, see Climate.temp_range().
REGION_NUMBERS = {
    "target_temp": (24.0, None, None),
    "temp_tolerance": (1.0, 0.1, 10),
    "target_humidity": (50.0, *HUMIDITY_RANGE),
    "humidity_tolerance": (5.0, 1, 30),
    "importance": (50.0, 0, 100),  # % of each step given to temperature when both are off target
    "step_minutes": (10.0, 1, 240),
    "min_cycle_minutes": (3.0, 0, 60),
}
HA_COMMANDS = ("enabled", "target_temp", "target_humidity", "importance")


def name_temperature(name):
    """Setpoint written in an IR config's name, e.g. 26 for "Heat 26° · Fan Auto"."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*°", name) or re.search(r"\b(\d{2}(?:[.,]\d+)?)\b", name)
    return float(m.group(1).replace(",", ".")) if m else None


def clean_step(kind, step):
    if not isinstance(step, dict):
        return None
    if kind == "ir":
        return {"control": str(step["control"])} if step.get("control") else None
    if not step.get("hvac_mode"):
        return None
    try:
        temp = None if step.get("temperature") in (None, "") else float(step["temperature"])
    except (TypeError, ValueError):
        temp = None
    if temp is not None and not math.isfinite(temp):
        temp = None
    return {
        "hvac_mode": str(step["hvac_mode"]),
        "temperature": temp,
        "relative": bool(step.get("relative")) and temp is not None,
        "fan_mode": str(step["fan_mode"]) if step.get("fan_mode") else None,
    }


def clean_ac(ac):
    """An AC in a region: an IR device of this app or an HA climate entity, plus its ladders."""
    if not isinstance(ac, dict) or ac.get("type") not in ("ir", "climate"):
        return None
    out = {"id": str(ac.get("id") or new_id())[:32], "type": ac["type"]}
    if ac["type"] == "ir":
        if not ac.get("device_id"):
            return None
        out["device_id"] = str(ac["device_id"])
        out["off_control"] = str(ac["off_control"]) if ac.get("off_control") else None
    else:
        if not str(ac.get("entity_id") or "").startswith("climate."):
            return None
        out["entity_id"] = ac["entity_id"]
    ladders = ac.get("ladders") if isinstance(ac.get("ladders"), dict) else {}
    out["ladders"] = {}
    for direction in TEMP_DIRS + HUMIDITY_DIRS:
        steps = ladders.get(direction) if isinstance(ladders.get(direction), list) else []
        out["ladders"][direction] = [s for s in (clean_step(ac["type"], x) for x in steps) if s]
    return out


class Climate:
    """Keeps each region's room at its targets by stepping its ACs up their ladders.

    Time runs in steps of `step_minutes`. While the room is outside the accepted difference,
    every AC runs the current step of its heat/cool (or dry/humidify) ladder. When a step ends
    and the room still isn't in range, the next, more aggressive step is used. If temperature
    and humidity are both off target, each step is split between them by `importance`
    (the percentage of the step given to temperature, which goes first).
    """

    def __init__(self, remote):
        self.remote = remote
        self.ha = remote.ha
        self.store = remote.store
        self.unit = "°C"
        self.areas = {}  # area_id -> name
        self.states = {}  # watched entity_id -> {"s": state, "a": attributes}
        self.rt = {}  # region id -> runtime state
        self._meta_loaded = False
        self._watch = None  # (subscription id, watched entity ids)
        self._commands = None  # subscription id for commands from HA entities
        self._published = {}  # region id -> last state payload
        self._area_synced = {}  # region id -> area id assigned in the device registry
        self._entity_cache = (0.0, [])
        self._wake = asyncio.Event()

    @property
    def regions(self):
        return self.store.data["regions"]

    def region(self, rid):
        for r in self.regions:
            if r["id"] == rid:
                return r
        raise web.HTTPNotFound(text="Region not found")

    def temp_range(self):
        return (50, 90) if self.unit == "°F" else (10, 32)

    def poke(self):
        """Re-evaluate now instead of at the next tick."""
        self._wake.set()

    # ---------- configuration ----------
    def new_region(self):
        region = {"id": new_id(), "name": "", "area_id": None, "enabled": False, "humidity_control": True,
                  "temp_sensors": [], "humidity_sensors": [], "acs": []}
        region.update({key: default for key, (default, _, _) in REGION_NUMBERS.items()})
        if self.unit == "°F":
            region.update(target_temp=75.0, temp_tolerance=2.0)
        return region

    def update(self, region, data):
        """Apply a partial update from the UI or from Home Assistant; all fields are validated first."""
        changes = {}
        if "name" in data:
            changes["name"] = str(data["name"] or "").strip()[:80]
            if not changes["name"]:
                raise web.HTTPBadRequest(text="Name is required")
        if "area_id" in data:
            changes["area_id"] = str(data["area_id"]) if data["area_id"] else None
        for key in ("enabled", "humidity_control"):
            if key in data:
                changes[key] = bool(data[key])
        for key, (_, lo, hi) in REGION_NUMBERS.items():
            if key not in data:
                continue
            try:
                value = float(data[key])
            except (TypeError, ValueError):
                value = math.nan
            if not math.isfinite(value):
                raise web.HTTPBadRequest(text=f"{key} must be a number")
            if key == "target_temp":
                lo, hi = self.temp_range()
            changes[key] = round(min(max(value, lo), hi), 2)
        for key in ("temp_sensors", "humidity_sensors"):
            if key in data:
                ids = data[key] if isinstance(data[key], list) else []
                changes[key] = list(dict.fromkeys(e for e in ids if isinstance(e, str) and "." in e))
        if "acs" in data:
            acs = data["acs"] if isinstance(data["acs"], list) else []
            changes["acs"] = [ac for ac in map(clean_ac, acs) if ac]
        region.update(changes)
        self.poke()

    # ---------- Home Assistant data ----------
    async def load_meta(self):
        config, areas = await asyncio.gather(
            self.ha.call({"type": "get_config"}),
            self.ha.call({"type": "config/area_registry/list"}),
        )
        self.unit = (config.get("unit_system") or {}).get("temperature") or "°C"
        self.areas = {a["area_id"]: a["name"] for a in areas}
        self._meta_loaded = True

    async def entities(self, force=False):
        """Climate entities and temperature/humidity sensors, for the region editor."""
        ts, cached = self._entity_cache
        if not force and time.monotonic() - ts < 15:
            return cached
        await self.load_meta()
        states, ents, devs = await asyncio.gather(
            self.ha.call({"type": "get_states"}),
            self.ha.call({"type": "config/entity_registry/list"}),
            self.ha.call({"type": "config/device_registry/list"}),
        )
        dev_area = {d["id"]: d.get("area_id") for d in devs}
        registry = {e["entity_id"]: e for e in ents}
        result = []
        for st in states:
            eid = st["entity_id"]
            reg = registry.get(eid, {})
            if (reg.get("unique_id") or "").startswith("irremote_region_"):
                continue  # our own averages must not feed back into a region
            domain = eid.split(".", 1)[0]
            attrs = st.get("attributes") or {}
            dclass, unit = attrs.get("device_class"), attrs.get("unit_of_measurement")
            if domain == "climate":
                kind = "climate"
            elif domain == "sensor" and (dclass == "temperature" or unit in ("°C", "°F")):
                kind = "temperature"
            elif domain == "sensor" and (dclass == "humidity" or (unit == "%" and "humid" in eid)):
                kind = "humidity"
            else:
                continue
            ent = {
                "entity_id": eid, "kind": kind, "name": attrs.get("friendly_name") or eid,
                "state": st.get("state"), "unit": unit,
                "area_id": reg.get("area_id") or dev_area.get(reg.get("device_id")),
            }
            if kind == "climate":
                ent.update({k: attrs.get(k) for k in ("hvac_modes", "fan_modes", "min_temp", "max_temp",
                                                     "target_temp_step")})
            result.append(ent)
        result.sort(key=lambda e: e["name"].lower())
        self._entity_cache = (time.monotonic(), result)
        return result

    async def _prepare(self):
        """(Re)subscribe to HA commands and to the entities the regions read."""
        if not self._meta_loaded:
            await self.load_meta()
        if self._commands is None:
            self._commands = await self.ha.call(
                {"type": "mqtt/subscribe", "topic": f"{CLIMATE_TOPIC}/+/+/set"}, self._on_command)
        wanted = sorted({e for r in self.regions for e in r["temp_sensors"] + r["humidity_sensors"]}
                        | {ac["entity_id"] for r in self.regions for ac in r["acs"] if ac["type"] == "climate"})
        if self._watch and self._watch[1] != wanted:
            sub, self._watch = self._watch[0], None
            await self.ha.unsubscribe(sub)
        if wanted and not self._watch:
            sub = await self.ha.call({"type": "subscribe_entities", "entity_ids": wanted}, self._on_entities)
            self._watch = (sub, wanted)
            await asyncio.sleep(0.5)  # let the initial snapshot land before deciding anything
            self.states = {k: v for k, v in self.states.items() if k in wanted}

    def _on_entities(self, ev):
        if ev is None:  # disconnected
            self._watch = None
            self.states.clear()
            return
        for eid, st in (ev.get("a") or {}).items():
            self.states[eid] = {"s": st.get("s"), "a": dict(st.get("a") or {})}
        for eid, diff in (ev.get("c") or {}).items():
            cur = self.states.setdefault(eid, {"s": None, "a": {}})
            plus = diff.get("+") or {}
            if "s" in plus:
                cur["s"] = plus["s"]
            cur["a"].update(plus.get("a") or {})
            for key in (diff.get("-") or {}).get("a") or []:
                cur["a"].pop(key, None)
        for eid in ev.get("r") or []:
            self.states.pop(eid, None)

    def _on_command(self, ev):
        """A region's switch/number entity was changed in Home Assistant."""
        if ev is None:  # disconnected; resubscribe and republish everything on reconnect
            self._commands = None
            self._meta_loaded = False
            self._published.clear()
            return
        parts = (ev.get("topic") or "").split("/")
        if len(parts) != 5 or parts[3] not in HA_COMMANDS:
            return
        region = next((r for r in self.regions if r["id"] == parts[2]), None)
        if not region:
            return
        payload = str(ev.get("payload") or "").strip()
        value = payload.upper() in ("ON", "TRUE", "1") if parts[3] == "enabled" else payload
        try:
            self.update(region, {parts[3]: value})
        except web.HTTPException:
            return
        LOG.info("Climate %s: %s set to %s from Home Assistant", region["name"], parts[3], payload)
        self.store.save()
        self.publish_soon(region)

    # ---------- control loop ----------
    async def run(self):
        while True:
            await self.ha.connected.wait()
            try:
                await self._prepare()
                for region in list(self.regions):
                    if region not in self.regions:  # deleted while we were busy
                        continue
                    try:
                        await self._tick(region, time.time())
                        await self.publish_state(region)
                    except HAError as err:
                        LOG.warning("Climate %s: %s", region["name"], err)
            except HAError as err:
                LOG.warning("Climate control: %s", err)
            except Exception:  # noqa: BLE001 - never let the loop die
                LOG.exception("Climate control error")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    def _average(self, entity_ids):
        values = []
        for eid in entity_ids:
            try:
                value = float((self.states.get(eid) or {}).get("s"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
        return round(sum(values) / len(values), 2) if values else None

    def _runtime(self, region):
        if region["id"] not in self.rt:
            self.rt[region["id"]] = {
                "phase": "idle", "changed_at": 0.0, "active": None, "status": "Starting…",
                "section_start": None, "switch_at": None, "hold_until": None,
                "temperature": None, "humidity": None,
                "temp_dir": None, "temp_level": 0, "temp_levels": 0, "temp_since": 0.0,
                "hum_dir": None, "hum_level": 0, "hum_levels": 0, "hum_since": 0.0,
                "sent": {}, "errors": {}, "steps": {},
            }
        return self.rt[region["id"]]

    async def _tick(self, region, now):
        rt = self._runtime(region)
        rt["temperature"] = self._average(region["temp_sensors"])
        rt["humidity"] = self._average(region["humidity_sensors"])

        if not region["enabled"]:
            if rt["phase"] != "disabled":
                offs = {ac["id"]: self._off(ac) for ac in region["acs"]}
                if not await self._apply(region, rt, offs, now, only_running=True):
                    rt["status"] = "Turning off…"
                    return  # retried on a later tick
                if rt["phase"] == "active":
                    rt["changed_at"] = now
                rt.update(phase="disabled", active=None, section_start=None, switch_at=None, hold_until=None,
                          temp_dir=None, temp_level=0, hum_dir=None, hum_level=0, steps={})
            rt["status"] = "Off"
            return
        if rt["phase"] == "disabled":
            rt["phase"] = "idle"

        t_dir = self._temp_need(region, rt["temperature"])
        h_dir = self._humidity_need(region, rt["humidity"]) if region["humidity_control"] else None

        # Short-cycle protection: don't switch the ACs on/off again too soon after the last switch.
        wants, running = bool(t_dir or h_dir), rt["phase"] == "active"
        wait = region["min_cycle_minutes"] * 60 - (now - rt["changed_at"])
        if wants != running and wait > 0:
            rt["hold_until"] = now + wait
            rt["status"] = "In range · keeping ACs on briefly" if running else "Starting soon (short-cycle protection)"
            return
        rt["hold_until"] = None

        for key, direction in (("temp", t_dir), ("hum", h_dir)):
            if rt[f"{key}_dir"] != direction:  # newly off target (or the other way): start gently
                rt.update({f"{key}_dir": direction, f"{key}_level": 0, f"{key}_since": now})
            rt[f"{key}_levels"] = self._levels(region, direction) if direction else 0
            rt[f"{key}_level"] = min(rt[f"{key}_level"], max(rt[f"{key}_levels"] - 1, 0))
        needs = [key for key in ("temp", "hum") if rt[f"{key}_dir"]]

        section = region["step_minutes"] * 60
        if not needs:
            rt["section_start"] = None
        elif rt["section_start"] is None:
            rt["section_start"] = now
        elif now - rt["section_start"] >= section:
            for key in needs:  # a whole step went by and it's still off target: go one step harder
                if rt[f"{key}_since"] <= rt["section_start"]:
                    rt[f"{key}_level"] = min(rt[f"{key}_level"] + 1, rt[f"{key}_levels"] - 1)
            rt["section_start"] = now

        rt["switch_at"] = None
        if len(needs) == 2:
            temp_share = section * region["importance"] / 100
            if now - rt["section_start"] < temp_share:
                active = "temp"
                rt["switch_at"] = rt["section_start"] + temp_share
            else:
                active = "hum"
        else:
            active = needs[0] if needs else None
        if bool(active) != running:
            rt["changed_at"] = now
        rt["phase"] = "active" if active else "idle"
        rt["active"] = active

        commands, rt["steps"] = {}, {}
        for ac in region["acs"]:
            command = self._off(ac)
            if active:
                direction = rt[f"{active}_dir"]
                ladder = self._ladder(region, ac, direction)
                if ladder:
                    step = ladder[min(rt[f"{active}_level"], len(ladder) - 1)]
                    command = self._command(region, ac, step)
                    rt["steps"][ac["id"]] = {"dir": direction, "index": ac["ladders"][direction].index(step)}
            commands[ac["id"]] = command
        await self._apply(region, rt, commands, now)
        rt["status"] = self._status_text(region, rt)

    def _temp_need(self, region, temp):
        if temp is None:
            return None
        target, tol = region["target_temp"], region["temp_tolerance"]
        direction = "heat" if temp < target - tol else "cool" if temp > target + tol else None
        return direction if direction and self._levels(region, direction) else None

    def _humidity_need(self, region, humidity):
        if humidity is None:
            return None
        target, tol = region["target_humidity"], region["humidity_tolerance"]
        direction = "dry" if humidity > target + tol else "humidify" if humidity < target - tol else None
        return direction if direction and self._levels(region, direction) else None

    def _status_text(self, region, rt):
        if not region["temp_sensors"] and not region["humidity_sensors"]:
            return "Add sensors to start"
        if not region["acs"]:
            return "Add an AC to start"
        if rt["active"]:
            key = rt["active"]
            return f"{VERBS[rt[key + '_dir']]} · step {rt[key + '_level'] + 1} of {rt[key + '_levels']}"
        if rt["temperature"] is None and rt["humidity"] is None:
            return "No sensor data"
        return "Idle · in range"

    # ---------- ladders and commands ----------
    def _ir(self, ac, control_id=None):
        dev = next((d for d in self.store.data["devices"] if d["id"] == ac.get("device_id")), None)
        ctl = next((c for c in dev["controls"] if c["id"] == control_id), None) if dev and control_id else None
        return dev, ctl

    def _levels(self, region, direction):
        return max((len(self._ladder(region, ac, direction)) for ac in region["acs"]), default=0)

    def _ladder(self, region, ac, direction):
        """An AC's usable steps; heat/cool steps set on the wrong side of the target are skipped."""
        steps = ac["ladders"].get(direction, [])
        if ac["type"] == "ir":
            steps = [s for s in steps if (self._ir(ac, s["control"])[1] or {}).get("code")]
        if direction in TEMP_DIRS and len(steps) > 1:
            target = region["target_temp"]

            def reaches(step):
                if ac["type"] == "ir":
                    temp = name_temperature(self._ir(ac, step["control"])[1]["name"])
                else:
                    temp = self._command(region, ac, step)["temperature"]
                return temp is None or (temp >= target if direction == "heat" else temp <= target)
            steps = [s for s in steps if reaches(s)] or steps[-1:]
        return steps

    def _command(self, region, ac, step):
        """A ladder step resolved to what gets sent (compared by value to avoid resending)."""
        if ac["type"] == "ir":
            return {"control": step["control"]}
        temp = step.get("temperature")
        if temp is not None:
            attrs = (self.states.get(ac["entity_id"]) or {}).get("a") or {}
            if step.get("relative"):
                temp += region["target_temp"]
            if isinstance(attrs.get("min_temp"), (int, float)):
                temp = max(temp, attrs["min_temp"])
            if isinstance(attrs.get("max_temp"), (int, float)):
                temp = min(temp, attrs["max_temp"])
            inc = attrs.get("target_temp_step") or (1 if self.unit == "°F" else 0.5)
            temp = round(round(temp / inc) * inc, 1)
        return {"hvac_mode": step["hvac_mode"], "temperature": temp, "fan_mode": step.get("fan_mode")}

    @staticmethod
    def _off(ac):
        if ac["type"] == "ir":
            return {"control": ac["off_control"]} if ac.get("off_control") else None
        return {"hvac_mode": "off", "temperature": None, "fan_mode": None}

    def _label(self, ac, command):
        if ac["type"] == "ir":
            ctl = self._ir(ac, command["control"])[1]
            return ctl["name"] if ctl else "deleted config"
        if command["hvac_mode"] == "off":
            return "Off"
        text = command["hvac_mode"].replace("_", " ").capitalize()
        if command.get("temperature") is not None:
            text += f" {command['temperature']:g}{self.unit}"
        if command.get("fan_mode"):
            text += f" · fan {command['fan_mode']}"
        return text

    async def _apply(self, region, rt, commands, now, only_running=False):
        """Send each AC its command if it changed. Returns False if any send failed."""
        ok = True
        for ac in region["acs"]:
            command = commands.get(ac["id"])
            if command is None:
                continue
            sig = json.dumps(command, sort_keys=True)
            last = rt["sent"].get(ac["id"])
            if last and last["sig"] == sig:
                continue
            if only_running and (not last or last["off"]):
                continue
            error = rt["errors"].get(ac["id"])
            if error and error["sig"] == sig and now < error["retry"]:
                ok = False
                continue
            try:
                await self._send(ac, command)
            except (HAError, web.HTTPException) as err:
                message = getattr(err, "text", None) or str(err)
                rt["errors"][ac["id"]] = {"sig": sig, "retry": now + RETRY_SECONDS, "message": message}
                LOG.warning("Climate %s: could not send to AC: %s", region["name"], message)
                ok = False
                continue
            rt["errors"].pop(ac["id"], None)
            label = self._label(ac, command)
            rt["sent"][ac["id"]] = {"sig": sig, "off": command == self._off(ac), "label": label}
            LOG.info("Climate %s: %s -> %s", region["name"], self._ac_name(ac), label)
        return ok

    def _ac_name(self, ac):
        if ac["type"] == "ir":
            dev = self._ir(ac)[0]
            return dev["name"] if dev else "deleted IR device"
        return ac["entity_id"]

    async def _send(self, ac, command):
        if ac["type"] == "ir":
            dev, ctl = self._ir(ac, command["control"])
            if not dev or not ctl:
                raise HAError("The IR device or config was deleted")
            await self.remote.send_control(dev, ctl)
            return
        target = {"entity_id": ac["entity_id"]}
        mode = command["hvac_mode"]
        if mode != "off" and command.get("temperature") is not None:
            data = {"hvac_mode": mode, "temperature": command["temperature"]}
            await self._service("set_temperature", target, data)
        else:
            await self._service("set_hvac_mode", target, {"hvac_mode": mode})
        if mode != "off" and command.get("fan_mode"):
            await self._service("set_fan_mode", target, {"fan_mode": command["fan_mode"]})

    async def _service(self, service, target, data):
        await self.ha.call({"type": "call_service", "domain": "climate", "service": service,
                            "service_data": data, "target": target})

    # ---------- reporting ----------
    def status(self):
        out = {}
        for region in self.regions:
            rt = self._runtime(region)
            acs = {}
            for ac in region["acs"]:
                info = {"label": (rt["sent"].get(ac["id"]) or {}).get("label"),
                        "error": (rt["errors"].get(ac["id"]) or {}).get("message"),
                        "step": rt["steps"].get(ac["id"])}
                if ac["type"] == "climate" and ac["entity_id"] in self.states:
                    st = self.states[ac["entity_id"]]
                    temp = st["a"].get("temperature")
                    info["current"] = f"{st['s']}" + (f" {temp:g}{self.unit}" if isinstance(temp, (int, float)) else "")
                acs[ac["id"]] = info
            out[region["id"]] = {
                "phase": rt["phase"], "status": rt["status"], "active": rt["active"],
                "temperature": rt["temperature"], "humidity": rt["humidity"],
                "temp": {"dir": rt["temp_dir"], "level": rt["temp_level"], "levels": rt["temp_levels"]},
                "hum": {"dir": rt["hum_dir"], "level": rt["hum_level"], "levels": rt["hum_levels"]},
                "section_end": rt["section_start"] + region["step_minutes"] * 60 if rt["section_start"] else None,
                "switch_at": rt["switch_at"], "hold_until": rt["hold_until"],
                "readings": {e: (self.states.get(e) or {}).get("s")
                             for e in region["temp_sensors"] + region["humidity_sensors"]},
                "acs": acs,
            }
        return out

    async def publish_state(self, region):
        rt = self._runtime(region)
        rounded = {k: (round(rt[k], 1) if rt[k] is not None else None) for k in ("temperature", "humidity")}
        body = json.dumps({
            "enabled": region["enabled"], "target_temp": region["target_temp"],
            "target_humidity": region["target_humidity"], "importance": region["importance"],
            "status": rt["status"], **rounded,
        }, sort_keys=True)
        if self._published.get(region["id"]) != body:
            await self.ha.mqtt_publish(f"{CLIMATE_TOPIC}/{region['id']}/state", body, retain=True)
            self._published[region["id"]] = body

    def publish_soon(self, region):
        async def publish():
            try:
                await self.publish_state(region)
            except HAError as err:
                LOG.warning("Could not publish climate state: %s", err)
        asyncio.ensure_future(publish())

    async def remove(self, region):
        """A region was deleted: switch off what it had running and drop its retained state."""
        rt = self.rt.pop(region["id"], None)
        self._published.pop(region["id"], None)
        try:
            if rt:
                await self._apply(region, rt, {ac["id"]: self._off(ac) for ac in region["acs"]},
                                  time.time(), only_running=True)
            await self.ha.mqtt_publish(f"{CLIMATE_TOPIC}/{region['id']}/state", "", retain=True)
        except HAError as err:
            LOG.warning("Could not clean up climate region: %s", err)

    def discovery(self, prefix):
        """MQTT discovery configs: a switch, target sliders and readings for every region."""
        wanted = {}
        tmin, tmax = self.temp_range()
        for region in self.regions:
            rid, slug = region["id"], slugify(region["name"])
            base = f"{CLIMATE_TOPIC}/{rid}"
            device = {"identifiers": [f"irremote_region_{rid}"], "name": region["name"],
                      "manufacturer": "Zigbee IR Remote", "model": "Climate region"}
            if self.areas.get(region.get("area_id")):
                device["suggested_area"] = self.areas[region["area_id"]]
            entities = {
                ("switch", "enabled"): {
                    "name": "Climate control", "icon": "mdi:thermostat-auto",
                    "command_topic": f"{base}/enabled/set",
                    "value_template": "{{ 'ON' if value_json.enabled else 'OFF' }}"},
                ("number", "target_temp"): {
                    "name": "Target temperature", "command_topic": f"{base}/target_temp/set",
                    "value_template": "{{ value_json.target_temp }}", "min": tmin, "max": tmax, "step": 0.5,
                    "mode": "slider", "unit_of_measurement": self.unit, "device_class": "temperature"},
                ("number", "target_humidity"): {
                    "name": "Target humidity", "command_topic": f"{base}/target_humidity/set",
                    "value_template": "{{ value_json.target_humidity }}", "min": HUMIDITY_RANGE[0],
                    "max": HUMIDITY_RANGE[1], "step": 1, "mode": "slider", "unit_of_measurement": "%",
                    "device_class": "humidity"},
                ("number", "importance"): {
                    "name": "Temperature priority", "icon": "mdi:scale-balance",
                    "command_topic": f"{base}/importance/set", "value_template": "{{ value_json.importance }}",
                    "min": 0, "max": 100, "step": 5, "mode": "slider", "unit_of_measurement": "%"},
                ("sensor", "temperature"): {
                    "name": "Temperature", "value_template": "{{ value_json.temperature }}",
                    "unit_of_measurement": self.unit, "device_class": "temperature", "state_class": "measurement"},
                ("sensor", "humidity"): {
                    "name": "Humidity", "value_template": "{{ value_json.humidity }}",
                    "unit_of_measurement": "%", "device_class": "humidity", "state_class": "measurement"},
                ("sensor", "status"): {
                    "name": "Status", "icon": "mdi:information-outline",
                    "value_template": "{{ value_json.status }}"},
            }
            for (component, key), cfg in entities.items():
                wanted[f"{prefix}/{component}/irremote_region_{rid}/{key}/config"] = {
                    **cfg,
                    "unique_id": f"irremote_region_{rid}_{key}",
                    "default_entity_id": f"{component}.{slug}_{slugify(cfg['name'])}",
                    "state_topic": f"{base}/state",
                    "device": device,
                }
        return wanted

    async def assign_areas(self):
        """Move region devices into their area (suggested_area only applies on creation)."""
        todo = {f"irremote_region_{r['id']}": r for r in self.regions
                if r.get("area_id") and self._area_synced.get(r["id"]) != r["area_id"]}
        if not todo:
            return
        for dev in await self.ha.call({"type": "config/device_registry/list"}):
            for domain, ident in dev.get("identifiers") or []:
                region = todo.get(ident) if domain == "mqtt" else None
                if not region:
                    continue
                if dev.get("area_id") != region["area_id"]:
                    await self.ha.call({"type": "config/device_registry/update", "device_id": dev["id"],
                                        "area_id": region["area_id"]})
                self._area_synced[region["id"]] = region["area_id"]


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
    climate = remote.climate
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
        asyncio.ensure_future(remote.sync_entities())

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
        for region in store.data["regions"]:
            region["acs"] = [ac for ac in region["acs"] if ac.get("device_id") != dev["id"]]
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
        except LearnTimeout as err:
            return web.json_response({"error": str(err)}, status=408)
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
        await remote.send_control(dev, ctl)
        return web.json_response({"ok": True})

    # --- climate regions
    @routes.get("/api/climate")
    @ha_errors
    async def climate_state(request):
        entities = await climate.entities(force=request.query.get("refresh") == "1")
        return web.json_response({
            "unit": climate.unit,
            "temp_range": climate.temp_range(),
            "humidity_range": HUMIDITY_RANGE,
            "areas": [{"id": k, "name": v} for k, v in sorted(climate.areas.items(), key=lambda a: a[1].lower())],
            "entities": entities,
            "regions": climate.regions,
            "status": climate.status(),
            "now": time.time(),
        })

    @routes.get("/api/climate/status")
    async def climate_status(_):
        return web.json_response({"regions": climate.regions, "status": climate.status(), "now": time.time()})

    @routes.post("/api/regions")
    async def add_region(request):
        data = await body(request)
        region = climate.new_region()
        climate.update(region, {"name": data.get("name"), "area_id": data.get("area_id")})
        store.data["regions"].append(region)
        changed()
        return web.json_response(region)

    @routes.put("/api/regions/{rid}")
    async def edit_region(request):
        data = await body(request)
        region = climate.region(request.match_info["rid"])
        data.pop("id", None)
        climate.update(region, data)
        changed()
        climate.publish_soon(region)
        return web.json_response(region)

    @routes.delete("/api/regions/{rid}")
    async def delete_region(request):
        region = climate.region(request.match_info["rid"])
        store.data["regions"].remove(region)
        changed()
        asyncio.ensure_future(climate.remove(region))
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
    remote.climate = Climate(remote)
    ha_task = asyncio.create_task(ha.run())

    async def initial_sync():
        await ha.connected.wait()
        try:
            await remote.climate.load_meta()  # areas are needed for the region devices
        except HAError as err:
            LOG.warning("Could not load Home Assistant areas: %s", err)
        await remote.sync_entities()
    asyncio.create_task(initial_sync())
    climate_task = asyncio.create_task(remote.climate.run())

    runner = web.AppRunner(make_app(remote))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    LOG.info("Web UI listening on port %d", PORT)
    try:
        await ha_task
    finally:
        climate_task.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
