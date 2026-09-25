# Architecture

Zigbee IR Remote is a Home Assistant app (add-on) with two jobs:

1. **Remotes**: learn IR codes on Zigbee2MQTT IR blasters, organise them into devices,
   and expose them to Home Assistant as `button` entities (or one `select` per full-config AC).
2. **Climate**: a controller that keeps rooms ("regions") at a target temperature and
   humidity by stepping ACs (IR devices or HA `climate` entities) through increasingly
   aggressive settings.

Everything runs in one Python process (`aiohttp`) that serves a vanilla-JS single-page UI
through Home Assistant ingress.

```
 Browser (HA sidebar, ingress)                    Home Assistant core
 ┌──────────────────────────┐   HTTP /api/*    ┌──────────────────────────────────────────┐
 │ static/index.html        │ ───────────────▶ │                                          │
 │ static/app.js  (SPA)     │                  │  websocket API  ◀──── HAClient ────┐     │
 │ static/style.css         │ ◀─────────────── │   (via Supervisor proxy)           │     │
 └──────────────────────────┘      JSON        │                                    │     │
                                               │  MQTT integration ── broker ── Zigbee2MQTT ── IR blaster
 add-on container: app/main.py                 │                                          │
 ┌─────────────────────────────────────────┐   │  climate.* entities (normal ACs)         │
 │ make_app()      aiohttp routes          │   │  sensor.* temperature / humidity         │
 │ IRRemote        blasters, learn, send,  │   └──────────────────────────────────────────┘
 │                 MQTT discovery sync     │
 │ Climate         region control loop     │   /data/remotes.json  (Store)
 │ HAClient        websocket + reconnect   │   /data/options.json  (add-on options)
 └─────────────────────────────────────────┘
```

The add-on never connects to the MQTT broker itself. It **publishes** through Home
Assistant's `mqtt.publish` service and **subscribes** through the websocket command
`mqtt/subscribe`. So it needs no MQTT credentials, only `homeassistant_api: true`.

## Files

