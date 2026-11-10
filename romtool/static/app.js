/* ROMtool canvas UI (vanilla JS, no build step). */
"use strict";

const $ = (id) => document.getElementById(id);
const cv = $("cv");
const ctx = cv.getContext("2d");

const S = {
  cfg: null, projects: [], pid: null, project: null,
  floorId: null, band: null, solve: null, predict: null, bom: null,
  tool: "select", elTool: null, mat: "drywall",
  wallDraft: null, cableFrom: null, polyDraft: null, selCa: null,
  sel: null, selWall: null,
  drag: null, hoverPt: null, calib: null, imgCache: {}, zoom: 6,
};

function toast(msg, err) {
  const t = $("toast");
  t.textContent = msg; t.className = "show" + (err ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => (t.className = ""), 3500);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) {
    let msg;
    if (body && body.detail) {
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } else { msg = res.status + " " + res.statusText; }
    throw new Error(msg);
  }
  return body;
}

const P = () => "/projects/" + S.pid;

/* ---------------- color scale (matches engine piecewise-linear) ---------- */
function colorForDbm(v) {
  const src = (S.cfg && S.cfg.report && S.cfg.report.color_scale) || [];
  const stops = src.slice().sort((a, b) => a.dbm - b.dbm);
  if (!stops.length) return "#404040";
  const at = (i) => stops[i];
  if (v == null) v = at(0).dbm - 1;
  if (v <= at(0).dbm) return rgb(at(0).rgb);
  if (v >= at(stops.length - 1).dbm) return rgb(at(stops.length - 1).rgb);
  for (let i = 0; i < stops.length - 1; i++) {
    const a = stops[i], b = stops[i + 1];
    if (v >= a.dbm && v <= b.dbm) {
      const t = (v - a.dbm) / (b.dbm - a.dbm);
      return rgb([0, 1, 2].map((k) => Math.round(a.rgb[k] + (b.rgb[k] - a.rgb[k]) * t)));
    }
  }
  return rgb(at(stops.length - 1).rgb);
}
const rgb = (a) => "rgb(" + a[0] + "," + a[1] + "," + a[2] + ")";

/* ---------------- coordinate transforms --------------------------------- */
function floor() { return S.project && S.project.model.floors.find((f) => f.id === S.floorId); }
function layout() {
  const f = floor();
  const w = f ? f.w_px : 200, h = f ? f.h_px : 150;
  S.zoom = Math.max(0.5, Math.min(14, Math.min((cv.width - 60) / w, (cv.height - 60) / h)));
  return { w, h, ox: (cv.width - w * S.zoom) / 2, oy: (cv.height - h * S.zoom) / 2 };
}
const toScr = (x, y) => { const L = layout(); return [L.ox + x * S.zoom, L.oy + y * S.zoom]; };
const toModel = (sx, sy) => { const L = layout(); return [(sx - L.ox) / S.zoom, (sy - L.oy) / S.zoom]; };

/* ---------------- rendering ---------------------------------------------- */
function allElements() {
  const out = [];
  if (!S.project) return out;
  for (const fl of S.project.model.floors)
    for (const el of fl.elements) out.push({ fl, el });
  return out;
}
function elById(id) {
  for (const { fl, el } of allElements()) if (el.id === id) return { fl, el };
  return null;
}

function drawIcon(el, scr) {
  const [x, y] = scr; const r = Math.max(5, S.zoom * 1.2);
  ctx.lineWidth = 1.5;
  const col = { source: "#e6b34d", repeater: "#5a8dd6", splitter: "#b085d0",
                tapper: "#d085a8", coupler: "#d0a085", antenna: "#e05555",
                cable_run: "#6f7d8c" }[el.type] || "#aaa";
  ctx.strokeStyle = col; ctx.fillStyle = "#14171c";
  if (el.type === "antenna") {
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.arc(x, y, r + 3, 0, Math.PI * 2); ctx.stroke();
  } else if (el.type === "cable_run") {
    ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI * 2); ctx.stroke();
  } else {
    ctx.beginPath(); ctx.rect(x - r, y - r, 2 * r, 2 * r); ctx.fill(); ctx.stroke();
    ctx.fillStyle = col; ctx.font = "bold " + Math.max(7, r) + "px monospace";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(el.type[0].toUpperCase(), x, y + 0.5);
  }
  if (el.label) {
    ctx.fillStyle = "#dfe6ef"; ctx.font = "10px system-ui"; ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText(el.label, x + r + 3, y - r - 1);
  }
}

