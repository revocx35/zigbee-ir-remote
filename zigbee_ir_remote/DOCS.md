# Zigbee IR Remote

A sidebar panel for learning and managing IR codes on Zigbee2MQTT IR blasters
(Tuya ZS06 / TS1201, Moes UFO-R11 and other "zosung" based blasters).

## How to use
1. Open **IR Remote** in the sidebar.
2. Pick your **IR blaster** at the top. Every Zigbee2MQTT device that has a
   `learned_ir_code` entity is detected automatically.
3. Click **+ New** to create a device (TV, AC, …). A template can pre-fill controls.
4. Click **Learn** on a control. The blaster goes into learning mode. Point the
   original remote at the blaster (5–20 cm) and press the button. The code is
   saved to that control. Use **▶ Test** to send it back.
5. Add or rename controls, edit/paste codes by hand (✎), drag tiles to reorder.

## Using the codes in Home Assistant
With `expose_buttons` enabled (the default), every learned control becomes an MQTT
`button` entity grouped under a device with the same name, so you can put it on
dashboards or call `button.press` in automations.

To send a code manually:
```yaml
action: mqtt.publish
data:
  topic: zigbee2mqtt/<blaster friendly name>/set
  payload: '{"ir_code_to_send": "<code>"}'
```

## Notes
- Learned codes are read straight from the blaster's MQTT state (through Home
  Assistant's MQTT integration), so codes longer than Home Assistant's
  255-character state limit still work. The `learned_ir_code` entity is
  also watched as a fallback.
- If the Zigbee2MQTT friendly name differs from the Home Assistant device name,
  set the correct topic in ⚙ Settings. Blasters can also be added there by topic.
- Data is stored in `/data/remotes.json` and included in app backups. You can
  export/import all devices as JSON from Settings.

## Options
| Option | Default | Description |
|---|---|---|
| `z2m_base_topic` | `zigbee2mqtt` | Zigbee2MQTT base topic |
| `learn_timeout` | `30` | Seconds to wait for a button press |
| `expose_buttons` | `true` | Publish learned controls as HA button entities |
| `discovery_prefix` | `homeassistant` | MQTT discovery prefix |
