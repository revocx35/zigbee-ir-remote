"use strict";

// All URLs are relative so the page works behind Home Assistant ingress.
const TEMPLATES = [
  { key: "blank", emoji: "➕", name: "Blank", controls: [] },
  { key: "tv", emoji: "📺", name: "TV", controls: ["Power", "Volume Up", "Volume Down", "Mute", "Channel Up", "Channel Down", "Input", "Up", "Down", "Left", "Right", "OK", "Back", "Home", "Menu"] },
  { key: "ac", emoji: "❄️", name: "AC (buttons)", controls: ["Power On", "Power Off", "Temp Up", "Temp Down", "Mode", "Fan Speed", "Swing"] },
  { key: "ac_configs", emoji: "🎛️", name: "AC (full config)", kind: "configs", controls: ["Off"] },
  { key: "fan", emoji: "🌀", name: "Fan", controls: ["Power", "Speed Up", "Speed Down", "Oscillate", "Timer"] },
  { key: "audio", emoji: "🔊", name: "Soundbar / Amp", controls: ["Power", "Volume Up", "Volume Down", "Mute", "Input"] },
  { key: "projector", emoji: "📽️", name: "Projector", controls: ["Power On", "Power Off", "Input", "Menu", "OK", "Back"] },
  { key: "light", emoji: "💡", name: "LED light", controls: ["On", "Off", "Brighter", "Dimmer", "Red", "Green", "Blue", "White"] },
];
// Config builder for "configs" devices (ACs whose remote sends the whole state on every press)
const AC_MODES = ["Cool", "Heat", "Dry", "Fan only", "Auto"];
const AC_FANS = ["Auto", "Low", "Medium", "High", "Turbo", "Quiet"];
const AC_TEMPS = Array.from({ length: 17 }, (_, i) => 16 + i);
const CONFIG_PRESETS = ["Off", "Cool 22°", "Cool 24°", "Heat 24°", "Dry", "Fan only"];
const SUGGESTIONS = ["Power", "Power On", "Power Off", "Volume Up", "Volume Down", "Mute", "Channel Up", "Channel Down", "Input", "OK", "Back", "Home", "Menu", "Up", "Down", "Left", "Right", "Play", "Pause", "Stop"];

const state = {
  blasters: [],
  devices: [],
  learning: {},
  options: { learn_timeout: 30 },
  blasterId: localGet("blaster"),
  deviceId: localGet("device"),
  view: localGet("view") === "climate" ? "climate" : "remotes",
  climate: null,
  regionId: localGet("region"),
  clockOffset: 0,
  banner: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function localGet(k) { try { return localStorage.getItem("irremote." + k); } catch { return null; } }
function localSet(k, v) { try { v == null ? localStorage.removeItem("irremote." + k) : localStorage.setItem("irremote." + k, v); } catch { /* ignore */ } }

// ------------------------------------------------------------------ API
async function api(method, path, body) {
  const res = await fetch("api/" + path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    const err = new Error((data && data.error) || text || res.statusText);
    err.status = res.status;
    throw err;
  }
  return data;
}

async function load(refresh = false) {
  const data = await api("GET", "state" + (refresh ? "?refresh=1" : ""));
  Object.assign(state, {
    blasters: data.blasters, devices: data.devices, learning: data.learning, options: data.options,
  });
  if (!data.connected || data.error) {
    state.banner = { text: data.error || "Connecting to Home Assistant…", always: true };
  } else if (!data.blasters.length) {
    state.banner = { text: "No IR blasters found. The app looks for Zigbee2MQTT devices with a “learned_ir_code” entity. You can also add a blaster by MQTT topic in Settings (⚙)." };
  } else {
    state.banner = null;
  }
  if (!state.blasters.some((b) => b.id === state.blasterId)) {
    state.blasterId = state.blasters[0]?.id ?? null;
  }
  render();
}

function toast(msg, error = false) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), error ? 6000 : 2500);
}

async function run(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); throw e; }
}

// ------------------------------------------------------------------ helpers
const blaster = (id) => state.blasters.find((b) => b.id === id);
const currentDevices = () => state.devices.filter((d) => d.blaster_id === state.blasterId);
const isConfigs = (dev) => dev?.kind === "configs";
const currentDevice = () => state.devices.find((d) => d.id === state.deviceId && d.blaster_id === state.blasterId);

function selectBlaster(id) {
  state.blasterId = id;
  localSet("blaster", id);
  if (!currentDevice()) selectDevice(currentDevices()[0]?.id ?? null, false);
  render();
}
function selectDevice(id, rerender = true) {
  state.deviceId = id;
  localSet("device", id);
  if (rerender) render();
}

