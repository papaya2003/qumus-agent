// Top view of the lab bench as bench.yaml describes it (world frame in mm,
// x right, y away from the operator = up on screen), drawn from the lab twin
// (runtime/twin.py): arms, the two machines as modules (Jialei's sketch,
// 2026-10-22) and every object where the devices say they are. Exfoliation:
// one slide rail along the back, two rolls hanging from sliders on it, the
// tape between them over the vacuum stage. Search & stacking: the microscope
// has fixed XY and independent vertical focus; coarse/fine XY and rotation
// carry the sample beneath it. The stamp holder moves on its rails with
// the glass slide lying on it and the stamp at the slide's front end.
//
// Physical dimensions use one world-mm scale, including chips and slot frames.
// Text, strokes and observation markers remain screen-readable annotations.
//
// The static part (table, storage boards, module frames, rails) is drawn
// once. The moving part keeps one SVG element per thing and only updates its
// attributes, so the page can redraw it every animation frame. Every part
// carries a <title>, so hovering explains it.

import { svg, tapeText } from './ui.js';

const ARM_LABELS = { arm_left: 'Arm 1', arm_right: 'Arm 2', arm_reactor: 'Arm 3' };
const HOLD_MM = { chip: [10, 10], stamp: [40, 25], carrier: [25, 40], slide: [25, 40] };
const MODULE_PAD = 18;  // mm around a module's parts when bench.yaml has no `module` box
// A stamp is a PDMS dome on a 40 x 25 mm glass slide, DOME_INSET from its left
// end (bench.yaml stamp_slide). The slide lies along x the same way in its
// storage frame, on Arm 2's nozzle and on the holder, and the world gives a
// stamp's position as its slide's centre (the point an arm holds) everywhere,
// so it is drawn the same way everywhere: at the hand-off the dome lands right
// under the holder's head (flows/bench.py stamp_handoff) and nothing turns or
// shifts. (Older recordings: slides standing upright, the dome at the front end.)
const DOME_INSET = 7, DOME_R = 5;
// the stamp holder seen from above, relative to the head point (the dome), mm:
// the slide lies on the body (vacuum hole in its top face) and sticks out to the left
const SLIDE = { x: [-DOME_INSET, 40 - DOME_INSET], y: [-12.5, 12.5] }, BODY = { x: [12, 48], y: [-16, 16] }, VAC_X = 24, BODY_CX = 30;
const STORAGE_HINTS = { left_storage: 'blank chips · material slides', middle_storage: 'exfoliated chips · hand-over',
  right_storage: 'stamps · finished devices', reactor_storage: 'reactor chips' };
const SHORT_LABELS = { left_storage: 'Storage', middle_storage: 'Storage', right_storage: 'Storage',
  exfoliation_stage: 'Exfoliation', transfer_stage: 'Search & stacking', reactor: 'Reactor', reactor_storage: 'Storage' };
const STATION_TIPS = {
  exfoliation: 'Exfoliation: white tape runs between two rolls hanging from a slide rail (the right one stays put, the left one moves along the rail and up / down); the heated vacuum stage holds the chip; Arm 1 presses the tape on with its rubber tip',
  microscope: 'Search & stacking: coarse/fine XY and rotation carry the sample beneath the microscope. Independent microscope Z focuses without changing the sample/stamp gap; the stamp holder translates in XYZ for stacking.',
  storage: 'Storage board: black frames hold objects; QR markers in the corners locate the board for the overview camera',
  reactor: 'Reactor (stage 2, not built): only a port to put a chip in and take it out',
};
const BASE_R = 9;  // Half side of the base square: a readable zero-position annotation, not a measured footprint.
const QR_HALF = 2.2;  // half side of a drawn QR marker (annotation scale)
const BASE_ROUND = 0.22;  // corner radius as a fraction of the side

export function isStationLayout(cfg) {
  return !!(cfg && cfg.stations && typeof cfg.stations === 'object' && Object.keys(cfg.stations).length);
}

// arm poses are in each arm's own frame; world = base + pose
export function armBase(cfg, id) {
  const b = cfg && cfg[id] && cfg[id].base;
  return b && Number.isFinite(+b.x) && Number.isFinite(+b.y) ? { x: +b.x, y: +b.y } : { x: 0, y: 0 };
}

// World point under the stamp holder's head (flows/bench.py manipulator_head).
// Two layouts: a 3-axis stage (`origin` = world point of the head at x = y = 0)
// or the older rail (`rail_dir`, l axis; recordings made before the change).
export function headPoint(cfg, pos) {
  const ta = cfg.transfer_arm || {}, c = (cfg.microscope || {}).center || { x: 0, y: 0 };
  if (ta.origin) return { x: +ta.origin.x + (+pos.x || 0), y: +ta.origin.y + (+pos.y || 0) };
  const [dx, dy] = ta.rail_dir || [-1, 0];
  const over = +(ta.over_stage_l ?? 120), [rx, ry] = ta.ref_xy || [12.5, 12.5];
  const dl = (pos.l ?? over) - over;
  return { x: c.x + dx * dl + (pos.x ?? rx) - rx, y: c.y + dy * dl + (pos.y ?? ry) - ry };
}

const bbox = (...rs) => {
  const ok = rs.filter(r => r && Array.isArray(r.x) && Array.isArray(r.y));
  return { x: [Math.min(...ok.map(r => r.x[0])), Math.max(...ok.map(r => r.x[1]))],
    y: [Math.min(...ok.map(r => r.y[0])), Math.max(...ok.map(r => r.y[1]))] };
};
const pad = (r, m) => ({ x: [r.x[0] - m, r.x[1] + m], y: [r.y[0] - m, r.y[1] + m] });

// The exfoliator's slide rail seen from above; older layouts only had the
// roll's x travel along the tape.
export function exfoliatorRail(cfg) {
  const ex = cfg.exfoliator || {};
  if (ex.rail && Array.isArray(ex.rail.x)) return { x: ex.rail.x.map(Number), y: +ex.rail.y };
  const o = exfoliatorOrigin(cfg), tr = ex.travel || { x: [-150, 60] }, rl = (ex.roller || {}).length_mm || 34;
  return { x: [o.x + tr.x[0] - 8, o.x + tr.x[1] + 8], y: o.y + rl / 2 + 19 };
}

// The dome's offset from its slide's centre (flows/bench.py stamp_dome_offset):
// a stamp on the holder has its slide's centre at the head point minus this.
export function stampDomeOffset(cfg) {
  const s = (cfg && cfg.stamp_slide) || {}, len = +((s.size || [0])[0]) || 0;
  return { x: -(len / 2 - (s.dome_inset_mm ?? len / 2)), y: 0 };
}

