# Zigbee IR Remote – Home Assistant app repository

Home Assistant app (add-on) that gives you a GUI to learn and manage IR codes on
Zigbee2MQTT IR blasters. See [zigbee_ir_remote/DOCS.md](zigbee_ir_remote/DOCS.md).

## Install
1. Push this folder to a Git repository (e.g. GitHub). Or copy `zigbee_ir_remote/`
   into `/addons/` on your Home Assistant host (Samba/SSH app) to install it as a local app.
2. **Settings → Apps → App store → ⋮ → Repositories**, add the repository URL.
3. Install **Zigbee IR Remote**, start it, enable **Show in sidebar**.

Requirements: Home Assistant OS/Supervised, the MQTT integration, and Zigbee2MQTT.

## Development without Home Assistant OS
```sh
pip install aiohttp
DATA_DIR=./data HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> \
  python zigbee_ir_remote/app/main.py   # UI on http://localhost:8099
```