// ------------------------------------------------------------------ rendering
function render() {
  const climate = state.view === "climate";
  document.body.dataset.view = state.view;
  document.querySelectorAll("#tabs [data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
  $("#sidebar-title").textContent = climate ? "Regions" : "Devices";
  const banner = $("#banner");
  banner.hidden = !state.banner || (climate && !state.banner.always);
  banner.textContent = state.banner?.text ?? "";
  if (climate) {
    $("#new-btn").disabled = false;
    renderRegionList();
    renderRegionMain();
    return;
  }
  renderBlasters();
  renderDevices();
  renderMain();
}

function setView(view) {
  state.view = view;
  localSet("view", view);
  render();
  if (view === "climate") run(() => loadClimate());
}

function renderBlasters() {
  const sel = $("#blaster-select");
  const orphans = [...new Set(state.devices.map((d) => d.blaster_id))].filter((id) => !blaster(id));
  sel.innerHTML = state.blasters.map((b) =>
    `<option value="${esc(b.id)}">${esc(b.name)}${b.model ? " — " + esc(b.model) : ""}</option>`).join("")
    + orphans.map((id) => `<option value="${esc(id)}">⚠ Missing blaster (${esc(id.slice(0, 8))})</option>`).join("")
    || `<option value="">No blasters found</option>`;
  sel.value = state.blasterId ?? "";
  sel.disabled = !state.blasters.length && !orphans.length;
  $("#new-btn").disabled = !blaster(state.blasterId);
}

function renderDevices() {
  const list = $("#device-list");
  const devs = currentDevices();
  if (!currentDevice() && devs.length) state.deviceId = devs[0].id;
  if (!devs.length) {
    list.innerHTML = `<li class="empty">${state.blasterId ? "No devices on this blaster yet." : "Select an IR blaster first."}</li>`;
    return;
  }
  list.innerHTML = devs.map((d) => {
    const learned = d.controls.filter((c) => c.code).length;
    return `<li data-id="${esc(d.id)}" class="${d.id === state.deviceId ? "active" : ""}">
      <span>${esc(d.name)}</span><span class="count">${learned}/${d.controls.length}</span></li>`;
  }).join("");
}

function renderMain() {
  const main = $("#main");
  const b = blaster(state.blasterId);
  const dev = currentDevice();
  if (!state.blasterId) {
    main.innerHTML = `<div class="empty-state"><h2>No IR blaster selected</h2>
      <p>Pair a Zigbee IR blaster (e.g. Tuya ZS06 / Moes UFO-R11) with Zigbee2MQTT and it will show up here automatically.</p></div>`;
    return;
  }
  if (!dev) {
    main.innerHTML = `<div class="empty-state"><h2>Create your first device</h2>
      <p>A device is something you control with an IR remote — a TV, air conditioner, fan… Add it, then learn each button from its original remote.</p>
      <button class="btn primary" data-action="new-device" ${b ? "" : "disabled"}>+ New device</button></div>`;
    return;
  }
  const learned = dev.controls.filter((c) => c.code).length;
  const pct = dev.controls.length ? Math.round((learned / dev.controls.length) * 100) : 0;
  const nextUnlearned = dev.controls.find((c) => !c.code);
  const cfg = isConfigs(dev);
  const noun = cfg ? "configs" : "controls";
  const suggestions = cfg ? CONFIG_PRESETS : SUGGESTIONS;
  main.innerHTML = `
    <div class="device-head">
      <div>
        <h1>${esc(dev.name)}</h1>
        <div class="sub">via ${b ? esc(b.name) + ` · <code>${esc(b.topic)}</code>` : "⚠ blaster not found — edit the device to pick another"}</div>
      </div>
      <div class="actions">
        ${nextUnlearned ? `<button class="btn primary" data-action="learn" data-id="${esc(nextUnlearned.id)}">Learn next: ${esc(nextUnlearned.name)}</button>` : ""}
        <button class="btn" data-action="edit-device">Edit device</button>
      </div>
    </div>
    ${cfg ? `<div class="info-box">Full-config device: each config is a complete AC state (mode, temperature, fan…).
      ${state.options.expose_buttons ? `Home Assistant gets <b>one selector</b> listing the ${learned} learned config${learned === 1 ? "" : "s"}; choosing one sends its code.` : "Publishing to Home Assistant is turned off in the app's Configuration tab."}</div>` : ""}
    ${dev.controls.length ? `<div class="progress-line"><div class="progress"><div style="width:${pct}%"></div></div>${learned} of ${dev.controls.length} ${noun} learned</div>` : ""}
    <div class="controls-grid" id="controls-grid">
      ${dev.controls.map((c) => controlTile(c, !!b)).join("")}
    </div>
    ${dev.controls.length ? "" : `<p class="hint">${cfg ? "No configs yet. Build some below, e.g. Cool 22° · Fan Auto." : "No controls yet. Add the buttons you want to learn below."}</p>`}
    <form class="add-control" id="add-control-form">
      <input name="name" placeholder="${cfg ? "New config name, e.g. Cool 22° · Fan Auto" : "New control name, e.g. Power"}" autocomplete="off" maxlength="80">
      <button class="btn" type="submit" name="mode" value="add">Add</button>
      <button class="btn primary" type="submit" name="mode" value="learn" ${b ? "" : "disabled"}>Add &amp; learn</button>
      ${cfg ? `<button class="btn" type="button" data-action="build-configs">🎛️ Config builder…</button>` : ""}
    </form>
    <div class="chips">${suggestions.filter((s) => !dev.controls.some((c) => c.name.toLowerCase() === s.toLowerCase()))
      .map((s) => `<button class="chip" data-action="suggest" data-name="${esc(s)}">+ ${esc(s)}</button>`).join("")}</div>`;
}

function controlTile(c, hasBlaster) {
  const when = c.learned_at ? new Date(c.learned_at * 1000).toLocaleString() : null;
  return `<div class="control" draggable="true" data-id="${esc(c.id)}">
    <div class="control-top">
      <span class="handle" title="Drag to reorder">⋮⋮</span>
      <span class="dot ${c.code ? "ok" : ""}"></span>
      <span class="control-name" title="${esc(c.name)}">${esc(c.name)}</span>
    </div>
    <div class="control-status">${c.code ? (when ? "Learned " + esc(when) : "Code set") : "Not learned yet"}</div>
    <div class="control-actions">
      <button class="btn small ${c.code ? "" : "primary"}" data-action="learn" data-id="${esc(c.id)}" ${hasBlaster ? "" : "disabled"}>${c.code ? "Re-learn" : "Learn"}</button>
      <button class="btn small" data-action="send" data-id="${esc(c.id)}" ${c.code && hasBlaster ? "" : "disabled"} title="Send this code">▶ Test</button>
      <button class="btn small ghost" data-action="edit-control" data-id="${esc(c.id)}" title="Edit">✎</button>
    </div>
  </div>`;
}

// ------------------------------------------------------------------ dialogs
const dlg = $("#dialog");

function openDialog(html, onSubmit) {
  dlg.innerHTML = html;
  dlg.onclose = null;
  const form = $("form", dlg);
  if (form && onSubmit) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.submitter;
      if (btn?.value === "cancel") { dlg.close(); return; }
      if (btn) btn.disabled = true;
      try {
        if ((await onSubmit(new FormData(form), btn?.value)) !== false) dlg.close();
      } catch (err) {
        toast(err.message, true);
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  }
  if (!dlg.open) dlg.showModal();
  $("[autofocus]", dlg)?.focus();
}

function blasterOptions(selected) {
  return state.blasters.map((b) =>
    `<option value="${esc(b.id)}" ${b.id === selected ? "selected" : ""}>${esc(b.name)}</option>`).join("");
}

function newDeviceDialog() {
  let tpl = "tv";
  openDialog(`<form method="dialog">
    <div class="dlg-head">New IR device</div>
    <div class="dlg-body">
      <label>Name<input name="name" required maxlength="80" placeholder="Living room TV" autofocus></label>
      <label>IR blaster<select name="blaster_id">${blasterOptions(state.blasterId)}</select></label>
      <div><div class="hint" style="margin-bottom:6px">Start from a template (you can add/remove controls later)</div>
        <div class="template-grid">${TEMPLATES.map((t) =>
          `<button type="button" class="template ${t.key === tpl ? "selected" : ""}" data-tpl="${t.key}"><span class="emoji">${t.emoji}</span>${esc(t.name)}</button>`).join("")}</div>
        <div class="hint" id="tpl-hint" style="margin-top:8px" hidden>For ACs whose remote sends the whole state (mode, temperature, fan…) with every press.
          You learn complete configs like “Cool 22° · Fan Auto”, and Home Assistant gets a single selector to switch between them.</div>
      </div>
    </div>
    <div class="dlg-foot">
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="ok">Create</button>
    </div></form>`, async (fd) => {
    const t = TEMPLATES.find((x) => x.key === tpl);
    const dev = await api("POST", "devices", { name: fd.get("name"), blaster_id: fd.get("blaster_id"), kind: t.kind || "buttons", controls: t.controls });
    state.devices.push(dev);
    state.blasterId = dev.blaster_id;
    localSet("blaster", dev.blaster_id);
    selectDevice(dev.id);
    renderBlasters();
    toast(`Created “${dev.name}”`);
  });
  dlg.querySelectorAll(".template").forEach((el) => el.addEventListener("click", () => {
    tpl = el.dataset.tpl;
    dlg.querySelectorAll(".template").forEach((x) => x.classList.toggle("selected", x === el));
    $("#tpl-hint", dlg).hidden = TEMPLATES.find((x) => x.key === tpl).kind !== "configs";
  }));
}

function editDeviceDialog(dev) {
  const known = blaster(dev.blaster_id);
  openDialog(`<form method="dialog">
    <div class="dlg-head">Edit device</div>
    <div class="dlg-body">
      <label>Name<input name="name" required maxlength="80" value="${esc(dev.name)}" autofocus></label>
      <label>IR blaster<select name="blaster_id">${known ? "" : `<option value="${esc(dev.blaster_id)}" selected>⚠ Missing blaster</option>`}${blasterOptions(dev.blaster_id)}</select></label>
      <span class="hint">Moving a device to another blaster keeps its learned codes (IR codes are the same for any blaster of the same model).</span>
      <label>Type<select name="kind">
        <option value="buttons" ${isConfigs(dev) ? "" : "selected"}>Buttons: one Home Assistant button per control</option>
        <option value="configs" ${isConfigs(dev) ? "selected" : ""}>Full config (AC): one Home Assistant selector of configs</option>
      </select></label>
      <span class="hint">Use “Full config” for AC remotes that send mode, temperature and fan together on every press. Learned codes are kept when switching.</span>
    </div>
    <div class="dlg-foot">
      <button class="btn danger left" value="delete" formnovalidate>Delete device</button>
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="ok">Save</button>
    </div></form>`, async (fd, action) => {
    if (action === "delete") {
      if (!confirm(`Delete “${dev.name}” and all its ${dev.controls.length} controls?`)) return false;
      await api("DELETE", `devices/${dev.id}`);
      state.devices = state.devices.filter((d) => d.id !== dev.id);
      selectDevice(currentDevices()[0]?.id ?? null);
      toast("Device deleted");
      return;
    }
    Object.assign(dev, await api("PUT", `devices/${dev.id}`, { name: fd.get("name"), blaster_id: fd.get("blaster_id"), kind: fd.get("kind") }));
    if (dev.blaster_id !== state.blasterId) selectBlaster(dev.blaster_id); else render();
  });
}

function editControlDialog(dev, ctl) {
  openDialog(`<form method="dialog">
    <div class="dlg-head">Edit ${isConfigs(dev) ? "config" : "control"}</div>
    <div class="dlg-body">
      <label>Name<input name="name" required maxlength="80" value="${esc(ctl.name)}" autofocus></label>
      <label>IR code<textarea name="code" class="mono" placeholder="Learn it, or paste a Zigbee2MQTT (base64) IR code">${esc(ctl.code || "")}</textarea></label>
      <span class="hint">${ctl.code ? `${ctl.code.length} characters` : "No code yet"}</span>
    </div>
    <div class="dlg-foot">
      <button class="btn danger left" value="delete" formnovalidate>Delete</button>
      ${ctl.code ? `<button class="btn" type="button" id="copy-code">Copy</button>` : ""}
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="ok">Save</button>
    </div></form>`, async (fd, action) => {
    if (action === "delete") {
      if (!confirm(`Delete ${isConfigs(dev) ? "config" : "control"} “${ctl.name}”?`)) return false;
      await api("DELETE", `devices/${dev.id}/controls/${ctl.id}`);
      dev.controls = dev.controls.filter((c) => c.id !== ctl.id);
      render();
      return;
    }
    Object.assign(ctl, await api("PUT", `devices/${dev.id}/controls/${ctl.id}`, { name: fd.get("name"), code: fd.get("code") }));
    render();
  });
  $("#copy-code", dlg)?.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(ctl.code); toast("Code copied"); } catch { toast("Clipboard not available", true); }
  });
}