// The stamp holder's rails as a path of head points: park -> load -> up to the
// objective's y -> left to over the stage (bench.yaml transfer_arm).
export function holderPath(cfg) {
  const ta = cfg.transfer_arm || {};
  if (!ta.origin) return [];
  const over = ta.over_stage || { x: 0, y: 0 }, load = ta.load || over, park = ta.park || load;
  return [park, load, { x: load.x, y: over.y }, over].map(q => headPoint(cfg, q));
}

// The fixed exfoliator roll (gantry x 0); older layouts had no origin.
export function exfoliatorOrigin(cfg) {
  const ex = cfg.exfoliator || {}, st = (cfg.stations || {}).exfoliation_stage || {};
  if (ex.origin) return { x: +ex.origin.x, y: +ex.origin.y };
  return { x: (+st.x || 0) + 40, y: +st.y || 0 };
}

const fmt1 = v => (v == null ? '?' : (+v).toFixed(1));

export function stampStatusText(status, film) {
  if (status === 'used') return film === true || typeof film === 'string' && film.length
    ? 'used (support retained)' : film === false || film === null ? 'used (no film)' : 'used';
  return {fresh: 'new film', in_use: 'in use', torn: 'film torn'}[status] || status;
}

// The drawn table is the configured table grown to hold every drawn part
// (boards, slot frames, QR markers, module frames, arm bases and their
// captions) plus a narrow rim, so nothing hangs over its edge. It is display
// only: world coordinates, distances and the one uniform mm scale stay as
// configured; the whole picture just fits the window a little smaller.
const TABLE_RIM = 12;     // mm of table around the outermost drawn part
const VIEW_MARGIN = 4;    // mm of background around the drawn table

// A slot's black frame on its board (world mm). `frame` / `frame_offset` are
// drawing-only (the linked layout's real frames); without them the frame is
// the held object's size plus a 1.2 mm border.
export function slotFrame(sl) {
  const [w, h] = sl.frame || (sl.size || HOLD_MM[sl.holds] || HOLD_MM.chip).map(v => v + 2.4);
  const [ox, oy] = sl.frame_offset || [0, 0];
  return {x: [sl.x + ox - w / 2, sl.x + ox + w / 2], y: [sl.y + oy - h / 2, sl.y + oy + h / 2]};
}

// A QR marker's half side: its real printed size when the layout gives one
// (the linked layout's markers fill the board corners), else a readable mark.
const qrHalf = (cfg, board, k) => {
  const mm = +((cfg.qr_size_mm || {})[board]);
  return mm > 0 ? mm / 2 : QR_HALF * k;
};

export function benchViewport(cfg) {
  const parts = [];
  const add = r => { if (r && Array.isArray(r.x) && Array.isArray(r.y)) parts.push(r); };
  const stations = cfg.stations || {};
  add(cfg.table);
  for (const st of Object.values(stations)) { add(st.region); add(stationFootprint(cfg, st)); }
  for (const [id, sl] of Object.entries(cfg.slots || {})) {
    if (!Number.isFinite(sl.x) || !Number.isFinite(sl.y) || (stations[sl.station] || {}).placeholder) continue;
    if (id === 'reactor_port' && cfg.reactor_module) continue;
    add(slotFrame(sl));
  }
  const bases = Object.keys(ARM_LABELS).map(id => cfg[id]).filter(a => a?.base && !a.planned);
  for (const a of bases) add({x: [+a.base.x, +a.base.x], y: [+a.base.y, +a.base.y]});
  const c = bbox(...parts);
  // Fix annotation scale before fitting its extents: no padding/scale feedback.
  const k = Math.max(1, (c.x[1] - c.x[0] + 36) / 360);
  const half = (BASE_R + .3) * k;  // the base square's half side plus half its stroke
  for (const a of bases) add({x: [+a.base.x - half, +a.base.x + half], y: [+a.base.y - half, +a.base.y + half]});
  for (const [board, pts] of Object.entries(cfg.qr_markers || {})) {
    const qr = qrHalf(cfg, board, k);
    for (const p of pts || []) add({x: [p[0] - qr, p[0] + qr], y: [p[1] - qr, p[1] + qr]});
  }
  // a station's caption sits above its frame; a storage board's hint below it
  for (const [id, st] of Object.entries(stations)) {
    const f = stationFootprint(cfg, st);
    if (!f) continue;
    add({x: f.x, y: [f.y[0], f.y[1] + 7 * k]});
    const hint = st.kind === 'storage' && STORAGE_HINTS[id];
    if (hint) {  // centred under the board, monospace: about 0.62 em per character
      const mx = (st.region.x[0] + st.region.x[1]) / 2, half = hint.length * 0.62 * 3.4 * k / 2;
      add({x: [mx - half, mx + half], y: [f.y[0] - 11 * k, f.y[0]]});
    }
  }
  const table = pad(bbox(...parts), TABLE_RIM);
  const view = pad(table, VIEW_MARGIN);
  const b = {x0: view.x[0], x1: view.x[1], y0: view.y[0], y1: view.y[1]};
  return {b, w: b.x1 - b.x0, h: b.y1 - b.y0, k, table};
}

// The frame drawn around a station: its `module` box, or for layouts without
// one (recordings made before 2026-10-22) everything the machine has at its
// furthest positions plus a margin; the reactor assembly; else its region.
export function stationFootprint(cfg, st) {
  if (!st || !st.region) return null;
  const r = st.region;
  return reactorFootprint(cfg, st) || st.module
    || (st.kind === 'exfoliation' ? exfoliationFootprint(cfg, r) : st.kind === 'microscope' ? scopeFootprint(cfg, r) : r);
}

function exfoliationFootprint(cfg, r) {
  const ex = cfg.exfoliator || {}, rail = exfoliatorRail(cfg);
  return pad(bbox(r, ex.region, { x: rail.x, y: [rail.y, rail.y] }), MODULE_PAD);
}

function scopeFootprint(cfg, r) {
  const c = (cfg.microscope || {}).center || { x: (r.x[0] + r.x[1]) / 2, y: (r.y[0] + r.y[1]) / 2 };
  const m = (cfg.transfer_stage || {}).microscope || { x: 40, y: 25 };
  const parts = [r, { x: [r.x[0] + m.x, r.x[1] + m.x], y: [r.y[0] + m.y, r.y[1] + m.y] },
    { x: [c.x - 33, c.x + 33], y: [c.y - 33, c.y + 33] }];
  for (const p of holderPath(cfg)) parts.push({ x: [p.x + SLIDE.x[0], p.x + BODY.x[1]], y: [p.y + BODY.y[0], p.y + BODY.y[1]] });
  return pad(bbox(...parts), MODULE_PAD);
}

// One rigid XY assembly encloses the chamber and the full loading stroke.
// Trim display-only side padding to separate the frame from Arm 3 and
// Reactor Storage. Physical coordinates and the fixed microscope stay put.
function reactorFootprint(cfg, st) {
  if (!st || st.kind !== 'reactor' || st.placeholder || !st.region || !(cfg.reactor_module?.mechanics_version >= 2)) return null;
  const r = st.region, geo = cfg.reactor_module.geometry;
  const inset = Math.min(6, (r.x[1] - r.x[0]) * 0.05);
  return { x: [r.x[0] + inset, r.x[1] - inset], y: [Math.min(r.y[0], geo.load.y - (geo.rod_length_mm || 96) - 12) - 24,
    r.y[1] + 12] };
}

