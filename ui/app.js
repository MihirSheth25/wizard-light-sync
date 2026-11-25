/* WiZard window. Talks to Python through pywebview (or the dev server), renders pages,
   and redraws only what changed: values update in place, pages rebuild only when their
   structure changes (a light added, a room renamed, a different music style). */
(() => {
"use strict";

// ------------------------------------------------------------------ tiny helpers
const $ = (sel, root = document) => root.querySelector(sel);
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "html") el.innerHTML = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
}
const ICONS = {
  lights: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.4 1 1.1 1 1.8V16h5v-.3c0-.7.4-1.4 1-1.8A6 6 0 0 0 12 3z"/>',
  rooms: '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-5h4v5"/>',
  scenes: '<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>',
  screen: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  music: '<path d="M4 12h1M8 8v8M12 5v14M16 9v6M20 11v2"/>',
  schedules: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  settings: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke="none"/>',
  edit: '<path d="M4 20h4L19.5 8.5l-4-4L4 16z"/>',
  trash: '<path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v5h-5"/>',
  calm: '<path d="M2 12c2.5-6 5-6 7.5 0s5 6 7.5 0 3.5-4 5-2"/>',
  dynamic: '<path d="M3 18V8M8 18V5M13 18v-7M18 18V8"/><path d="M2 21h20" opacity=".5"/>',
  pulse: '<path d="M2 12h4l2-5 4 10 2-5h8"/>',
  spectrum: '<path d="M4 20v-6M8 20V9M12 20V4M16 20v-9M20 20v-4"/>',
  mellow: '<path d="M2 15c3-3 6-3 9 0s6 3 11-1"/><path d="M2 9c3-2 6-2 9 0s6 2 11-1" opacity=".5"/>',
};
const icon = (name, size = 18) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("width", size); s.setAttribute("height", size); s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "1.7");
  s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
  s.innerHTML = ICONS[name] || ""; return s;
};
const rgbCss = (c, a = 1) => c ? `rgba(${c[0]},${c[1]},${c[2]},${a})` : "transparent";
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
function throttle(fn, ms) {
  let last = 0, timer = null, lastArgs;
  return (...args) => {
    lastArgs = args; const now = performance.now();
    if (now - last >= ms) { last = now; fn(...args); }
    else if (!timer) timer = setTimeout(() => { timer = null; last = performance.now(); fn(...lastArgs); }, ms - (now - last));
  };
}
function hueName(rgb) {
  const [r, g, b] = rgb.map(v => v / 255), mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  if (mx === 0 || (mx - mn) / mx < 0.18) return "White";
  let hh; const d = mx - mn;
  if (mx === r) hh = ((g - b) / d) % 6; else if (mx === g) hh = (b - r) / d + 2; else hh = (r - g) / d + 4;
  const deg = (hh * 60 + 360) % 360;
  for (const [lim, name] of [[15, "Red"], [40, "Orange"], [65, "Yellow"], [160, "Green"], [195, "Cyan"], [250, "Blue"], [290, "Purple"], [335, "Pink"], [361, "Red"]])
    if (deg < lim) return name;
  return "Color";
}
function hsvToRgb(hh, s, v) {
  const f = n => { const k = (n + hh * 6) % 6; return v - v * s * Math.max(0, Math.min(k, 4 - k, 1)); };
  return [f(5), f(3), f(1)].map(x => Math.round(x * 255));
}
function rgbToHsv([r, g, b]) {
  r /= 255; g /= 255; b /= 255; const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let hh = 0;
  if (d) { if (mx === r) hh = ((g - b) / d) % 6; else if (mx === g) hh = (b - r) / d + 2; else hh = (r - g) / d + 4; }
  return [((hh / 6) + 1) % 1, mx ? d / mx : 0, mx];
}

// ------------------------------------------------------------------ bridge to Python
const bridge = {
  ready: null,
  call(name, ...args) {
    if (window.pywebview && window.pywebview.api) return window.pywebview.api[name](...args);
    return fetch(`/api/${name}`, { method: "POST", body: JSON.stringify(args) }).then(r => r.json());
  },
};
bridge.ready = new Promise(resolve => {
  if (window.pywebview && window.pywebview.api) return resolve();
  if (window.chrome && window.chrome.webview) {          // inside the app: wait for pywebview
    window.addEventListener("pywebviewready", () => resolve(), { once: true });
    return;
  }
  const es = new EventSource("/events");                  // dev preview in a normal browser
  es.onmessage = e => { const [k, d] = JSON.parse(e.data); WZ.recv(k, d); };
  resolve();
});
const api = new Proxy({}, { get: (_, name) => (...args) => bridge.call(name, ...args) });

// ------------------------------------------------------------------ app state
let S = null, STATIC = null, MONITORS = [], TRAY = false;
let page = "lights", drawerKey = null, drawerTab = "Control", ctlMode = "color";
let pageSig = "", drawerSig = "";
let updaters = [], drawerUpdaters = [], liveHooks = [];
let stageCanvases = new Set();
let pendingPreset = false;
let sink = null;          // when set, new controls register their refresh here (used by dialogs)
let drawerBuilding = false;
let pendingOpen = null;   // a tile click waits a moment, so a double-click can rename instead
const reg = f => (sink || (drawerBuilding ? drawerUpdaters : updaters)).push(f);
const PAGES = [
  ["lights", "Lights"], ["rooms", "Rooms"], ["scenes", "Scenes"], ["screen", "Screen sync"],
  ["music", "Music sync"], ["schedules", "Schedules"], null, ["settings", "Settings"],
];

const lightByIp = ip => S.lights.find(l => l.ip === ip);
const roomById = id => S.rooms.find(r => r.id === id);
const keyIps = key => key === "all" ? S.lights.map(l => l.ip) : key.startsWith("light:") ? [key.slice(6)] : (roomById(key.slice(5))?.lights || []);
const keyName = key => key === "all" ? "All lights" : key.startsWith("light:") ? (lightByIp(key.slice(6))?.name || "Light") : (roomById(key.slice(5))?.name || "Room");
const isOn = l => !!l.rgb;
function lookText(l) {
  if (!l.rgb) return l.words === "Not answering" ? "Not answering" : "Off";
  const w = l.words === "Color" ? hueName(l.rgb) : (l.words || "On");
  return w === "Syncing" ? `Syncing, ${l.dim}%` : `${w}, ${l.dim}%`;
}

// ------------------------------------------------------------------ messages from Python
window.WZ = {
  recv(kind, data) {
    if (kind === "state") applyState(data);
    else if (kind === "live") applyLive(data);
    else if (kind === "toast") toast(data);
  },
};

function applyState(st) {
  S = st;
  renderChrome();
  const sig = signature(page);
  if (sig !== pageSig) renderPage(false); else updaters.forEach(f => f());
  if (drawerKey) {
    if (drawerKey.startsWith("light:") && !lightByIp(drawerKey.slice(6))) closeDrawer();
    else if (drawerKey.startsWith("room:") && !roomById(drawerKey.slice(5))) closeDrawer();
    else { const ds = drawerSignature(); if (ds !== drawerSig) renderDrawer(); else drawerUpdaters.forEach(f => f()); }
  }
  liveHooks.forEach(f => f());
  drawStages();
}

function applyLive(d) {
  if (!S) return;
  for (const l of S.lights) {
    const c = d.colors[l.ip];
    if (c) { l.rgb = c[0]; l.dim = c[1]; l.words = c[2]; }
  }
  S.sync.fps = d.fps; S.sync.bpm = d.bpm;
  liveHooks.forEach(f => f());
  if (drawerKey) drawerUpdaters.forEach(f => f());
  drawStages();
  renderSyncMeta();
}

// ------------------------------------------------------------------ chrome: sidebar, top bar
function buildChrome() {
  const nav = $("#nav"); nav.innerHTML = "";
  for (const p of PAGES) {
    if (!p) { nav.append(h("div", { class: "nav-sep" })); continue; }
    const [id, label] = p;
    nav.append(h("button", { class: "nav-item", "data-page": id, title: label, onclick: () => go(id) },
      icon(id === "screen" ? "screen" : id), h("span", { class: "nav-label", text: label })));
  }
  const col = $("#collapse"); col.append(icon("menu"));
  col.onclick = () => {
    const c = !$("#app").classList.contains("collapsed");
    $("#app").classList.toggle("collapsed", c); api.set_setting("sidebar_open", !c);
  };
  const seg = $("#sourceSeg");
  for (const src of ["Screen", "Music"]) seg.append(h("button", { "data-src": src, text: src, onclick: () => api.set_setting("sync_source", src) }));
  $("#syncBtn").onclick = async () => {
    if (S.sync.on) return api.stop_sync();
    const r = await api.start_sync(S.settings.sync_source);
    if (r && !r.ok) { toast(r.message); go("lights"); }
  };
  $("#scrim").onclick = closeDrawer;
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { if ($("#modal").classList.contains("open")) closeModal(); else if (drawerKey) closeDrawer(); }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) drawStages(); });
  liveHooks.push(renderNow);
}

function renderChrome() {
  document.querySelectorAll(".nav-item").forEach(b => b.classList.toggle("on", b.dataset.page === page));
  $("#app").classList.toggle("collapsed", !S.settings.sidebar_open);
  document.querySelectorAll("#sourceSeg button").forEach(b => b.classList.toggle("on", b.dataset.src === S.settings.sync_source));
  const btn = $("#syncBtn");
  btn.textContent = S.sync.on ? "Stop sync" : "Start sync";
  btn.className = "btn " + (S.sync.on ? "stop" : "primary");
  renderSyncMeta();
  $("#collapse").title = S.settings.sidebar_open ? "Collapse menu" : "Expand menu";
}