function render() {
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = "#101318"; ctx.fillRect(0, 0, cv.width, cv.height);
  const f = floor();
  if (!f) return;
  const L = layout();

  // floor plate
  const [fx, fy] = toScr(0, 0);
  const fw = f.w_px * S.zoom, fh = f.h_px * S.zoom;
  ctx.fillStyle = "#1b2029"; ctx.fillRect(fx, fy, fw, fh);

  // image
  const img = f.image && f.image.url;
  if (img) {
    let im = S.imgCache[f.id];
    if (!im || im._url !== img) {
      im = new Image(); im.src = img; im._url = img; S.imgCache[f.id] = im;
      im.onload = () => render();
    }
    if (im.complete) {
      ctx.imageSmoothingEnabled = false;
      ctx.globalAlpha = 0.85;
      ctx.drawImage(im, fx, fy, fw, fh);
      ctx.globalAlpha = 1;
    }
  }

  // heatmap overlay
  if (S.predict) drawHeatmap();

  // critical-area polygons (magenta, fill + outline; current floor solid)
  for (const fl of S.project.model.floors) {
    if (fl.id !== S.floorId) continue;
    for (const ca of fl.critical_areas || []) {
      const pts = (ca.points || []).map((p) => toScr(p[0], p[1]));
      if (pts.length < 3) continue;
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (const [px, py] of pts.slice(1)) ctx.lineTo(px, py);
      ctx.closePath();
      ctx.fillStyle = "rgba(200, 40, 200, 0.14)";
      ctx.strokeStyle = S.selCa === ca.id ? "#ffffff" : "#c828c8";
      ctx.lineWidth = 2;
      ctx.fill(); ctx.stroke();
      if (ca.label) {
        ctx.fillStyle = "#e060e0"; ctx.font = "10px system-ui"; ctx.textAlign = "center";
        ctx.fillText(ca.label, pts.reduce((s, p) => s + p[0], 0) / pts.length,
                     pts.reduce((s, p) => s + p[1], 0) / pts.length);
      }
    }
  }

  // walls
  const mats = (S.cfg && S.cfg.wall_materials) || {};
  for (const w of f.walls) {
    const c = (mats[w.material] && mats[w.material].color) || "#888";
    const [x1, y1] = toScr(w.x1, w.y1), [x2, y2] = toScr(w.x2, w.y2);
    ctx.strokeStyle = c; ctx.lineWidth = (w.material === "concrete" ? 4 : 2.5) * Math.min(1, S.zoom / 4 + 0.6);
    if (S.selWall === w.id) { ctx.strokeStyle = "#fff"; }
    if (S.tool === "wall" && w.material === S.mat && S.hoverPt) ctx.globalAlpha = 0.95;
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // cable runs (all floors' runs drawn here; endpoints snap by id)
  ctx.setLineDash([4, 3]);
  for (const { fl, el } of allElements()) {
    if (el.type !== "cable_run") continue;
    const a = elById(el.from_id), b = elById(el.to_id);
    if (!a || !b) continue;
    const [x1, y1] = toScr(a.el.x, a.el.y), [x2, y2] = toScr(b.el.x, b.el.y);
    if (a.fl.id === S.floorId || b.fl.id === S.floorId) {
      ctx.strokeStyle = (fl.id === S.floorId) ? "#7d8b99" : "#4a5560";
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    }
  }
  ctx.setLineDash([]);

  // ghost polygon (draft in progress)
  if (S.polyDraft && S.hoverPt) {
    const pts = S.polyDraft.map((p) => toScr(p[0], p[1]))
      .concat([toScr(S.hoverPt[0], S.hoverPt[1])]);
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (const [px, py] of pts.slice(1)) ctx.lineTo(px, py);
    ctx.strokeStyle = "#e060e0"; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
    ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = "#e060e0";
    for (const [px, py] of pts) ctx.fillRect(px - 2, py - 2, 4, 4);
  }

  // ghost wall
  if (S.wallDraft && S.hoverPt) {
    const [x1, y1] = toScr(S.wallDraft[0], S.wallDraft[1]);
    const [x2, y2] = toScr(S.hoverPt[0], S.hoverPt[1]);
    ctx.strokeStyle = (mats[S.mat] || {}).color || "#fff";
    ctx.lineWidth = 2.5; ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
  }
  // ghost cable
  if (S.cableFrom && S.hoverPt) {
    const src = elById(S.cableFrom);
    if (src) {
      const [x1, y1] = toScr(src.el.x, src.el.y);
      const [x2, y2] = toScr(S.hoverPt[0], S.hoverPt[1]);
      ctx.strokeStyle = "#cfe0ff"; ctx.lineWidth = 1.5;
      ctx.setLineDash([2, 2]); ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // elements: this floor solid, other floors faint
  for (const { fl, el } of allElements()) {
    if (el.type === "cable_run") continue;
    const thisFloor = fl.id === S.floorId;
    if (!thisFloor) continue;
    const [x, y] = toScr(el.x, el.y);
    drawIcon(el, [x, y]);
    if (S.sel === el.id) {
      ctx.strokeStyle = "#4aa3ff"; ctx.lineWidth = 1.5;
      ctx.strokeRect(x - 9, y - 9, 18, 18);
    }
  }
  // other-floor antennas as faint
  for (const { fl, el } of allElements()) {
    if (fl.id === S.floorId || el.type !== "antenna") continue;
    const [x, y] = toScr(el.x, el.y);
    ctx.globalAlpha = 0.35; drawIcon(el, [x, y]); ctx.globalAlpha = 1;
  }

  // scale bar (1 m)
  ctx.strokeStyle = "#8b95a3"; ctx.fillStyle = "#8b95a3"; ctx.lineWidth = 1;
  const bx = fx, by = fy + fh + 12;
  const pMeter = (f.ppm || 10) * S.zoom;
  ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(bx + pMeter, by); ctx.stroke();
  ctx.font = "10px system-ui"; ctx.textAlign = "left";
  ctx.fillText("1 m  (" + (f.ppm || 10) + " ppm, zoom " + S.zoom.toFixed(1) + "x)", bx + pMeter + 6, by + 3);
}

function drawHeatmap() {
  const pr = S.predict; const L = layout();
  const cols = pr.grid.cols, rows = pr.grid.rows, step = pr.grid.step_px;
  const off = document.createElement("canvas");
  off.width = cols; off.height = rows;
  const octx = off.getContext("2d");
  const im = octx.createImageData(cols, rows);
  for (let i = 0; i < pr.values_dbm.length; i++) {
    const v = pr.values_dbm[i];
    const color = colorHexParts(colorForDbm(v));
    im.data[i * 4] = color[0]; im.data[i * 4 + 1] = color[1]; im.data[i * 4 + 2] = color[2];
    im.data[i * 4 + 3] = v == null ? 0 : 150;
  }
  octx.putImageData(im, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, L.ox, L.oy, cols * step * S.zoom, rows * step * S.zoom);
}
function colorHexParts(css) { // "rgb(r,g,b)" -> [r,g,b]
  const m = css.match(/\d+/g); return m ? [+m[0], +m[1], +m[2]] : [80, 80, 80];
}

/* ---------------- interaction ------------------------------------------- */
function canvasPos(ev) {
  const r = cv.getBoundingClientRect();
  const scaleX = cv.width / r.width, scaleY = cv.height / r.height;
  const [mx, my] = toModel((ev.clientX - r.left) * scaleX, (ev.clientY - r.top) * scaleY);
  return [mx, my];
}
function nearestElement(mx, my, onlyChainNodes) {
  let best = null, bestD = Infinity;
  for (const { fl, el } of allElements()) {
    if (el.type === "cable_run") continue;
    if (onlyChainNodes && el.type === "antenna") continue;
    if (fl.id !== S.floorId) continue;
    const d = Math.hypot(el.x - mx, el.y - my);
    if (d < bestD) { bestD = d; best = { fl, el }; }
  }
  const f = floor();
  return best && bestD < 14 / S.zoom * Math.min(f.ppm || 10, 12) ? best : null;
}

cv.addEventListener("mousemove", (ev) => {
  const [mx, my] = canvasPos(ev);
  S.hoverPt = [mx, my];
  if (S.drag) {
    S.drag.el.x = mx; S.drag.el.y = my; render(); return;
  }
  if (S.tool === "wall" || S.tool === "cable" || S.tool === "poly") render();
});
cv.addEventListener("mousedown", (ev) => {
  const [mx, my] = canvasPos(ev);
  if (S.tool === "wall") { S.wallDraft = [mx, my]; return; }
  if (S.tool === "select") {
    const hit = nearestElement(mx, my, false);
    if (hit) { S.drag = hit; S.sel = hit.el.id; S.selWall = null; inspect(hit.el); render(); }
  }
});
cv.addEventListener("mouseup", async (ev) => {
  const [mx, my] = canvasPos(ev);
  if (S.tool === "wall" && S.wallDraft) {
    const [x1, y1] = S.wallDraft; S.wallDraft = null;
    if (Math.hypot(x1 - mx, y1 - my) > 2 / S.zoom) {
      await addWall(x1, y1, mx, my);
    }
    render(); return;
  }
  if (S.drag) {
    const d = S.drag; S.drag = null;
    try {
      await api(P() + "/elements/" + d.el.id, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x: Math.round(d.el.x), y: Math.round(d.el.y) }),
      });
      toast("Moved " + d.el.label);
    } catch (e) { toast(e.message, true); }
    render();
  }
});
cv.addEventListener("click", async (ev) => {
  const [mx, my] = canvasPos(ev);
  if (S.calib) {
    S.calib.push([mx, my]);
    if (S.calib.length === 2) {
      const [a, b] = S.calib; S.calib = null;
      const px = Math.hypot(b[0] - a[0], b[1] - a[1]) / S.zoom;
      const meters = parseFloat(prompt("Real-world distance between the two points (meters):", "5"));
      if (!meters || meters <= 0) { toast("calibration cancelled"); return; }
      const ppm = px / meters;
      try {
        await api(P() + "/floors/" + S.floorId, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ppm: ppm.toFixed(3) }),
        });
        const f = floor(); f.ppm = ppm; $("inpPpm").value = ppm.toFixed(2);
        toast("Scale set: " + ppm.toFixed(2) + " px/m");
      } catch (e) { toast(e.message, true); }
      render();
    } else toast("Now click the second point");
    return;
  }
  if (S.tool === "poly") {
    if (!S.polyDraft) S.polyDraft = [];
    if (S.polyDraft.length >= 3) {
      const [x0, y0] = S.polyDraft[0];
      if (Math.hypot(x0 - mx, y0 - my) < 8 / S.zoom) { await commitPoly(); return; }
    }
    S.polyDraft.push([Math.round(mx), Math.round(my)]);
    $("btnFinishPoly").hidden = S.polyDraft.length < 3;
    render();
    return;
  }
  if (S.elTool) { await placeElement(S.elTool, mx, my); return; }
  if (S.tool === "cable") {
    const hit = nearestElement(mx, my, false);
    if (!hit) { S.cableFrom = null; toast("Cable: click a chain element (source/repeater/splitter/tap/coupler)"); return; }
    if (!S.cableFrom) {
      S.cableFrom = hit.el.id;
      toast("Cable from " + hit.el.label + ": now click the receiving element");
      render(); return;
    }
    if (S.cableFrom === hit.el.id) { S.cableFrom = null; return; }
    await addCable(S.cableFrom, hit.el.id); S.cableFrom = null;
    return;
  }
  if (S.tool === "select") {
    const hit = nearestElement(mx, my, false);
    if (hit) { S.sel = hit.el.id; S.selWall = null; inspect(hit.el); render(); return; }
    const wl = nearestWall(mx, my);
    if (wl) { S.selWall = wl.id; S.sel = null; inspectWall(wl); render(); return; }
    S.sel = null; S.selWall = null; inspect(null); render();
  }
});
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    S.elTool = null; S.cableFrom = null; S.wallDraft = null;
    S.calib = null; S.polyDraft = null; S.selCa = null;
    const fp = $("btnFinishPoly"); if (fp) fp.hidden = true;
    render();
  }
  if (e.key === "Delete" && S.sel) deleteElement(S.sel);
});