export class StationBenchView {
  constructor(svgEl, cfg) {
    this.svg = svgEl;
    this.cfg = cfg;
    // two views on one page (the bench, the Lab map): their pattern ids must differ
    this.uid = svgEl.id || 'bench';
    Object.assign(this, benchViewport(cfg));
    svgEl.setAttribute('viewBox', `0 0 ${this.w} ${this.h}`);
    svgEl.replaceChildren();
    this.drawStatic();
    this.gStage = svg('g', { class: 's-dyn' }, svgEl);
    this.gTape = svg('g', {}, svgEl);
    this.gObjects = svg('g', {}, svgEl);
    this.gHolder = svg('g', {}, svgEl);
    this.gArm = svg('g', {}, svgEl);
    this.objs = new Map(); this.arms = new Map();
    this.devEls = new Map(); this.lit = new Set();  // device id -> its drawn groups; highlighted ids
    this.buildMoving();
  }

  sx(x) { return x - this.b.x0; }
  sy(y) { return this.b.y1 - y; }
  rect(r, attrs, parent) {
    return svg('rect', { x: this.sx(r.x[0]), y: this.sy(r.y[1]), width: r.x[1] - r.x[0], height: r.y[1] - r.y[0], ...attrs }, parent);
  }
  text(parent, x, y, str, cls, size, anchor = 'start') {
    const t = svg('text', { x, y, class: cls, 'text-anchor': anchor, style: `font-size:${size}px` }, parent);
    t.textContent = str;
    return t;
  }
  tip(node, str) { svg('title', {}, node).textContent = str; return node; }
  drawStatic() {
    const s = this.svg, k = this.k, cfg = this.cfg;
    const defs = svg('defs', {}, s);
    const step = 10 * Math.round(k * 2);
    const pat = svg('pattern', { id: `${this.uid}-sgrid`, width: step, height: step, patternUnits: 'userSpaceOnUse' }, defs);
    svg('path', { d: `M${step} 0H0V${step}`, class: 'b-grid', fill: 'none', style: `stroke-width:${0.25 * k}` }, pat);
    const hatch = svg('pattern', { id: `${this.uid}-shatch`, width: 6 * k, height: 6 * k, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, defs);
    svg('path', { d: `M0 0V${6 * k}`, class: 's-hatch', style: `stroke-width:${0.8 * k}` }, hatch);
    const blur = svg('filter', { id: `${this.uid}-sshadow`, x: '-50%', y: '-50%', width: '200%', height: '200%' }, defs);
    svg('feGaussianBlur', { stdDeviation: 1.6 * k }, blur);
    svg('rect', { width: this.w, height: this.h, fill: `url(#${this.uid}-sgrid)` }, s);
    this.rect(this.table, { rx: 6 * k, class: 's-table', style: `stroke-width:${0.5 * k}` }, s);
    if (cfg.geometry_profile) {
      s.setAttribute('data-geometry-profile', cfg.geometry_profile.id);
      this.tip(this.text(s, this.sx(this.table.x[0]) + 4 * k, this.sy(this.table.y[1]) + 5 * k,
        cfg.geometry_profile.label, 's-label', 3 * k), cfg.geometry_profile.uncertainty);
    }

    for (const [id, st] of Object.entries(cfg.stations)) {
      if (!st.region) continue;
      const kind = st.kind || 'station', r = st.region;
      const g = svg('g', { class: `s-station ${kind}`, 'data-station': id }, s);
      this.tip(g, kind === 'reactor' && !st.placeholder
        ? (cfg.reactor_module?.mechanics_version >= 2 ? 'Reactor chamber and rod mechanism ride together on bottom X / top Y stages; insert moves the rod, and the microscope remains fixed in XY' : 'Legacy reactor recording: combined insertion and fine-Y carrier')
        : STATION_TIPS[kind] || st.label || id.replace(/_/g, ' '));
      const assembly = kind === 'reactor' && !st.placeholder && cfg.reactor_module?.mechanics_version >= 2;
      const foot = stationFootprint(cfg, st);
      // storage boards are plain plates with square corners (QR markers sit right in them)
      this.rect(foot, { rx: (kind === 'storage' ? 0 : st.module || assembly || kind === 'exfoliation' || kind === 'microscope' ? 5 : 3) * k,
        class: 's-region' + (foot !== r ? ' module' : '') + (assembly ? ' s-reactor-frame' : ''), style: `stroke-width:${0.5 * k}` }, g);
      if (kind === 'reactor' && st.placeholder) this.rect(r, { rx: 3 * k, fill: `url(#${this.uid}-shatch)`, class: 's-hatchfill' }, g);
      // the comb-shaped edge of the stamp board: a stamp's dome hangs over a gap
      // (open to the left: the fill covers the board's edge line, the outline runs top, right, bottom)
      for (const n of st.notches || []) {
        const w = 0.5 * k, x0 = this.sx(n.x[0]), x1 = this.sx(n.x[1]), yt = this.sy(n.y[1]), yb = this.sy(n.y[0]);
        const gap = svg('g', { class: 's-notch-gap' }, g);
        // the fill just covers the board's edge line; the outline ends flush with the board's outer edge
        svg('rect', { x: x0 - w / 2, y: yt, width: x1 - x0 + w / 2, height: yb - yt, class: 's-notch' }, gap);
        svg('path', { d: `M${x0 - w / 2} ${yt}H${x1}V${yb}H${x0 - w / 2}`, class: 's-notch-edge', style: `stroke-width:${w}` }, gap);
        this.tip(gap, 'Gap in the comb-shaped board edge: the stamp\'s PDMS dome hangs over it, touching nothing');
      }
      if (kind === 'reactor' && !st.placeholder && cfg.reactor_module) {
        const geo = cfg.reactor_module.geometry;
        this.reactorChamber = g;
        g.classList.add('built');
        if (assembly) {
          this.reactorAssembly = g;
          g.classList.add('s-reactor-assembly');
          g.setAttribute('data-device', 'reactor_slot');
          this.rect({ x: [foot.x[0] + 6, foot.x[1] - 6], y: r.y },
            { class: 's-reactor-chamber', rx: 3 * k }, g);
          this.reactorAssemblyReadout = this.text(g, this.sx((r.x[0] + r.x[1]) / 2),
            this.sy(foot.y[0]) - 3 * k, '', 's-small', 2.6 * k, 'middle');
          this.tip(this.reactorAssemblyReadout, 'Bottom X / top Y move the chamber, rod and seated sample together. The microscope is fixed.');
        }
        this.rect({ x: [geo.load.x - 15, geo.load.x + 15], y: [r.y[0], r.y[1]] },
          { class: 's-reactor-passage', rx: 12 }, g);
      }
      if (kind === 'exfoliation') this.exfoliationStatic(g, r);
      if (kind === 'microscope') this.microscopeStatic(g, r);
      const x = this.sx(foot.x[0]) + 3 * k;
      const y = kind === 'reactor' && !assembly ? this.sy(foot.y[1]) + 7 * k : this.sy(foot.y[1]) - 2.5 * k;
      this.text(g, x, y, assembly ? 'VDW REACTOR' : (SHORT_LABELS[id] || st.label || id.replace(/_/g, ' ')).toUpperCase(), 's-label', 4.2 * k);
      const mx = (r.x[0] + r.x[1]) / 2;
      if (kind === 'storage' && STORAGE_HINTS[id]) this.text(g, this.sx(mx), this.sy(foot.y[0]) + 6 * k, STORAGE_HINTS[id], 's-small', 3.4 * k, 'middle');
      if (kind === 'reactor' && st.placeholder) this.text(g, this.sx(mx), this.sy((r.y[0] + r.y[1]) / 2), 'not built yet', 's-small', 3.6 * k, 'middle');
    }
    for (const [board, pts] of Object.entries(cfg.qr_markers || {})) {
      for (const p of pts || []) {
        const a = qrHalf(cfg, board, k), cx = this.sx(p[0]), cy = this.sy(p[1]);
        svg('rect', { x: cx - a, y: cy - a, width: 2 * a, height: 2 * a, class: 's-qr' }, s);
        svg('rect', { x: cx - a / 2.4, y: cy - a / 2.4, width: a / 1.2, height: a / 1.2, class: 's-qr-in' }, s);
      }
    }
    for (const [id, sl] of Object.entries(cfg.slots || {})) {
      if (id === 'reactor_port' && cfg.reactor_module) continue; // the carrier is drawn at its measured XY pose
      if (!Number.isFinite(sl.x) || !Number.isFinite(sl.y)) continue;
      if (((cfg.stations || {})[sl.station] || {}).placeholder) continue;  // nothing built there yet (the reactor)
      const f = slotFrame(sl);
      // a stamp's frame starts at the comb edge: open to the left, over the gap
      const r = sl.frame_open_left
        ? svg('path', { d: `M${this.sx(f.x[0])} ${this.sy(f.y[1])}H${this.sx(f.x[1])}V${this.sy(f.y[0])}H${this.sx(f.x[0])}`,
          class: 's-slot open', style: `stroke-width:${0.35 * k}` }, s)
        : this.rect(f, { rx: 1, class: 's-slot', style: `stroke-width:${0.35 * k}` }, s);
      this.tip(r, `${id} · ${sl.holds || 'chip'}`);
    }
    for (const id of Object.keys(ARM_LABELS)) {
      const a = cfg[id];
      if (!a || !a.base || a.planned) continue;  // a planned arm (the reactor's) is not drawn until it exists
      // a rounded square, like the real arms' bases (Jialei, 2026-11-02)
      const side = 2 * BASE_R * k;
      const c = svg('rect', { x: this.sx(a.base.x) - side / 2, y: this.sy(a.base.y) - side / 2, width: side, height: side,
        rx: side * BASE_ROUND, class: `s-base ${id}`, style: `stroke-width:${0.6 * k}` }, s);
      this.tip(c, `${ARM_LABELS[id]} base/zero position marker · footprint uncalibrated · serves ${(a.reach || []).map(x => x.replace(/_/g, ' ')).join(', ')}`);
      this.text(s, this.sx(a.base.x), this.sy(a.base.y) + 1.3 * k, ARM_LABELS[id], `s-base-label ${id}`, 3.6 * k, 'middle');
    }
  }

  // Exfoliation, seen from above: the vacuum stage (a square with a ring of
  // suction holes) and the slide rail along the back of the module; the rolls
  // are drawn over the tape (buildMoving).
  exfoliationStatic(g, r) {
    const k = this.k, cfg = this.cfg, ex = cfg.exfoliator;
    const st = this.rect(r, { rx: 1.5 * k, class: 's-stage', style: `stroke-width:${0.45 * k}` }, g);
    this.tip(st, 'Exfoliation stage: holds the chip by vacuum, heats and cools; it does not move');
    const cx = (r.x[0] + r.x[1]) / 2, cy = (r.y[0] + r.y[1]) / 2, rr = Math.min(r.x[1] - r.x[0], r.y[1] - r.y[0]) * 0.42;
    svg('circle', { cx: this.sx(cx), cy: this.sy(cy), r: rr, class: 's-vacring', style: `stroke-width:${0.4 * k};stroke-dasharray:${0.9 * k} ${0.9 * k}` }, g);
    if (!ex) return;
    const o = exfoliatorOrigin(cfg), rl = (ex.roller || {}).length_mm || 34, rd = (ex.roller || {}).diameter_mm || 12;
    this.rollerSize = [rd, rl];
    const rail = exfoliatorRail(cfg);
    this.railY = rail.y;
    const line = svg('path', { d: `M${this.sx(rail.x[0])} ${this.sy(rail.y)}H${this.sx(rail.x[1])}`, class: 's-xrail', style: `stroke-width:${1.6 * k}` }, g);
    this.tip(line, 'Slide rail: the left roll\'s slider runs along it');
  }

  // A roll hanging from its slider on the rail: {g, shadow, stem, body, slider}
  roll(parent, upper) {
    const k = this.k, [rd, rl] = this.rollerSize, g = svg('g', {}, parent);
    const shadow = upper ? svg('rect', { width: rd, height: rl, rx: rd / 2, class: 's-roller-shadow', filter: `url(#${this.uid}-sshadow)` }, g) : null;
    const stem = svg('path', { class: 's-stem', style: `stroke-width:${0.7 * Math.min(k, 2)}` }, g);
    const body = svg('rect', { width: rd, height: rl, rx: rd / 2, class: 's-roller' + (upper ? ' upper' : ''), style: `stroke-width:${0.45 * k}` }, g);
    const slider = svg('rect', { width: 16, height: 10, rx: 1.6, class: 's-slider', style: `stroke-width:${0.45 * k}` }, g);
    return { g, shadow, stem, body, slider };
  }

  placeRoll(e, x, y) {
    const [rd, rl] = this.rollerSize, ux = this.sx(x) - rd / 2, uy = this.sy(y) - rl / 2;
    e.body.setAttribute('x', ux); e.body.setAttribute('y', uy);
    e.stem.setAttribute('d', `M${this.sx(x)} ${uy}V${this.sy(this.railY) + 5}`);
    e.slider.setAttribute('x', this.sx(x) - 8); e.slider.setAttribute('y', this.sy(this.railY) - 5);
    return [ux, uy];
  }

  // Search & stacking, static part: the stage's lane from the arm drop-off
  // point up to under the objective, and the stamp holder's rails.
  microscopeStatic(g, r) {
    const k = this.k, cfg = this.cfg;
    const m = (cfg.transfer_stage || {}).microscope || { x: 40, y: 25 };
    const env = { x: [Math.min(r.x[0], r.x[0] + m.x) - 2, Math.max(r.x[1], r.x[1] + m.x) + 2], y: [Math.min(r.y[0], r.y[0] + m.y) - 2, Math.max(r.y[1], r.y[1] + m.y) + 2] };
    const e = this.rect(env, { rx: 2.5, class: 's-travel', style: `stroke-width:${0.35 * k};stroke-dasharray:${1.2 * k} ${1.2 * k}` }, g);
    this.tip(e, 'Stage travel: coarse/fine XY and rotation carry the sample beneath the objective; independent microscope Z changes focus without changing the mechanical sample/stamp gap');
    const pts = holderPath(cfg).map(p => ({ x: p.x + BODY_CX, y: p.y }));
    if (pts.length) {
      const rail = svg('path', { d: pts.map((p, i) => `${i ? 'L' : 'M'}${this.sx(p.x).toFixed(2)} ${this.sy(p.y).toFixed(2)}`).join(''),
        class: 's-xrail', style: `stroke-width:${1.6 * k};stroke-linejoin:round` }, g);
      this.tip(rail, 'Stamp holder rails: for stacking it goes up, then left, until the stamp is under the objective; back out: right, then down');
    }
  }


  buildMoving() {
    const k = this.k, cfg = this.cfg;
    if (cfg.reactor_module) {
      const geo = cfg.reactor_module.geometry, p = geo.load;
      const v2 = cfg.reactor_module.mechanics_version >= 2;
      const assemblyParent = this.reactorAssembly || this.gStage;
      this.reactorMechanism = svg('g', { class: 's-reactor-mechanism' }, assemblyParent);
      const rodLength = geo.rod_length_mm || 96;
      const rail = this.rect({ x: [p.x - 11, p.x + 11], y: [p.y - rodLength - 12, p.y - 20] },
        { class: 's-travel', rx: 3 }, this.reactorMechanism);
      this.tip(rail, 'Insertion carriage and guide, carried by the XY assembly');
      this.reactorCarrier = svg('g', { class: 's-reactor-carrier', 'data-device': v2 ? 'reactor_insert' : 'reactor_slot' }, assemblyParent);
      const rw = geo.rod_width_mm || 5;
      this.rect({ x: [p.x - rw / 2, p.x + rw / 2], y: [p.y - rodLength, p.y] },
        { class: 's-reactor-rod', rx: 1 }, this.reactorCarrier);
      const [sw, sh] = geo.carrier_slot_size_mm || [5, 5];
      this.rect({ x: [p.x - sw / 2 - 1.5, p.x + sw / 2 + 1.5], y: [p.y - sh / 2 - 1.5, p.y + sh / 2 + 1.5] },
        { class: 's-stage moving', rx: 1 }, this.reactorCarrier);
      this.rect({ x: [p.x - sw / 2, p.x + sw / 2], y: [p.y - sh / 2, p.y + sh / 2] },
        { class: 's-slot', rx: 0.5 }, this.reactorCarrier);
      this.reactorTip = svg('title', {}, this.reactorCarrier);
      this.reactorObjective = this.objective('reactor_microscope', geo.microscope_center);
      if (v2) this.text(this.reactorObjective.g, this.sx(geo.microscope_center.x),
        this.sy(geo.microscope_center.y + 58), 'FIXED MICROSCOPE', 's-fixed-optics', 3 * k, 'middle');
      this.devEls.set('reactor_slot', this.reactorAssembly ? [this.reactorAssembly] : [this.reactorChamber, this.reactorMechanism]);
      this.devEls.set('reactor_insert', [this.reactorCarrier]);
    }
    // stage plate (moves) and the fixed microscope above it
    const st = cfg.stations.transfer_stage;
    if (st && st.region) {
      this.plate = svg('g', { class: 's-plate' }, this.gStage);
      this.devEls.set('transfer_stage', [this.plate]);
      const p = this.rect(st.region, { rx: 1.5 * k, class: 's-stage moving', style: `stroke-width:${0.45 * k}` }, this.plate);
      this.plateTip = svg('title', {}, p);
      const c = (cfg.microscope || {}).center;
      if (c) {
        const objective = this.objective('microscope', c);
        this.objTip = objective.tip;
        this.objLabel = objective.label;
      }
    }
    if (cfg.exfoliator) {
      const tw = cfg.exfoliator.tape_width_mm || 25;
      this.tape = svg('rect', { height: tw, class: 's-tape-band' }, this.gTape);
      this.tapeTip = svg('title', {}, this.tape);
      this.pressed = svg('rect', { class: 's-pressed' }, this.gTape);
      const o = exfoliatorOrigin(cfg), fixed = this.roll(this.gTape, false);
      this.placeRoll(fixed, o.x, o.y);
      this.tip(fixed.g, 'Tape roll on its slider (stays put); its axis lies along y, so from above it is a bar across the tape');
      this.mroll = this.roll(this.gTape, true);
      this.devEls.set('exfoliator', [fixed.g, this.mroll.g, this.tape]);
      this.rollerTip = svg('title', {}, this.mroll.g);
    }
    if (cfg.transfer_arm) {
      // the holder body with its vacuum hole; a stamp's slide lies on it (drawn as an object)
      const R = (r, cls, parent, extra = {}) => svg('rect', { x: r.x[0], y: -r.y[1], width: r.x[1] - r.x[0], height: r.y[1] - r.y[0], class: cls, ...extra }, parent);
      this.holder = svg('g', { class: 's-holder' }, this.gHolder);
      this.devEls.set('transfer_arm', [this.holder]);
      this.holderShadow = R(BODY, 's-holder-shadow', this.holder, { rx: 2, filter: `url(#${this.uid}-sshadow)` });
      this.holderBody = R(BODY, 's-holder-body', this.holder, { rx: 2, style: `stroke-width:${0.5 * k}` });
      svg('circle', { cx: VAC_X, cy: 0, r: 4, class: 's-vac', style: `stroke-width:${0.5 * Math.min(k, 2)}` }, this.holder);
      this.contact = svg('circle', { r: 0, class: 's-contact' }, this.holder);
      this.holderTip = svg('title', {}, this.holder);
    }
  }

  // Both microscopes use the same body, crosshair, field and objective label.
  objective(id, c) {
    const k = this.k, g = svg('g', { class: 's-objective', 'data-device': id }, this.gArm);
    const cx = this.sx(c.x), cy = this.sy(c.y), r = 26, tk = 7;
    this.devEls.set(id, [g]);
    const tip = svg('title', {}, g);
    svg('circle', { cx, cy, r, class: 's-scope', style: `stroke-width:${0.6 * k}` }, g);
    svg('path', { d: `M${cx - r - tk} ${cy}h${2 * tk}M${cx + r - tk} ${cy}h${2 * tk}M${cx} ${cy - r - tk}v${2 * tk}M${cx} ${cy + r - tk}v${2 * tk}`,
      class: 's-cross', style: `stroke-width:${0.6 * k}` }, g);
    svg('circle', { cx, cy, r: 3.2, class: 's-fov', style: `stroke-width:${0.45 * Math.min(k, 2)};stroke-dasharray:${0.8 * Math.min(k, 2)} ${0.6 * Math.min(k, 2)}` }, g);
    const label = this.text(g, cx + r * 0.78 + 1.5 * k, cy - r * 0.78 - k, '', 's-part', 3.4 * k);
    return { g, tip, label };
  }

  object(id, o, parent = this.gObjects) {
    let e = this.objs.get(id);
    if (!e) {
      const g = svg('g', { class: 'b-obj' }, this.gObjects);
      const r = svg('rect', {}, g);
      const dome = svg('circle', { class: 'b-dome', r: 0 }, g);
      const flakes = svg('circle', { class: 'b-flakes' }, g);
      const tip = svg('title', {}, g);
      e = { g, r, dome, flakes, tip, key: '' };
      this.objs.set(id, e);
    }
    if (e.g.parentNode !== parent) parent.appendChild(e.g);
    if (this.observed) {
      // Evidence markers have no inferred chip/slide dimensions, mount, film or
      // hidden inventory identity. A point locates only its recorded coordinate.
      e.point ||= svg('circle', {class: 's-observed-point', r: 2.4 * this.k,
        fill: '#3979aa', stroke: '#173e59', 'stroke-width': 0.6 * this.k}, e.g);
      e.label ||= svg('text', {x: 4 * this.k, y: -3 * this.k,
        fill: 'currentColor', style: `font-size:${3.2 * this.k}px`}, e.g);
      for (const node of [e.r, e.dome, e.flakes]) node.setAttribute('display', 'none');
      e.point.removeAttribute('display'); e.label.removeAttribute('display');
      e.g.setAttribute('class', 'b-obj observed-point');
      e.g.setAttribute('data-object-key', id);
      e.g.setAttribute('data-identity-status', o.identity_status || 'unidentified');
      e.label.textContent = o.label || o.kind || 'Observed object';
      e.tip.textContent = `${e.label.textContent} · ${o.identity_status || 'unidentified'} · dimensions unknown`;
      e.g.setAttribute('transform', `translate(${this.sx(o.x).toFixed(2)} ${this.sy(o.y).toFixed(2)})`);
      e.key = '';
      return;
    }
    for (const node of [e.r, e.dome, e.flakes]) node.removeAttribute?.('display');
    e.point?.setAttribute('display', 'none'); e.label?.setAttribute('display', 'none');
    e.g.removeAttribute?.('data-object-key'); e.g.removeAttribute?.('data-identity-status');
    const [w, h] = o.size_mm || HOLD_MM[o.kind] || HOLD_MM.chip;
    // a stamp's status (new / used / torn): the ledger's (statusOf, set by the page) if known
    const st = (o.kind === 'stamp' ? this.statusOf?.(id) : undefined) ?? o.status;
    const stamp = o.kind === 'stamp' ? this.stampOf?.(id) : null;
    const film = stamp?.props && Object.hasOwn(stamp.props, 'film') ? stamp.props.film : o.film;
    const key = `${o.kind}|${w}|${h}|${o.held_by}|${o.askew}|${st}|${film}|${o.n_flakes}|${o.material}`;
    if (key !== e.key) {
      e.key = key;
      e.g.setAttribute('class', `b-obj ${o.kind || 'chip'}` + (o.held_by ? ' held' : '') + (o.askew ? ' askew' : '') + (st === 'used' || st === 'torn' ? ` ${st}` : ''));
      Object.entries({ x: -w / 2, y: -h / 2, width: w, height: h, rx: Math.min(w, h) * 0.08, style: `stroke-width:${0.35 * Math.min(this.k, 2)}` })
        .forEach(([a, v]) => e.r.setAttribute(a, v));
      const fr = Math.min(w, h) * 0.14;
      e.flakes.setAttribute('r', o.n_flakes && (o.kind === 'chip' || o.kind === 'stamp') ? fr : 0);
      e.flakes.setAttribute('cx', w / 2 - fr * 1.1); e.flakes.setAttribute('cy', -h / 2 + fr * 1.1);
      // a stamp's PDMS dome sits near the slide's left end (front end of an upright one)
      e.dome.setAttribute('r', o.kind === 'stamp' ? DOME_R : 0);
      e.dome.setAttribute('cx', w > h ? -w / 2 + DOME_INSET : 0); e.dome.setAttribute('cy', w > h ? 0 : h / 2 - DOME_INSET);
      const kind = { chip: 'chip', carrier: 'material slide (carrier)', stamp: 'stamp (glass slide with a PDMS dome)', device: 'device' }[o.kind] || o.kind;
      e.tip.textContent = `${id} · ${kind}${o.material ? ` · ${o.material}` : ''}${o.kind === 'stamp' && st ? ` · ${stampStatusText(st, film)}` : ''}${o.n_flakes ? ` · ${o.n_flakes} flakes` : ''}${o.held_by ? ` · held by ${o.held_by.replace(/_/g, ' ')}` : ''}${o.askew ? ' · askew' : ''}`;
    }
    e.g.setAttribute('transform', `translate(${this.sx(o.x).toFixed(2)} ${this.sy(o.y).toFixed(2)}) rotate(${(-(o.rotation || 0)).toFixed(2)})`);
  }

  arm(id, a, offline) {
    const k = this.k;
    let e = this.arms.get(id);
    const side = id === 'arm_left' ? ' left' : '';
    if (!e) {
      e = { link: svg('path', { class: 's-link' + side, style: `stroke-width:${2.4 * k}` }, this.gArm),
        halo: svg('circle', { class: 'b-nozzle-halo' + side, style: `stroke-width:${0.35 * k}` }, this.gArm),
        noz: svg('circle', { r: 2.4 * Math.min(k, 2), class: 'b-nozzle' + side, style: `stroke-width:${0.8 * Math.min(k, 2)}` }, this.gArm),
        lbl: this.text(this.gArm, 0, 0, '', 'b-arm-label' + side, 3.6 * k), key: '' };
      this.tip(e.noz, `${ARM_LABELS[id] || id}: suction nozzle (the halo grows with its height)`);
      this.arms.set(id, e);
      this.devEls.set(id, [e.link, e.halo, e.noz]);
      if (this.lit.has(id)) for (const n of this.devEls.get(id)) n.classList.add('s-lit');
    }
    const ax = this.sx(a.x), ay = this.sy(a.y), base = this.cfg[id] && this.cfg[id].base;
    if (base) {
      // the link leaves the base square at its edge
      const bx = this.sx(base.x), by = this.sy(base.y), half = BASE_R * k;
      const m = Math.max(Math.abs(ax - bx), Math.abs(ay - by)), t = m > half ? half / m : 1;
      e.link.setAttribute('d', m > half ? `M${(bx + (ax - bx) * t).toFixed(2)} ${(by + (ay - by) * t).toFixed(2)}L${ax.toFixed(2)} ${ay.toFixed(2)}` : '');
    }
    const rr = (3 + Math.max(0, Math.min(120, a.z || 0)) / 14) * Math.min(k, 2.2);
    e.halo.setAttribute('cx', ax); e.halo.setAttribute('cy', ay); e.halo.setAttribute('r', rr);
    e.noz.setAttribute('cx', ax); e.noz.setAttribute('cy', ay);
    // The third arm parks between the right storage board and the reactor
    // frame, so its caption sits centred above the nozzle instead of beside it.
    const labelAbove = id === 'arm_reactor' && !!this.reactorAssembly;
    e.lbl.setAttribute('text-anchor', labelAbove ? 'middle' : 'start');
    e.lbl.setAttribute('x', labelAbove ? ax : ax + rr + 2.5 * k);
    e.lbl.setAttribute('y', labelAbove ? ay - rr - 1.5 * k : ay - rr);
    const off = offline ? ' off' : '', vac = a.vacuum ? ' vac' : '';
    const text = offline ? `${ARM_LABELS[id] || id} · offline` : a.homed === false ? `${ARM_LABELS[id] || id} · not homed`
      : `${ARM_LABELS[id] || id} · z ${Math.round(a.z || 0)}${a.vacuum ? ' · holding' : ''}`;
    const key = off + vac + text;
    if (key !== e.key) {
      e.key = key;
      const lit = this.lit.has(id) ? ' s-lit' : '';
      e.link.setAttribute('class', 's-link' + side + off + lit);
      e.halo.setAttribute('class', 'b-nozzle-halo' + side + off + lit);
      e.noz.setAttribute('class', 'b-nozzle' + side + vac + off + lit);
      e.lbl.setAttribute('class', 'b-arm-label' + side + off);
      e.lbl.textContent = text;
    }
  }

  // A soft glow on these devices (bench ids, e.g. ['transfer_arm']) until the
  // next call; [] clears. Nothing else is redrawn.
  highlight(ids = []) {
    this.lit = new Set(ids);
    for (const [id, nodes] of this.devEls) for (const n of nodes) n.classList.toggle('s-lit', this.lit.has(id));
  }

  // view: {arms, stages, manipulator, exfoliator, microscope, objects}; offline: Set of device ids
  render(view, offline = new Set(), {observed = false} = {}) {
    const k = this.k, cfg = this.cfg;
    this.observed = observed;
    for (const node of [this.gStage, this.gTape, this.gHolder, this.gArm,
      this.reactorAssembly, this.reactorChamber, this.reactorMechanism, this.reactorCarrier]) {
      if (observed) node?.setAttribute('display', 'none');
      else node?.removeAttribute('display');
    }
    if (observed) {
      const seen = new Set();
      for (const [id, o] of Object.entries(view.objects || {})) {
        if (!o || !Number.isFinite(o.x) || !Number.isFinite(o.y)) continue;
        seen.add(id);
        this.object(id, o);
      }
      for (const [id, e] of this.objs) if (!seen.has(id)) { e.g.remove(); this.objs.delete(id); }
      return;
    }
    if (this.reactorCarrier) {
      const p = view.stages?.reactor_slot || { x: 0, y: 0 };
      const v2 = cfg.reactor_module.mechanics_version >= 2;
      const insert = v2 ? (view.stages?.reactor_insert?.y || 0) : 0;
      const assembly = v2 ? p : { x: 0, y: 0 };
      const transform = `translate(${+assembly.x || 0} ${-(+assembly.y || 0)})`;
      this.reactorChamber?.setAttribute('transform', transform);
      if (this.reactorAssembly) {
        // Rod motion is local to the common assembly; XY is inherited once.
        this.reactorCarrier.setAttribute('transform', `translate(0 ${-insert})`);
        this.reactorAssemblyReadout.textContent = `X ${fmt1(p.x)} · Y ${fmt1(p.y)} mm`;
      } else {
        this.reactorMechanism.setAttribute('transform', transform);
        this.reactorCarrier.setAttribute('transform', `translate(${+p.x || 0} ${-((+p.y || 0) + insert)})`);
      }
      this.reactorTip.textContent = v2
        ? `Reactor assembly · bottom X ${fmt1(p.x)} · top Y ${fmt1(p.y)} mm · separate insertion ${fmt1(insert)} mm`
        : `Legacy XY carrier · X ${fmt1(p.x)} · Y ${fmt1(p.y)} mm`;
      const m = view.reactor_microscope || {};
      this.reactorObjective.label.textContent = `${m.objective || 10}×`;
      this.reactorObjective.tip.textContent = `Reactor microscope · fixed XY · ${m.objective || 10}× · Z focus ${fmt1(m.focus_z)} mm`;
    }
    const stage = (view.stages || {}).transfer_stage || { x: 0, y: 0 };
    const modern = (view.geometry?.mechanics_version ?? cfg.transfer_stage?.mechanics_version ?? 1) >= 2;
    if (this.plate) {
      const load = (cfg.transfer_stage || {}).load || { x: 0, y: 0 };
      const fine = modern ? stage.fine_xy_mm || [0, 0] : [0, 0];
      const station = cfg.stations.transfer_stage;
      const px = station.x ?? (station.region.x[0]+station.region.x[1])/2;
      const py = station.y ?? (station.region.y[0]+station.region.y[1])/2;
      this.plate.setAttribute('transform', `translate(${((+stage.x || 0)+(+fine[0] || 0)-(+load.x || 0)).toFixed(3)} ${(-((+stage.y || 0)+(+fine[1] || 0)-(+load.y || 0))).toFixed(3)})`
        + (modern ? ` rotate(${-(stage.rotation || 0)} ${this.sx(px)} ${this.sy(py)})` : ''));
      const hot = this.heat && this.heat.transfer_stage;
      this.plate.classList.toggle('hot', !!hot);
      this.plateTip.textContent = `Stage · coarse x ${fmt1(stage.x)} · y ${fmt1(stage.y)} · z ${stage.z != null ? (+stage.z).toFixed(2) : '?'} mm`
        + (modern ? ` · fine XY ${fmt1(fine[0])}, ${fmt1(fine[1])} mm · rotation ${fmt1(stage.rotation)}° · Z is mechanical height; microscope Z focuses`
          : ' (legacy stage Z focuses the chip)');
    }
    if (this.objLabel) {
      const m = view.microscope || {}, obj = m.objective;
      this.objLabel.textContent = obj ? `${obj}×` : '';
      this.objTip.textContent = `Microscope (${modern ? `fixed XY · independent Z focus ${fmt1(m.focus_z)} mm` : 'legacy fixed focus'})${obj ? ` · ${obj}× objective` : ''}${m.focus_plane ? ` · ${m.focus_plane} in focus` : ''}: the field of view is under the crosshair`;
    }
    if (this.tape) {
      const ex = view.exfoliator || {}, o = exfoliatorOrigin(cfg);
      const rolls = ex.rolls || {};
      const rx = (rolls.movable || {}).x ?? ex.roller_x ?? (o.x + (ex.gantry_x || 0));
      const ry = (rolls.movable || {}).y ?? o.y;
      const park = (cfg.exfoliator || {}).park_height_mm ?? 78;
      const z = (rolls.movable || {}).z ?? (park + (ex.gantry_z || 0));  // movable roll height above the stage top
      const low = Math.max(0, Math.min(1, 1 - z / park));               // 0 parked high, 1 down at the stage
      const tw = cfg.exfoliator.tape_width_mm || 25;
      const x0 = Math.min(rx, o.x), x1 = Math.max(rx, o.x);
      this.tape.setAttribute('x', this.sx(x0)); this.tape.setAttribute('width', Math.max(0.01, x1 - x0));
      this.tape.setAttribute('y', this.sy(o.y) - tw / 2);
      this.tape.setAttribute('class', 's-tape-band' + (ex.applied ? ' applied' : '') + (ex.laid && !ex.applied ? ' laid' : '') + (this.lit.has('exfoliator') ? ' s-lit' : ''));
      this.tapeTip.textContent = `White tape between the rolls${ex.applied ? ' · stuck to the chip' : ex.laid ? ' · laid over the chip, not pressed yet' : ''}${ex.roll_left_mm != null ? ` · ${tapeText(ex.roll_left_mm)} left on the roll` : ''}`;
      // the pressed part of the tape on the stage (the rubber tip's strokes)
      const st = cfg.stations.exfoliation_stage, pf = ex.applied ? (ex.pressed_frac ?? 1) : 0;
      if (st && st.region) {
        const r = st.region, pw = (r.x[1] - r.x[0]) * 0.6 * pf, ph = (r.y[1] - r.y[0]) * 0.6;
        this.pressed.setAttribute('x', this.sx((r.x[0] + r.x[1]) / 2 - pw / 2)); this.pressed.setAttribute('y', this.sy((r.y[0] + r.y[1]) / 2) - ph / 2);
        this.pressed.setAttribute('width', Math.max(0, pw)); this.pressed.setAttribute('height', ph);
      }
      const [ux, uy] = this.placeRoll(this.mroll, rx, ry);
      // the higher the roll, the further its shadow falls from it
      const lift = (1 - low) * 3.5;
      this.mroll.shadow.setAttribute('x', ux + lift * 0.6); this.mroll.shadow.setAttribute('y', uy + lift);
      this.mroll.shadow.style.opacity = (0.18 + 0.32 * low).toFixed(2);
      this.mroll.body.style.opacity = (0.75 + 0.25 * (1 - low)).toFixed(2);
      this.rollerTip.textContent = `Tape roll on its slider (moves along the rail and up / down) · x ${fmt1(rx - o.x)} · ${fmt1(z)} mm above the stage${ex.peel_angle_deg != null ? ` · last peel ${fmt1(ex.peel_angle_deg)}°` : ''}`;
    }
    let onHolder = null, seat = null;
    if (this.holder) {
      const m = view.manipulator || {}, hp = m.head || headPoint(cfg, m), dome = stampDomeOffset(cfg);
      seat = { x: hp.x - dome.x, y: hp.y - dome.y };  // slide centre of a stamp on the holder
      // a stamp the holder holds, or one lying on it (put down at the hand-off, vacuum still off)
      for (const [id, o] of Object.entries(view.objects || {})) {
        if (o && o.kind === 'stamp' && (o.held_by === 'transfer_arm'
          || (!o.held_by && Number.isFinite(o.x) && Math.hypot(o.x - seat.x, o.y - seat.y) < 3))) onHolder = id;
      }
      this.holder.setAttribute('transform', `translate(${this.sx(hp.x).toFixed(2)} ${this.sy(hp.y).toFixed(2)})`);
      const ta = cfg.transfer_arm, z = m.z ?? ta.safe_z ?? 6.5, low = z < (ta.approach_z ?? 5.4) + 0.05;
      this.holderShadow.setAttribute('transform', `translate(${(1.5 + Math.max(0, z - 4.5) * 0.8).toFixed(2)} ${(2 + Math.max(0, z - 4.5) * 1.1).toFixed(2)})`);
      this.holder.setAttribute('class', 's-holder' + (low ? ' low' : '') + (m.vacuum ? ' gripping' : '') + (offline.has('transfer_arm') ? ' off' : '')
        + (this.lit.has('transfer_arm') ? ' s-lit' : ''));
      const ct = m.contact;
      this.contact.setAttribute('r', ct && ct.radius_mm ? Math.max(1.5, ct.radius_mm) : 0);
      this.holderTip.textContent = `Stamp holder (x/y/z stage) · ${m.l != null ? `l ${fmt1(m.l)} · ` : ''}x ${fmt1(m.x)} · y ${fmt1(m.y)} · z ${m.z != null ? (+m.z).toFixed(2) : '?'} mm`
        + (onHolder ? ` · ${onHolder} on it${m.vacuum ? ', held by vacuum' : ''}` : m.vacuum ? ' · vacuum on' : '') + (m.over_stage ? ' · over the stage' : '')
        + (ct ? ` · pressing ${ct.depth_mm != null ? `${(+ct.depth_mm).toFixed(2)} mm` : ''} on ${ct.chip || 'the chip'}` : '');
    }
    const seen = new Set();
    for (const [id, o] of Object.entries(view.objects || {})) {
      if (!o || !Number.isFinite(o.x) || !Number.isFinite(o.y)) continue;
      seen.add(id);
      // a stamp the holder grips rides on it: drawn at the seat from the holder's
      // own tween, so the two move together frame by frame. One lying on the
      // holder, or an object an arm carries (above the holder), is drawn on top
      // of the holder body too.
      const top = id === onHolder || (o.held_by && o.held_by !== 'exfoliator');
      if (id === onHolder && o.held_by === 'transfer_arm') this.object(id, { ...o, x: seat.x, y: seat.y }, this.gHolder);
      else this.object(id, o, top ? this.gHolder : this.gObjects);
    }
    for (const [id, e] of this.objs) if (!seen.has(id)) { e.g.remove(); this.objs.delete(id); }
    for (const [id, a] of Object.entries(view.arms || {})) if (a && Number.isFinite(a.x)) this.arm(id, a, offline.has(id));
  }
}