// ------------------------------------------------------------------ learning
let learnTimer = null;

function learnView(dev, ctl, phase, extra = "") {
  const b = blaster(dev.blaster_id);
  const icon = { waiting: "📡", ok: "✅", fail: "⚠️" }[phase];
  return `<div class="learn">
    <div class="pulse ${phase === "waiting" ? "active" : phase}">${icon}</div>
    ${extra}
  </div>`.replace("%BLASTER%", esc(b?.name ?? "the blaster"));
}

async function startLearn(dev, ctl) {
  const b = blaster(dev.blaster_id);
  if (!b) { toast("This device's blaster was not found", true); return; }
  let remaining = state.options.learn_timeout || 30;
  const how = isConfigs(dev)
    ? `<h3>Send “${esc(ctl.name)}” from your remote</h3>
      <p>Point the remote at <b>%BLASTER%</b> (5–20 cm) and change it to this config; the <b>last</b> press is what gets learned.
      Example: for Cool 22°, set it to Cool 21° first, then press Temp ▲ once.</p>`
    : `<h3>Press “${esc(ctl.name)}” on your remote</h3>
      <p>Point the original remote at <b>%BLASTER%</b> from close range (5–20 cm) and press the button once.</p>`;
  openDialog(`<form method="dialog">${learnView(dev, ctl, "waiting", `
      ${how}
      <p>Waiting… <span class="countdown" id="countdown">${remaining}s</span></p>`)}
    <div class="dlg-foot"><button class="btn" value="abort">Cancel</button></div></form>`, async (_, action) => {
    if (action === "abort") {
      await api("POST", "learn/cancel", { blaster_id: b.id }).catch(() => {});
      return false; // the pending learn request resolves with {cancelled}, which closes the dialog
    }
  });
  dlg.onclose = () => { clearInterval(learnTimer); api("POST", "learn/cancel", { blaster_id: b.id }).catch(() => {}); };
  learnTimer = setInterval(() => {
    remaining = Math.max(0, remaining - 1);
    const el = $("#countdown", dlg);
    if (el) el.textContent = remaining + "s";
  }, 1000);

  let result, error;
  try {
    result = await api("POST", `devices/${dev.id}/controls/${ctl.id}/learn`);
  } catch (e) {
    error = e;
  }
  clearInterval(learnTimer);
  dlg.onclose = null;
  if (!dlg.open) return; // user closed with Esc

  if (result?.cancelled) { dlg.close(); return; }
  if (error) {
    dlg.innerHTML = `<form method="dialog">${learnView(dev, ctl, "fail", `
        <h3>Nothing learned</h3><p>${esc(error.message)}</p>`)}
      <div class="dlg-foot"><button class="btn" value="close">Close</button>
      <button class="btn primary" type="button" id="retry">Try again</button></div></form>`;
    $("#retry", dlg).onclick = () => startLearn(dev, ctl);
    return;
  }

  Object.assign(ctl, result);
  render();
  const next = dev.controls.find((c) => !c.code);
  dlg.innerHTML = `<form method="dialog">${learnView(dev, ctl, "ok", `
      <h3>Learned “${esc(ctl.name)}”</h3>
      <p>The code was saved to this ${isConfigs(dev) ? "config" : "control"}. Press <b>Test</b> to make sure the device reacts.</p>
      <div class="code-preview mono">${esc(ctl.code)}</div>`)}
    <div class="dlg-foot">
      <button class="btn left" type="button" id="test">▶ Test</button>
      <button class="btn" type="button" id="again">Re-learn</button>
      ${next ? `<button class="btn primary" type="button" id="next">Next: ${esc(next.name)}</button>` : `<button class="btn primary" value="close">Done</button>`}
    </div></form>`;
  $("#test", dlg).onclick = () => sendControl(dev, ctl);
  $("#again", dlg).onclick = () => startLearn(dev, ctl);
  if (next) $("#next", dlg).onclick = () => startLearn(dev, next);
}

async function sendControl(dev, ctl) {
  await run(() => api("POST", `devices/${dev.id}/controls/${ctl.id}/send`));
  toast(`Sent “${ctl.name}”`);
}

// ------------------------------------------------------------------ config builder
function configName(mode, temp, fan, swing) {
  const parts = [mode + (temp ? ` ${temp}°` : "")];
  if (fan) parts.push(`Fan ${fan}`);
  if (swing) parts.push(`Swing ${swing}`);
  return parts.join(" · ");
}