function nearestWall(mx, my) {
  const f = floor(); if (!f) return null;
  let best = null, bestD = Infinity;
  for (const w of f.walls) {
    const dx = w.x2 - w.x1, dy = w.y2 - w.y1;
    const len2 = dx * dx + dy * dy || 1;
    let t = ((mx - w.x1) * dx + (my - w.y1) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    const d = Math.hypot(w.x1 + t * dx - mx, w.y1 + t * dy - my);
    if (d < bestD) { bestD = d; best = w; }
  }
  return bestD < 8 / S.zoom ? best : null;
}

/* ---------------- palette actions -------------------------------------- */
document.querySelectorAll(".tool[data-tool]").forEach((b) => {
  b.onclick = () => {
    S.tool = b.dataset.tool; S.elTool = null; S.cableFrom = null;
    document.querySelectorAll(".tool").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    toast(S.tool === "wall" ? "Drag on canvas to draw a wall (" + S.mat + ")" :
          S.tool === "cable" ? "Click chain element, then receiving element" :
          S.tool === "poly" ? "Click polygon corners; click near the first corner (or Finish) to close" :
          "Select tool");
    render();
  };
});
document.querySelectorAll(".tool[data-el]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".tool").forEach((x) => x.classList.remove("active"));
    b.classList.add("active"); b._prev = true;
    S.elTool = b.dataset.el;
    toast("Click the floor to place: " + S.elTool + (S.elTool === "antenna" ? " (" + $("antSel").value + ")" : ""));
  };
});
$("matSel").onchange = () => (S.mat = $("matSel").value);