function renderSyncMeta() {
  const m = $("#syncMeta");
  if (!S.sync.on) { m.textContent = S.fade || ""; return; }
  m.textContent = S.sync.kind === "Music" && S.sync.bpm ? `${S.sync.bpm} BPM` : (S.sync.fps ? `${S.sync.fps} updates/s` : "");
}

function renderNow() {
  const now = $("#now"); if (!S) return;
  const state = S.sync.on ? `${S.sync.kind} sync on` : S.fade ? S.fade : S.scene_playing ? (STATIC.scenes.find(x => x.id === S.scene_playing[0])?.name + " scene") : "Not syncing";
  if (!now.firstChild) {
    now.append(h("div", { class: "now-head" }, h("span", { text: "Now" }), h("span", { class: "num", id: "nowCount" })),
      h("div", { class: "now-state", id: "nowState" }), h("div", { class: "now-dots", id: "nowDots" }));
  }
  $("#nowState").textContent = state;
  $("#nowCount").textContent = `${S.lights.filter(isOn).length} of ${S.lights.length} on`;
  const dots = $("#nowDots");
  if (dots.children.length !== S.lights.length) { dots.innerHTML = ""; S.lights.forEach(() => dots.append(h("span", { class: "dot" }))); }
  S.lights.forEach((l, i) => {
    const d = dots.children[i]; d.title = l.name;
    d.style.background = l.rgb ? rgbCss(l.rgb) : ""; d.style.boxShadow = l.rgb ? `0 0 8px ${rgbCss(l.rgb, 0.6)}` : "";
  });
}

// ------------------------------------------------------------------ navigation and page rendering
function go(p) {
  if (p === page) return;
  page = p; pendingPreset = false;
  renderPage(true); renderChrome();
  $("#content").scrollTop = 0;
}

function signature(p) {
  const L = S.lights.map(l => [l.ip, l.name, l.room, l.zone, l.enabled].join("|")).join(";");
  const R = S.rooms.map(r => [r.id, r.name, r.lights.join(",")].join("|")).join(";");
  switch (p) {
    case "lights": return L + "#" + R + "#" + S.discovering;
    case "rooms": return L + "#" + R;
    case "scenes": return L + "#" + R;
    case "screen": return L + "#" + Object.keys(S.settings.screen_presets).join(",") + "#" + pendingPreset + "#" + MONITORS.length;
    case "music": return S.settings.music_mode + "#" + L;
    case "schedules": return JSON.stringify(S.schedules) + L + R;
    default: return "static";
  }
}

function renderPage(animate) {
  updaters = []; stageCanvases.clear();
  const c = $("#content"); c.innerHTML = "";
  $("#title").textContent = PAGES.find(p => p && p[0] === page)[1];
  ({ lights: pageLights, rooms: pageRooms, scenes: pageScenes, screen: pageScreen, music: pageMusic,
     schedules: pageSchedules, settings: pageSettings })[page](c);
  pageSig = signature(page);
  if (animate) { c.classList.remove("fade"); void c.offsetWidth; c.classList.add("fade"); }
  updaters.forEach(f => f());
  drawStages();
}
const onUpdate = f => updaters.push(f);

// ------------------------------------------------------------------ shared controls
function toggle(get, set, label) {
  const input = h("input", { type: "checkbox", "aria-label": label || "On or off" });
  input.onchange = e => { e.stopPropagation(); set(input.checked); };
  const el = h("label", { class: "toggle", onclick: e => e.stopPropagation() }, input, h("span"));
  const up = () => { input.checked = !!get(); }; up(); reg(up);
  return el;
}

function slider({ min, max, step = 1, get, onInput, onChange, kelvin = false, label }) {
  const el = h("input", { type: "range", min, max, step, class: kelvin ? "kelvin" : null, "aria-label": label || "" });
  let dragging = false;
  const paint = () => el.style.setProperty("--p", `${((el.value - min) / (max - min)) * 100}%`);
  el.addEventListener("pointerdown", e => { dragging = true; e.stopPropagation(); });
  el.addEventListener("click", e => e.stopPropagation());
  el.addEventListener("input", () => { paint(); onInput && onInput(+el.value); });
  el.addEventListener("change", () => { dragging = false; paint(); onChange && onChange(+el.value); });
  el.addEventListener("pointerup", () => { dragging = false; });
  const up = () => { if (dragging || document.activeElement === el && el.matches(":active")) return; const v = get(); if (v != null) { el.value = v; paint(); } };
  up(); reg(up);
  return el;
}

// a labelled slider bound to one setting
function settingSlider(key, label, min, max, step, fmt, note) {
  const value = h("span", { class: "value" });
  const send = throttle(v => api.set_setting(key, v), 60);
  const sl = slider({ min, max, step, label, get: () => S.settings[key],
    onInput: v => { value.textContent = fmt(v); S.settings[key] = v; send(v); },
    onChange: v => { api.set_setting(key, v); } });
  const up = () => { value.textContent = fmt(+sl.value); }; up(); updaters.push(up);
  const row = h("div", { class: "field" }, h("label", { text: label }), sl, value);
  return note ? h("div", { class: "stack", style: { gap: "4px" } }, row, h("div", { class: "field" }, h("span"), h("span", { class: "muted", style: { gridColumn: "2 / -1", fontSize: "12px" }, text: note }))) : row;
}

function seg(options, get, set) {
  const el = h("div", { class: "seg" });
  for (const o of options) {
    const [v, label] = Array.isArray(o) ? o : [o, o];
    el.append(h("button", { "data-v": v, text: label, onclick: e => { e.stopPropagation(); set(v); } }));
  }
  const up = () => el.querySelectorAll("button").forEach(b => b.classList.toggle("on", String(get()) === b.dataset.v));
  up(); reg(up);
  return el;
}

let openMenu = null;
function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
document.addEventListener("pointerdown", e => { if (openMenu && !openMenu.contains(e.target)) closeMenu(); }, true);
window.addEventListener("resize", () => { closeMenu(); drawStages(); });

function select(options, get, set, width) {
  // options: [{value, label, sep}] or a function returning that list
  const btn = h("button", { type: "button" }, h("span", { class: "sel-label" }), icon("down", 16));
  const el = h("div", { class: "select", style: width ? { width: width + "px" } : null }, btn);
  const opts = () => (typeof options === "function" ? options() : options).map(o => typeof o === "object" ? o : { value: o, label: o });
  const up = () => { const cur = opts().find(o => String(o.value) === String(get())); btn.querySelector(".sel-label").textContent = cur ? cur.label : String(get() ?? ""); };
  btn.onclick = e => {
    e.stopPropagation();
    if (openMenu) { closeMenu(); return; }
    const r = btn.getBoundingClientRect();
    const m = h("div", { class: "menu", role: "listbox" });
    let items = [];
    for (const o of opts()) {
      if (o.sep) { m.append(h("div", { class: "menu-sep" })); continue; }
      const b = h("button", { class: String(o.value) === String(get()) ? "chosen" : null, text: o.label,
        onclick: ev => { ev.stopPropagation(); closeMenu(); set(o.value); } });
      items.push(b); m.append(b);
    }
    m.style.minWidth = r.width + "px"; m.style.left = r.left + "px";
    document.body.append(m);
    const below = window.innerHeight - r.bottom;
    m.style.top = (below > m.offsetHeight + 8 ? r.bottom + 4 : Math.max(8, r.top - m.offsetHeight - 4)) + "px";
    openMenu = m;
    let idx = Math.max(0, items.findIndex(b => b.classList.contains("chosen")));
    const focus = () => { items.forEach((b, i) => b.classList.toggle("active", i === idx)); items[idx]?.scrollIntoView({ block: "nearest" }); };
    focus();
    m.tabIndex = -1; m.focus();
    m.addEventListener("keydown", ev => {
      if (ev.key === "ArrowDown") { idx = Math.min(items.length - 1, idx + 1); focus(); ev.preventDefault(); }
      else if (ev.key === "ArrowUp") { idx = Math.max(0, idx - 1); focus(); ev.preventDefault(); }
      else if (ev.key === "Enter" || ev.key === " ") { items[idx]?.click(); ev.preventDefault(); }
      else if (ev.key === "Escape") { closeMenu(); btn.focus(); ev.stopPropagation(); }
    });
  };
  up(); reg(up);
  return el;
}

function confirmBtn(text, action, cls = "btn danger") {
  const b = h("button", { class: cls, text });
  let timer = null;
  b.onclick = e => {
    e.stopPropagation();
    if (b.classList.contains("armed")) { clearTimeout(timer); b.classList.remove("armed"); b.textContent = text; action(); return; }
    b.classList.add("armed"); b.textContent = "Click again to confirm";
    timer = setTimeout(() => { b.classList.remove("armed"); b.textContent = text; }, 3000);
  };
  return b;
}