function configBuilderDialog(dev) {
  const opts = (values, blank) => (blank ? `<option value="">${blank}</option>` : "")
    + values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  openDialog(`<form method="dialog">
    <div class="dlg-head">Config builder</div>
    <div class="dlg-body">
      <div class="hint">Pick the settings; a config is created for each temperature in the range. Leave a field on “—” to keep it out of the name.</div>
      <div class="field-grid">
        <label>Mode<select name="mode">${opts(AC_MODES)}</select></label>
        <label>Fan<select name="fan">${opts(AC_FANS, "—")}</select></label>
        <label>Temperature from<select name="from">${opts(AC_TEMPS, "—")}</select></label>
        <label>to<select name="to">${opts(AC_TEMPS, "only one")}</select></label>
        <label>Swing<select name="swing">${opts(["On", "Off"], "—")}</select></label>
      </div>
      <div><div class="hint" id="cb-summary"></div><div class="chips" id="cb-preview"></div></div>
    </div>
    <div class="dlg-foot">
      ${dev.controls.some((c) => c.name === "Off") ? "" : `<button class="btn left" type="button" id="cb-off">+ Off</button>`}
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn" value="add">Add</button>
      <button class="btn primary" value="learn" ${blaster(dev.blaster_id) ? "" : "disabled"}>Add &amp; learn</button>
    </div></form>`, async (fd, action) => {
    const names = preview();
    if (!names.length) { toast("Nothing new to add", true); return false; }
    const added = [];
    try {
      for (const name of names) {
        const ctl = await api("POST", `devices/${dev.id}/controls`, { name });
        dev.controls.push(ctl);
        added.push(ctl);
      }
    } finally {
      render();
    }
    toast(`Added ${added.length} config${added.length === 1 ? "" : "s"}`);
    if (action === "learn") { startLearn(dev, added[0]); return false; }
  });
  const form = $("form", dlg);
  form.elements.from.value = "22";
  function preview() {
    const f = form.elements;
    let temps = [f.from.value];
    if (f.from.value && f.to.value) {
      const [a, b] = [Number(f.from.value), Number(f.to.value)].sort((x, y) => x - y);
      temps = AC_TEMPS.filter((t) => t >= a && t <= b).map(String);
    }
    const all = temps.map((t) => configName(f.mode.value, t, f.fan.value, f.swing.value));
    const fresh = all.filter((n) => !dev.controls.some((c) => c.name === n));
    $("#cb-summary", dlg).textContent = fresh.length
      ? `Will add ${fresh.length} config${fresh.length === 1 ? "" : "s"}${all.length > fresh.length ? ` (${all.length - fresh.length} already exist)` : ""}:`
      : "These configs already exist.";
    $("#cb-preview", dlg).innerHTML = fresh.map((n) => `<span class="chip static">${esc(n)}</span>`).join("");
    return fresh;
  }
  form.addEventListener("change", preview);
  preview();
  $("#cb-off", dlg)?.addEventListener("click", () => run(async () => {
    dev.controls.push(await api("POST", `devices/${dev.id}/controls`, { name: "Off" }));
    $("#cb-off", dlg).remove();
    render();
    toast("Added “Off”");
  }));
}

// ------------------------------------------------------------------ settings
function settingsDialog() {
  openDialog(`<form method="dialog">
    <div class="dlg-head">Settings</div>
    <div class="dlg-body">
      <div class="hint">IR blasters are detected from Home Assistant automatically. Only change the MQTT topic if your Zigbee2MQTT friendly name differs from the Home Assistant device name.</div>
      ${state.blasters.map((b) => `<div class="blaster-row" data-id="${esc(b.id)}">
        <div><b>${esc(b.name)}</b> <span class="meta">${esc(b.model || "")}${b.learned_entity ? " · " + esc(b.learned_entity) : ""}</span></div>
        <div class="row"><input name="topic" value="${esc(b.topic)}" placeholder="${esc(b.default_topic)}">
          <button class="btn small" type="button" data-save>Save</button>
          ${b.manual ? `<button class="btn small danger" type="button" data-remove>Remove</button>` : ""}</div>
      </div>`).join("") || `<div class="hint">No blasters detected.</div>`}
      <details><summary>Add a blaster manually</summary>
        <div class="dlg-body" style="padding:10px 0 0">
          <label>Name<input id="mb-name" placeholder="Bedroom IR"></label>
          <label>Zigbee2MQTT topic<input id="mb-topic" placeholder="${esc(state.options.z2m_base_topic)}/Bedroom IR"></label>
          <button class="btn" type="button" id="mb-add">Add blaster</button>
        </div>
      </details>
      <div class="row" style="display:flex;gap:8px;flex-wrap:wrap">
        <a class="btn" href="api/export" download="ir-remotes.json">Export all devices</a>
        <label class="btn" style="flex-direction:row;color:var(--text)">Import…<input type="file" id="import-file" accept="application/json,.json" hidden></label>
      </div>
      <div class="hint">Learned controls ${state.options.expose_buttons ? "are" : "are not"} published to Home Assistant: as button entities, or as one selector per full-config AC (change this in the app's Configuration tab).</div>
    </div>
    <div class="dlg-foot"><button class="btn primary" value="close">Close</button></div></form>`);

  dlg.querySelectorAll(".blaster-row").forEach((row) => {
    const id = row.dataset.id;
    $("[data-save]", row).onclick = () => run(async () => {
      await api("PUT", `blasters/${id}`, { topic: $("input", row).value });
      await load(true);
      toast("Topic saved");
    });
    const rm = $("[data-remove]", row);
    if (rm) rm.onclick = () => run(async () => {
      await api("DELETE", `blasters/${id}`);
      row.remove();
      await load(true);
    });
  });
  $("#mb-add", dlg).onclick = () => run(async () => {
    const { id } = await api("POST", "blasters", { name: $("#mb-name", dlg).value, topic: $("#mb-topic", dlg).value });
    await load(true);
    dlg.close();
    selectBlaster(id);
  });
  $("#import-file", dlg).onchange = (e) => run(async () => {
    const file = e.target.files[0];
    if (!file) return;
    let data;
    try { data = JSON.parse(await file.text()); } catch { throw new Error("That file is not valid JSON"); }
    const target = state.blasterId && confirm("Assign all imported devices to the currently selected blaster?\n(Cancel keeps the blaster stored in the file.)")
      ? state.blasterId : undefined;
    const { imported } = await api("POST", "import", { ...data, blaster_id: target });
    await load();
    dlg.close();
    toast(`Imported ${imported} device(s)`);
  });
}

// ------------------------------------------------------------------ climate regions
const LADDERS = [
  { dir: "heat", label: "🔥 Heat", when: "room too cold", humidity: false },
  { dir: "cool", label: "❄️ Cool", when: "room too warm", humidity: false },
  { dir: "dry", label: "💧 Dehumidify", when: "too humid", humidity: true },
  { dir: "humidify", label: "💦 Humidify", when: "too dry (few ACs can)", humidity: true },
];
const VERBS = { heat: "Heating", cool: "Cooling", dry: "Dehumidifying", humidify: "Humidifying" };
const VERB_ICONS = { heat: "🔥", cool: "❄️", dry: "💧", humidify: "💦" };
const DEFAULT_MODE = { heat: "heat", cool: "cool", dry: "dry", humidify: "fan_only" };

const cl = () => state.climate;
const currentRegion = () => cl()?.regions.find((r) => r.id === state.regionId);
const deviceById = (id) => state.devices.find((d) => d.id === id);
const entityById = (id) => cl()?.entities.find((e) => e.entity_id === id);
const areaName = (id) => cl()?.areas.find((a) => a.id === id)?.name;

function fmtNum(v, unit = "", digits = 1) {
  const n = Number(v);
  if (v == null || v === "" || !Number.isFinite(n)) return "—";
  return `${n.toFixed(digits).replace(/\.0+$/, "")}${unit}`;
}

// Setpoint written in an IR config's name, e.g. 26 for "Heat 26° · Fan Auto" (mirrors the backend).
function nameTemp(name) {
  const m = /(\d+(?:[.,]\d+)?)\s*°/.exec(name) || /\b(\d{2}(?:[.,]\d+)?)\b/.exec(name);
  return m ? Number(m[1].replace(",", ".")) : null;
}

function acName(ac) {
  if (ac.type === "ir") return deviceById(ac.device_id)?.name ?? "⚠ Deleted IR device";
  return entityById(ac.entity_id)?.name ?? ac.entity_id;
}