| Path | What it is |
|---|---|
| `repository.yaml` | HA app-store repository manifest |
| `zigbee_ir_remote/config.yaml` | Add-on manifest: **version**, options schema, ingress, panel |
| `zigbee_ir_remote/translations/en.yaml` | Option names/descriptions shown in the Configuration tab |
| `zigbee_ir_remote/Dockerfile`, `build.yaml` | Alpine HA base image + `python3 py3-aiohttp`; runs `python3 -u /app/main.py` |
| `zigbee_ir_remote/DOCS.md` | User documentation (shown in the add-on's Documentation tab) |
| `zigbee_ir_remote/CHANGELOG.md` | Release notes (shown by HA on update) |
| `zigbee_ir_remote/app/main.py` | The whole backend (single file, sections below) |
| `zigbee_ir_remote/app/static/*` | The whole frontend (no build step, no dependencies) |

## Backend (`app/main.py`)

Sections, top to bottom:

| Section | Responsibility |
|---|---|
| constants, `load_options`, `new_id`, `slugify` | Options come from `/data/options.json` merged over `DEFAULT_OPTIONS`. IDs are 8 hex chars. |
| `HAClient` | Websocket client: auth, request/response futures keyed by message id, event subscriptions, auto-reconnect every 5 s. **On disconnect every subscription callback is called with `None` and dropped**; owners must resubscribe. |
| `Store` | JSON persistence of `/data/remotes.json` (atomic write via `.tmp` + rename). |
| `IRRemote` | Blaster discovery, learning, sending, and MQTT discovery sync (`sync_entities`). |
| climate section (`Climate`, `clean_ac`, `clean_step`, `name_temperature`) | Region validation, the control loop, and region entities. |
| `make_app` | aiohttp routes (`/api/*`) plus static files. `changed()` = save + background `sync_entities()`. |
| `supervisor_token`, `main` | Token from env or `/run/s6/container_environment` (s6 hides it from the env). Starts `HAClient.run`, the initial sync, `Climate.run` and the web server on port 8099. |

### Home Assistant websocket calls used
`auth`, `get_config` (temperature unit), `get_states`, `config/entity_registry/list`,
`config/device_registry/list`, `config/device_registry/update` (region device → area),
`config/area_registry/list`, `subscribe_entities`, `mqtt/subscribe`, `unsubscribe_events`,
`call_service` (`mqtt.publish`, `climate.set_temperature|set_hvac_mode|set_fan_mode`).

### Data model (`/data/remotes.json`)
```jsonc
{
  "devices": [{
    "id": "a1b2c3d4", "name": "Bedroom AC", "blaster_id": "<HA device id | manual_xxxxxxxx>",
    "kind": "buttons" | "configs",        // configs = full-state AC, exposed as one select
    "icon": "mdi:air-conditioner",
    "controls": [{ "id": "…", "name": "Cool 22° · Fan Auto", "code": "<base64>" | null,
                   "learned_at": 1790000000, "icon": null }]
  }],
  "blasters": {                            // only overrides; blasters are discovered live
    "<HA device id>": { "topic": "zigbee2mqtt/Other name" },
    "manual_xxxxxxxx": { "manual": true, "name": "Bedroom IR", "topic": "zigbee2mqtt/Bedroom IR" }
  },
  "regions": [{
    "id": "…", "name": "Bedroom", "area_id": "bedroom" | null,
    "enabled": false, "humidity_control": true,
    "target_temp": 24, "temp_tolerance": 1, "target_humidity": 50, "humidity_tolerance": 5,
    "importance": 50,                      // % of each step given to temperature
    "step_minutes": 10, "min_cycle_minutes": 3,
    "temp_sensors": ["sensor.x"], "humidity_sensors": ["sensor.y"],
    "acs": [
      { "id": "…", "type": "ir", "device_id": "…", "off_control": "<control id>" | null,
        "ladders": { "heat": [{ "control": "<control id>" }], "cool": [], "dry": [], "humidify": [] } },
      { "id": "…", "type": "climate", "entity_id": "climate.living_room",
        "ladders": { "heat": [{ "hvac_mode": "heat", "temperature": 3, "relative": true, "fan_mode": null }], … } }
    ]
  }],
  "published": ["<discovery topics currently retained>"],
  "published_sig": { "<topic>": "<last published JSON>" }
}
```
Numeric region fields are clamped by `REGION_NUMBERS` (`target_temp` by `Climate.temp_range()`,
which is 10–32 °C or 50–90 °F from HA's unit system). `Climate.update()` validates the whole
patch before applying it, so a bad field changes nothing.

### MQTT topics

| Topic | Direction | Payload |
|---|---|---|
| `<z2m_base>/<blaster>/set` | app → Z2M | `{"learn_ir_code": "ON"}` or `{"ir_code_to_send": "<code>"}` |
| `<z2m_base>/<blaster>` | Z2M → app | device state: `learned_ir_code`, `learned_ir_timings` (recent Z2M) |
| `<prefix>/button/irremote_<dev>/<ctl>/config` | discovery | one button per learned control (`kind: buttons`) |
| `<prefix>/select/irremote_<dev>/config/config` | discovery | one select per `kind: configs` device |
| `irremote/device/<dev>/state` | app → HA | option name of the config the app last sent (not retained) |
| `<prefix>/{switch,number,sensor}/irremote_region_<rid>/<key>/config` | discovery | 7 entities per region |
| `irremote/climate/<rid>/state` | app → HA | retained JSON: `enabled, target_temp, target_humidity, importance, temperature, humidity, status` |
| `irremote/climate/<rid>/<field>/set` | HA → app | `field` ∈ `enabled`, `target_temp`, `target_humidity`, `importance` |

`<prefix>` is the `discovery_prefix` option (default `homeassistant`), and `<z2m_base>` is
`z2m_base_topic` (default `zigbee2mqtt`).

### Blaster discovery (`IRRemote.blasters`)
Any HA entity whose unique_id or entity_id contains `learned_ir_code` marks its device as a
blaster. The MQTT topic is `<z2m_base>/<HA device name>`. Zigbee2MQTT's discovery sets the HA
device name to the Z2M friendly name; `name_by_user` is only used for display. Topic overrides
and manual blasters live in `store.data["blasters"]`. Results are cached for 10 s.

### Learning (`IRRemote.learn`)
1. Subscribe to the blaster's state topic (`mqtt/subscribe`) and to its `learned_ir_code`
   entity (fallback). Record the codes already present as *stale*.
2. Publish `{"learn_ir_code": "ON"}` and wait up to `learn_timeout` seconds.
3. A code is accepted when either:
   - the message carries `learned_ir_timings.timestamp` ≥ learning start − `CLOCK_SKEW`
     (recent Z2M stamps every capture, so even a code identical to the last one is accepted), or
   - its code is not stale and it didn't arrive in the first `ACK_WINDOW` seconds.
4. **Nothing is sent to stop learning.** Z2M's zosung converter sends "start learning" for any
   `learn_ir_code` value, so publishing `OFF` re-arms the blaster and swallows the next press
   (this was the 1.2.1 bug). The blaster leaves learning mode by itself.
5. On timeout `LearnTimeout` carries a specific message: nothing arrived (topic or blaster
   problem) versus only the old code arrived. Every message during learning is logged as
   `Learning: message from …`.

Codes are read from MQTT because HA entity states are capped at 255 characters and AC codes
are longer.

### Entity sync (`IRRemote.sync_entities`)
Builds the full set of wanted discovery configs, then publishes (retained) only the ones whose
JSON changed (`published_sig`) and clears (empty retained payload) topics that are no longer
wanted. It runs after every change and at startup, serialised by a lock. If blaster lookup
fails it aborts rather than deleting entities. `expose_buttons` gates device entities only;
region entities are always published. It then calls `Climate.assign_areas()`.

- **Buttons**: `payload_press` = `{"ir_code_to_send": code}` sent to the blaster's `/set`.
- **Full-config select**: the option → code table is embedded in `command_template` as a Jinja
  dict (a JSON object is a valid Jinja dict literal), so HA sends codes straight to the blaster
  even when the add-on is stopped. `optimistic: true` plus a `state_topic` that the app writes
  when it sends a config itself (UI Test, climate controller).
- Entity ids are set with `default_entity_id` (`object_id` was removed in HA 2026.4).

### Climate controller (`Climate`)
`Climate.run()` loops every `TICK_SECONDS` (5 s), or immediately after `poke()`, which every
update calls:

1. `_prepare()`: load unit and areas once per connection, subscribe to
   `irremote/climate/+/+/set`, and `subscribe_entities` for every sensor and climate entity used
   by any region. It resubscribes when that set changes or after a reconnect. Live states land
   in `self.states`.
2. For each region, `_tick(region, now)`, then `publish_state(region)` (only when the JSON changed).

Per-region runtime state lives in memory only (`self.rt[rid]`, see `_runtime`):
`phase` (`idle|active|disabled`), `active` (`temp|hum|None`), the direction and level of each
objective (`temp_dir/temp_level`, `hum_dir/hum_level`), `section_start`, `changed_at`, and
per-AC `sent`/`errors`/`steps`.

`_tick` in order:
1. Average the sensors (`None` if none are readable).
2. Disabled: turn off every AC whose last command wasn't off (`only_running`), then idle.
3. Needs: `heat` if temp < target − tol, `cool` if temp > target + tol; `dry` / `humidify` the same
   for humidity. A direction only counts if some AC has a usable ladder for it.
4. **Short-cycle guard**: switching between idle and active is postponed until
   `min_cycle_minutes` have passed since the last switch (`hold_until`).
5. When a direction changes, its level resets to 0.
6. **Steps**: a step lasts `step_minutes`. At the end of a step, every objective that was off
   target for the whole step goes up one level (capped at the longest ladder).
7. **Split**: if both objectives need action, temperature is active for the first
   `importance`% of the step and humidity for the rest (`switch_at`).
8. Commands: each AC runs `ladder[min(level, len-1)]` for the active objective, or its off
   command. `_ladder()` drops IR steps without a learned code and, for heat/cool, steps whose
   setpoint is on the wrong side of the target (IR setpoints are parsed from the config name by
   `name_temperature`). If that removes everything, the last step is kept.
9. `_apply()` sends only commands that differ from the last one sent to that AC (compared as
   JSON). A failed send is retried after `RETRY_SECONDS`.

Sending: IR → `IRRemote.send_control` (which also updates the select state topic).
`climate` → `set_temperature` (with `hvac_mode`) or `set_hvac_mode`, then `set_fan_mode`.
Relative temperatures are `target + value`, clamped to the entity's `min_temp`/`max_temp` and
rounded to `target_temp_step`.

Region entities in HA: `switch.<slug>_climate_control`, `number.<slug>_target_temperature`,
`number.<slug>_target_humidity`, `number.<slug>_temperature_priority`,
`sensor.<slug>_temperature`, `sensor.<slug>_humidity`, `sensor.<slug>_status`. They are grouped
under device `irremote_region_<rid>` with `suggested_area`. Because `suggested_area` only
applies when a device is created, `assign_areas()` moves existing devices with
`config/device_registry/update`. The editor's entity list (`Climate.entities`) hides entities
whose unique_id starts with `irremote_region_`, so a region can't read its own average.

### HTTP API

| Method & path | Purpose |
|---|---|
| `GET /api/state[?refresh=1]` | blasters, devices, learning sessions, options, connection state |
| `POST/PUT/DELETE /api/blasters[/{bid}]` | manual blasters and topic overrides |
| `POST/PUT/DELETE /api/devices[/{did}]` | devices (`kind`, name, blaster, control order) |
| `POST/PUT/DELETE /api/devices/{did}/controls[/{cid}]` | controls (config names must be unique in `configs` devices) |
| `POST /api/devices/{did}/controls/{cid}/learn` | blocks until learned; 408 with a reason on timeout |
| `POST /api/learn/cancel` | cancel a running learn session |
| `POST /api/devices/{did}/controls/{cid}/send` | send a code (the UI's ▶ Test button) |
| `GET /api/climate[?refresh=1]` | unit, ranges, areas, candidate entities, regions, status |
| `GET /api/climate/status` | regions and live status (polled every 5 s by the UI) |
| `POST/PUT/DELETE /api/regions[/{rid}]` | regions; `PUT` takes any partial set of fields |
| `GET /api/export`, `POST /api/import` | backup of devices (regions are not included) |

## Frontend (`app/static`)

- `index.html`: shell with a top bar (tabs, blaster picker, refresh, settings), a sidebar list, a
  `#main` area, a single `<dialog id="dialog">` and toasts.
- `app.js`: plain script, no framework and no build step. One `state` object. Views render
  with template strings into `innerHTML`, and every interpolated value goes through `esc()`.
  **All URLs are relative** (`api/...`) so the page works behind ingress.
  - Remotes view: `renderBlasters`, `renderDevices`, `renderMain`, `controlTile`; dialogs for
    devices, controls, settings and the config builder; `startLearn` runs the learn dialog.
  - Climate view (`// climate regions` section): `renderRegionList`, `renderRegionMain` (cards:
    live status, targets, algorithm, sensors, ACs with ladder editors), `addAcDialog`,
    `addStepDialog`, `autoLadders` (guesses ladders from config names or HVAC modes).
    `pollClimate` refreshes status every 5 s and `updateClimateLive` patches only the live parts
    and inputs that don't have focus. Countdowns (`data-until`) tick every second using the
    server clock offset.
  - Event delegation on `#main`: `click` / `change` / `input` handlers branch on `state.view`.
  - `localStorage` (`irremote.*`) remembers the selected blaster, device, view and region.
- `style.css`: CSS variables with light and dark themes; no external assets.