function orb(l, cls = "orb") {
  const el = h("span", { class: cls });
  paintOrb(el, l);
  return el;
}
function paintOrb(el, l) {
  const lit = l && l.rgb;
  el.classList.toggle("lit", !!lit);
  if (lit) {
    const a = 0.45 + 0.55 * (l.dim / 100);
    el.style.background = `radial-gradient(circle at 50% 40%, ${rgbCss(l.rgb.map(v => Math.round(v + (255 - v) * 0.45)))}, ${rgbCss(l.rgb)} 60%, ${rgbCss(l.rgb, 0.85)})`;
    el.style.setProperty("--glow", rgbCss(l.rgb, 0.55 * a));
    el.style.opacity = 0.55 + 0.45 * a;
  } else { el.style.background = ""; el.style.opacity = ""; }
}

// rename in place: double-click the name
function nameCell(text, save, cls = "tile-name") {
  const el = h("div", { class: cls, text, title: "Double-click to rename" });
  el.addEventListener("dblclick", e => {
    e.stopPropagation(); clearTimeout(pendingOpen); pendingOpen = null;
    const input = h("input", { class: "inline-edit", value: el.textContent, maxlength: 40 });
    el.replaceWith(input); input.focus(); input.select();
    let done = false;
    const finish = keep => { if (done) return; done = true; const v = input.value.trim(); input.replaceWith(el); if (keep && v && v !== el.textContent) { el.textContent = v; save(v); } };
    input.addEventListener("keydown", ev => { ev.stopPropagation(); if (ev.key === "Enter") finish(true); if (ev.key === "Escape") finish(false); });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("click", ev => ev.stopPropagation());
  });
  return el;
}

function toast(text) {
  if (!text) return;
  const t = h("div", { class: "toast", text });
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 220); }, 3200);
  while ($("#toasts").children.length > 3) $("#toasts").firstChild.remove();
}

function card(title, sub, ...kids) {
  return h("div", { class: "card" }, title ? h("h2", { text: title }) : null, sub ? h("p", { class: "sub", text: sub }) : null, ...kids);
}

// ------------------------------------------------------------------ the room picture
function stage(height, caption) {
  const cv = h("canvas");
  const el = h("div", { class: "stage", style: { height: height + "px" } }, cv, caption ? h("div", { class: "stage-cap", text: caption }) : null);
  stageCanvases.add(cv);
  return el;
}
let stageQueued = false;
function drawStages() {
  if (stageQueued || document.hidden) return;
  stageQueued = true;
  requestAnimationFrame(() => { stageQueued = false; for (const cv of stageCanvases) if (cv.isConnected) drawStage(cv); });
}

function monitorArt() {
  if (S.sync.on) return { kind: S.sync.kind === "Music" ? "music" : "screen" };
  const looks = new Map();
  for (const l of S.lights) {
    if (!l.rgb) continue;
    let label = l.words || "On"; if (label === "Color") label = hueName(l.rgb);
    if (!looks.has(label)) looks.set(label, l.rgb);
  }
  if (!looks.size) return { kind: "off" };
  const scene = STATIC.scenes.find(x => x.name === [...looks.keys()][0]);
  if (looks.size === 1 && scene) return { kind: "scene", name: scene.name, colors: scene.look };
  return { kind: "manual", items: [...looks].slice(0, 3).map(([label, rgb]) => {
    const sc = STATIC.scenes.find(x => x.name === label); return [sc ? sc.look[0] : rgb, label]; }) };
}

function drawStage(cv) {
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const sc = Math.min(H / 250, W / 760);
  // wall
  const wall = g.createLinearGradient(0, 0, 0, H); wall.addColorStop(0, "#121318"); wall.addColorStop(1, "#0c0d10");
  g.fillStyle = wall; g.fillRect(0, 0, W, H);
  // layout
  const mw = 250 * sc, mh = 141 * sc, my = H * 0.42 - mh / 2, desk = my + mh + 28 * sc;
  const side = S.side_display, lw = 150 * sc, lh = 94 * sc, gap = 40 * sc;
  let mx, lx = null, left, right;
  if (side) { const gx = (W - (mw + gap + lw)) / 2; if (side === "right") { mx = gx; lx = gx + mw + gap; } else { lx = gx; mx = gx + lw + gap; } left = gx; right = gx + mw + gap + lw; }
  else { mx = (W - mw) / 2; left = mx; right = mx + mw; }
  // bulbs: left and right of the desk
  const sides = { left: [], right: [] };
  S.lights.forEach((l, i) => sides[l.side || (i % 2 ? "right" : "left")].push(l));
  const room = left - 50 * sc, bulbs = [];
  for (const s of ["left", "right"]) {
    const items = sides[s]; if (!items.length) continue;
    const step = Math.min(150 * sc, Math.max(70 * sc, room / items.length));
    items.forEach((l, j) => bulbs.push({ l, x: s === "left" ? left - step * (j + 1) : right + step * (j + 1), y: H * 0.36 }));
  }
  // light on the wall
  g.save(); g.globalCompositeOperation = "lighter";
  for (const b of bulbs) {
    if (!b.l.rgb) continue;
    const a = 0.16 + 0.34 * (b.l.dim / 100), R = 150 * sc;
    const grd = g.createRadialGradient(b.x, b.y, 0, b.x, b.y, R * 1.6);
    grd.addColorStop(0, rgbCss(b.l.rgb, a)); grd.addColorStop(0.45, rgbCss(b.l.rgb, a * 0.45)); grd.addColorStop(1, rgbCss(b.l.rgb, 0));
    g.fillStyle = grd; g.beginPath(); g.ellipse(b.x, b.y, R * 1.6, R * 1.9, 0, 0, Math.PI * 2); g.fill();
  }
  g.restore();
  // desk line
  g.fillStyle = "rgba(255,255,255,0.03)"; g.fillRect(0, desk, W, H - desk);
  // laptop
  const round = (x, y, w, hgt, r) => { g.beginPath(); g.roundRect(x, y, w, hgt, r); };
  if (lx != null) {
    const ly = desk - 7 * sc - lh, bz = 4 * sc;
    round(lx - bz, ly - bz, lw + 2 * bz, lh + 2 * bz, 6 * sc); g.fillStyle = "#0a0b0e"; g.fill(); g.strokeStyle = "#2c3038"; g.lineWidth = 1; g.stroke();
    g.fillStyle = "#15171c"; g.fillRect(lx, ly, lw, lh);
    g.fillStyle = "#1e2128"; g.fillRect(lx, ly + lh - 8 * sc, lw, 8 * sc);
    g.fillStyle = "#23262e"; round(lx + 14 * sc, ly + 14 * sc, 70 * sc, 44 * sc, 3 * sc); g.fill();
    g.fillStyle = "#2b2f38"; round(lx + 64 * sc, ly + 30 * sc, 70 * sc, 46 * sc, 3 * sc); g.fill();
    g.fillStyle = "#30343d"; g.beginPath(); g.moveTo(lx - bz, desk - 7 * sc); g.lineTo(lx + lw + bz, desk - 7 * sc); g.lineTo(lx + lw + bz + 14 * sc, desk); g.lineTo(lx - bz - 14 * sc, desk); g.closePath(); g.fill();
  }
  // monitor
  const bz = 5 * sc;
  round(mx - bz, my - bz, mw + 2 * bz, mh + 2 * bz, 7 * sc); g.fillStyle = "#0a0b0e"; g.fill(); g.strokeStyle = "#2c3038"; g.lineWidth = 1; g.stroke();
  const art = monitorArt();
  g.fillStyle = art.kind === "off" ? "#0b0c0f" : "#14161b"; g.fillRect(mx, my, mw, mh);
  paintArt(g, art, mx, my, mw, mh, sc, sc < 0.7);
  const sx = mx + mw / 2;
  g.fillStyle = "#30343d"; g.fillRect(sx - 7 * sc, my + mh + bz, 14 * sc, 20 * sc);
  round(sx - 46 * sc, my + mh + 22 * sc, 92 * sc, desk - (my + mh + 22 * sc), 3 * sc); g.fill();
  // bulbs and their names
  const small = sc < 0.7;
  for (const b of bulbs) {
    const r = 18 * sc;
    if (b.l.rgb) {
      g.fillStyle = rgbCss(b.l.rgb); g.beginPath(); g.arc(b.x, b.y, r, 0, Math.PI * 2); g.fill();
      g.fillStyle = rgbCss(b.l.rgb.map(v => Math.round(v + (255 - v) * 0.4))); g.beginPath(); g.arc(b.x, b.y - 3 * sc, r * 0.55, 0, Math.PI * 2); g.fill();
    } else {
      g.fillStyle = "#23262d"; g.beginPath(); g.arc(b.x, b.y, r, 0, Math.PI * 2); g.fill(); g.strokeStyle = "#353943"; g.stroke();
    }
    g.fillStyle = "#6d727d"; round(b.x - 8 * sc, b.y + r - 2 * sc, 16 * sc, 10 * sc, 2 * sc); g.fill();
    if (small) continue;
    g.textAlign = "center"; g.fillStyle = "#e9ebef"; g.font = `600 ${Math.max(11, 13 * sc)}px "Segoe UI Variable Text","Segoe UI",system-ui`;
    g.fillText(b.l.name, b.x, b.y + r + 30 * sc);
    g.fillStyle = "#8b919c"; g.font = `${Math.max(10, 11.5 * sc)}px "Segoe UI Variable Text","Segoe UI",system-ui`;
    g.fillText(b.l.rgb ? lookText(b.l) : "Off", b.x, b.y + r + 48 * sc);
  }
}

