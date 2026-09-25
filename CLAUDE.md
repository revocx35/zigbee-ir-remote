# CLAUDE.md

Notes for future Claude sessions working on this repo. Read `ARCHITECTURE.md` for how the code
is structured; this file covers workflow, testing, and lessons that aren't obvious from the code.

## What this is
A Home Assistant **app (add-on)** repository, `revocx35/zigbee-ir-remote` on GitHub. It contains
one add-on, `zigbee_ir_remote/`:
- **Remotes tab**: learn and send IR codes via Zigbee2MQTT IR blasters (Tuya ZS06/TS1201,
  Moes UFO-R11: "zosung" devices). Devices are exposed to HA as buttons, or as one `select` for
  "full-config" ACs whose remote sends the whole state on every press.
- **Climate tab**: regions (rooms) keep a target temperature and humidity by stepping ACs (IR
  devices or HA `climate` entities) through ladders of increasingly aggressive settings.

Stack: one Python file (`app/main.py`, aiohttp only), and vanilla HTML/CSS/JS in `app/static/`.
There is no build step, no npm, and no framework.

## Release workflow
- Users install from the **`main` branch** through the HA app store, so pushing to `main` is a
  release. The owner is fine with committing straight to `main`.
- On every user-visible change:
  1. bump `version:` in `zigbee_ir_remote/config.yaml` (HA only offers an update when it changes);
  2. add an entry at the top of `zigbee_ir_remote/CHANGELOG.md`;
  3. update `zigbee_ir_remote/DOCS.md`, which is the user documentation shown in HA;
  4. if you add or rename options, update **both** `config.yaml` (`options`/`schema`) and
     `translations/en.yaml`.
- Commit messages: imperative summary with the version in parentheses, e.g.
  `Fix IR learning getting stuck (1.2.1)`, then a body explaining why.
- `.gitignore` excludes `__pycache__/`, `*.pyc` and `data/`. Don't commit compiled files. Run
  `py_compile` with `PYTHONDONTWRITEBYTECODE=1`, or delete `app/__pycache__` afterwards.

## Running and testing
There is no real Home Assistant in the dev environment and no automated test suite. What has
worked:

- **Syntax**: `python3 -m py_compile zigbee_ir_remote/app/main.py` and
  `node --check zigbee_ir_remote/app/static/app.js`.
- **Backend logic with a fake HA**: every HA interaction goes through `HAClient.call`,
  `mqtt_publish`, `unsubscribe` and `connected`. Replace the client with a stub and drive the code
  directly. The host has no pip, so run it in `docker run --rm -v $PWD/zigbee_ir_remote/app:/app:ro
  python:3.12-alpine sh -c "pip -q install aiohttp && python /path/to/test.py"`.
  ```python
  class FakeHA:
      def __init__(self): self.log = []; self.connected = asyncio.Event(); self.connected.set()
      async def mqtt_publish(self, topic, payload, retain=False): self.log.append((topic, payload))
      async def unsubscribe(self, sid): pass
      async def call(self, payload, callback=None, timeout=15):
          t = payload["type"]
          if callback: return 1                      # subscriptions; call callback(event) to inject
          if t == "config/entity_registry/list":     # makes device "B1" a blaster
              return [{"entity_id": "sensor.ir_learned_ir_code", "unique_id": "x_learned_ir_code", "device_id": "B1"}]
          if t == "config/device_registry/list": return [{"id": "B1", "name": "IR"}]
          if t == "get_config": return {"unit_system": {"temperature": "°C"}}
          if t == "config/area_registry/list": return []
          if t == "call_service": self.log.append(payload); return None
  remote = main.IRRemote(FakeHA(), main.Store(tmp / "r.json"), dict(main.DEFAULT_OPTIONS))
  remote.climate = main.Climate(remote)
  ```
  - Climate: set `climate.states[eid] = {"s": "20.5", "a": {}}` and call
    `await climate._tick(region, now)` with a synthetic `now` (seconds) to walk through steps,
    escalation, splits and short-cycle holds without waiting.
  - Learning: have `mqtt_publish` schedule `callback({"topic": ..., "payload": json.dumps({...})})`
    when it sees `{"learn_ir_code": "ON"}`, to simulate the blaster.
  - HTTP API: `aiohttp.test_utils.TestClient(TestServer(main.make_app(remote)))`.