function stepLabel(ac, s) {
  if (ac.type === "ir") {
    const ctl = deviceById(ac.device_id)?.controls.find((c) => c.id === s.control);
    if (!ctl) return "⚠ deleted";
    return ctl.code ? ctl.name : `${ctl.name} (not learned)`;
  }
  let text = s.hvac_mode.replace(/_/g, " ").replace(/^./, (ch) => ch.toUpperCase());
  if (s.temperature != null) {
    text += s.relative ? ` target ${s.temperature >= 0 ? "+" : "−"}${Math.abs(s.temperature)}°` : ` ${s.temperature}${cl().unit}`;
  }
  if (s.fan_mode) text += ` · fan ${s.fan_mode}`;
  return text;
}

// Sensible starting ladders: IR configs named "Heat 26°"/"Cool 22°"/"Dry…", or target-relative setpoints.
function autoLadders(ac) {
  if (ac.type === "ir") {
    const ctls = deviceById(ac.device_id)?.controls ?? [];
    const pick = (re, sign) => ctls.filter((c) => re.test(c.name.trim()))
      .map((c) => ({ c, t: nameTemp(c.name) ?? 0 }))
      .sort((a, b) => (a.t - b.t) * sign)
      .map(({ c }) => ({ control: c.id }));
    const off = ctls.find((c) => /^(power\s*)?off$/i.test(c.name.trim())) ?? ctls.find((c) => /\boff\b/i.test(c.name));
    return { off_control: off?.id ?? null, ladders: { heat: pick(/^heat/i, 1), cool: pick(/^cool/i, -1), dry: pick(/^dry/i, 1), humidify: [] } };
  }
  const modes = entityById(ac.entity_id)?.hvac_modes ?? [];
  const rel = (mode, offsets) => modes.includes(mode)
    ? offsets.map((o) => ({ hvac_mode: mode, temperature: o, relative: true, fan_mode: null })) : [];
  return {
    ladders: {
      heat: rel("heat", [1, 3, 5]),
      cool: rel("cool", [-1, -3, -5]),
      dry: modes.includes("dry") ? [{ hvac_mode: "dry", temperature: null, relative: false, fan_mode: null }] : [],
      humidify: [],
    },
  };
}

async function loadClimate(refresh = false) {
  const data = await api("GET", "climate" + (refresh ? "?refresh=1" : ""));
  state.climate = data;
  state.clockOffset = data.now - Date.now() / 1000;
  if (!data.regions.some((r) => r.id === state.regionId)) state.regionId = data.regions[0]?.id ?? null;
  render();
}

async function pollClimate() {
  if (state.view !== "climate" || !cl() || document.hidden) return;
  let data;
  try { data = await api("GET", "climate/status"); } catch { return; }
  state.clockOffset = data.now - Date.now() / 1000;
  cl().status = data.status;
  for (const fresh of data.regions) {
    const r = cl().regions.find((x) => x.id === fresh.id);
    if (r) for (const k of ["enabled", "target_temp", "target_humidity", "importance"]) r[k] = fresh[k];
  }
  updateClimateLive();
}

function selectRegion(id) {
  state.regionId = id;
  localSet("region", id);
  render();
}

async function saveRegion(r, patch) {
  Object.assign(r, await run(() => api("PUT", `regions/${r.id}`, patch)));
  setTimeout(pollClimate, 800); // the controller re-evaluates right away
  return r;
}

async function saveAcs(r, acs) {
  await saveRegion(r, { acs });
  render();
}

function phaseClass(r, st) {
  if (!r.enabled) return "off";
  if (st?.phase === "active") return st.active === "temp" ? st.temp?.dir : "dry";
  return "idle";
}

function renderRegionList() {
  const list = $("#device-list");
  const c = cl();
  if (!c) { list.innerHTML = `<li class="empty">Loading…</li>`; return; }
  if (!c.regions.length) { list.innerHTML = `<li class="empty">No regions yet.</li>`; return; }
  list.innerHTML = c.regions.map((r) => {
    const st = c.status[r.id] || {};
    return `<li data-id="${esc(r.id)}" class="${r.id === state.regionId ? "active" : ""}">
      <span><span class="phase-dot ${phaseClass(r, st)}"></span>${esc(r.name)}</span>
      <span class="count">${fmtNum(st.temperature, "°")}${st.humidity != null ? " · " + fmtNum(st.humidity, "%", 0) : ""}</span></li>`;
  }).join("");
}

function renderRegionMain() {
  const main = $("#main");
  const c = cl();
  if (!c) { main.innerHTML = `<div class="empty-state"><h2>Loading…</h2></div>`; return; }
  const r = currentRegion();
  if (!r) {
    main.innerHTML = `<div class="empty-state"><h2>Smart climate control</h2>
      <p>A region is a room with one or more ACs and temperature/humidity sensors. It keeps the room at your target:
      it heats or cools with gentle settings first and gets more aggressive every few minutes until the room is in range.</p>
      <button class="btn primary" data-action="new-region">+ New region</button></div>`;
    return;
  }
  const u = c.unit;
  const area = areaName(r.area_id);
  const sensors = r.temp_sensors.length + r.humidity_sensors.length;
  main.innerHTML = `
    <div class="device-head">
      <div>
        <h1>${esc(r.name)}</h1>
        <div class="sub">${area ? "Area: " + esc(area) : "No area"} · ${r.acs.length} AC${r.acs.length === 1 ? "" : "s"} · ${sensors} sensor${sensors === 1 ? "" : "s"}</div>
      </div>
      <div class="actions">
        <label class="toggle"><input type="checkbox" data-field="enabled" ${r.enabled ? "checked" : ""}><span>Automatic control</span></label>
        <button class="btn" data-action="edit-region">Edit region</button>
      </div>
    </div>
    <div class="region-grid">
      <section class="card" id="live">${liveCard(r)}</section>
      <section class="card">
        <h3>Targets</h3>
        ${rangeField("target_temp", "Target temperature", r.target_temp, c.temp_range[0], c.temp_range[1], 0.5, u)}
        ${r.humidity_control ? rangeField("target_humidity", "Target humidity", r.target_humidity, c.humidity_range[0], c.humidity_range[1], 1, "%") : ""}
        ${r.humidity_control ? `<div class="range-field">
          <div class="slider-head"><span>Priority</span><output data-out="importance">${esc(importanceText(r.importance))}</output></div>
          <div class="importance-row"><span>💧 Humidity</span>
            <input type="range" data-field="importance" min="0" max="100" step="5" value="${r.importance}">
            <span>🌡️ Temperature</span></div>
          <div class="hint" data-out="importance-hint">${esc(importanceHint(r, r.importance))}</div>
        </div>` : ""}
      </section>
      <section class="card">
        <h3>Algorithm</h3>
        <div class="field-grid">
          ${numField("temp_tolerance", `Accepted temperature difference (± ${u})`, r.temp_tolerance, 0.1, 0.1)}
          ${numField("humidity_tolerance", "Accepted humidity difference (± %)", r.humidity_tolerance, 1, 1)}
          ${numField("step_minutes", "Step length (minutes)", r.step_minutes, 1, 1)}
          ${numField("min_cycle_minutes", "Min. time between on/off (minutes)", r.min_cycle_minutes, 1, 0)}
        </div>
        <label class="toggle"><input type="checkbox" data-field="humidity_control" ${r.humidity_control ? "checked" : ""}><span>Control humidity</span></label>
        <div class="hint">When the room leaves the accepted difference, every AC starts at step 1 of its ladder. After each step it
          goes one step further until the room is back in range, then the ACs are switched off. Heat/cool steps set on the wrong
          side of the target (e.g. Heat 22° for a 25° target) are skipped.</div>
      </section>
      <section class="card">
        <h3>Sensors</h3>
        ${sensorBlock(r, "temp_sensors", "Temperature", "temperature")}
        ${sensorBlock(r, "humidity_sensors", "Humidity", "humidity")}
        <div class="hint">The room value is the average of all its sensors.</div>
      </section>
      <section class="card wide">
        <div class="card-head"><h3>Air conditioners</h3><button class="btn small primary" data-action="add-ac">+ Add AC</button></div>
        ${r.acs.map((ac) => acCard(r, ac)).join("") || `<div class="hint">Add a Home Assistant AC (climate entity) or an IR remote from the Remotes tab.</div>`}
      </section>
    </div>`;
  updateClimateLive();
}