function paintArt(g, art, x, y, w, hgt, sc, small) {
  const cx = x + w / 2, accent = [139, 124, 255], pink = [255, 120, 170];
  const font = (size, weight = 600) => `${weight} ${Math.max(10, size * sc)}px "Segoe UI Variable Display","Segoe UI",system-ui`;
  g.textAlign = "center";
  if (art.kind === "screen") {
    const sw = 58 * sc, sh = 36 * sc, cy = y + hgt * 0.38;
    g.strokeStyle = rgbCss(accent); g.lineWidth = Math.max(2, 3 * sc); g.beginPath(); g.roundRect(cx - sw / 2, cy - sh / 2, sw, sh, 6 * sc); g.stroke();
    const t = 9 * sc; g.fillStyle = rgbCss(accent); g.beginPath(); g.moveTo(cx - t * 0.7, cy - t); g.lineTo(cx - t * 0.7, cy + t); g.lineTo(cx + t, cy); g.closePath(); g.fill();
    for (const s of [-1, 1]) {
      const ex = cx + s * (sw / 2 + 9 * sc);
      for (const dy of [-1, 0, 1]) { g.beginPath(); g.moveTo(ex, cy + dy * 9 * sc); g.lineTo(ex + s * 13 * sc, cy + dy * 15 * sc); g.stroke(); }
    }
    if (!small) { g.fillStyle = "#e9ebef"; g.font = font(15); g.fillText("Screen sync", cx, y + hgt * 0.78); }
  } else if (art.kind === "music") {
    const lv = [0.35, 0.6, 0.85, 0.55, 0.72, 0.95, 0.62, 0.42, 0.78, 0.5, 0.3, 0.66, 0.45, 0.25];
    const pad = 16 * sc, gap = Math.max(2, 4 * sc), bw = (w - 2 * pad - gap * (lv.length - 1)) / lv.length, base = y + hgt - 14 * sc;
    lv.forEach((v, i) => {
      const t = i / (lv.length - 1), col = accent.map((a, k) => Math.round(a + (pink[k] - a) * t));
      const bh = (hgt - 34 * sc) * v; g.fillStyle = rgbCss(col); g.beginPath(); g.roundRect(x + pad + i * (bw + gap), base - bh, bw, bh, 2 * sc); g.fill();
    });
  } else if (art.kind === "scene") {
    const r = 13 * sc, cy = y + hgt * 0.36;
    art.colors.forEach((c, i) => { g.fillStyle = rgbCss(c); g.beginPath(); g.arc(cx + (i ? 17 : -17) * sc, cy, r, 0, Math.PI * 2); g.fill(); });
    if (!small) { g.fillStyle = "#e9ebef"; g.font = font(15); g.fillText(art.name, cx, y + hgt * 0.72); }
  } else if (art.kind === "manual") {
    const n = art.items.length, size = (n === 1 ? 44 : 34) * sc, step = size + 40 * sc, cy = y + hgt * 0.38;
    art.items.forEach(([c, label], i) => {
      const px = cx - step * (n - 1) / 2 + i * step;
      g.fillStyle = rgbCss(c); g.beginPath(); g.roundRect(px - size / 2, cy - size / 2, size, size, 9 * sc); g.fill();
      if (!small) { g.fillStyle = "#e9ebef"; g.font = n === 1 ? font(15) : font(10.5, 500); g.fillText(label, px, cy + size / 2 + 20 * sc); }
    });
  } else {
    const r = 17 * sc, mxx = cx - 10 * sc, cy = y + hgt / 2, o = 8 * sc;
    g.fillStyle = "#c6c9d1"; g.beginPath(); g.arc(mxx, cy, r, 0, Math.PI * 2); g.fill();
    g.fillStyle = "#0b0c0f"; g.beginPath(); g.arc(mxx + o, cy - o / 2, r, 0, Math.PI * 2); g.fill();
    if (!small) { g.fillStyle = "#8b919c"; g.font = font(10, 500); g.fillText("z z", cx + 26 * sc, cy - 14 * sc); }
  }
}

// ------------------------------------------------------------------ Lights page
function lightTile(l) {
  const ip = l.ip;
  const o = orb(l);
  const sub = h("div", { class: "tile-sub" });
  const value = h("span", { class: "num" });
  const send = throttle(v => api.set_brightness(`light:${ip}`, v, false), 70);
  const sl = slider({ min: 10, max: 100, label: `${l.name} brightness`, get: () => lightByIp(ip)?.dim || null,
    onInput: v => { value.textContent = v + "%"; send(v); }, onChange: v => api.set_brightness(`light:${ip}`, v, true) });
  const tile = h("div", { class: "tile", onclick: () => openSoon(`light:${ip}`) },
    h("div", { class: "tile-head" }, o, h("div", { class: "tile-body" }, nameCell(l.name, v => api.set_light(ip, "name", v)), sub),
      toggle(() => isOn(lightByIp(ip)), on => api.power(`light:${ip}`, on), `${l.name} power`)),
    h("div", { class: "tile-foot" }, icon("lights", 16), sl, value));
  const up = () => {
    const cur = lightByIp(ip); if (!cur) return;
    paintOrb(o, cur);
    const room = cur.room ? roomById(cur.room)?.name : null;
    sub.textContent = (room ? room + "  ·  " : "") + lookText(cur);
    if (!sl.matches(":active")) value.textContent = (cur.rgb ? cur.dim : 0) + "%";
    tile.classList.toggle("selected", drawerKey === `light:${ip}`);
  };
  up(); updaters.push(up); liveHooks.push(up);
  return tile;
}

function pageLights(c) {
  liveHooks = [renderNow];
  c.append(stage(260));
  const ipBox = h("input", { class: "text", placeholder: "Add by IP address", style: { width: "180px" } });
  const add = async () => { const r = await api.add_ip(ipBox.value); toast(r.message); if (r.ok) ipBox.value = ""; };
  ipBox.addEventListener("keydown", e => { if (e.key === "Enter") add(); });
  const find = h("button", { class: "btn primary", onclick: () => api.discover() }, icon("refresh", 16), h("span", { text: S.discovering ? "Searching" : "Find lights" }));
  find.disabled = S.discovering;
  c.append(h("div", { class: "toolbar" }, h("h2", { text: `Your lights` }), ipBox, h("button", { class: "btn", text: "Add", onclick: add }), find));
  if (!S.lights.length) {
    c.append(h("div", { class: "empty" }, h("p", { text: "No lights yet. Click Find lights to search your Wi-Fi, or add one by its IP address." })));
    return;
  }
  const grid = h("div", { class: "grid" });
  S.lights.forEach(l => grid.append(lightTile(l)));
  c.append(grid);
}

// ------------------------------------------------------------------ Rooms page
function roomTile(r) {
  const stack = h("div", { class: "orb-stack" });
  const sub = h("div", { class: "tile-sub" });
  const value = h("span", { class: "num" });
  const key = `room:${r.id}`;
  const send = throttle(v => api.set_brightness(key, v, false), 70);
  const sl = slider({ min: 10, max: 100, label: `${r.name} brightness`,
    get: () => { const on = keyIps(key).map(lightByIp).filter(l => l && l.rgb); return on.length ? Math.round(on.reduce((a, l) => a + l.dim, 0) / on.length) : null; },
    onInput: v => { value.textContent = v + "%"; send(v); }, onChange: v => api.set_brightness(key, v, true) });
  const tile = h("div", { class: "tile", onclick: () => openSoon(key) },
    h("div", { class: "tile-head" }, h("div", { class: "tile-body" }, nameCell(r.name, v => api.rename_room(r.id, v)), sub),
      toggle(() => keyIps(key).some(ip => isOn(lightByIp(ip) || {})), on => api.power(key, on), `${r.name} power`)),
    h("div", { class: "tile-foot" }, stack, sl, value));
  const up = () => {
    const room = roomById(r.id); if (!room) return;
    const ls = room.lights.map(lightByIp).filter(Boolean);
    if (stack.children.length !== ls.length) { stack.innerHTML = ""; ls.forEach(() => stack.append(h("span", { class: "orb" }))); }
    ls.forEach((l, i) => paintOrb(stack.children[i], l));
    const on = ls.filter(isOn).length;
    sub.textContent = !ls.length ? "No lights yet" : `${ls.length} ${ls.length === 1 ? "light" : "lights"}  ·  ${on ? on + " on" : "All off"}`;
    if (!sl.matches(":active")) { const v = sl.value; value.textContent = (on ? v : 0) + "%"; }
    tile.classList.toggle("selected", drawerKey === key);
  };
  up(); updaters.push(up); liveHooks.push(up);
  return tile;
}

function pageRooms(c) {
  liveHooks = [renderNow];
  c.append(h("div", { class: "toolbar", style: { marginTop: 0 } }, h("h2", { text: "Rooms" }),
    h("button", { class: "btn primary", onclick: () => roomModal() }, icon("plus", 16), h("span", { text: "New room" }))));
  const grid = h("div", { class: "grid" });
  S.rooms.forEach(r => grid.append(roomTile(r)));
  grid.append(h("div", { class: "tile add", onclick: () => roomModal() }, h("div", { class: "row" }, icon("plus", 18), h("span", { text: "New room" }))));
  c.append(grid);
}

