"use strict";

const $ = (id) => document.getElementById(id);
let lastLog = 0;
let results = [];
let lastPreviewT = 0;
let cfg = {};
let site = { lat: null, lon: null };

// ------------------------------------------------------------------ API
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const d = (await r.json()).detail;
      msg = typeof d === "string" ? d : JSON.stringify(d);
    } catch (_) { /* sin cuerpo */ }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

function logLocal(level, msg) {
  addLog({ t: new Date().toTimeString().slice(0, 8), level, msg });
}

function addLog(l) {
  const div = document.createElement("div");
  div.className = l.level;
  div.textContent = `${l.t}  ${l.msg}`;
  $("log").prepend(div);
  while ($("log").childElementCount > 300) $("log").lastChild.remove();
}

async function act(fn) {
  try { await fn(); } catch (e) { logLocal("error", e.message); }
}

// ------------------------------------------------------------- formatos
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "–" : Number(v).toFixed(d);

function hms(deg) {
  if (deg === null || deg === undefined) return "–";
  const h = ((deg / 15) % 24 + 24) % 24;
  const hh = Math.floor(h); const m = (h - hh) * 60; const mm = Math.floor(m); const s = (m - mm) * 60;
  return `${String(hh).padStart(2, "0")}h${String(mm).padStart(2, "0")}m${s.toFixed(1).padStart(4, "0")}s`;
}

function dms(deg) {
  if (deg === null || deg === undefined) return "–";
  const sg = deg < 0 ? "-" : "+"; const a = Math.abs(deg);
  const d = Math.floor(a); const m = (a - d) * 60; const mm = Math.floor(m); const s = (m - mm) * 60;
  return `${sg}${String(d).padStart(2, "0")}°${String(mm).padStart(2, "0")}'${s.toFixed(0).padStart(2, "0")}"`;
}

// Altura aproximada en el navegador (para mostrar; el servidor usa la suya al medir)
function altitude(raDeg, decDeg, lat, lon) {
  if ([raDeg, decDeg, lat, lon].some((v) => v === null || v === undefined)) return null;
  const rad = Math.PI / 180;
  const jd = Date.now() / 86400000 + 2440587.5;
  const lst = (280.46061837 + 360.98564736629 * (jd - 2451545) + lon) % 360;
  const ha = (lst - raDeg) * rad;
  const s = Math.sin(lat * rad) * Math.sin(decDeg * rad) + Math.cos(lat * rad) * Math.cos(decDeg * rad) * Math.cos(ha);
  return Math.asin(Math.max(-1, Math.min(1, s))) / rad;
}

// --------------------------------------------------------------- config
function fillForm() {
  for (const el of $("cfg-form").elements) {
    if (!(el.name in cfg)) continue;
    if (el.type === "checkbox") el.checked = !!cfg[el.name];
    else el.value = cfg[el.name] ?? "";
  }
}

async function loadConfig() {
  cfg = await api("/api/config");
  fillForm();
}