- **UI**: serve `make_app(remote)` with the fake HA (start `climate.run()` as a task for live
  status) and screenshot with headless Chromium
  (`chromium --headless --screenshot=… --virtual-time-budget=4000 URL`). To open a dialog, inject
  a small script that clicks the right element.
- **Against a real HA** (outside HA OS):
  `DATA_DIR=./data HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> python zigbee_ir_remote/app/main.py`
  and open http://localhost:8099.

## Hard-won lessons (don't regress these)
- **Never publish `learn_ir_code: "OFF"`.** Zigbee2MQTT's zosung converter
  (`zigbee-herdsman-converters/src/lib/zosung.ts`, `tzZosung.zosung_learn_ir_code`) sends
  `{study: 0}`, i.e. *start learning*, for any value. Sending OFF after a timeout re-armed the
  blaster, so the next press was captured while nobody listened, and retries then failed as
  "old code". Fixed in 1.2.1; the blaster exits learning mode by itself.
- **Capture detection**: since zigbee-herdsman-converters of July 2026, Z2M publishes `learned_ir_timings: {timings, modulation,
  timestamp}` with each capture and clears it to `""` 500 ms later. The timestamp is how a
  repeated identical code is recognised. Older Z2M has no timestamp, so the fallback is
  "code differs from the one before" plus `ACK_WINDOW`.
- **Read learned codes from MQTT, not the HA entity**: HA states are capped at 255 characters
  and AC codes are longer. The `learned_ir_code` entity is only a fallback.
- **Supervisor token**: with `init: false` s6-overlay keeps `SUPERVISOR_TOKEN` out of the process
  env. Read it from `/run/s6/container_environment/` (fixed in 1.0.1).
- **MQTT discovery**: use `default_entity_id` (full `domain.object`), not `object_id`, which
  stopped working in HA 2026.4. `suggested_area` only applies when a device is first created;
  move existing devices with `config/device_registry/update`.
- **Full-config select**: the option → code table is a JSON object embedded in `command_template`
  (JSON is a valid Jinja dict literal, and `\uXXXX` escapes work), so it works with the add-on
  stopped. Keep `optimistic: true` together with the `state_topic`.
- **HAClient subscriptions die on reconnect**: callbacks receive `None` and are dropped.
  Long-lived subscribers (`Climate._prepare`) must detect this and resubscribe.
- **Don't feed a region its own output**: `Climate.entities()` hides entities whose unique_id
  starts with `irremote_region_`.
- The Climate loop deliberately waits about 0.5 s after `subscribe_entities` so the initial
  snapshot arrives before any decision. Otherwise missing readings look like "no sensor data"
  and the ACs get switched off.

## Behaviour decisions (agreed with the owner, keep unless asked)
- Climate: once back inside the accepted difference, ACs are switched **off**, and the next run
  starts at step 1. Escalation happens once per full `step_minutes` that an objective stays off
  target. Priority splits each step, with temperature first.
- Heat/cool ladder steps on the wrong side of the target are skipped automatically.
- If all sensors are unavailable the ACs are turned off. Disabling or deleting a region turns
  off the ACs it had running.
- Commands are only sent when they change. Manual changes to an AC (physical remote, HA UI)
  aren't overridden until the controller's next change.
- Export/import covers devices only, not regions.

## History
| Version | Change |
|---|---|
| 1.0.0 | Blaster discovery, devices/controls editor, learn + test, HA buttons, import/export |
| 1.0.1 | Startup crash fix (Supervisor token from s6 env) |
| 1.1.0 | Full-config AC devices → one HA `select`; config builder; `default_entity_id` |
| 1.2.0 | Climate tab: regions, sensors, AC ladders, escalation, humidity priority split, HA entities |
| 1.2.1 | Learning fix: never send `learn_ir_code: OFF`; timestamp-based capture detection; clearer errors |

Possible next steps (not requested yet): include regions in export/import; an HA `climate`
entity per region; a real test suite (the fake-HA snippets above are a good start); detecting
manual AC changes on `climate` entities and re-applying.