function roomModal(preselect = []) {
  const chosen = new Set(preselect);
  const name = h("input", { class: "text", placeholder: "Room name, for example Bedroom", style: { width: "100%" } });
  const list = h("div", { class: "check-list" }, S.lights.map(l => {
    const cb = h("input", { type: "checkbox" }); cb.checked = chosen.has(l.ip);
    cb.onchange = () => cb.checked ? chosen.add(l.ip) : chosen.delete(l.ip);
    const inRoom = l.room ? roomById(l.room)?.name : null;
    return h("label", { class: "check" }, cb, orb(l, "orb sm"), h("span", { text: l.name }), h("span", { class: "spacer" }), inRoom ? h("span", { class: "faint", text: `In ${inRoom}` }) : null);
  }));
  const save = async () => {
    if (!name.value.trim()) { name.focus(); toast("Give the room a name."); return; }
    await api.save_room(null, name.value, [...chosen]); closeModal(); go("rooms");
  };
  name.addEventListener("keydown", e => { if (e.key === "Enter") save(); });
  showModal(h("div", { class: "modal" }, h("h2", { text: "New room" }),
    h("div", { class: "stack" }, name, h("div", { class: "muted", text: "Lights in this room. A light can be in one room." }), list),
    h("div", { class: "modal-foot" }, h("button", { class: "btn ghost", text: "Cancel", onclick: closeModal }), h("button", { class: "btn primary", text: "Add room", onclick: save }))));
  setTimeout(() => name.focus(), 30);
}

// ------------------------------------------------------------------ Scenes page
let sceneTarget = "all";
function sceneGrid(getKey, compact) {
  const wrap = h("div");
  for (const [group, ids] of STATIC.scene_groups) {
    const grid = h("div", { class: "scene-grid" + (compact ? " compact" : "") });
    for (const id of ids) {
      const sc = STATIC.scenes.find(x => x.id === id);
      const [a, b] = sc.look;
      const tile = h("button", { class: "scene", title: `Play ${sc.name}`, onclick: () => api.play_scene(getKey(), id) },
        h("div", { class: "swatch", style: { background: `radial-gradient(120% 90% at 15% 20%, ${rgbCss(a)}, transparent 60%), radial-gradient(120% 90% at 85% 80%, ${rgbCss(b)}, transparent 60%), linear-gradient(135deg, ${rgbCss(a, 0.8)}, ${rgbCss(b, 0.8)})` } }),
        h("div", { class: "shade" }), h("span", { class: "play" }, icon("play", 14)), h("span", { class: "label", text: sc.name }));
      const up = () => tile.classList.toggle("playing", !!S.scene_playing && S.scene_playing[0] === id);
      up(); reg(up);
      grid.append(tile);
    }
    wrap.append(h("div", { class: "section-title" }, h("h2", { text: group }), h("span", { class: "muted", text: group.startsWith("Moving") ? "Animated by the bulb. Speed applies." : "Steady light for everyday use." })), grid);
  }
  return wrap;
}

function sceneControls() {
  return h("div", { class: "fields" },
    settingSlider("scene_speed", "Speed", 10, 200, 10, v => `${v}%`),
    settingSlider("scene_brightness", "Brightness", 10, 100, 1, v => `${v}%`));
}

function pageScenes(c) {
  liveHooks = [renderNow];
  if (sceneTarget !== "all" && !keyIps(sceneTarget).length) sceneTarget = "all";
  const chips = h("div", { class: "chips" });
  const targets = [["all", "All lights"], ...S.rooms.map(r => [`room:${r.id}`, r.name]), ...S.lights.map(l => [`light:${l.ip}`, l.name])];
  for (const [k, label] of targets) chips.append(h("button", { class: "chip" + (k === sceneTarget ? " on" : ""), text: label, onclick: () => { sceneTarget = k; chips.querySelectorAll(".chip").forEach(x => x.classList.toggle("on", x.textContent === label)); } }));
  c.append(h("div", { class: "split", style: { gridTemplateColumns: "minmax(0,1fr) 340px", marginBottom: "4px" } },
    card("Play on", "Pick where a scene plays, then click a scene.", chips),
    h("div", { class: "narrow" }, card("Scene settings", null, sceneControls()))));
  c.append(sceneGrid(() => sceneTarget, false));
}

// ------------------------------------------------------------------ Screen sync page
function pageScreen(c) {
  liveHooks = [renderNow];
  const f = (v, d = 0) => (+v).toFixed(d);
  // presets
  const presetChoices = () => [...Object.keys(S.settings.screen_presets).map(n => ({ value: n, label: n })), { sep: true }, { value: "__new__", label: "Create new" }];
  const status = h("div", { class: "muted", style: { marginTop: "10px" } });
  const saveBtn = h("button", { class: "btn primary", text: "Save" });
  let presetRow;
  if (pendingPreset) {
    const nameBox = h("input", { class: "text", placeholder: "Name the new preset", style: { width: "220px" }, maxlength: 30 });
    const create = async () => { const r = await api.create_preset(nameBox.value); if (r.ok) { pendingPreset = false; renderPage(false); } else { status.textContent = r.message; status.style.color = "var(--danger)"; nameBox.focus(); } };
    nameBox.addEventListener("keydown", e => { if (e.key === "Enter") create(); if (e.key === "Escape") { pendingPreset = false; renderPage(false); } });
    nameBox.addEventListener("input", () => { status.textContent = "Name it, change the settings below, then click Save."; status.style.color = ""; });
    saveBtn.onclick = create;
    presetRow = h("div", { class: "row" }, nameBox, saveBtn, h("button", { class: "btn ghost", text: "Cancel", onclick: () => { pendingPreset = false; renderPage(false); } }));
    status.textContent = "Name it, change the settings below, then click Save.";
    setTimeout(() => nameBox.focus(), 30);
  } else {
    saveBtn.onclick = () => api.save_preset();
    const del = confirmBtn("Delete", () => api.delete_preset(S.settings.screen_preset));
    presetRow = h("div", { class: "row" },
      select(presetChoices, () => S.settings.screen_preset, v => { if (v === "__new__") { pendingPreset = true; renderPage(false); } else api.pick_preset(v); }, 220),
      saveBtn, del);
    onUpdate(() => {
      const changed = S.preset_changed;
      saveBtn.disabled = !changed;
      status.textContent = changed ? `Changed since you picked ${S.settings.screen_preset}. Save to keep it.` : `Using the ${S.settings.screen_preset} preset.`;
      status.style.color = changed ? "var(--accent)" : "";
      del.disabled = S.settings.screen_preset === "Default";
    });
  }
  const left = h("div", { class: "stack" },
    card("Presets", "Pick a preset to load its settings. Pick Create new at the end of the list to make your own.", presetRow, status),
    card("Color", "How the app turns your screen into one color for each light.", h("div", { class: "fields" },
      h("div", { class: "field two" }, h("label", { text: "Color mode" }), h("div", {}, seg(STATIC.color_modes, () => S.settings.color_mode, v => api.set_setting("color_mode", v)))),
      h("div", { class: "muted", style: { fontSize: "12px", marginLeft: "184px" }, text: "Vivid lets strong colors win over gray. Average is the true average. Dominant takes the most common strong color, good for movies and games." }),
      settingSlider("saturation", "Color intensity", 0.5, 3, 0.1, v => f(v, 1) + "x"))),
    card("Motion", null, h("div", { class: "fields" },
      settingSlider("smoothing", "Smoothing", 0, 0.95, 0.05, v => f(v, 2)),
      settingSlider("fps", "Updates per second", 1, 20, 1, v => f(v)),
      settingSlider("change_threshold", "Ignore small changes", 0, 20, 1, v => f(v)))),
    card("Brightness", null, h("div", { class: "fields" },
      h("div", { class: "field two" }, h("label", { text: "Mode" }), h("div", {}, seg(STATIC.brightness_modes, () => S.settings.brightness_mode, v => api.set_setting("brightness_mode", v)))),
      settingSlider("min_brightness", "Lowest", 10, 100, 1, v => v + "%"),
      settingSlider("max_brightness", "Highest", 10, 100, 1, v => v + "%"),
      settingSlider("brightness_gain", "Response", 0.5, 6, 0.1, v => f(v, 1) + "x"),
      settingSlider("fixed_brightness", "Fixed level", 10, 100, 1, v => v + "%"))),
    card("Dark scenes", "What the lights do when the screen is almost black.", h("div", { class: "fields" },
      h("div", { class: "field two" }, h("label", { text: "When the screen is dark" }), h("div", {}, select(STATIC.dark_scene, () => S.settings.dark_scene, v => api.set_setting("dark_scene", v), 220))),
      settingSlider("dark_threshold", "Counts as dark below", 0, 0.2, 0.01, v => f(v, 2)))),
    card("Monitor and capture area", "Leave out parts of the screen, such as the taskbar.", h("div", { class: "fields" },
      h("div", { class: "field two" }, h("label", { text: "Capture" }), h("div", { class: "row" },
        select(() => MONITORS.map(m => ({ value: m.index, label: m.label })), () => S.settings.monitor, v => api.set_setting("monitor", +v), 280),
        h("button", { class: "icon-btn", title: "Refresh the list", onclick: async () => { MONITORS = await api.monitors(); renderPage(false); } }, icon("refresh", 16)))),
      h("div", { class: "field switch" }, h("label", { text: "Ignore black bars on movies" }), h("div", {}, toggle(() => S.settings.ignore_black_bars, v => api.set_setting("ignore_black_bars", v)))),
      settingSlider("crop_top", "Ignore top", 0, 40, 1, v => v + "%"),
      settingSlider("crop_bottom", "Ignore bottom", 0, 40, 1, v => v + "%"),
      settingSlider("crop_left", "Ignore left", 0, 40, 1, v => v + "%"),
      settingSlider("crop_right", "Ignore right", 0, 40, 1, v => v + "%"),
      settingSlider("sample_step", "Pixel sampling", 2, 24, 1, v => `1 in ${v}`))));
  const zones = h("div", { class: "fields" }, S.lights.map(l => h("div", { class: "row" },
    orbLive(l.ip), h("div", { class: "tile-body" }, h("div", { class: "tile-name", style: { fontSize: "13px" }, text: l.name })),
    select(STATIC.zones, () => lightByIp(l.ip)?.zone, v => api.set_light(l.ip, "zone", v), 150),
    toggle(() => lightByIp(l.ip)?.enabled, v => api.set_light(l.ip, "enabled", v), `Include ${l.name} in sync`))));
  const right = h("div", { class: "stack sticky" }, stage(220),
    card("Lights and screen areas", "Each light follows its part of the screen. Switch a light off here to leave it out of sync.", S.lights.length ? zones : h("div", { class: "muted", text: "No lights yet." })));
  c.append(h("div", { class: "toolbar", style: { marginTop: 0 } }, h("h2", { text: "Screen sync" }), confirmBtn("Reset to defaults", () => api.reset("screen"), "btn ghost danger")));
  c.append(h("div", { class: "split" }, left, right));
}