function rangeField(field, label, value, min, max, step, unit) {
  return `<div class="range-field">
    <div class="slider-head"><span>${label}</span><output data-out="${field}" data-unit="${esc(unit)}">${fmtNum(value, unit)}</output></div>
    <input type="range" data-field="${field}" min="${min}" max="${max}" step="${step}" value="${value}">
  </div>`;
}

function numField(field, label, value, step, min) {
  return `<label>${label}<input type="number" data-field="${field}" value="${value}" step="${step}" min="${min}"></label>`;
}

function importanceText(v) {
  return `${100 - v}% humidity · ${v}% temperature`;
}

function importanceHint(r, v) {
  const t = (r.step_minutes * v) / 100;
  return `When both are off target, each ${fmtNum(r.step_minutes)}-min step spends ${fmtNum(t)} min on temperature, then ${fmtNum(r.step_minutes - t)} min on humidity.`;
}

function sensorBlock(r, field, title, kind) {
  const st = cl().status[r.id] || {};
  const unit = kind === "humidity" ? "%" : cl().unit;
  const taken = new Set(r[field]);
  const options = cl().entities.filter((e) => e.kind === kind && !taken.has(e.entity_id));
  const inArea = options.filter((e) => r.area_id && e.area_id === r.area_id);
  const other = options.filter((e) => !inArea.includes(e));
  const opt = (e) => `<option value="${esc(e.entity_id)}">${esc(e.name)} (${fmtNum(e.state, e.unit ?? "")})${e.area_id && e.area_id !== r.area_id ? " — " + esc(areaName(e.area_id) ?? "") : ""}</option>`;
  return `<div class="sensor-block">
    <div class="sub-head">${title}</div>
    ${r[field].map((id) => `<div class="sensor-row"><span>${esc(entityById(id)?.name ?? id)}</span>
      <span class="val" data-reading="${esc(id)}" data-unit="${esc(unit)}">${fmtNum(st.readings?.[id] ?? entityById(id)?.state, unit)}</span>
      <button class="btn small ghost" data-action="remove-sensor" data-field="${field}" data-id="${esc(id)}" title="Remove">✕</button></div>`).join("")}
    <select data-add-sensor="${field}"><option value="">+ Add ${kind} sensor…</option>
      ${inArea.length ? `<optgroup label="In ${esc(areaName(r.area_id))}">${inArea.map(opt).join("")}</optgroup>` : ""}
      ${other.length ? `<optgroup label="${inArea.length ? "Other areas" : "All sensors"}">${other.map(opt).join("")}</optgroup>` : ""}
    </select>
  </div>`;
}

function acCard(r, ac) {
  const dev = ac.type === "ir" ? deviceById(ac.device_id) : null;
  const ladders = LADDERS.filter((l) => r.humidity_control || !l.humidity);
  return `<div class="ac" data-ac="${esc(ac.id)}">
    <div class="ac-head">
      <div><b>${esc(acName(ac))}</b>
        <span class="meta">${ac.type === "ir" ? `IR remote${dev ? " via " + esc(blaster(dev.blaster_id)?.name ?? "missing blaster") : ""}` : esc(ac.entity_id)}</span></div>
      <div class="ac-actions">
        <button class="btn small" data-action="autofill" title="Rebuild the steps from the AC's configs/modes">Auto-fill steps</button>
        <button class="btn small ghost danger" data-action="remove-ac" title="Remove from region">✕</button>
      </div>
    </div>
    ${ac.type === "ir" ? `<label class="inline-field">Turn off with
      <select data-ac-field="off_control"><option value="">— nothing —</option>${(dev?.controls ?? []).map((c) =>
        `<option value="${esc(c.id)}" ${c.id === ac.off_control ? "selected" : ""}>${esc(c.name)}${c.code ? "" : " (not learned)"}</option>`).join("")}</select></label>` : ""}
    ${ladders.map((l) => `<div class="ladder" data-dir="${l.dir}">
      <div class="ladder-label">${l.label}<span>${l.when}</span></div>
      <div class="steps">
        ${(ac.ladders[l.dir] ?? []).map((s, i) => `<span class="step" data-i="${i}"><span class="n">${i + 1}</span>${esc(stepLabel(ac, s))}
          <button type="button" data-action="step-left" title="Use earlier" ${i ? "" : "disabled"}>‹</button><button type="button" data-action="step-right" title="Use later" ${i < ac.ladders[l.dir].length - 1 ? "" : "disabled"}>›</button><button type="button" data-action="step-remove" title="Remove">✕</button></span>`).join('<span class="arrow">→</span>')}
        <button class="chip" data-action="add-step">+ step</button>
      </div>
    </div>`).join("")}
  </div>`;
}

function liveCard(r) {
  const c = cl();
  const st = c.status[r.id] || {};
  const until = (t) => (t ? `<span class="countdown" data-until="${t}"></span>` : "");
  const objective = (key) => {
    const o = st[key];
    if (!o?.dir) return "";
    const on = st.active === key;
    let text = `${VERB_ICONS[o.dir]} <b>${VERBS[o.dir]}</b> · step ${o.level + 1} of ${o.levels}`;
    if (!on) text += " · waiting for its share of the step";
    else if (st.switch_at) text += ` · humidity's turn in ${until(st.switch_at)}`;
    if (o.level + 1 < o.levels && st.section_end) text += ` · next step in ${until(st.section_end)}`;
    else if (o.level + 1 >= o.levels) text += " · strongest step";
    return `<div class="objective ${on ? "on" : ""}">${text}</div>`;
  };
  return `<div class="readings">
      <div class="reading"><div class="value">${fmtNum(st.temperature, c.unit)}</div>
        <div class="label">Room · target ${fmtNum(r.target_temp, c.unit)} ± ${r.temp_tolerance}</div></div>
      <div class="reading"><div class="value">${fmtNum(st.humidity, "%", 0)}</div>
        <div class="label">Humidity · ${r.humidity_control ? `target ${fmtNum(r.target_humidity, "%", 0)} ± ${r.humidity_tolerance}` : "not controlled"}</div></div>
    </div>
    <div class="status-line ${phaseClass(r, st)}">${esc(st.status || "…")}${st.hold_until ? ` · ${until(st.hold_until)}` : ""}</div>
    ${r.enabled ? objective("temp") + objective("hum") : ""}
    ${r.acs.length ? `<div class="ac-live">${r.acs.map((ac) => {
      const a = st.acs?.[ac.id] || {};
      return `<div><span>${esc(acName(ac))}</span><span>${a.error ? `<span class="err">⚠ ${esc(a.error)}</span>`
        : `${esc(a.label || "—")}${a.current ? ` <span class="meta">(now ${esc(a.current)})</span>` : ""}`}</span></div>`;
    }).join("")}</div>` : ""}`;
}