async function saveConfig() {
  const data = {};
  for (const el of $("cfg-form").elements) {
    if (!el.name) continue;
    data[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  const oldPhys = cfg.physical_pixscale;
  cfg = await api("/api/config", data);
  logLocal("ok", "Ajustes guardados");
  if (cfg.pixscale_override_arcsec > 0 && Math.abs(cfg.physical_pixscale - oldPhys) > 1e-4) {
    logLocal("warn", "Cambió la óptica pero hay una escala calibrada: recalibre o pulse 'Usar escala teórica'.");
  }
  await loadStars();  // lat/lon pudo cambiar
}

// --------------------------------------------------------------- visor
const view = $("view");
const vctx = view.getContext("2d");
const img = new Image();

img.onload = () => {
  const m = img._meta;
  if (view.width !== m.w || view.height !== m.h) { view.width = m.w; view.height = m.h; }
  vctx.drawImage(img, 0, 0);
  const r = Math.max(6, m.w / 60);
  vctx.lineWidth = Math.max(1, m.w / 400);
  vctx.strokeStyle = m.valid ? "#3fb950" : "#d29922";
  for (const s of m.spots) {
    vctx.beginPath(); vctx.arc(s.x, s.y, r, 0, 2 * Math.PI); vctx.stroke();
  }
  if (m.spots.length >= 2) {
    vctx.setLineDash([4, 4]);
    vctx.beginPath(); vctx.moveTo(m.spots[0].x, m.spots[0].y); vctx.lineTo(m.spots[1].x, m.spots[1].y); vctx.stroke();
    vctx.setLineDash([]);
  }
};

function updatePreview(meta) {
  if (!meta || !meta.t || meta.t === lastPreviewT) return;
  lastPreviewT = meta.t;
  img._meta = meta;
  img.src = `/api/preview.jpg?t=${meta.t}`;
  const st = $("frame-status");
  st.textContent = `${meta.valid ? "✓ 2 imágenes detectadas" : "⚠ " + meta.reason}  ·  máx ${meta.max}  fondo ${meta.bg}`;
  st.style.color = meta.valid ? "var(--ok)" : "var(--warn)";
}

// --------------------------------------------------------------- gráfica
function drawChart() {
  const c = $("chart");
  const dpr = window.devicePixelRatio || 1;
  const W = c.clientWidth; const H = 220;
  c.width = W * dpr; c.height = H * dpr;
  const g = c.getContext("2d");
  g.scale(dpr, dpr);
  const css = getComputedStyle(document.body);
  const colText = css.getPropertyValue("--muted").trim();
  const colLine = css.getPropertyValue("--accent").trim();
  const colZen = css.getPropertyValue("--ok").trim();
  const colGrid = css.getPropertyValue("--border").trim();
  g.font = "11px system-ui, sans-serif";

  const pad = { l: 36, r: 12, t: 22, b: 24 };
  const w = W - pad.l - pad.r; const h = H - pad.t - pad.b;
  if (!results.length) {
    g.fillStyle = colText; g.fillText("Sin mediciones todavía", pad.l + 8, pad.t + 20);
    return;
  }
  const ts = results.map((r) => Date.parse(r.time_utc));
  const t0 = Math.min(...ts); const t1 = Math.max(...ts, t0 + 10 * 60000);
  const vals = results.flatMap((r) => [r.seeing_arcsec, r.seeing_zenith].filter((v) => v != null));
  const ymax = Math.max(1, Math.ceil(Math.max(...vals) * 1.15 * 2) / 2);
  const X = (t) => pad.l + (t - t0) / (t1 - t0) * w;
  const Y = (v) => pad.t + h - v / ymax * h;

  g.strokeStyle = colGrid; g.fillStyle = colText; g.lineWidth = 1;
  for (let v = 0; v <= ymax + 1e-9; v += ymax > 4 ? 1 : 0.5) {
    g.beginPath(); g.moveTo(pad.l, Y(v)); g.lineTo(W - pad.r, Y(v)); g.stroke();
    g.fillText(`${v}"`, 4, Y(v) + 4);
  }
  for (let i = 0; i <= 4; i++) {
    const t = t0 + (t1 - t0) * i / 4;
    const lbl = new Date(t).toTimeString().slice(0, 5);
    g.fillText(lbl, Math.min(X(t) - 14, W - 34), H - 6);
  }

  const series = (key, color) => {
    const pts = results.map((r, i) => [ts[i], r[key]]).filter((p) => p[1] != null);
    if (!pts.length) return;
    g.strokeStyle = color; g.fillStyle = color; g.lineWidth = 2;
    g.beginPath();
    pts.forEach(([t, v], i) => (i ? g.lineTo(X(t), Y(v)) : g.moveTo(X(t), Y(v))));
    g.stroke();
    for (const [t, v] of pts) { g.beginPath(); g.arc(X(t), Y(v), 3, 0, 2 * Math.PI); g.fill(); }
  };
  series("seeing_arcsec", colLine);
  series("seeing_zenith", colZen);

  g.fillStyle = colLine; g.fillRect(W - 170, 8, 10, 3);
  g.fillStyle = colText; g.fillText("línea de vista", W - 156, 13);
  g.fillStyle = colZen; g.fillRect(W - 80, 8, 10, 3);
  g.fillStyle = colText; g.fillText("cenit", W - 66, 13);
}

// --------------------------------------------------------------- estado
function render(s) {
  const pill = $("status-pill");
  const modeTxt = {
    idle: "En espera", live: "En vivo", calibrate: "Calibrando escala",
    measure: s.continuous ? "Midiendo (continuo)" : "Midiendo",
  };
  pill.textContent = s.camera ? modeTxt[s.mode] : "Sin cámara";
  pill.className = "pill " + (s.camera ? s.mode : "");

  $("cam-info").textContent = s.camera
    ? `${s.camera.name} · ${s.camera.width}×${s.camera.height}${s.camera.note ? " · " + s.camera.note : ""}` : "";
  $("fps").textContent = s.camera && s.mode !== "idle" ? `${s.fps} fps` : "";

  // progreso de medición / calibración
  const p = s.progress;
  const busy = s.mode === "measure" || s.mode === "calibrate";
  let pct = 0; let txt = "";
  if (s.mode === "measure") {
    pct = p.target ? p.valid / p.target * 100 : 0;
    txt = p.waiting && !p.valid ? "esperando la estrella…" : `${p.valid}/${p.target} frames`;
  } else if (s.mode === "calibrate" && s.calibrating) {
    pct = s.calibrating.elapsed / s.calibrating.duration * 100;
    txt = `calibrando ${fmt(s.calibrating.elapsed, 0)}/${s.calibrating.duration} s`;
  }
  $("progress-bar").style.width = `${Math.min(100, pct)}%`;
  $("progress-txt").textContent = txt;
  $("btn-measure-one").disabled = !s.camera || busy;
  $("btn-measure-cont").disabled = !s.camera || busy;
  $("btn-measure-stop").disabled = !busy;
  $("btn-calib").disabled = !s.camera || busy;
  $("btn-solve").disabled = !s.camera || s.solving;
  $("btn-solve").textContent = s.solving ? "Resolviendo…" : "Resolver placa";

  // escala
  $("scale-used").textContent = fmt(s.pixscale.used, 3);
  $("scale-phys").textContent = fmt(s.pixscale.physical, 3);
  const badge = $("scale-badge");
  badge.textContent = s.pixscale.calibrated ? "calibrada" : "teórica";
  badge.className = "badge" + (s.pixscale.calibrated ? " ok" : "");
  $("btn-calib-reset").disabled = !s.pixscale.calibrated;

  updatePreview(s.preview);

  for (const l of s.logs) { addLog(l); lastLog = Math.max(lastLog, l.seq); }
  if (s.results.length) {
    results = results.concat(s.results);
    drawChart();
  }
  const last = results[results.length - 1];
  if (last) {
    $("seeing-now").textContent = `${fmt(last.seeing_arcsec)}"`;
    $("seeing-zen").textContent = `cenit: ${last.seeing_zenith != null ? fmt(last.seeing_zenith) + '"' : "– (elija estrella y sitio)"}`;
    $("r0").textContent = `${fmt(last.r0_cm, 1)} cm`;
    $("sigmas").textContent = `${fmt(last.sigma_l_arcsec, 3)}" / ${fmt(last.sigma_t_arcsec, 3)}"`;
    $("airmass").textContent = fmt(last.airmass, 2);
    $("nframes").textContent = `${last.n_frames}`;
  }
  $("csv-path").textContent = s.csv_path ? `Guardado en: ${s.csv_path}` : "";

  // objeto apuntado
  site = s.site;
  const tel = s.telescope;
  const c = s.coords.source ? s.coords
    : { source: tel.connected ? "LX200" : null, ra_deg: tel.ra_deg, dec_deg: tel.dec_deg };
  $("coord-src").textContent = c.source === "catalog" ? `★ ${c.name}`
    : ({ platesolve: "Plate solve", manual: "Manual" }[c.source] || c.source || "– (elija estrella)");
  $("ra").textContent = hms(c.ra_deg);
  $("dec").textContent = dms(c.dec_deg);
  const alt = altitude(c.ra_deg, c.dec_deg, site.lat, site.lon) ?? tel.alt_deg;
  $("alt").textContent = alt == null ? "– (falta sitio)" : `${fmt(alt, 1)}°`;
}

async function poll() {
  try {
    const s = await api(`/api/state?since_log=${lastLog}&since_result=${results.length}`);
    render(s);
  } catch (_) {
    $("status-pill").textContent = "App cerrada: vuelva a abrir DIMM-Seeing";
    $("status-pill").className = "pill";
  }
  setTimeout(poll, 400);
}

// ------------------------------------------------------------ listas
async function loadPorts() {
  const ports = await api("/api/serial-ports");
  const sel = $("serial-port");
  sel.innerHTML = "";
  if (!ports.length) sel.innerHTML = "<option value=''>(no hay puertos)</option>";
  for (const p of ports) {
    const o = document.createElement("option");
    o.value = p.device; o.textContent = `${p.device} — ${p.description}`;
    sel.appendChild(o);
  }
}

async function loadWebcams() {
  const sel = $("webcam-select");
  sel.innerHTML = "<option>Buscando…</option>";
  const cams = await api("/api/webcams");
  sel.innerHTML = "";
  if (!cams.length) {
    // puede estar en uso por esta misma app: dejar el índice guardado
    const o = document.createElement("option");
    o.value = cfg.camera_index ?? 0; o.textContent = `Cámara #${o.value} (no detectada / en uso)`;
    sel.appendChild(o);
  }
  for (const c of cams) {
    const o = document.createElement("option");
    o.value = c.index;
    o.textContent = `#${c.index} ${c.name} (${c.width}×${c.height})${c.in_use ? " · conectada" : ""}`;
    sel.appendChild(o);
  }
  sel.value = String(cfg.camera_index ?? 0);
  if (!sel.value && sel.options.length) sel.selectedIndex = 0;
}

async function loadStars() {
  const stars = await api("/api/stars");
  const sel = $("star-select");
  const prev = sel.value;
  sel.innerHTML = "";
  if (!stars.length) sel.innerHTML = "<option value=''>(ninguna sobre el horizonte)</option>";
  for (const s of stars) {
    const o = document.createElement("option");
    o.value = s.name;
    o.textContent = s.alt_deg == null
      ? `${s.name} (mag ${s.mag})`
      : `${s.name} · alt ${s.alt_deg}° · mag ${s.mag}`;
    sel.appendChild(o);
  }
  if (prev) sel.value = prev;
  const noSite = cfg.site_lat_deg == null || cfg.site_lon_deg == null;
  $("star-hint").textContent = noSite
    ? "⚠ Ponga latitud y longitud en Ajustes para ordenar por altura y corregir al cenit."
    : "Ordenadas por altura ahora. Mejor > 45° y magnitud < 2.";
  $("star-hint").style.color = noSite ? "var(--warn)" : "";
}

function onBackendChange() {
  const b = $("cam-backend").value;
  $("webcam-row").classList.toggle("hidden", b !== "webcam");
  $("zwo-row").classList.toggle("hidden", b !== "zwo");
  $("ascom-row").classList.toggle("hidden", b !== "ascom");
}

// --------------------------------------------------------------- inicio
async function init() {
  await act(loadConfig);
  const info = await api("/api/info");
  const sel = $("cam-backend");
  for (const [k, v] of Object.entries(info.backends)) {
    const o = document.createElement("option"); o.value = k; o.textContent = v; sel.appendChild(o);
  }
  sel.value = cfg.camera_backend || "webcam";
  $("cam-progid").value = cfg.ascom_prog_id || "";
  $("cam-index").value = cfg.camera_index || 0;
  onBackendChange();
  sel.onchange = onBackendChange;

  let night = false;
  try { night = localStorage.getItem("night") === "1"; } catch (_) { /* ok */ }
  $("night").checked = night;
  document.body.classList.toggle("night", night);
  $("night").onchange = (e) => {
    document.body.classList.toggle("night", e.target.checked);
    try { localStorage.setItem("night", e.target.checked ? "1" : "0"); } catch (_) { /* ok */ }
    drawChart();
  };

  $("btn-webcams").onclick = () => act(loadWebcams);
  $("btn-ascom-choose").onclick = () => act(async () => {
    const r = await api("/api/camera/ascom-choose", {});
    $("cam-progid").value = r.prog_id;
  });
  $("btn-cam-connect").onclick = () => act(async () => {
    const b = sel.value;
    const index = b === "webcam" ? Number($("webcam-select").value) : Number($("cam-index").value);
    await api("/api/camera/connect", { backend: b, prog_id: $("cam-progid").value || null, index });
    cfg = await api("/api/config");
  });
  $("btn-cam-disconnect").onclick = () => act(() => api("/api/camera/disconnect", {}));

  $("btn-stars").onclick = () => act(loadStars);
  $("btn-star").onclick = () => act(() => api("/api/coords/star", { name: $("star-select").value }));
  $("btn-man").onclick = () => act(() => api("/api/coords/manual", { ra: $("man-ra").value, dec: $("man-dec").value }));
  $("btn-solve").onclick = () => act(() => api("/api/platesolve", {}));

  $("btn-calib").onclick = () => act(() => api("/api/calibrate/start", { duration_s: 15 }));
  $("btn-calib-reset").onclick = () => act(async () => { cfg = await api("/api/calibrate/reset", {}); });

  $("btn-measure-one").onclick = () => act(() => api("/api/measure/start", { continuous: false }));
  $("btn-measure-cont").onclick = () => act(() => api("/api/measure/start", { continuous: true }));
  $("btn-measure-stop").onclick = () => act(() => api("/api/measure/stop", {}));

  $("btn-cfg-save").onclick = () => act(saveConfig);
  $("cfg-form").onsubmit = (e) => { e.preventDefault(); act(saveConfig); };

  $("btn-ports").onclick = () => act(loadPorts);
  $("btn-tel-connect").onclick = () => act(async () => {
    await api("/api/telescope/connect", { port: $("serial-port").value });
    await loadConfig();  // el GPS puede haber llenado lat/lon
    await loadStars();
  });
  $("btn-tel-disconnect").onclick = () => act(() => api("/api/telescope/disconnect", {}));

  window.addEventListener("resize", drawChart);
  await act(loadStars);
  setInterval(() => act(loadStars), 5 * 60 * 1000);  // las alturas cambian con la noche
  drawChart();
  poll();
  if (sel.value === "webcam") act(loadWebcams);
  sel.addEventListener("change", () => { if (sel.value === "webcam") act(loadWebcams); });
  act(loadPorts);
}

init();
