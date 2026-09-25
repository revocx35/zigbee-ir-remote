# Changelog

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