// Refresh only the live parts so inputs being edited are left alone.
function updateClimateLive() {
  if (state.view !== "climate" || !cl()) return;
  renderRegionList();
  const r = currentRegion();
  if (!r) return;
  const live = $("#live");
  if (live) live.innerHTML = liveCard(r);
  document.querySelectorAll("#main [data-field]").forEach((el) => {
    if (el === document.activeElement) return;
    const v = r[el.dataset.field];
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.type === "range" && Number(el.value) !== v) { el.value = v; showRange(el); }
  });
  const st = cl().status[r.id] || {};
  document.querySelectorAll("#main [data-reading]").forEach((el) => {
    const v = st.readings?.[el.dataset.reading];
    if (v !== undefined) el.textContent = fmtNum(v, el.dataset.unit);
  });
  document.querySelectorAll("#main .step").forEach((el) => {
    const cur = st.acs?.[el.closest("[data-ac]").dataset.ac]?.step;
    el.classList.toggle("current", r.enabled && !!cur && cur.dir === el.closest("[data-dir]").dataset.dir && cur.index === Number(el.dataset.i));
  });
  tickCountdowns();
}

function showRange(el) {
  const r = currentRegion();
  const v = Number(el.value);
  if (el.dataset.field === "importance") {
    $('[data-out="importance"]').textContent = importanceText(v);
    $('[data-out="importance-hint"]').textContent = importanceHint(r, v);
  } else {
    const out = $(`[data-out="${el.dataset.field}"]`);
    out.textContent = fmtNum(v, out.dataset.unit);
  }
}

function tickCountdowns() {
  const now = Date.now() / 1000 + (state.clockOffset || 0);
  document.querySelectorAll("[data-until]").forEach((el) => {
    const left = Math.max(0, Number(el.dataset.until) - now);
    el.textContent = `${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")}`;
  });
}