async function placeElement(kind, mx, my) {
  const body = { type: kind, x: Math.round(mx), y: Math.round(my) };
  if (kind === "antenna") body.antenna_id = $("antSel").value;
  if (kind === "splitter") body.ways = parseInt($("waysSel").value, 10);
  try {
    const r = await api(P() + "/floors/" + S.floorId + "/elements", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    floor().elements.push(r.element);
    if (kind !== "antenna") deactivatePlacing();
    S.sel = r.element.id; inspect(r.element);
    toast("Placed " + r.element.label);
  } catch (e) { toast(e.message, true); }
  render();
}
function deactivatePlacing() {
  S.elTool = null;
  const sel = document.querySelector('.tool[data-tool="select"]');
  document.querySelectorAll(".tool").forEach((x) => x.classList.remove("active"));
  if (sel) sel.classList.add("active");
}

async function addWall(x1, y1, x2, y2) {
  try {
    const r = await api(P() + "/floors/" + S.floorId + "/walls", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x1: Math.round(x1), y1: Math.round(y1), x2: Math.round(x2), y2: Math.round(y2), material: S.mat }),
    });
    floor().walls.push(...r.walls);
    toast("Wall added: " + S.mat);
  } catch (e) { toast(e.message, true); }
}
async function addCable(fromId, toId) {
  const a = elById(fromId), b = elById(toId);
  const f = floor();
  const px = Math.hypot(b.el.x - a.el.x, b.el.y - a.el.y);
  const len = +(px / (f.ppm || 10)).toFixed(2);
  try {
    const r = await api(P() + "/floors/" + S.floorId + "/elements", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "cable_run", from_id: fromId, to_id: toId,
                             cable_id: $("cableSel").value, length_m: len }),
    });
    floor().elements.push(r.element);
    inspect(r.element);
    toast("Cable run " + len + " m (" + $("cableSel").value + ")");
  } catch (e) { toast(e.message, true); }
  render();
}

async function deleteElement(id) {
  try {
    const r = await api(P() + "/elements/" + id, { method: "DELETE" });
    const f = floor();
    f.elements = f.elements.filter((e) => e.id !== id && e.from_id !== id && e.to_id !== id);
    S.sel = null; inspect(null);
    toast("Deleted " + id + " (removed " + r.removed_including_runs + " elements)");
  } catch (e) { toast(e.message, true); }
  render();
}