function orbLive(ip) {
  const o = orb(lightByIp(ip), "orb");
  o.style.width = o.style.height = "26px";
  const up = () => paintOrb(o, lightByIp(ip)); liveHooks.push(up); updaters.push(up);
  return o;
}

// ------------------------------------------------------------------ Music sync page
const MUSIC_INFO = {
  "Calm rhythm": ["calm", "Holds one color and swells with each beat, from low to high. Moves to a new color every few beats."],
  "Dynamic rhythm": ["dynamic", "Holds one color and flashes it on with each beat, off in between. Moves to a new color every few beats."],
  "Beat pulse": ["pulse", "Jumps to a new color on every beat."],
  "Spectrum": ["spectrum", "Bass shows as red, mids as green and highs as blue."],
  "Mellow": ["mellow", "Drifts slowly through colors and breathes with the volume."],
};
function pageMusic(c) {
  liveHooks = [renderNow];
  if (!STATIC.soundcard) c.append(h("div", { class: "banner", text: "Music sync needs one more package. Run  python -m pip install soundcard  and restart WiZard." }));
  const setDefault = h("button", { class: "btn", text: "Set as default", title: "Make these settings what Reset to defaults brings back for this style", onclick: () => api.set_music_default() });
  onUpdate(() => { setDefault.style.display = S.music_changed ? "" : "none"; });
  c.append(h("div", { class: "toolbar", style: { marginTop: 0 } }, h("h2", { text: "Style" }),
    h("span", { class: "muted", text: "Each style keeps its own settings." }), setDefault,
    confirmBtn("Reset to defaults", () => api.reset("music"), "btn ghost danger")));
  const styles = h("div", { class: "styles" });
  for (const m of STATIC.music_modes) {
    const [ic, text] = MUSIC_INFO[m] || ["music", ""];
    const b = h("button", { class: "style", onclick: () => api.set_setting("music_mode", m) }, icon(ic, 22), h("b", { text: m }), h("span", { text }));
    onUpdate(() => b.classList.toggle("on", S.settings.music_mode === m));
    styles.append(b);
  }
  c.append(styles);
  const mode = S.settings.music_mode, rhythm = mode.endsWith("rhythm");
  const palette = h("div", { class: "chips" }, STATIC.palettes.map(p => {
    const b = h("button", { class: "chip", text: p, onclick: () => api.set_setting("music_palette", p) });
    onUpdate(() => b.classList.toggle("on", S.settings.music_palette === p)); return b;
  }));
  const colors = card("Colors", rhythm ? "The colors the rhythm moves through." : "The colors Beat pulse and Mellow move through.", h("div", { class: "fields" }, palette,
    h("div", { class: "field switch" }, h("label", { text: "Different color on each light" }), h("div", {}, toggle(() => S.settings.music_split_colors, v => api.set_setting("music_split_colors", v))))));
  const fields = [];
  if (rhythm) {
    fields.push(h("div", { class: "field two" }, h("label", { text: "Beats per color" }),
      h("div", {}, seg([[2, "2"], [4, "4"], [8, "8"], [16, "16"]], () => S.settings.music_beats_per_color, v => api.set_setting("music_beats_per_color", +v)))));
    fields.push(h("div", { class: "muted", style: { fontSize: "12px", marginLeft: "184px" }, text: "A color also changes early when the music suddenly gets louder, like a drop or a chorus." }));
    if (mode === "Calm rhythm") fields.push(settingSlider("music_rhythm_low", "Between beats", 10, 100, 1, v => v + "%"));
    fields.push(settingSlider("music_rhythm_high", "On the beat", 10, 100, 1, v => v + "%"));
    if (mode === "Dynamic rhythm") fields.push(settingSlider("music_on_length", "On time per beat", 15, 90, 5, v => `${v}%`));
    fields.push(settingSlider("music_sensitivity", "Beat sensitivity", 0.4, 2, 0.1, v => (+v).toFixed(1) + "x"));
  } else {
    fields.push(settingSlider("music_sensitivity", "Beat sensitivity", 0.4, 2, 0.1, v => (+v).toFixed(1) + "x"));
    fields.push(settingSlider("music_min_brightness", "Quiet parts", 10, 100, 1, v => v + "%"));
    fields.push(settingSlider("music_max_brightness", "Loud parts", 10, 100, 1, v => v + "%"));
    fields.push(settingSlider("music_smoothing", "Smoothing", 0, 0.9, 0.05, v => (+v).toFixed(2)));
    fields.push(settingSlider("music_fps", "Updates per second", 4, 20, 1, v => String(v)));
  }
  const left = h("div", { class: "stack" }, card(rhythm ? "Rhythm" : "Response", null, h("div", { class: "fields" }, fields)), colors);
  const bpm = h("div", { class: "bpm" }), bpmNote = h("div", { class: "muted" });
  const live = () => {
    const on = S.sync.on && S.sync.kind === "Music";
    bpm.textContent = on && S.sync.bpm ? `${S.sync.bpm} BPM` : on ? "Listening" : "Not syncing";
    bpmNote.textContent = on ? (rhythm ? "Tempo found from the drums. Beats in between are filled in." : "Reacting to the music.") : "Pick Music at the top and click Start sync.";
  };
  live(); liveHooks.push(live); onUpdate(live);
  const right = h("div", { class: "stack sticky" }, stage(220), card(null, null, bpm, bpmNote));
  c.append(h("div", { class: "split", style: { marginTop: "16px" } }, left, right));
}

// ------------------------------------------------------------------ Schedules
function scheduleRow(sc, compact) {
  return h("div", { class: "sched" + (sc.enabled === false ? " off" : "") },
    h("div", { class: "time", text: sc.when.split(",")[0] }),
    h("div", { class: "what" }, h("div", { style: { fontWeight: 600 }, text: sc.summary }),
      h("div", { class: "muted", text: `${compact ? "" : sc.target_name + "  ·  "}${sc.when.split(", ").slice(1).join(", ")}` })),
    toggle(() => S.schedules.find(x => x.id === sc.id)?.enabled !== false, v => api.toggle_schedule(sc.id, v), "Schedule on or off"),
    h("div", { class: "row", style: { gap: "2px" } },
      h("button", { class: "icon-btn", title: "Edit", onclick: () => scheduleModal(sc) }, icon("edit", 16)),
      h("button", { class: "icon-btn", title: "Delete", onclick: () => api.delete_schedule(sc.id) }, icon("trash", 16))));
}

function pageSchedules(c) {
  liveHooks = [renderNow];
  c.append(h("div", { class: "toolbar", style: { marginTop: 0 } }, h("h2", { text: "Schedules" }),
    h("button", { class: "btn primary", onclick: () => scheduleModal(null, "all") }, icon("plus", 16), h("span", { text: "Add schedule" }))));
  if (!S.schedules.length) { c.append(h("div", { class: "empty", text: "No schedules yet. Wake up to a slow sunrise, fade out at bedtime, or start sync at a set time." })); return; }
  S.schedules.forEach(sc => c.append(scheduleRow(sc)));
}