function areaSelect(selected) {
  return `<select name="area_id"><option value="">— No area —</option>${cl().areas.map((a) =>
    `<option value="${esc(a.id)}" ${a.id === selected ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select>`;
}

function newRegionDialog() {
  openDialog(`<form method="dialog">
    <div class="dlg-head">New climate region</div>
    <div class="dlg-body">
      <label>Home Assistant area (room)${areaSelect(null)}</label>
      <label>Name<input name="name" required maxlength="80" placeholder="Bedroom"></label>
      <span class="hint">The region's controls (switch, target sliders, readings) are added to Home Assistant as a device in this area.
        Sensors and ACs in the area are listed first when you add them.</span>
    </div>
    <div class="dlg-foot">
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="ok">Create</button>
    </div></form>`, async (fd) => {
    const r = await api("POST", "regions", { name: fd.get("name"), area_id: fd.get("area_id") || null });
    cl().regions.push(r);
    selectRegion(r.id);
    toast(`Created “${r.name}”`);
  });
  const form = $("form", dlg);
  form.elements.area_id.addEventListener("change", (e) => {
    if (!form.elements.name.value) form.elements.name.value = areaName(e.target.value) ?? "";
  });
  form.elements.area_id.focus();
}

function editRegionDialog(r) {
  openDialog(`<form method="dialog">
    <div class="dlg-head">Edit region</div>
    <div class="dlg-body">
      <label>Name<input name="name" required maxlength="80" value="${esc(r.name)}" autofocus></label>
      <label>Home Assistant area (room)${areaSelect(r.area_id)}</label>
    </div>
    <div class="dlg-foot">
      <button class="btn danger left" value="delete" formnovalidate>Delete region</button>
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="ok">Save</button>
    </div></form>`, async (fd, action) => {
    if (action === "delete") {
      if (!confirm(`Delete “${r.name}”? ACs it switched on are turned off.`)) return false;
      await api("DELETE", `regions/${r.id}`);
      cl().regions = cl().regions.filter((x) => x.id !== r.id);
      selectRegion(cl().regions[0]?.id ?? null);
      toast("Region deleted");
      return;
    }
    await saveRegion(r, { name: fd.get("name"), area_id: fd.get("area_id") || null });
    render();
  });
}

function addAcDialog(r) {
  const used = new Set(r.acs.map((a) => a.entity_id || "ir:" + a.device_id));
  const climates = cl().entities.filter((e) => e.kind === "climate" && !used.has(e.entity_id))
    .sort((a, b) => (b.area_id === r.area_id) - (a.area_id === r.area_id));
  const devices = state.devices.filter((d) => !used.has("ir:" + d.id))
    .sort((a, b) => (b.kind === "configs") - (a.kind === "configs"));
  if (!climates.length && !devices.length) { toast("No ACs found: add a climate entity in Home Assistant or an IR device in Remotes", true); return; }
  openDialog(`<form method="dialog">
    <div class="dlg-head">Add air conditioner</div>
    <div class="dlg-body">
      <label>AC<select name="ac">
        ${climates.length ? `<optgroup label="Home Assistant ACs (climate entities)">${climates.map((e) =>
          `<option value="${esc(e.entity_id)}">${esc(e.name)}${e.area_id ? " — " + esc(areaName(e.area_id) ?? "") : ""}</option>`).join("")}</optgroup>` : ""}
        ${devices.length ? `<optgroup label="IR remotes from this app">${devices.map((d) =>
          `<option value="ir:${esc(d.id)}">${esc(d.name)}${d.kind === "configs" ? " (full config)" : ""}</option>`).join("")}</optgroup>` : ""}
      </select></label>
      <span class="hint">Steps are filled in automatically (IR configs named like “Heat 26°”, “Cool 22°”, “Dry”, and target-relative
        setpoints for Home Assistant ACs). You can change them afterwards.</span>
    </div>
    <div class="dlg-foot">
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="ok">Add</button>
    </div></form>`, async (fd) => {
    const v = fd.get("ac");
    const ac = v.startsWith("ir:") ? { type: "ir", device_id: v.slice(3) } : { type: "climate", entity_id: v };
    await saveAcs(r, [...r.acs, { ...ac, ...autoLadders(ac) }]);
  });
}

function addStepDialog(r, ac, dir) {
  const ladder = LADDERS.find((l) => l.dir === dir);
  const add = async (steps) => {
    const copy = structuredClone(ac);
    copy.ladders[dir] = [...(copy.ladders[dir] ?? []), ...steps];
    await saveAcs(r, r.acs.map((a) => (a.id === ac.id ? copy : a)));
  };
  if (ac.type === "ir") {
    const ctls = deviceById(ac.device_id)?.controls ?? [];
    openDialog(`<form method="dialog">
      <div class="dlg-head">Add ${esc(ladder.label)} steps</div>
      <div class="dlg-body">
        <div class="check-list">${ctls.map((c) => `<label class="check"><input type="checkbox" name="control" value="${esc(c.id)}">
          ${esc(c.name)}${c.code ? "" : ` <span class="meta">(not learned)</span>`}</label>`).join("") || `<div class="hint">This device has no configs.</div>`}</div>
        <span class="hint">Checked configs are added in list order. Each step should be stronger than the one before; reorder with ‹ ›.</span>
      </div>
      <div class="dlg-foot">
        <button class="btn" value="cancel" formnovalidate>Cancel</button>
        <button class="btn primary" value="ok">Add</button>
      </div></form>`, async (fd) => {
      const ids = fd.getAll("control");
      if (!ids.length) { toast("Pick at least one config", true); return false; }
      await add(ids.map((control) => ({ control })));
    });
    return;
  }
  const ent = entityById(ac.entity_id);
  const modes = ent?.hvac_modes?.length ? ent.hvac_modes : ["heat", "cool", "dry", "fan_only", "auto", "off"];
  const last = (ac.ladders[dir] ?? []).at(-1);
  const guess = last?.relative ? last.temperature + (dir === "cool" ? -2 : 2) : { heat: 1, cool: -1 }[dir] ?? "";
  openDialog(`<form method="dialog">
    <div class="dlg-head">Add ${esc(ladder.label)} step</div>
    <div class="dlg-body">
      <label>Mode<select name="hvac_mode">${modes.map((m) => `<option ${m === DEFAULT_MODE[dir] ? "selected" : ""}>${esc(m)}</option>`).join("")}</select></label>
      <div class="field-grid">
        <label>Temperature<input type="number" name="temperature" step="0.5" value="${guess}" placeholder="don't set"></label>
        <label>Meaning<select name="relative"><option value="1" ${guess !== "" ? "selected" : ""}>Target ± this value</option><option value="">Exact temperature</option></select></label>
      </div>
      <label>Fan<select name="fan_mode"><option value="">Don't change</option>${(ent?.fan_modes ?? []).map((f) => `<option>${esc(f)}</option>`).join("")}</select></label>
      <span class="hint">“Target ± value” follows the target slider: with a 25° target, +3 sets the AC to 28°.</span>
    </div>
    <div class="dlg-foot">
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="ok">Add</button>
    </div></form>`, async (fd) => {
    const temp = fd.get("temperature");
    await add([{ hvac_mode: fd.get("hvac_mode"), temperature: temp === "" ? null : Number(temp), relative: !!fd.get("relative"), fan_mode: fd.get("fan_mode") || null }]);
  });
}

function climateClick(e) {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const r = currentRegion();
  const ac = r?.acs.find((a) => a.id === btn.closest("[data-ac]")?.dataset.ac);
  const dir = btn.closest("[data-dir]")?.dataset.dir;
  const i = Number(btn.closest("[data-i]")?.dataset.i);
  const editLadder = (fn) => {
    const copy = structuredClone(ac);
    fn(copy.ladders[dir]);
    return saveAcs(r, r.acs.map((a) => (a.id === ac.id ? copy : a)));
  };
  switch (btn.dataset.action) {
    case "new-region": return newRegionDialog();
    case "edit-region": return editRegionDialog(r);
    case "add-ac": return addAcDialog(r);
    case "add-step": return addStepDialog(r, ac, dir);
    case "remove-sensor":
      return saveRegion(r, { [btn.dataset.field]: r[btn.dataset.field].filter((x) => x !== btn.dataset.id) }).then(render);
    case "remove-ac":
      if (!confirm(`Remove “${acName(ac)}” from ${r.name}?`)) return;
      return saveAcs(r, r.acs.filter((a) => a.id !== ac.id));
    case "autofill":
      if (!confirm("Replace this AC's steps with automatically detected ones?")) return;
      return saveAcs(r, r.acs.map((a) => (a.id === ac.id ? { ...a, ...autoLadders(a) } : a)));
    case "step-left": return editLadder((l) => l.splice(i - 1, 0, l.splice(i, 1)[0]));
    case "step-right": return editLadder((l) => l.splice(i + 1, 0, l.splice(i, 1)[0]));
    case "step-remove": return editLadder((l) => l.splice(i, 1));
  }
}

async function climateChange(e) {
  const r = currentRegion();
  const el = e.target;
  if (!r) return;
  if (el.dataset.field) {
    if (el.type !== "checkbox" && el.value === "") return;
    await saveRegion(r, { [el.dataset.field]: el.type === "checkbox" ? el.checked : Number(el.value) });
    render();
  } else if (el.dataset.addSensor) {
    if (!el.value) return;
    await saveRegion(r, { [el.dataset.addSensor]: [...r[el.dataset.addSensor], el.value] });
    render();
  } else if (el.dataset.acField) {
    const acs = r.acs.map((a) => (a.id === el.closest("[data-ac]").dataset.ac ? { ...a, [el.dataset.acField]: el.value || null } : a));
    await saveAcs(r, acs);
  }
}

setInterval(pollClimate, 5000);
setInterval(tickCountdowns, 1000);

// ------------------------------------------------------------------ events
$("#blaster-select").addEventListener("change", (e) => selectBlaster(e.target.value));
$("#refresh-btn").addEventListener("click", () => run(async () => {
  await load(true);
  if (state.view === "climate") await loadClimate(true);
  toast(state.view === "climate" ? "Entities refreshed" : "Blasters re-scanned");
}));
$("#tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-view]");
  if (btn && btn.dataset.view !== state.view) setView(btn.dataset.view);
});
$("#settings-btn").addEventListener("click", settingsDialog);
$("#new-btn").addEventListener("click", () => (state.view === "climate" ? newRegionDialog() : newDeviceDialog()));
$("#device-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li) state.view === "climate" ? selectRegion(li.dataset.id) : selectDevice(li.dataset.id);
});

$("#main").addEventListener("click", (e) => {
  if (state.view === "climate") return climateClick(e);
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const dev = currentDevice();
  const ctl = dev?.controls.find((c) => c.id === btn.dataset.id);
  switch (btn.dataset.action) {
    case "new-device": return newDeviceDialog();
    case "edit-device": return editDeviceDialog(dev);
    case "learn": return startLearn(dev, ctl);
    case "send": return sendControl(dev, ctl);
    case "edit-control": return editControlDialog(dev, ctl);
    case "suggest": return addControl(dev, btn.dataset.name, false);
    case "build-configs": return configBuilderDialog(dev);
  }
});

$("#main").addEventListener("change", (e) => {
  if (state.view === "climate") climateChange(e).catch(() => {});
});
$("#main").addEventListener("input", (e) => {
  if (state.view === "climate" && e.target.type === "range") showRange(e.target);
});

$("#main").addEventListener("submit", (e) => {
  if (e.target.id !== "add-control-form") return;
  e.preventDefault();
  const name = e.target.elements.name.value.trim();
  if (!name) { e.target.elements.name.focus(); return; }
  addControl(currentDevice(), name, e.submitter?.value === "learn");
});

async function addControl(dev, name, learn) {
  const ctl = await run(() => api("POST", `devices/${dev.id}/controls`, { name }));
  dev.controls.push(ctl);
  render();
  $("#add-control-form input")?.focus();
  if (learn) startLearn(dev, ctl);
}

// Drag & drop reordering of control tiles
let dragId = null;
$("#main").addEventListener("dragstart", (e) => {
  const tile = e.target.closest?.(".control");
  if (!tile) return;
  dragId = tile.dataset.id;
  tile.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
});
$("#main").addEventListener("dragover", (e) => {
  const tile = e.target.closest(".control");
  if (!dragId || !tile) return;
  e.preventDefault();
  document.querySelectorAll(".drop-target").forEach((t) => t.classList.remove("drop-target"));
  if (tile.dataset.id !== dragId) tile.classList.add("drop-target");
});
$("#main").addEventListener("dragend", () => {
  dragId = null;
  document.querySelectorAll(".dragging,.drop-target").forEach((t) => t.classList.remove("dragging", "drop-target"));
});
$("#main").addEventListener("drop", (e) => {
  const tile = e.target.closest(".control");
  const dev = currentDevice();
  if (!dragId || !tile || !dev || tile.dataset.id === dragId) return;
  e.preventDefault();
  const ids = dev.controls.map((c) => c.id).filter((id) => id !== dragId);
  ids.splice(ids.indexOf(tile.dataset.id), 0, dragId);
  const pos = Object.fromEntries(ids.map((id, i) => [id, i]));
  dev.controls.sort((a, b) => pos[a.id] - pos[b.id]);
  render();
  run(() => api("PUT", `devices/${dev.id}`, { order: ids }));
});

if (state.view === "climate") loadClimate().catch((e) => toast(e.message, true));
load().catch((e) => {
  $("#banner").hidden = false;
  $("#banner").textContent = "Could not load: " + e.message;
});