/* ---------------- critical areas + NFPA report --------------------------- */
async function commitPoly() {
  if (!S.polyDraft || S.polyDraft.length < 3) { toast("need at least 3 corners", true); return; }
  const label = prompt("Critical-area label (e.g. IT room):", "Critical area");
  if (label === null) { return; }  // keep draft; user cancelled labeling
  try {
    const r = await api(P() + "/floors/" + S.floorId + "/critical_areas", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, points: S.polyDraft }),
    });
    floor().critical_areas = (floor().critical_areas || []).concat(r.critical_areas);
    S.polyDraft = null; S.selCa = r.critical_areas[0].id;
    $("btnFinishPoly").hidden = true;
    toast("Critical area added: " + label);
    if (S.predict) runPredict();
    renderNfpa();
  } catch (e) { toast(e.message, true); }
  render();
}
$("btnFinishPoly").onclick = commitPoly;

async function deleteCriticalArea(id) {
  try {
    await api(P() + "/critical_areas/" + id, { method: "DELETE" });
    const f = floor();
    f.critical_areas = (f.critical_areas || []).filter((ca) => ca.id !== id);
    S.selCa = null;
    toast("critical area deleted");
    if (S.predict) runPredict();
  } catch (e) { toast(e.message, true); }
  renderNfpa(); render();
}

function renderNfpa() {
  const box = $("nfpas");
  if (!box) return;
  box.innerHTML = "";
  const cas = (floor() && floor().critical_areas) || [];
  // defined polygons with delete
  if (cas.length) {
    const list = document.createElement("div");
    for (const ca of cas) {
      const row = document.createElement("div");
      row.className = "fld";
      row.style.cursor = "pointer";
      row.title = "click to highlight";
      row.onclick = () => {
        S.selCa = S.selCa === ca.id ? null : ca.id; render();
      };
      const lab = document.createElement("label");
      lab.textContent = "◧ " + (ca.label || ca.id) + " (" + (ca.points || []).length + " pts)";
      row.appendChild(lab);
      const del = document.createElement("button");
      del.textContent = "×"; del.className = "mini";
      del.onclick = (ev) => { ev.stopPropagation(); deleteCriticalArea(ca.id); };
      row.appendChild(del);
      list.appendChild(row);
    }
    box.appendChild(list);
  }
  // tier rows from the current predict (if any)
  const pr = S.predict;
  const tiers = pr ? pr.kpi.presets.filter((p) => p.preset === "nfpa_two_tier") : [];
  if (!tiers.length) {
    const c = document.createElement("div"); c.className = "chip";
    c.innerHTML = '<div class="v">Run predict (heatmap) to evaluate NFPA tiers.</div>';
    box.appendChild(c);
    return;
  }
  for (const t of tiers) {
    const c = document.createElement("div");
    c.className = "chip " + (t.pass === null ? "" : (t.pass ? "pass" : "fail"));
    let body = '<span class="h">' + t.name + " · " + t.scope + "</span>" +
      '<span class="big">' + (t.covered_pct == null ? "—" : t.pass ? "PASS" : "FAIL") + "</span>" +
      '<div class="v">' + (t.covered_pct == null ? "—" : t.covered_pct.toFixed(1) + "% covered") +
      " (@" + t.threshold_dbm + " dBm, need " + t.min_area_pct + "% min" +
      (t.scope === "critical_areas" ? ", inside polygons" : ", whole floor") + ")</div>";
    if (t.note) body += '<div class="v">' + t.note + "</div>";
    c.innerHTML = body;
    box.appendChild(c);
    for (const p of t.per_polygon || []) {
      const pc = document.createElement("div");
      pc.className = "chip " + (p.pass ? "pass" : "fail");
      pc.innerHTML = '<span class="h">◧ ' + (p.label || p.id) + "</span>" +
        '<span class="big">' + (p.pass ? "PASS" : "FAIL") + "</span>" +
        '<div class="v">' + (p.covered_pct == null ? "—" : p.covered_pct.toFixed(1) + "%") +
        " (" + p.n_covered + "/" + p.n_points + " pts)</div>";
      box.appendChild(pc);
    }
  }
  const nfpaBtn = document.createElement("a");
  nfpaBtn.className = "mini"; nfpaBtn.textContent = "NFPA report (JSON)";
  nfpaBtn.href = P() + "/floors/" + S.floorId + "/nfpa_report.json?band=" + encodeURIComponent(S.band || "") + "&step=1";
  nfpaBtn.download = "";
  box.appendChild(nfpaBtn);
  const pngLink = document.createElement("a");
  pngLink.className = "mini"; pngLink.textContent = " NFPA PNG";
  pngLink.href = P() + "/floors/" + S.floorId + "/nfpa_report.png?band=" + encodeURIComponent(S.band || "") + "&step=1";
  pngLink.target = "_blank";
  box.appendChild(pngLink);
}

