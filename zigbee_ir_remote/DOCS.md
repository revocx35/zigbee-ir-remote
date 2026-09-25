# Zigbee IR Remote

A sidebar panel for learning and managing IR codes on Zigbee2MQTT IR blasters
(Tuya ZS06 / TS1201, Moes UFO-R11 and other "zosung" based blasters), plus smart climate
control for your rooms. The panel has two tabs: **📡 Remotes** and **🌡️ Climate**.

## How to use
1. Open **IR Remote** in the sidebar (Remotes tab).
2. Pick your **IR blaster** at the top. Every Zigbee2MQTT device that has a
   `learned_ir_code` entity is detected automatically.
3. Click **+ New** to create a device (TV, AC, …). A template can pre-fill controls.
4. Click **Learn** on a control. The blaster goes into learning mode. Point the
   original remote at the blaster (5–20 cm) and press the button. The code is
   saved to that control. Use **▶ Test** to send it back.
5. Add or rename controls, edit/paste codes by hand (✎), drag tiles to reorder.

### If learning fails
The dialog says what happened:
- **"Nothing arrived from the blaster on …"**: the app heard nothing from the blaster's MQTT
  topic. If the blaster didn't react when learning started, its topic is probably wrong: set
  the Zigbee2MQTT friendly name in ⚙ Settings. Otherwise hold the remote closer (5–20 cm) and
  try again.
- **"The blaster only reported the code it had already learned"**: the code was identical to
  the last one learned and your Zigbee2MQTT is too old to tell them apart. Press the button
  again, or update Zigbee2MQTT.
- The app's **Log** tab shows a `Learning: message from …` line for everything the blaster sends
  while learning.
- The blaster leaves learning mode by itself. If it seems stuck, unplug it for a few seconds.

## Full-config ACs (one selector instead of buttons)
Many AC remotes don't send "temp up" or "fan up". Every press sends the **entire state**
(power, mode, temperature, fan, swing). For those, create the device with the
**AC (full config)** template, or switch an existing device's *Type* in **Edit device**.

- Each entry is a **config**, a complete AC state such as `Cool 22° · Fan Auto`, plus `Off`.
- **🎛️ Config builder…** creates many at once: pick mode, fan, swing and a temperature
  range, e.g. Cool 18–26° creates 9 configs.
- To learn a config, point the remote at the blaster and change it to that state. The
  **last** press is what gets learned: for Cool 22°, set the remote to 21° first, then press
  Temp ▲ once.
- In Home Assistant the device gets **one `select` entity** (named after the device) whose
  options are the learned configs. Choosing an option sends that config's code:
  ```yaml
  action: select.select_option
  target:
    entity_id: select.bedroom_ac
  data:
    option: "Cool 22° · Fan Auto"
  ```
  The code table is stored in the entity's command template, so it keeps working even
  when this app is stopped. The selector shows the last option chosen from Home
  Assistant; it can't know when the physical remote was used.

## Smart climate control (Climate tab)
The **🌡️ Climate** tab keeps rooms at a target temperature and humidity.

1. **+ New** creates a *region*: pick the Home Assistant area (room) it belongs to.
2. Add **temperature** and **humidity sensors**. The room value is the average of all of them.
   Sensors in the region's area are listed first.
3. **+ Add AC**: a Home Assistant AC (`climate.*` entity) or an IR device from the Remotes tab
   (full-config ACs work best). Its steps are filled in automatically and can be edited:
   - **IR devices**: pick configs, e.g. Heat ladder `Heat 26° → Heat 28° → Heat 30°`, plus the
     config used to turn it off.
   - **Home Assistant ACs**: mode + temperature (+ optional fan). A temperature can be exact or
     *target ± value*, so `heat, target +3` means 28° when the target is 25°.
4. Set the targets with the sliders and switch on **Automatic control**.

### How it decides
- If the room is colder than *target − accepted difference* it heats; warmer than
  *target + accepted difference* it cools; the same for humidity (dehumidify / humidify).
- Every AC starts at **step 1** of its ladder. After each **step length** (default 10 min) that
  the room is still outside the accepted difference, it moves to the next, stronger step
  (e.g. room 20°, target 25°: Heat 26° → 10 min later Heat 28° → 10 min later Heat 30°).
- Once back in range, the ACs are turned off and the next time starts again at step 1.
- Heat/cool steps set on the wrong side of the target are skipped: with a 27° target, a
  `Heat 26°` step is skipped.
- **Priority** (💧 humidity ← → 🌡️ temperature): when both are off target, each step is split
  between them. At 50/50 with 10-minute steps the ACs work on temperature for 5 minutes, then on
  humidity for 5 minutes. At 100% temperature, humidity waits until the temperature is in range.
- **Min. time between on/off** stops the ACs from being switched on and off rapidly around the
  edge of the accepted range.
- If every sensor becomes unavailable the ACs are turned off. Turning a region off (or deleting it)
  turns off the ACs it had running.

### In Home Assistant
Each region becomes a device (in its area) with:
`switch.<region>_climate_control`, `number.<region>_target_temperature`,
`number.<region>_target_humidity`, `number.<region>_temperature_priority`,
and sensors for the average temperature, humidity and current status. Changing the sliders in
Home Assistant or in the app updates both.

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
- Data (devices, blasters, climate regions) is stored in `/data/remotes.json` and included
  in app backups. **Export/Import** in Settings covers devices only; climate regions are not
  part of the export.

## Options
| Option | Default | Description |
|---|---|---|
| `z2m_base_topic` | `zigbee2mqtt` | Zigbee2MQTT base topic |
| `learn_timeout` | `30` | Seconds to wait for a button press |
| `expose_buttons` | `true` | Publish learned controls as HA button entities (or one select per full-config AC) |
| `discovery_prefix` | `homeassistant` | MQTT discovery prefix |
