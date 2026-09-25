# Zigbee IR Remote – Home Assistant app repository

Home Assistant app (add-on) that gives you a GUI to learn and manage IR codes on
Zigbee2MQTT IR blasters, and smart room climate control that steps your ACs (IR or Home
Assistant climate entities) until a room reaches its target temperature and humidity.
See [zigbee_ir_remote/DOCS.md](zigbee_ir_remote/DOCS.md).

## Install
Add the repository to home asistant store and search zigbee-ır-remote and download it 

Requirements: Home Assistant OS/Supervised, the MQTT integration, and Zigbee2MQTT.

## Development without Home Assistant OS
```sh
pip install aiohttp
DATA_DIR=./data HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> \
  python zigbee_ir_remote/app/main.py   # UI on http://localhost:8099
```