/* ---------------- inspector --------------------------------------------- */
function numField(label, val, key, step) {
  return { label, key, val, type: "number", step: step || 0.1 };
}
function inspect(el) {
  const box = $("insp");
  if (!el) { box.innerHTML = "Nothing selected."; return; }
  box.innerHTML = "";
  const head = document.createElement("div"); head.className = "head";
  head.textContent = el.type + " — " + (el.label || el.id);
  box.appendChild(head);
  const fields = [];
  fields.push({ label: "label", key: "label", val: el.label || "", type: "text" });
  if (el.type !== "cable_run") {
    fields.push(numField("x (px)", Math.round(el.x), "x", 1));
    fields.push(numField("y (px)", Math.round(el.y), "y", 1));
  }
  if (el.type === "source") {
    const subs = (S.cfg.source.subtypes && Object.keys(S.cfg.source.subtypes).length)
      ? Object.keys(S.cfg.source.subtypes) : ["off_air"];
    fields.push({ label: "subtype", key: "subtype", type: "select",
      val: el.subtype || S.cfg.source.subtype_default || "off_air",
      options: subs.map((s) => [s, s]) });
    fields.push(numField("donor gain (dB)", el.donor_gain_db ?? S.cfg.source.donor_gain_db, "donor_gain_db"));
    fields.push(numField("feeder loss (dB)", el.feeder_loss_db ?? S.cfg.source.feeder_loss_db, "feeder_loss_db"));
    fields.push(numField("inject level (dBm, direct)", el.injection_dbm ?? "", "injection_dbm"));
  }
  if (el.type === "repeater") {
    fields.push(numField("DL gain (dB)", el.dl_gain_db ?? S.cfg.repeater.dl_gain_db, "dl_gain_db"));
    fields.push(numField("max DL out (dBm)", el.max_dl_out_dbm ?? S.cfg.repeater.max_dl_out_dbm, "max_dl_out_dbm"));
    fields.push(numField("ports", el.ports ?? S.cfg.repeater.ports, "ports", 1));
  }
  if (el.type === "splitter") {
    fields.push({ label: "ways", key: "ways", type: "select",
      val: el.ways || 2, options: [["2", "2-way"], ["3", "3-way"], ["4", "4-way"]] });
    fields.push(numField("IL per branch (dB)", el.il_db ?? "", "il_db"));
  }
  if (el.type === "tapper") {
    fields.push(numField("through IL (dB)", el.through_il_db ?? S.cfg.parts.tappers.default.through_il_db, "through_il_db"));
    fields.push(numField("tap loss (dB)", el.tap_loss_db ?? S.cfg.parts.tappers.default.tap_loss_db, "tap_loss_db"));
  }
  if (el.type === "coupler") {
    fields.push(numField("through IL (dB)", el.through_il_db ?? S.cfg.parts.couplers.default.through_il_db, "through_il_db"));
    fields.push(numField("coupled loss (dB)", el.coupled_loss_db ?? S.cfg.parts.couplers.default.coupled_loss_db, "coupled_loss_db"));
  }
  if (el.type === "antenna") {
    fields.push({ label: "type", key: "antenna_id", type: "select", val: el.antenna_id,
      options: Object.entries(S.cfg.antennas).map(([k, v]) => [k, v.label]) });
    fields.push(numField("gain (dBi)", el.gain_db ?? S.cfg.antennas[el.antenna_id].gain_db, "gain_db"));
    fields.push(numField("max port (dBm)", el.max_port_dbm ?? S.cfg.antennas[el.antenna_id].max_port_dbm, "max_port_dbm"));
  }
  if (el.type === "cable_run") {
    const a = elById(el.from_id), b = elById(el.to_id);
    const info = document.createElement("div");
    info.textContent = (a ? a.el.label : "?") + " -> " + (b ? b.el.label : "?");
    box.appendChild(info);
    fields.push({ label: "cable", key: "cable_id", type: "select", val: el.cable_id,
      options: Object.entries(S.cfg.cables).map(([k, v]) => [k, v.label]) });
    fields.push(numField("length (m)", el.length_m, "length_m"));
    fields.push({ label: "port", key: "port", type: "select", val: el.port || "",
      options: [["", "auto"], ["through", "through"], ["tap", "tap"], ["coupled", "coupled"]] });
  }
  for (const f of fields) buildField(box, el, f);
  const del = document.createElement("button");
  del.textContent = "Delete"; del.className = "mini";
  del.onclick = () => deleteElement(el.id);
  box.appendChild(del);
}
function inspectWall(wl) {
  const box = $("insp");
  box.innerHTML = "";
  const head = document.createElement("div"); head.className = "head";
  head.textContent = "wall — " + (S.cfg.wall_materials[wl.material] || {}).label || wl.material;
  box.appendChild(head);
  buildField(box, wl, { label: "material", key: "material", type: "select", val: wl.material,
    options: Object.entries(S.cfg.wall_materials).map(([k, v]) => [k, v.label]) });
  const del = document.createElement("button");
  del.textContent = "Delete wall"; del.className = "mini";
  del.onclick = async () => {
    try { await api(P() + "/walls/" + wl.id, { method: "DELETE" });
      const f = floor(); f.walls = f.walls.filter((w) => w.id !== wl.id);
      S.selWall = null; inspect(null); toast("wall deleted");
    } catch (e) { toast(e.message, true); }
    render();
  };
  box.appendChild(del);
}
function buildField(box, target, f) {
  const row = document.createElement("div"); row.className = "fld";
  const lab = document.createElement("label"); lab.textContent = f.label;
  let input;
  if (f.type === "select") {
    input = document.createElement("select");
    for (const [val, txt] of f.options) {
      const o = document.createElement("option"); o.value = val; o.textContent = txt;
      input.appendChild(o);
    }
    input.value = String(f.val);
  } else {
    input = document.createElement("input");
    input.type = f.type; input.step = f.step; input.value = f.val ?? "";
  }
  input.onchange = async () => {
    let v = input.value;
    if (f.type === "number") v = parseFloat(v);
    try {
      const r = await api(P() + "/elements/" + target.id, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [f.key]: v }),
      });
      Object.assign(target, r.element);
      toast(target.label + ": " + f.key + " = " + v);
      inspect(target); render();
    } catch (e) { toast(e.message, true); }
  };
  row.appendChild(lab); row.appendChild(input); box.appendChild(row);
}

