# Changelog

## 1.2.1
- Fix learning getting stuck: after a timeout or cancel the app sent `learn_ir_code: OFF`, which
  Zigbee2MQTT treats as *start* learning, so the blaster swallowed the next button press and later
  attempts timed out.
- Re-learning a button whose code is identical to the last learned one now works (uses the capture
  timestamp that recent Zigbee2MQTT versions publish).
- Failed learns say what happened (nothing received from the blaster vs. only the old code) and each
  message from the blaster is logged.

## 1.2.0
- New **Climate** tab: smart temperature/humidity control per room ("region").
  - A region is linked to a Home Assistant area and holds one or more ACs (HA `climate` entities
    or IR devices from this app, including full-config ACs) plus temperature/humidity sensors
    (the room value is their average).
  - Each AC has step ladders for heat, cool, dehumidify and humidify. When the room is outside
    the accepted difference the ACs start at step 1, and every *step length* (10 min by default)
    that the room is still off target they move to the next, stronger step. Back in range, they
    are switched off.
  - Humidity ↔ temperature priority slider splits each step between the two when both are off target.
  - Adjustable: accepted differences, step length, minimum time between on/off (short-cycle protection).
  - Home Assistant gets a device per region: on/off switch, target temperature, target humidity
    and priority sliders, and average temperature, humidity and status sensors.
- Full-config AC selectors now show the config last sent by this app.

## 1.1.0
- New device type **AC (full config)** for AC remotes that send the whole state (mode, temperature,
  fan, swing) on every press. You learn complete configs like "Cool 22° · Fan Auto" and Home Assistant
  gets a single `select` entity to switch between them, instead of one button per control.
- Config builder: create a whole temperature range of configs (e.g. Cool 18–26°, Fan Auto) in one go.
- Existing devices can be switched between "Buttons" and "Full config" in *Edit device*.
- MQTT discovery now uses `default_entity_id` instead of `object_id` (removed in Home Assistant 2026.4).

## 1.0.1
- Fix startup crash (`KeyError: 'HA_TOKEN'`): read the Supervisor token from the s6 container environment.

## 1.0.0
- Initial release: blaster auto-discovery, devices/controls editor, learn + test, HA button export, import/export.