function scheduleModal(existing, key = "all") {
  const sc = existing ? JSON.parse(JSON.stringify(existing)) : { enabled: true, time: "07:00", days: [0, 1, 2, 3, 4], target: key, action: "Wake-up fade", value: { minutes: 30 } };
  sc.value = sc.value || {};
  const targets = [{ value: "all", label: "All lights" }, ...S.rooms.map(r => ({ value: `room:${r.id}`, label: r.name })), ...S.lights.map(l => ({ value: `light:${l.ip}`, label: l.name }))];
  const time = h("input", { type: "time", class: "text", value: sc.time });
  const days = h("div", { class: "days" }, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d, i) => {
    const b = h("button", { class: sc.days.includes(i) ? "on" : null, text: d, onclick: () => { b.classList.toggle("on"); sc.days = [...days.querySelectorAll("button")].map((x, j) => x.classList.contains("on") ? j : -1).filter(j => j >= 0); } });
    return b;
  }));
  const valueBox = h("div", { class: "fields" });
  const buildValue = () => {
    sink = []; valueBox.innerHTML = "";
    const v = sc.value, a = sc.action;
    const vs = (label, k, min, max, step, def, fmt) => {
      if (v[k] == null) v[k] = def;
      const out = h("span", { class: "value", text: fmt(v[k]) });
      const s = h("input", { type: "range", min, max, step, value: v[k] });
      const paint = () => s.style.setProperty("--p", `${((s.value - min) / (max - min)) * 100}%`); paint();
      s.oninput = () => { v[k] = +s.value; out.textContent = fmt(v[k]); paint(); };
      return h("div", { class: "field" }, h("label", { text: label }), s, out);
    };
    if (["Turn on", "Set white", "Set color", "Play scene"].includes(a)) valueBox.append(vs("Brightness", "brightness", 10, 100, 1, 80, x => x + "%"));
    if (a === "Set white") valueBox.append(vs("White", "kelvin", STATIC.kelvin[0], STATIC.kelvin[1], 100, 3000, x => x + "K"));
    if (a === "Set color") {
      if (!v.color) v.color = STATIC.quick_colors[1];
      const sw = h("div", { class: "swatches", style: { justifyContent: "flex-start" } }, STATIC.quick_colors.map(col => {
        const b = h("button", { class: "swatch-btn", style: { background: rgbCss(col), outline: String(col) === String(v.color) ? "2px solid #fff" : "" }, onclick: () => { v.color = col; sw.querySelectorAll("button").forEach(x => x.style.outline = ""); b.style.outline = "2px solid #fff"; } });
        return b;
      }));
      valueBox.append(h("div", { class: "field two" }, h("label", { text: "Color" }), sw));
    }
    if (a === "Play scene") {
      if (!v.scene) v.scene = 6;
      valueBox.append(h("div", { class: "field two" }, h("label", { text: "Scene" }), h("div", {}, select(STATIC.scenes.map(s => ({ value: s.id, label: s.name })), () => v.scene, x => { v.scene = +x; buildValue(); }, 220))));
      valueBox.append(vs("Speed", "speed", 10, 200, 10, 100, x => x + "%"));
    }
    if (a === "Wake-up fade" || a === "Bedtime fade") valueBox.append(vs("Fade over", "minutes", 5, 60, 5, 30, x => x + " min"));
    sink.forEach(f => f()); sink = null;
  };
  sink = [];
  const actionSel = select(STATIC.actions, () => sc.action, v => { sc.action = v; sc.value = {}; actionSel.querySelector(".sel-label").textContent = v; buildValue(); }, 240);
  const targetSel = select(targets, () => sc.target, v => { sc.target = v; targetSel.querySelector(".sel-label").textContent = targets.find(t => t.value === v).label; }, 240);
  sink.forEach(f => f()); sink = null;
  buildValue();
  const save = async () => {
    sc.time = time.value || "07:00";
    if (!sc.days.length) { toast("Pick at least one day."); return; }
    delete sc.summary; delete sc.when; delete sc.target_name;
    await api.save_schedule(sc); closeModal();
  };
  showModal(h("div", { class: "modal" }, h("h2", { text: existing ? "Edit schedule" : "Add schedule" }),
    h("div", { class: "fields" },
      h("div", { class: "field two" }, h("label", { text: "Lights" }), h("div", {}, targetSel)),
      h("div", { class: "field two" }, h("label", { text: "Time" }), h("div", {}, time)),
      h("div", { class: "field two" }, h("label", { text: "Days" }), days),
      h("div", { class: "field two" }, h("label", { text: "Action" }), h("div", {}, actionSel)),
      valueBox),
    h("div", { class: "modal-foot" }, h("button", { class: "btn ghost", text: "Cancel", onclick: closeModal }), h("button", { class: "btn primary", text: "Save schedule", onclick: save }))));
}

function showModal(el) { const m = $("#modal"); m.innerHTML = ""; m.append(el); m.classList.add("open"); m.onclick = e => { if (e.target === m) closeModal(); }; }
function closeModal() { const m = $("#modal"); m.classList.remove("open"); m.innerHTML = ""; closeMenu(); }

// ------------------------------------------------------------------ Settings page
function pageSettings(c) {
  liveHooks = [renderNow];
  const sw = (label, key, note, disabled) => h("div", { class: "field switch" }, h("div", {}, h("div", { text: label }), note ? h("div", { class: "muted", style: { fontSize: "12px" }, text: note }) : null),
    h("div", { style: { justifySelf: "end", opacity: disabled ? 0.4 : 1, pointerEvents: disabled ? "none" : "" } }, toggle(() => S.settings[key], v => api.set_setting(key, v), label)));
  c.append(h("div", { class: "stack", style: { maxWidth: "760px" } },
    card("Startup", null, h("div", { class: "fields" },
      sw("Start syncing when WiZard opens", "start_sync_on_launch"),
      sw("Open at Windows sign-in, hidden in the tray", "launch_at_login"),
      sw("Put the lights back the way they were when sync stops", "restore_on_stop"))),
    card("Closing the window", null, h("div", { class: "fields" },
      sw("Keep running in the tray when I close the window", "close_to_tray",
        TRAY ? "Schedules and sleep timers keep working. Right-click the tray icon to exit." : "The tray needs the pystray package. Run  python -m pip install pystray  and restart.", !TRAY))),
    card("Exit", "When WiZard exits, every light switches to the Cozy scene at full brightness.", h("button", { class: "btn", text: "Exit WiZard", onclick: () => api.quit() })),
    card("Settings file", `Saved automatically to ${STATIC.settings_file}`, confirmBtn("Reset all settings", () => api.reset("all"))),
    card("About", "WiZard is an unofficial app. It is not affiliated with, endorsed by or sponsored by Signify, WiZ or Philips.")));
}

// ------------------------------------------------------------------ the detail drawer (one light or one room)
function openSoon(key) {
  clearTimeout(pendingOpen);
  pendingOpen = setTimeout(() => { pendingOpen = null; openDrawer(key); }, 220);
}
function openDrawer(key, tab) {
  drawerKey = key; drawerTab = tab || "Control";
  renderDrawer();
  $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false"); $("#scrim").classList.add("open");
  updaters.forEach(f => f());
}
function closeDrawer() {
  drawerKey = null; drawerUpdaters = [];
  $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); $("#scrim").classList.remove("open");
  updaters.forEach(f => f());
}
function drawerSignature() {
  if (!drawerKey) return "";
  const room = drawerKey.startsWith("room:") ? roomById(drawerKey.slice(5)) : null;
  const light = drawerKey.startsWith("light:") ? lightByIp(drawerKey.slice(6)) : null;
  return [drawerKey, drawerTab, ctlMode, room && room.lights.join(","), room && room.name, light && [light.name, light.room].join("|"),
    S.rooms.map(r => r.id + r.name).join(), S.lights.map(l => l.ip + l.name + l.room).join(),
    drawerTab === "Schedules" ? JSON.stringify(S.schedules.filter(x => x.target === drawerKey)) : ""].join("#");
}

function renderDrawer() {
  drawerUpdaters = []; drawerBuilding = true;
  const d = $("#drawer"); d.innerHTML = "";
  const key = drawerKey, isRoom = key.startsWith("room:");
  const ip = isRoom ? null : key.slice(6);
  const head = h("div", { class: "d-head" });
  const sub = h("div", { class: "tile-sub" });
  if (isRoom) {
    const r = roomById(key.slice(5));
    head.append(h("div", { class: "d-title" }, nameCell(r.name, v => api.rename_room(r.id, v)), sub));
    drawerUpdaters.push(() => { const ls = keyIps(key).map(lightByIp).filter(Boolean); sub.textContent = `${ls.length} ${ls.length === 1 ? "light" : "lights"}  ·  ${ls.filter(isOn).length} on`; });
  } else {
    const l = lightByIp(ip), o = orb(l);
    head.append(o, h("div", { class: "d-title" }, nameCell(l.name, v => api.set_light(ip, "name", v)), sub));
    drawerUpdaters.push(() => { const cur = lightByIp(ip); if (cur) { paintOrb(o, cur); sub.textContent = lookText(cur); } });
  }
  head.append(h("button", { class: "icon-btn", title: "Close", onclick: closeDrawer }, icon("close", 18)));
  const tabs = ["Control", "Scenes", "Schedules", isRoom ? "Lights" : "Settings"];
  const tabBar = h("div", { class: "d-tabs", role: "tablist" }, tabs.map(t => h("button", { class: t === drawerTab ? "on" : null, text: t, onclick: () => { drawerTab = t; renderDrawer(); } })));
  const body = h("div", { class: "d-body" });
  ({ Control: drawerControl, Scenes: drawerScenes, Schedules: drawerSchedules, Settings: drawerLightSettings, Lights: drawerRoomLights })[drawerTab](body, key);
  d.append(head, tabBar, body);
  drawerBuilding = false;
  drawerSig = drawerSignature();
  drawerUpdaters.forEach(f => f());
}

