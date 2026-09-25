# Zigbee IR Remote – Home Assistant app repository

A Home Assistant app (add-on) for Zigbee2MQTT IR blasters (Tuya ZS06 / TS1201, Moes UFO-R11 and
other "zosung" based blasters):

- **Remotes**: learn IR codes from your original remotes in a sidebar panel, test them, and use
  them in Home Assistant as buttons.
- **Full-config ACs**: for AC remotes that send the whole state (mode, temperature, fan…) on
  every press, learn complete configs like `Cool 22° · Fan Auto`. Home Assistant gets a single
  selector to switch between them.
- **Smart climate control**: create a region per room with its ACs (IR or normal Home Assistant
  `climate` entities) and temperature/humidity sensors. It heats, cools or dehumidifies with
  gentle settings first and steps up (e.g. Heat 26° → 28° → 30°, every 10 minutes) until the
  room reaches the target. A priority slider balances temperature against humidity, and the
  targets are sliders in both the app and Home Assistant.

User documentation: [zigbee_ir_remote/DOCS.md](zigbee_ir_remote/DOCS.md) ·
Changes: [zigbee_ir_remote/CHANGELOG.md](zigbee_ir_remote/CHANGELOG.md)

## Install
1. In Home Assistant go to **Settings → Apps (Add-ons) → App store**, open the **⋮** menu →
   **Repositories**, and add `https://github.com/revocx35/zigbee-ir-remote`.
2. Find **Zigbee IR Remote** in the store, install it and start it.
3. Open **IR Remote** in the sidebar.

Requirements: Home Assistant OS or Supervised, the MQTT integration, and Zigbee2MQTT with an IR
blaster paired.

## Development
- How the code is structured: [ARCHITECTURE.md](ARCHITECTURE.md)
- Workflow, testing approach and pitfalls (also used by AI coding sessions): [CLAUDE.md](CLAUDE.md)

Run it outside Home Assistant OS against a real Home Assistant:
```sh
pip install aiohttp
DATA_DIR=./data HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> \
  python zigbee_ir_remote/app/main.py   # UI on http://localhost:8099
```
Releases go out from `main`: bump `version` in `zigbee_ir_remote/config.yaml` and add a
`CHANGELOG.md` entry with every user-visible change.