/* ---------------- project/floor wiring --------------------------------- */
async function loadProjects() {
  const r = await api("/projects");
  S.projects = r.projects;
  const sel = $("projSel");
  sel.innerHTML = "";
  for (const p of S.projects) {
    const o = document.createElement("option");
    o.value = p.id; o.textContent = p.name + (p.example ? "  [example]" : "");
    sel.appendChild(o);
  }
  if (!S.pid && S.projects.length) {
    const ex = S.projects.find((p) => p.example) || S.projects[0];
    sel.value = ex.id; await loadProject(ex.id);
  }
}

async function loadProject(pid) {
  S.pid = pid;
  S.project = await api(P());
  const fls = S.project.model.floors;
  S.floorId = fls.length ? fls[0].id : null;
  S.solve = null; S.predict = null; S.sel = null; S.imgCache = {}; S.polyDraft = null; S.selCa = null;
  $("kpis").textContent = "Run solve, then enable the heatmap overlay.";
  buildFloorTabs(); setPpmBox(); render(); renderNfpa();
  $("lnkCsv").href = "/exports/projects/" + pid + "/bom.csv";
  $("lnkJson").href = "/exports/projects/" + pid + "/bom.json";
  await refreshBom();
}

function buildFloorTabs() {
  const tabs = $("floorTabs"); tabs.innerHTML = "";
  for (const f of S.project.model.floors) {
    const b = document.createElement("button");
    b.className = "tab tool" + (f.id === S.floorId ? " active" : "");
    b.innerHTML = f.name + ' <span class="x" title="delete floor">&times;</span>';
    b.onclick = (ev) => {
      if (ev.target.classList.contains("x")) { deleteFloor(f.id); return; }
      S.floorId = f.id; setPpmBox(); buildFloorTabs();
      S.predict = null; render(); renderNfpa();
    };
    tabs.appendChild(b);
  }
}
async function deleteFloor(fid) {
  if (!confirm("Delete this floor (walls + elements on it)?")) return;
  try {
    await api(P() + "/floors/" + fid, { method: "DELETE" });
    S.project.model.floors = S.project.model.floors.filter((f) => f.id !== fid);
    S.floorId = S.project.model.floors.length ? S.project.model.floors[0].id : null;
    buildFloorTabs(); setPpmBox(); render();
  } catch (e) { toast(e.message, true); }
}
function setPpmBox() { const f = floor(); $("inpPpm").value = f ? (f.ppm || 10) : 10; }
$("inpPpm").onchange = async () => {
  const v = parseFloat($("inpPpm").value);
  if (!v || v <= 0) return;
  try {
    await api(P() + "/floors/" + S.floorId, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ppm: v }),
    });
    floor().ppm = v;
  } catch (e) { toast(e.message, true); }
};
$("btnNewProj").onclick = async () => {
  const name = $("newProjName").value.trim();
  if (!name) { toast("enter a project name first", true); return; }
  const r = await api("/projects", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  $("newProjName").value = "";
  await loadProjects();
  $("projSel").value = r.id; await loadProject(r.id);
  toast("Project created");
};
$("projSel").onchange = () => loadProject(parseInt($("projSel").value, 10));
$("btnAddFloor").onclick = async () => {
  const name = prompt("Floor name:", "Floor " + (S.project.model.floors.length + 1));
  if (name == null) return;
  const lvl = prompt("Floor level (0 = ground):", S.project.model.floors.length);
  const r = await api(P() + "/floors", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, level: parseInt(lvl || "0", 10) }),
  });
  S.project.model.floors.push(r.floor);
  S.floorId = r.floor.id; buildFloorTabs(); setPpmBox(); render();
};

$("imgFile").onchange = async () => {
  const file = $("imgFile").files[0];
  if (!file) return;
  const fd = new FormData(); fd.append("file", file);
  try {
    const r = await api(P() + "/floors/" + S.floorId + "/image", { method: "POST", body: fd });
    Object.assign(floor(), r.floor);
    floor().image = { url: P() + "/floors/" + S.floorId + "/image", mime: r.floor.mime };
    S.imgCache = {};
    toast("Image uploaded; set ppm or calibrate");
  } catch (e) { toast(e.message, true); }
  $("imgFile").value = "";
  render();
};
$("btnCalib").onclick = () => {
  S.calib = [];
  toast("Click the first known point on the image");
};