function drawerControl(body, key) {
  const ips = () => keyIps(key);
  const anyOn = () => ips().some(ip => isOn(lightByIp(ip) || {}));
  const powerNote = h("div", { class: "muted" });
  drawerUpdaters.push(() => { const ls = ips().map(lightByIp).filter(Boolean), on = ls.filter(isOn).length;
    powerNote.textContent = ls.length > 1 ? `${on} of ${ls.length} lights on` : (on ? "On" : "Off"); });
  body.append(h("div", { class: "big-power" }, h("div", {}, h("div", { style: { fontWeight: 600 }, text: "Power" }), powerNote),
    toggle(anyOn, on => api.power(key, on), "Power")));
  body.append(h("div", { class: "row", style: { justifyContent: "center", marginBottom: "14px" } },
    seg([["color", "Color"], ["white", "White"]], () => ctlMode, v => { ctlMode = v; renderDrawer(); })));
  if (ctlMode === "color") {
    body.append(colorWheel(key));
    body.append(h("div", { class: "swatches", style: { margin: "10px 0 18px" } }, STATIC.quick_colors.map(col =>
      h("button", { class: "swatch-btn", title: hueName(col), style: { background: rgbCss(col) }, onclick: () => { S.settings.control_color = col; api.set_color(key, col, true); drawerUpdaters.forEach(f => f()); } }))));
  } else {
    const val = h("span", { class: "value" });
    const send = throttle(v => api.set_white(key, v, false), 70);
    const sl = slider({ min: STATIC.kelvin[0], max: STATIC.kelvin[1], step: 100, kelvin: true, label: "White temperature", get: () => S.settings.control_kelvin,
      onInput: v => { val.textContent = v + "K"; send(v); }, onChange: v => api.set_white(key, v, true) });
    drawerUpdaters.push(() => { val.textContent = sl.value + "K"; });
    body.append(h("div", { class: "field", style: { gridTemplateColumns: "90px 1fr 60px", marginBottom: "12px" } }, h("label", { text: "White" }), sl, val));
    body.append(h("div", { class: "swatches", style: { marginBottom: "18px" } }, STATIC.quick_whites.map(k =>
      h("button", { class: "chip", text: `${k}K`, onclick: () => { S.settings.control_kelvin = k; api.set_white(key, k, true); drawerUpdaters.forEach(f => f()); } }))));
  }
  const bval = h("span", { class: "value" });
  const bsend = throttle(v => api.set_brightness(key, v, false), 70);
  const bsl = slider({ min: 10, max: 100, label: "Brightness", get: () => S.settings.control_brightness,
    onInput: v => { bval.textContent = v + "%"; bsend(v); }, onChange: v => api.set_brightness(key, v, true) });
  drawerUpdaters.push(() => { bval.textContent = bsl.value + "%"; });
  body.append(h("div", { class: "field", style: { gridTemplateColumns: "90px 1fr 60px" } }, h("label", { text: "Brightness" }), bsl, bval));
  // sleep timer
  const sleepText = h("div", { class: "muted", style: { marginTop: "8px" } });
  const chips = h("div", { class: "chips" }, [[15, "15 min"], [30, "30 min"], [60, "1 hour"], [120, "2 hours"], [0, "Off"]].map(([m, label]) =>
    h("button", { class: "chip", text: label, onclick: () => api.set_sleep(key, m) })));
  drawerUpdaters.push(() => {
    const dl = S.sleep[key];
    sleepText.textContent = dl ? "Turns off at " + new Date(dl * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "No sleep timer.";
  });
  body.append(h("div", { class: "section-title", style: { marginTop: "22px" } }, h("h2", { text: "Sleep timer" })), chips, sleepText);
}

function colorWheel(key) {
  const size = 220, cv = h("canvas", { width: size * 2, height: size * 2 });
  const knob = h("div", { class: "knob" });
  const wrap = h("div", { class: "wheel" }, cv, knob);
  const g = cv.getContext("2d"), R = size;
  const conic = g.createConicGradient(0, R, R);
  for (let i = 0; i <= 12; i++) conic.addColorStop(i / 12, `hsl(${i * 30}, 100%, 50%)`);
  g.fillStyle = conic; g.beginPath(); g.arc(R, R, R, 0, Math.PI * 2); g.fill();
  const rad = g.createRadialGradient(R, R, 0, R, R, R); rad.addColorStop(0, "rgba(255,255,255,1)"); rad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = rad; g.beginPath(); g.arc(R, R, R, 0, Math.PI * 2); g.fill();
  const place = rgb => {
    const [hh, s] = rgbToHsv(rgb), ang = hh * Math.PI * 2, r = s * (size / 2);
    knob.style.left = size / 2 + Math.cos(ang) * r + "px"; knob.style.top = size / 2 + Math.sin(ang) * r + "px"; knob.style.background = rgbCss(rgb);
  };
  const send = throttle(rgb => api.set_color(key, rgb, false), 70);
  const pick = (e, final) => {
    const b = wrap.getBoundingClientRect(), x = e.clientX - b.left - size / 2, y = e.clientY - b.top - size / 2;
    const s = clamp(Math.hypot(x, y) / (size / 2), 0, 1), hh = ((Math.atan2(y, x) / (Math.PI * 2)) + 1) % 1;
    const rgb = hsvToRgb(hh, s, 1); place(rgb); S.settings.control_color = rgb;
    final ? api.set_color(key, rgb, true) : send(rgb);
  };
  let down = false;
  wrap.addEventListener("pointerdown", e => { down = true; wrap.setPointerCapture(e.pointerId); pick(e, false); });
  wrap.addEventListener("pointermove", e => { if (down) pick(e, false); });
  wrap.addEventListener("pointerup", e => { if (down) { down = false; pick(e, true); } });
  drawerUpdaters.push(() => { if (!down) place(S.settings.control_color); });
  return h("div", { class: "wheel-wrap" }, wrap);
}

function drawerScenes(body, key) {
  body.append(card(null, null, sceneControls()));
  body.append(sceneGrid(() => key, true));
}

function drawerSchedules(body, key) {
  body.append(h("div", { class: "row", style: { marginBottom: "12px" } }, h("div", { class: "muted spacer", text: `Schedules for ${keyName(key)}` }),
    h("button", { class: "btn primary sm", onclick: () => scheduleModal(null, key) }, icon("plus", 14), h("span", { text: "Add" }))));
  const list = S.schedules.filter(x => x.target === key);
  if (!list.length) body.append(h("div", { class: "empty", text: "No schedules here yet." }));
  list.forEach(sc => body.append(scheduleRow(sc, true)));
}

function drawerLightSettings(body, key) {
  const ip = key.slice(6), l = lightByIp(ip);
  const roomChoices = () => [{ value: "", label: "No room" }, ...S.rooms.map(r => ({ value: r.id, label: r.name })), { sep: true }, { value: "__new__", label: "New room" }];
  const scaleVal = h("span", { class: "value" });
  const scaleSend = throttle(v => api.set_light(ip, "brightness_scale", v), 80);
  const scale = slider({ min: 10, max: 100, label: "Brightness limit", get: () => lightByIp(ip)?.scale, onInput: v => { scaleVal.textContent = v + "%"; scaleSend(v); }, onChange: v => api.set_light(ip, "brightness_scale", v) });
  drawerUpdaters.push(() => { scaleVal.textContent = scale.value + "%"; });
  body.append(h("div", { class: "stack" },
    card("Room", null, select(roomChoices, () => lightByIp(ip)?.room || "", v => v === "__new__" ? roomModal([ip]) : api.set_light_room(ip, v || null))),
    card("Sync", "How this light behaves during screen and music sync.", h("div", { class: "fields" },
      h("div", { class: "field two", style: { gridTemplateColumns: "130px 1fr" } }, h("label", { text: "Screen area" }), select(STATIC.zones, () => lightByIp(ip)?.zone, v => api.set_light(ip, "zone", v))),
      h("div", { class: "field", style: { gridTemplateColumns: "130px 1fr 50px" } }, h("label", { text: "Brightness limit" }), scale, scaleVal),
      h("div", { class: "field two", style: { gridTemplateColumns: "1fr auto" } }, h("label", { text: "Include in screen and music sync" }), toggle(() => lightByIp(ip)?.enabled, v => api.set_light(ip, "enabled", v))))),
    card("This light", `IP address ${l.ip}`, h("div", { class: "row" },
      h("button", { class: "btn", text: "Identify", title: "Flashes the light soft yellow", onclick: () => api.identify(ip) }),
      confirmBtn("Remove light", () => { closeDrawer(); api.remove_light(ip); })))));
}

function drawerRoomLights(body, key) {
  const rid = key.slice(5), r = roomById(rid);
  const list = h("div", { class: "check-list" }, S.lights.map(l => {
    const cb = h("input", { type: "checkbox" }); cb.checked = r.lights.includes(l.ip);
    cb.onchange = () => { const cur = new Set(roomById(rid).lights); cb.checked ? cur.add(l.ip) : cur.delete(l.ip); api.save_room(rid, roomById(rid).name, [...cur]); };
    const other = l.room && l.room !== rid ? roomById(l.room)?.name : null;
    return h("label", { class: "check" }, cb, orb(l, "orb sm"), h("span", { text: l.name }), h("span", { class: "spacer" }), other ? h("span", { class: "faint", text: `In ${other}` }) : null);
  }));
  body.append(card("Lights in this room", "A light can be in one room. Checking a light here moves it from its other room.", list));
  body.append(h("div", { style: { marginTop: "14px" } }, confirmBtn("Delete room", () => { closeDrawer(); api.delete_room(rid); })));
}

// ------------------------------------------------------------------ start
async function start() {
  await bridge.ready;
  const init = await api.init();
  STATIC = init.static; MONITORS = init.monitors; TRAY = init.tray;
  buildChrome();
  applyState(init.state);
}
start();
})();