/* ---------------- solve / predict / KPI / BOM --------------------------- */
$("btnSolve").onclick = async () => {
  try {
    S.solve = await api(P() + "/cascade/solve", { method: "POST" });
    const bands = Object.values(S.solve.bands);
    if (S.band && !S.solve.bands[S.band]) S.band = null;
    const warnMsg = (S.solve.warnings || []).join("\n");
    toast("Solved " + bands.length + " band(s) · " +
      bands.reduce((n, b) => n + b.antennas.length, 0) + " antennas" +
      (S.solve.warnings.length ? "\nWarnings:\n" + warnMsg : ""));
    await refreshBom();
    if ($("chkHeatmap").checked) await runPredict();
    else { $("kpis").textContent = "Heatmap off — enable it to compute KPI."; renderNfpa(); }
  } catch (e) { toast(e.message, true); }
};
$("chkHeatmap").onchange = async () => { if ($("chkHeatmap").checked) { await runPredict(); } else { S.predict = null; render(); } };
$("bandSel").onchange = async () => { S.band = $("bandSel").value; if ($("chkHeatmap").checked) await runPredict(); };

async function runPredict() {
  if (!S.band || !S.solve) { toast("solve the cascade first", true); return; }
  try {
    S.predict = await api(P() + "/floors/" + S.floorId + "/predict?band=" + encodeURIComponent(S.band) + "&step=1", { method: "POST" });
    render(); renderKpi(); renderNfpa();
  } catch (e) { toast(e.message, true); }
}
function renderKpi() {
  const pr = S.predict; const box = $("kpis"); box.innerHTML = "";
  const chipMain = document.createElement("div");
  chipMain.className = "chip " + (pr.kpi.presets[0].pass ? "pass" : "fail");
  chipMain.innerHTML = '<span class="h">' + pr.band.label + "</span>" +
    '<span class="big">' + pr.kpi.covered_pct.toFixed(1) + "%</span>" +
    '<div class="v">>= ' + pr.kpi.threshold_dbm + " dBm · " + pr.grid.n_points + " grid pts</div>";
  box.appendChild(chipMain);
  for (const p of pr.kpi.presets) {
    const c = document.createElement("div");
    c.className = "chip " + (p.pass ? "pass" : "fail");
    const req = (p.min_area_pct == null ? "—" : p.min_area_pct) + "% min";
    c.innerHTML = '<span class="h">' + p.name + "</span>" +
      '<span class="big">' + (p.pass ? "PASS" : "FAIL") + "</span>" +
      '<div class="v">' + p.covered_pct.toFixed(1) + "% covered (@" + p.threshold_dbm + " dBm, need " + req + ")</div>";
    box.appendChild(c);
  }
  const s = S.solve.bands[pr.band.id];
  if (s && s.antennas.length) {
    let txt = "Port EIRP (dBm): ";
    txt += s.antennas.map((a) => a.label + " " + a.eirp_dbm.toFixed(1)).join(", ");
    const d = document.createElement("div"); d.className = "chip"; d.innerHTML = '<div class="v">' + txt + "</div>";
    box.appendChild(d);
  }
}

async function refreshBom() {
  try {
    S.bom = await api(P() + "/bom");
    const box = $("bomBox");
    if (!S.bom.items.length) { box.innerHTML = "No parts yet — build the chain."; return; }
    let html = '<table><tr><th>Category</th><th>Item</th><th class="q">Qty</th><th>Unit</th></tr>';
    for (const it of S.bom.items) {
      html += "<tr><td class='cat'>" + it.category + "</td><td>" + it.item +
        '</td><td class="q">' + it.qty + "</td><td>" + it.unit + "</td></tr>";
    }
    html += "</table>";
    box.innerHTML = html;
  } catch (e) { $("bomBox").textContent = e.message; }
}
$("btnBom").onclick = refreshBom;

/* ---------------- config-driven palette/legend -------------------------- */
function buildPaletteFromConfig() {
  const mats = $("matSel");
  for (const [k, v] of Object.entries(S.cfg.wall_materials)) {
    const o = document.createElement("option"); o.value = k; o.textContent = v.label + " (" + v.loss_db + " dB)";
    mats.appendChild(o);
  }
  const cables = $("cableSel");
  for (const [k, v] of Object.entries(S.cfg.cables)) {
    const o = document.createElement("option"); o.value = k; o.textContent = v.label;
    cables.appendChild(o);
  }
  const ants = $("antSel");
  for (const [k, v] of Object.entries(S.cfg.antennas)) {
    const o = document.createElement("option"); o.value = k; o.textContent = v.label + " (" + v.gain_db + " dBi)";
    ants.appendChild(o);
  }
  const bands = $("bandSel");
  for (const b of S.cfg.bands) {
    const o = document.createElement("option"); o.value = b.id; o.textContent = b.label;
    bands.appendChild(o);
  }
  S.band = S.cfg.bands[0] ? S.cfg.bands[0].id : null;
  $("bandSel").value = S.band;
  const lg = $("legend");
  const scale = S.cfg.report.color_scale;
  let lastDbm = null, lastSw = null;
  let html = '<div class="scale" style="background:linear-gradient(90deg,' +
    scale.map((s) => rgb(s.rgb)).join(",") + ')"></div>';
  html += '<div class="sw"><span>' + scale[0].dbm + " to " + scale[scale.length - 1].dbm + " dBm</span></div>";
  lg.innerHTML = html;
}

(async function init() {
  try {
    S.cfg = await api("/config");
    buildPaletteFromConfig();
    await loadProjects();
    render();
  } catch (e) { toast("init failed: " + e.message, true); }
})();
