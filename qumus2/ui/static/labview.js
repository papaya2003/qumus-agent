// Lab: left categories -> middle item cards -> right item details.
// Status groups stored instruments and supplies; Stock groups sample inventory.
// The bench map is an explicit overview category. Selecting a folder clears
// old details; selecting an item retains its sibling cards in the middle.
//
// "Needs a person" comes from the records only (stamp states, what is left in
// the boards, tape length, instrument states, jobs waiting); the thresholds
// are placeholders until the lab says what they should be.

import { h, svg, jobTitle, RUN_STATUS, fmt, tapeText, imgUrl } from './ui.js';
import { StationBenchView, armBase, holderPath } from './stations.js';
import { FlakeGrid, bestPhoto, hasPhoto, thickText, shortId } from './flakes.js';
import { chipCard, deviceCard, stampPill, whereText, STAMP_WORD, motherTapeCard, materialReview, samplePhoto } from './inventory.js';
import { sampleDetail } from './details.js';

const DEVICE_NAMES = { arm_left: 'Arm 1', arm_right: 'Arm 2', arm_reactor: 'Arm 3', transfer_arm: 'Stamp holder',
  transfer_stage: 'Microscope stage', exfoliator: 'Tape rolls', microscope: 'Microscope', overview_camera: 'Overview camera',
  transfer_stage_vacuum: 'Stage vacuum', transfer_arm_vacuum: 'Holder vacuum', exfoliation_vacuum: 'Exfoliation vacuum',
  exfoliation_heater: 'Exfoliation heater', exfoliation_fan: 'Exfoliation fan', stamp_heater: 'Stamp heater',
  transfer_stage_fan: 'Search & stacking fan',
  reactor_slot: 'Reactor assembly · bottom X / top Y', reactor_insert: 'Reactor rod · insert Y',
  reactor_microscope: 'Reactor microscope' };
export const devName = id => DEVICE_NAMES[id] || String(id || '').replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());

// the instruments as a person sees them: one row per machine, its parts inside
const GROUPS = [
  ['arm_left', 'Arm 1', ['arm_left']], ['arm_right', 'Arm 2', ['arm_right']], ['arm_reactor', 'Arm 3', ['arm_reactor']],
  ['exfoliation', 'Exfoliation', ['exfoliator', 'exfoliation_heater', 'exfoliation_fan', 'exfoliation_vacuum']],
  ['microscope', 'Microscope + stage', ['microscope', 'transfer_stage', 'transfer_stage_vacuum', 'transfer_stage_fan']],
  ['holder', 'Stamp holder', ['transfer_arm', 'transfer_arm_vacuum', 'stamp_heater']],
  ['cameras', 'Overview camera', ['overview_camera']],
  ['reactor', 'Experimental reactor', ['reactor_slot', 'reactor_insert', 'reactor_heater', 'reactor_flow', 'reactor_microscope']],
];
const RANK = { ok: 0, info: 1, warn: 2, danger: 3 };
// a command's arguments in a few words: "x 0 · y 110 · z 120 · rotation 0"
const argText = (params, depth = 0) => Object.entries(params || {}).filter(([, v]) => v != null).slice(0, 6).map(([k, v]) =>
  (typeof v === 'number' ? `${k.replace(/_/g, ' ')} ${+v.toFixed(1)}` : typeof v === 'boolean' ? `${k.replace(/_/g, ' ')} ${v ? 'on' : 'off'}`
    : v && typeof v === 'object' && !Array.isArray(v) && depth < 1 ? argText(v, depth + 1) : `${k.replace(/_/g, ' ')} ${String(v)}`)).join(' · ');
// placeholders until the lab sets them
const LOW_STAMPS = 1, LOW_TAPE = 0.1;
const OBSERVED_ROWS = 100;
const evidenceValue = value => {
  if (value == null) return 'unknown';
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value); } catch { return String(value); }
};
const evidenceSource = source => {
  if (Array.isArray(source)) return source.map(evidenceSource).join('; ');
  if (!source || typeof source !== 'object' || !source.kind) return evidenceValue(source);
  return [source.kind, source.reference, source.reporter && `reporter ${source.reporter}`,
    source.instruction_id && `instruction ${source.instruction_id}`,
    source.event_ids?.length && `source events ${source.event_ids.join(', ')}`,
    source.sha256 && `SHA-256 ${source.sha256}`].filter(Boolean).join(' · ');
};
const evidenceSourceSummary = source => {
  if (Array.isArray(source)) return source.map(evidenceSourceSummary).join('; ');
  if (!source || typeof source !== 'object') return 'unknown';
  const kind = {measured: 'Measurement', synthetic: 'Simulation', operator_report: 'Human report',
    historical_constant: 'Historical record', estimate: 'Estimate'}[source.kind] || 'unknown';
  return [kind, source.reporter].filter(Boolean).join(' · ');
};

// Read the recorded projection, never the animated diagnostic twin. A replay's
// first bench frame can be newer than its current event prefix after a seek.
export function recordedObservedScene(ctx) {
  const scene = ctx.bench()?.twin?.observed_scene;
  if (!scene || scene.schema_version !== 1 || scene.source !== 'stored_lab_evidence'
      || scene.uses_simulator_truth !== false) {
    return {scene: null, reason: 'No observed scene recorded at this point.'};
  }
  const events = ctx.s()?.events || [], cap = events.length ? events.at(-1).id : 0;
  if (!ctx.live() && (!Number.isFinite(scene.through_event_id) || !Number.isFinite(cap)
      || scene.through_event_id > cap)) {
    return {scene: null, reason: 'No observed scene recorded at this playback point.'};
  }
  return {scene, reason: null};
}

const evidenceBoundary = scene => h('div', {text: `Recorded boundary: event prefix ${scene.through_event_id} · as of ${evidenceValue(scene.as_of?.value)} (${scene.as_of?.timebase || 'unknown'}; ${scene.as_of?.basis || 'unknown'})`});

function evidenceDetails(evidence = {}, scene, alternatives = [], key) {
  const e = evidence || {}, freshness = e.freshness || {};
  const age = Number.isFinite(freshness.age_s) ? `${freshness.age_s.toFixed(1)} s` : 'unknown';
  const uncertainty = e.uncertainty ? [e.uncertainty.method,
    Object.entries(e.uncertainty.bounds || {}).map(([axis, range]) => `${axis} ${range[0]} to ${range[1]}`).join('; '),
    e.uncertainty.units].filter(Boolean).join(' · ') : 'unknown';
  const calibrated = e.calibration_id && !['unknown', 'unavailable'].includes(e.calibration_id.toLowerCase());
  return h('div', {class: 'small muted observed-provenance'},
    h('div', {text: `Source: ${evidenceSourceSummary(e.source)} · frame: ${e.frame || 'unknown'}`}),
    h('div', {text: `Freshness: ${freshness.status || 'unknown'} · age: ${age}`}),
    h('div', {text: `Calibration: ${calibrated ? 'recorded' : 'unknown'} · uncertainty: ${uncertainty}`}),
    h('details', {class: 'observed-evidence-details', dataset: {evidenceKey: JSON.stringify([key, e.assertion_ids])}}, h('summary', {text: 'Evidence details'}),
      h('div', {text: `Source: ${evidenceSource(e.source)}`}),
      h('div', {text: `Observed time: ${evidenceValue(e.observed_at)} · timebase: ${e.timebase || 'unknown'}`}),
      h('div', {text: `Freshness evaluated as of: ${evidenceValue(freshness.as_of)} · stale after: ${evidenceValue(freshness.stale_after_s)} s`}),
      h('div', {text: `Calibration: ${e.calibration_id || 'unknown'} · version: ${e.calibration_version || 'unknown'} · uncertainty: ${evidenceValue(e.uncertainty)}`}),
      h('div', {text: `Assertions: ${(e.assertion_ids || []).join(', ') || 'unknown'} · events: ${(e.event_ids || []).join(', ') || 'unknown'}`}),
      scene ? evidenceBoundary(scene) : null,
      e.omitted_assertions > 0 ? h('div', {text: `${e.omitted_assertions} additional assertions omitted from these details.`}) : null,
      e.transform ? h('div', {text: `Supported transform: ${evidenceValue(e.transform)}`}) : null,
      ...alternatives.slice(0, 8).map(alternative => h('div', {
        text: `Alternative source: ${evidenceSource(alternative.source)} · frame: ${alternative.frame || 'unknown'}`}))));
}

export class LabView {
  // ctx: { s() the Session on screen; bench() the last bench answer; view() the
  // bench drawing's state as drawn now; state() where it is going (the
  // instruments' states read that); live(); changed(part); showRight(); showCentre() }
  constructor(ctx) {
    this.ctx = ctx;
    this.tab = 'status';
    this.sel = null;          // { t: 'inst' | 'supply' | 'sample', id }
    // Status folder; selection is always an item inside it. Lab opens on the
    // bench overview instead of an empty middle column (2026-11-11).
    this.category = { t: 'overview', id: 'bench' };
    this.cat = 'all';         // Stock: the category on the left
    this.query = '';
    this.mat = null;
    this.map = null;          // the lab map (a second bench drawing)
    this.mapMode = null;      // choose once the bench's simulation provenance is available
    this.mapFrame = 'world';
    this.grid = new FlakeGrid({
      list: () => this.ctx.s().inv.of('flake'),
      selected: () => (this.sel && this.sel.t === 'sample' ? this.sel.id : null),
      select: id => this.select({ t: 'sample', id }),
      roleOf: id => this.ctx.s().roleOf(id), scoreOf: id => this.ctx.s().scoreOf(id),
    });
  }

  reset() { this.sel = null; this.category = { t: 'overview', id: 'bench' }; }
  setTab(t) { if (t !== this.tab) { this.tab = t; this.sel = null; } this.ctx.changed('all'); }
  select(item) {
    if (item.t === 'inst' || item.t === 'supply') { this.selectCategory(item); return; }
    this.sel = item; this.ctx.changed('all'); this.ctx.showRight();
  }
  selectCategory(item) {
    this.category = item; this.sel = null;
    this.ctx.changed('all'); this.ctx.showCentre?.();
  }
  isCategory(t, id) { return this.category?.t === t && this.category?.id === id; }
  isSel(t, id) { return !!this.sel && this.sel.t === t && this.sel.id === id; }

  // ------------------------------------------------------------ instruments
  devState(d) {
    const v = (this.ctx.state || this.ctx.view)() || {}, L = (this.ctx.bench() || {}).layout || {};
    const held = id => Object.entries(v.objects || {}).find(([, o]) => o.held_by === id)?.[0];
    if (d.offline) return ['danger', 'offline'];
    if (d.stuck_at != null) return ['danger', `stuck at ${d.stuck_at}`];
    if (d.error) return ['danger', 'error'];
    if (d.temperature != null && (d.target != null || d.temperature > 35)) return ['warn', `${fmt(d.temperature, 1)} °C${d.target != null ? ` → ${d.target} °C` : ''}`];
    if (d.flow_sccm != null) return [d.flow_sccm > 0.5 ? 'info' : 'ok', `${fmt(d.flow_sccm, 1)} sccm · target ${fmt(d.flow_target_sccm, 1)}`];
    const a = (v.arms || {})[d.id];
    if (a && a.homed === false) return ['warn', 'not homed'];
    if (a && a.vacuum) return ['info', `holding ${held(d.id) || 'something'}`];
    if (a) return ['ok', 'homed'];
    if (d.id === 'transfer_arm' && v.manipulator) {
      const m = v.manipulator, ta = L.transfer_arm || {};
      if (m.vacuum) return ['info', `holding ${held('transfer_arm') || 'a stamp'}`];
      if (m.z != null && ta.approach_z != null && m.z < ta.approach_z) return ['warn', 'lowered'];
      return ['ok', 'raised'];
    }
    if (d.id === 'exfoliator') {
      const ex = v.exfoliator || {}, roll = (L.exfoliator || {}).roll_length_mm, left = ex.roll_left_mm ?? d.roll_left_mm;
      if (left != null && roll && left < LOW_TAPE * roll) return ['warn', `tape low · ${Math.round(left)} mm`];
      if (ex.applied) return ['info', 'tape on the stage'];
      return ['ok', left != null ? `${tapeText(left)} tape` : 'ready'];
    }
    if (d.id === 'microscope') return ['ok', d.objective || (v.microscope || {}).objective ? `${d.objective || v.microscope.objective}× objective` : 'online'];
    if (d.on != null) return [d.on ? 'info' : 'ok', d.on ? 'on' : 'off'];
    if (d.temperature != null) return ['ok', `${fmt(d.temperature, 1)} °C`];
    return ['ok', 'online'];
  }

  instruments() {
    const devs = (this.ctx.bench() || {}).devices || [];
    const byId = new Map(devs.map(d => [d.id, d]));
    const seen = new Set(), out = [];
    for (const [key, name, ids] of GROUPS) {
      const parts = ids.map(id => byId.get(id)).filter(Boolean);
      if (!parts.length) continue;
      parts.forEach(d => seen.add(d.id));
      out.push(this.group(key, name, parts));
    }
    for (const d of devs) if (!seen.has(d.id)) out.push(this.group(d.id, devName(d.id), [d]));
    return out;
  }
  group(key, name, parts) {
    const states = parts.map(d => ({ d, st: this.devState(d) }));
    const worst = states.reduce((a, b) => (RANK[b.st[0]] > RANK[a.st[0]] ? b : a), states[0]);
    const text = worst.st[0] === 'ok' ? states.map(x => x.st[1]).filter(t => t !== 'off' && t !== 'online').slice(0, 2).join(' · ') || 'online' : `${parts.length > 1 ? `${devName(worst.d.id).toLowerCase()} ` : ''}${worst.st[1]}`;
    return { key, name, parts: states, cls: worst.st[0], text, n: parts.length };
  }

  supplies() {
    const inv = this.ctx.s().inv, v = (this.ctx.state || this.ctx.view)() || {}, b = this.ctx.bench() || {}, L = b.layout || {};
    const chips = inv.of('chip'), stamps = inv.of('stamp');
    const blank = chips.filter(c => !inv.isTarget(c) && c.status === 'bare');
    const targets = chips.filter(c => inv.isTarget(c));
    const free = targets.filter(c => !inv.of('device').some(d => d.location === c.id));
    const fresh = stamps.filter(x => x.status === 'fresh');
    const ex = v.exfoliator || {}, dev = (b.devices || []).find(d => d.id === 'exfoliator') || {};
    const left = ex.roll_left_mm ?? dev.roll_left_mm, roll = (L.exfoliator || {}).roll_length_mm;
    const out = [
      { id: 'materials', name: 'Mother tapes', value: inv.of('carrier').map(x => x.props.material || '?').join(' · ') || 'none', cls: 'ok', items: inv.of('carrier') },
      { id: 'blank', name: 'Blank chips', value: `${blank.length} left`, cls: blank.length ? 'ok' : 'warn', items: blank },
      { id: 'targets', name: 'Target chips', value: `${free.length} free of ${targets.length}`, cls: free.length ? 'ok' : 'warn', items: targets },
      { id: 'stamps', name: 'New stamps', value: `${fresh.length} of ${stamps.length}`, cls: fresh.length > LOW_STAMPS ? 'ok' : 'warn', items: stamps },
    ];
    if (left != null) out.push({ id: 'tape', name: 'Tape roll', value: tapeText(left), cls: roll && left < LOW_TAPE * roll ? 'warn' : 'ok', items: [] });
    return inv.known ? out : out.slice(-1);
  }

  // what needs a person, from the records only; `at` places its pin on the map
  needs() {
    const s = this.ctx.s(), inv = s.inv, L = (this.ctx.bench() || {}).layout || {}, out = [];
    const slot = loc => (L.slots || {})[loc];
    const station = id => { const r = ((L.stations || {})[id] || {}).region; return r ? { x: (r.x[0] + r.x[1]) / 2, y: (r.y[0] + r.y[1]) / 2 } : null; };
    for (const g of this.instruments()) {
      for (const { d, st } of g.parts) {
        if (st[0] === 'danger') out.push({ cls: 'danger', title: `${devName(d.id)} ${st[1]}`, text: 'check the instrument; jobs that need it stop', at: this.instPos(g.key), sel: { t: 'inst', id: g.key } });
        else if (st[1] === 'not homed') out.push({ cls: 'warn', title: `${devName(d.id)} not homed`, text: 'the safety layer refuses its moves until it homes', at: this.instPos(g.key), sel: { t: 'inst', id: g.key } });
      }
    }
    for (const r of s.book.runs.values()) {
      if (r.status === 'waiting_human' || r.status === 'parked') out.push({ cls: 'warn', title: `${jobTitle(r.flow, r.params)}: ${RUN_STATUS[r.status]}`, text: r.message || 'see the conversation', at: null });
    }
    for (const x of inv.of('carrier')) if (materialReview(x).status === 'uncertain') {
      out.push({cls: 'warn', title: `${x.id} · material identity uncertain`, text: 'Check the physical label or preparation record; excluded from exfoliation.',
        at: slot(x.location), sel: {t: 'sample', id: x.id}});
    }
    for (const x of inv.of('stamp')) {
      if (x.status === 'torn') out.push({ cls: 'warn', title: `${x.id} · PC film torn`, text: `${whereText(x, inv)} · put a new film on it, then "New PC film" in its details`, at: slot(x.location), sel: { t: 'sample', id: x.id } });
    }
    for (const sp of this.supplies()) {
      if (sp.cls !== 'warn') continue;
      if (sp.id === 'blank') out.push({ cls: 'warn', title: 'No blank chips', text: 'add blank chips before the next exfoliation', at: station('left_storage'), sel: { t: 'supply', id: 'blank' } });
      if (sp.id === 'targets') out.push({ cls: 'warn', title: 'No free target chip', text: 'add a chip with electrodes before the next stacking', at: station('right_storage'), sel: { t: 'supply', id: 'targets' } });
      if (sp.id === 'stamps') {
        const fresh = sp.items.filter(x => x.status === 'fresh');
        out.push({ cls: 'warn', title: fresh.length ? `1 new stamp left · ${fresh[0].id}` : 'No new stamp', text: fresh.length ? 'enough for one more stacking' : 'put a new PC film on a used stamp', at: station('right_storage'), sel: { t: 'supply', id: 'stamps' } });
      }
      if (sp.id === 'tape') out.push({ cls: 'warn', title: `Tape low · ${sp.value}`, text: 'change the tape roll', at: station('exfoliation_stage'), sel: { t: 'inst', id: 'exfoliation' } });
    }
    return out;
  }

  instPos(key) {
    const L = (this.ctx.bench() || {}).layout || {};
    if (key.startsWith('arm_')) return L[key] && !L[key].planned ? armBase(L, key) : null;
    const st = id => { const r = ((L.stations || {})[id] || {}).region; return r ? { x: (r.x[0] + r.x[1]) / 2, y: (r.y[0] + r.y[1]) / 2 } : null; };
    if (key === 'exfoliation') return st('exfoliation_stage');
    if (key === 'microscope') return (L.microscope || {}).center || st('transfer_stage');
    if (key === 'holder') { const p = holderPath(L)[0]; return p || null; }
    if (key === 'reactor') return st('reactor');
    if (key === 'cameras') { const t = L.table; return t ? { x: t.x[0] + 30, y: t.y[1] - 30 } : null; }
    return null;
  }

  // ------------------------------------------------------------ left
  renderIndex(box) {
    const rows = [];
    if (this.tab === 'status') {
      rows.push(h('button', {class: 'drow' + (this.isCategory('overview', 'bench') ? ' on' : ''),
        onclick: () => this.selectCategory({t: 'overview', id: 'bench'})}, h('span', {class: 'dt', text: 'Bench overview'})));
      const inst = this.instruments(), normal = inst.filter(g => g.cls === 'ok' || g.cls === 'info').length;
      rows.push(h('div', { class: 'sec-t', text: inst.length ? `Instruments · ${normal} of ${inst.length} normal` : 'Instruments' }));
      rows.push(...(inst.length ? inst.map(g => h('button', { class: `drow inst ${g.cls}${this.isCategory('inst', g.key) ? ' on' : ''}`, onclick: () => this.selectCategory({ t: 'inst', id: g.key }) },
        h('i', { class: 'sdot ' + g.cls }), h('span', { class: 'dt', text: g.name }), h('span', { class: 'dn', text: g.text })))
        : [h('div', { class: 'placeholder', text: 'No instrument list yet.' })]));
      const sup = this.supplies();
      if (sup.length) {
        rows.push(h('div', { class: 'sec-t', text: 'Supplies' }));
        rows.push(...sup.map(x => h('button', { class: `drow ${x.cls}${this.isCategory('supply', x.id) ? ' on' : ''}`, onclick: () => this.selectCategory({ t: 'supply', id: x.id }) },
          h('span', { class: 'dt', text: x.name }), h('span', { class: 'dn ' + x.cls, text: x.value }))));
      }
    } else {
      const inv = this.ctx.s().inv;
      if (!inv.known) rows.push(h('div', { class: 'placeholder', text: 'This recording was made before the sample ledger was recorded as events.' }));
      else {
        const groups = inv.groups(), all = groups.reduce((n, g) => n + (g.key === 'flakes' ? 0 : g.items.length), 0);
        rows.push(h('button', { class: 'drow' + (this.cat === 'all' ? ' on' : ''), onclick: () => { this.sel = null; this.cat = 'all'; this.ctx.changed('all'); this.ctx.showCentre?.(); } },
          h('span', { class: 'dt', text: 'All' }), h('span', { class: 'dn', text: `${all} items` })));
        const sec = (t, keys) => {
          rows.push(h('div', { class: 'sec-t', text: t }));
          for (const g of groups.filter(x => keys.includes(x.key))) {
            rows.push(h('button', { class: 'drow' + (this.cat === g.key ? ' on' : ''), onclick: () => { this.sel = null; this.cat = g.key; this.ctx.changed('all'); this.ctx.showCentre?.(); } },
              h('span', { class: 'dt', text: g.title }), h('span', { class: 'dn', text: `${g.items.length}${g.note && g.items.length ? ` · ${g.note}` : ''}` })));
          }
        };
        sec('Made', ['devices', 'exfoliated', 'flakes']);
        sec('Materials', ['materials', 'bare', 'target']);
        sec('Tools', ['stamps']);
      }
    }
    const keep = box.scrollTop;
    box.replaceChildren(...rows);
    box.scrollTop = keep;
  }

  // ------------------------------------------------------------ centre: the lab map
  // svgEl: the map; needsBox: the list under it
  renderMap(svgEl, needsBox, offline) {
    const b = this.ctx.bench() || {}, {scene, reason} = recordedObservedScene(this.ctx);
    this.mapMode ??= b.sim ? 'diagnostics' : 'observed';
    const frames = scene?.historical_frames || [];
    if (this.mapFrame !== 'world' && !frames.some(f => f.id === this.mapFrame)) this.mapFrame = 'world';
    const prior = this.mapMode === 'observed' && this.mapFrame !== 'world'
      ? frames.find(f => f.id === this.mapFrame) : null;
    const layout = prior ? {table: prior.bounds, stations: {}} : b.layout;
    const mapKey = JSON.stringify([this.mapMode, this.mapFrame, layout]);
    this.mapSvg = svgEl; this.mapNeeds = needsBox; this.mapOffline = offline;
    if (!this.map || this._mapKey !== mapKey || this.map.svg !== svgEl) {
      this._mapKey = mapKey;
      this.map = layout?.stations ? new StationBenchView(svgEl, layout) : null;
      if (!this.map) svgEl.replaceChildren();
      if (this.map) {
        this.map.statusOf = id => this.ctx.s().inv.stampStatus(id);
        this.map.stampOf = id => this.ctx.s().inv.samples.get(id);
        this.ov = svg('g', { class: 'mapov' }, svgEl);
      }
      this._pins = null;
    }
    if (this._needsRoot !== needsBox) {
      this._needsRoot = needsBox;
      this.mapControls = h('div', {class: 'observed-scene-controls'});
      this.mapDetails = h('div', {class: 'observed-scene-details'});
      needsBox.replaceChildren(this.mapControls, this.mapDetails);
      this._pins = null;
    }
    const chooseMode = mode => {
      this.mapMode = mode;
      this.renderMap(svgEl, needsBox, this.mapOffline);
    };
    this.mapControls.replaceChildren(h('div', {class: 'fl-fg', role: 'group', 'aria-label': 'Bench view source'},
      ...[['diagnostics', 'Simulation diagnostics'], ['observed', 'Observed evidence']].map(([mode, label]) =>
        h('button', {class: 'fchip' + (this.mapMode === mode ? ' on' : ''),
          'aria-pressed': String(this.mapMode === mode), text: label, onclick: () => chooseMode(mode)}))));
    if (this.mapMode === 'observed' && frames.length) {
      this.mapControls.append(h('label', {class: 'small', text: 'Coordinate frame '},
        h('select', {'aria-label': 'Observed coordinate frame', onchange: event => {
          this.mapFrame = event.target.value; this.renderMap(svgEl, needsBox, this.mapOffline);
        }}, ...[{id: 'world', label: 'World · supported observations'}, ...frames].map(frame =>
          h('option', {value: frame.id, selected: frame.id === this.mapFrame, text: frame.label || frame.id})))));
    }
    const legend = svgEl.closest?.('.vbody')?.querySelector('.map-legend');
    if (legend) legend.hidden = this.mapMode === 'observed';
    const expand = svgEl.closest?.('.view')?.querySelector('.expand-bench');
    if (expand) {
      expand.disabled = this.mapMode === 'observed';
      expand.hidden = this.mapMode === 'observed';
      expand.title = this.mapMode === 'observed'
        ? 'Select Simulation diagnostics to expand its animated bench. Observed evidence stays in this map.'
        : 'Open a larger animated simulation diagnostics view';
    }
    if (this.mapMode === 'observed') this.mapControls.append(h('div', {class: 'small muted',
      text: 'Expand is available in Simulation diagnostics. Observed evidence stays in this map.'}));
    this._renderedScene = scene; this._sceneReason = reason;
    if (this.mapMode === 'observed') {
      this.ov?.replaceChildren();
      this.map?.render({objects: prior?.objects || scene?.objects || {}}, new Set(), {observed: true});
      this.renderObservedDetails(scene, reason, prior);
      this._pins = null;
    } else {
      this.map?.render(this.ctx.view() || {}, offline);
      this.mapDetails.replaceChildren(); this._pins = null;
      if (this.map) this.renderPins(this.mapDetails);
      this.mapDetails.prepend(h('div', {class: 'muted small',
        text: 'Simulation diagnostics · same simulated state as Progress. Physical dimensions share one scale; laboratory positions remain uncalibrated.'}));
    }
  }
  // the map drawing alone (each animation frame while the lab moves)
  drawMap(offline) {
    // Animation can run between selecting Bench overview and its first DOM
    // render. There is no map target to update until renderMap mounts it.
    if (!this.mapSvg || !this.mapNeeds) return;
    if (this.mapMode === 'diagnostics') this.map?.render(this.ctx.view() || {}, offline);
    else {
      const {scene, reason} = recordedObservedScene(this.ctx);
      if (scene !== this._renderedScene || reason !== this._sceneReason)
        this.renderMap(this.mapSvg, this.mapNeeds, offline);
    }
  }

  renderObservedDetails(scene, reason, prior) {
    const openEvidence = new Set([...this.mapDetails.querySelectorAll('details')]
      .filter(detail => detail.open).map(detail => detail.dataset.evidenceKey));
    const objectEntries = Object.entries(prior?.objects || scene?.objects || {});
    const subjects = scene?.subjects || [], unresolved = scene?.unresolved || [];
    const title = prior ? `${prior.label || prior.id} · original-frame prior geometry`
      : 'World · observed evidence';
    const nodes = [h('div', {class: 'o-h', text: title}), h('div', {class: 'muted small', text: prior
      ? 'Original coordinates only. Age and calibration unknown; no world transform or motion authority.'
      : 'Configured layout is a reference. Points show recorded coordinates; dimensions and unobserved apparatus state are unknown.'})];
    if (!scene) nodes.push(h('div', {class: 'placeholder observed-scene-absent', text: `${reason} 0 observed objects.`}));
    else {
      const unlocated = subjects.filter(subject => !subject.pose_key).length;
      const located = prior ? prior.count ?? objectEntries.length : scene.counts?.objects ?? objectEntries.length;
      nodes.push(h('div', {class: 'small observed-scene-counts', text: `${located} ${prior ? `original-frame point${located === 1 ? '' : 's'}` : 'located'} · ${scene.omissions?.subjects ? 'at least ' : ''}${unlocated} named without a position · ${scene.counts?.unresolved ?? unresolved.length} unresolved`}));
      if (!objectEntries.length && !subjects.length && !unresolved.length) nodes.push(
        h('details', {class: 'observed-evidence-details', dataset: {evidenceKey: 'empty-scene'}},
          h('summary', {text: 'Evidence details'}), evidenceBoundary(scene)));
    }
    const card = (label, text, evidence, status = '', alternatives = [], key = label) => h('article', {class: 'card observed-evidence-row',
      style: {margin: '8px 0', padding: '10px', overflowWrap: 'anywhere'}},
      h('b', {text: label}), h('div', {text: `${status ? status+' · ' : ''}${text}`}), evidenceDetails(evidence, scene, alternatives, key));
    if (objectEntries.length) nodes.push(h('div', {class: 'sec-t', text: 'Located points'}));
    for (const [key, object] of objectEntries.slice(0, OBSERVED_ROWS)) {
      const coordinates = `x ${evidenceValue(object.x)}, y ${evidenceValue(object.y)}, z ${evidenceValue(object.z)} mm · rotation ${evidenceValue(object.rotation)}°`;
      const row = card(object.label || object.kind || key, coordinates, object.evidence,
        `${object.identity_status || 'unidentified'}${object.subject ? ' · '+object.subject : ''}`, [], key);
      if (Object.hasOwn(object, 'original_coordinates')) row.append(h('div', {class: 'small',
        text: `Original coordinates: ${evidenceValue(object.original_coordinates)}`}));
      if (Object.hasOwn(object, 'linear_mm')) row.append(h('div', {class: 'small',
        text: `Source linear axis: ${evidenceValue(object.linear_mm)} mm; this is not sample Z.`}));
      nodes.push(row);
    }
    if (subjects.length) nodes.push(h('div', {class: 'sec-t', text: 'Declared subjects and attributes'}));
    for (const subject of subjects.slice(0, OBSERVED_ROWS)) {
      nodes.push(h('div', {class: 'small', text: `${subject.subject} · ${subject.pose_key ? 'supported pose recorded' : 'unlocated; no supported pose recorded'}`}));
      const attributes = Object.entries(subject.attributes || {});
      for (const [property, attribute] of attributes.slice(0, 32)) {
        const value = attribute.status === 'conflicted' && attribute.reported_values
          ? `Reported values: ${evidenceValue(attribute.reported_values)}` : evidenceValue(attribute.value);
        nodes.push(card(`${subject.subject} · ${property}`, value, attribute.evidence, attribute.status || 'unknown'));
      }
      if (attributes.length > 32) nodes.push(h('div', {class: 'small muted', text: `${attributes.length-32} additional attributes omitted from this display.`}));
    }
    if (unresolved.length) nodes.push(h('div', {class: 'sec-t', text: 'Unresolved evidence'}));
    for (const fact of unresolved.slice(0, OBSERVED_ROWS)) {
      const row = card(`${fact.subject} · ${fact.property}`, `${fact.reason || 'unsupported pose'}${Object.hasOwn(fact, 'value') ? ' · '+evidenceValue(fact.value) : ''}`,
        fact.evidence, fact.status || 'unknown', fact.conflicting_values || []);
      if (fact.conflicting_values?.length) row.append(h('div', {class: 'small observed-alternatives'},
        h('b', {text: 'Conflicting alternatives'}), ...fact.conflicting_values.slice(0, 8).map(alternative =>
          h('div', {text: `${evidenceValue(alternative.value)} · source: ${evidenceSourceSummary(alternative.source)} · frame: ${alternative.frame || 'unknown'}`}))));
      nodes.push(row);
    }
    const omissions = scene?.omissions || {};
    const omitted = {objects: (prior ? prior.omitted_objects || 0 : omissions.objects || 0) + Math.max(0, objectEntries.length-OBSERVED_ROWS),
      subjects: (omissions.subjects || 0) + Math.max(0, subjects.length-OBSERVED_ROWS),
      unresolved: (omissions.unresolved || 0) + Math.max(0, unresolved.length-OBSERVED_ROWS)};
    if (Object.values(omitted).some(value => value > 0)) nodes.push(h('div', {class: 'small observed-omissions',
      text: `Omitted from details: ${omitted.objects} objects · ${omitted.subjects} subjects · ${omitted.unresolved} unresolved entries.`}));
    if (omissions.historical_frames) nodes.push(h('div', {class: 'small observed-omissions',
      text: `${omissions.historical_frames} original coordinate frames omitted from the recorded projection.`}));
    this.mapDetails.replaceChildren(...nodes);
    for (const detail of this.mapDetails.querySelectorAll('details'))
      if (openEvidence.has(detail.dataset.evidenceKey)) detail.open = true;
  }

  renderPins(needsBox) {
    const m = this.map, k = m.k, need = this.needs();
    const key = JSON.stringify([this.ctx.s().inv.version, need.map(n => [n.title, n.at]), this.instruments().map(g => [g.key, g.cls]), this.sel]);
    if (key === this._pins) return;
    this._pins = key;
    const g = this.ov;
    g.replaceChildren();
    // a status dot on every instrument
    for (const inst of this.instruments()) {
      const p = this.instPos(inst.key);
      if (!p) continue;
      const c = svg('g', { class: `mdot ${inst.cls}${this.isSel('inst', inst.key) ? ' on' : ''}`, transform: `translate(${m.sx(p.x)} ${m.sy(p.y)})` }, g);
      svg('circle', { r: 4.2 * k, class: 'md-halo' }, c);
      svg('circle', { r: 2.4 * k, class: 'md-dot' }, c);
      svg('title', {}, c).textContent = `${inst.name} · ${inst.text}`;
      c.addEventListener('click', () => this.select({ t: 'inst', id: inst.key }));
    }
    // a numbered pin on what needs a person
    need.forEach((n, i) => {
      if (!n.at) return;
      const c = svg('g', { class: `mpin ${n.cls}`, transform: `translate(${m.sx(n.at.x) + 7 * k} ${m.sy(n.at.y) - 7 * k})` }, g);
      svg('circle', { r: 5.2 * k }, c);
      const t = svg('text', { 'text-anchor': 'middle', y: 1.9 * k, style: `font-size:${5.4 * k}px` }, c);
      t.textContent = String(i + 1);
      svg('title', {}, c).textContent = `${n.title} · ${n.text}`;
      if (n.sel) c.addEventListener('click', () => this.select(n.sel));
    });
    needsBox.replaceChildren(
      h('div', { class: 'sec-t', text: need.length ? `Needs a person · ${need.length}` : 'Needs a person' }),
      need.length ? h('div', { class: 'needs' }, need.map((n, i) => h('button', { class: `need ${n.cls}`, onclick: n.sel ? () => this.select(n.sel) : null, disabled: !n.sel || null },
        h('span', { class: 'pin', text: String(i + 1) }), h('b', { text: n.title }), h('span', { class: 'muted', text: n.text }))))
        : h('div', { class: 'nothing', text: 'Nothing needs a person.' }));
  }

  // Status follows the same category -> item cards -> details navigation as Stock.
  renderStatus(body, sub) {
    const c = this.category, inv = this.ctx.s().inv;
    sub.textContent = this.ctx.live() ? 'stored state · select an item for details' : 'at this point of the recording';
    if (!c) { body.replaceChildren(h('div', {class: 'placeholder', text: 'Choose a category on the left to see its items here.'})); return; }
    const open = id => this.select({t: 'sample', id});
    const card = (id, title, text, click) => h('button', {class: 'card status-card' + (this.sel?.id === id ? ' on' : ''),
      dataset: {id}, onclick: click}, h('b', {text: title}), h('div', {class: 'muted', text}));
    let title = '', cards = [];
    if (c.t === 'inst') {
      const group = this.instruments().find(g => g.key === c.id);
      title = group?.name || 'Instruments';
      cards = (group?.parts || []).map(({d, st}) => card(d.id, devName(d.id), st[1], () => this.select({t: 'device', id: d.id})));
    } else if (c.t === 'supply') {
      const supply = this.supplies().find(s => s.id === c.id);
      title = supply?.name || 'Supplies';
      cards = (supply?.items || []).map(x => x.kind === 'carrier' ? motherTapeCard(x, inv, {open, on: this.sel?.id})
        : card(x.id, x.id, [STAMP_WORD[x.status] || x.status, whereText(x, inv)].filter(Boolean).join(' · '), () => open(x.id)));
      if (c.id === 'tape') cards = [card('exfoliator', 'Daughter tape roll', supply?.value || 'Unknown', () => this.select({t: 'device', id: 'exfoliator'}))];
    }
    body.replaceChildren(h('div', {class: 'o-h', text: `${title} · ${cards.length}`}),
      cards.length ? h('div', {class: 'wall'}, cards) : h('div', {class: 'placeholder', text: 'No items in this category.'}));
  }

  // ------------------------------------------------------------ centre: stock
  renderStock(body, sub) {
    const s = this.ctx.s(), inv = s.inv;
    if (!inv.known) { body.replaceChildren(h('div', { class: 'placeholder', text: this.ctx.live() ? 'Loading…' : 'This recording was made before the sample ledger was recorded as events.' })); return; }
    const groups = inv.groups(), sel = this.sel && this.sel.t === 'sample' ? this.sel.id : null;
    const open = id => this.select({ t: 'sample', id });
    if (this.cat === 'flakes') {
      sub.textContent = 'every flake in the lab · click one for its details';
      if (!body.querySelector(':scope > .fl-box')) body.replaceChildren(h('div', { class: 'fl-box' }));
      this.grid.render(body.querySelector('.fl-box'));
      return;
    }
    sub.textContent = this.ctx.live() ? 'everything in the lab now' : 'everything in the lab at this point of the recording';
    const q = this.query.trim().toLowerCase();
    const mats = [...new Set([...inv.samples.values()].map(x => x.props.material).filter(Boolean))].sort();
    const hit = x => (!q || x.id.toLowerCase().includes(q) || (x.props.material || '').toLowerCase() === q) && (!this.mat || x.props.material === this.mat);
    const find = h('input', { class: 'inv-find', type: 'search', placeholder: 'Find an item or material…', value: this.query, 'aria-label': 'Find',
      oninput: ev => { this.query = ev.target.value; this.renderStock(body, sub); const f = body.querySelector('.inv-find'); f.focus(); f.setSelectionRange(f.value.length, f.value.length); },
      onkeydown: ev => { if (ev.key === 'Escape' && this.query) { ev.stopPropagation(); this.query = ''; this.renderStock(body, sub); } } });
    const chip = (t, on, fn) => h('button', { class: 'fchip' + (on ? ' on' : ''), text: t, onclick: fn });
    const out = [h('div', { class: 'stock-top' }, find, mats.length > 1 ? h('div', { class: 'fl-fg' }, chip('All', !this.mat, () => { this.mat = null; this.renderStock(body, sub); }),
      ...mats.map(m => chip(m, this.mat === m, () => { this.mat = this.mat === m ? null : m; this.renderStock(body, sub); }))) : null)];
    let shown = 0;
    for (const g of groups) {
      if (this.cat !== 'all' && this.cat !== g.key) continue;
      if (g.key === 'flakes') {
        const fl = g.items.filter(hit);
        if (!fl.length) continue;
        shown += fl.length;
        const top = fl.filter(hasPhoto).sort((a, b) => (b.props.area_um2 || 0) - (a.props.area_um2 || 0)).slice(0, 12);
        out.push(h('div', { class: 'o-h' }, h('span', { text: `Flakes · ${fl.length} · ${fl.filter(hasPhoto).length} with photos` }),
          h('button', { class: 'lnk-btn', text: 'all flakes ›', onclick: () => { this.cat = 'flakes'; this.ctx.changed('all'); } })));
        if (!top.length) out.push(h('div', { class: 'muted small', text: 'No flake photos (recorded before flake photos were kept, or none was photographed at 20× or 100×). “all flakes” lists them.' }));
        else out.push(h('div', { class: 'strip' }, top.map(f => h('button', { class: 'st-f' + (sel === f.id ? ' on' : ''), title: `${f.id} · ${thickText(f.props.material, f.props.thickness_label)}`, onclick: () => open(f.id) },
          h('img', { src: imgUrl(bestPhoto(f)), alt: f.id, loading: 'lazy' }), h('span', { class: 'st-tag', text: shortId(f.id) })))));
        continue;
      }
      const items = g.items.filter(hit);
      if (!items.length && (q || this.mat || this.cat === 'all')) continue;
      shown += items.length;
      out.push(h('div', { class: 'o-h', text: `${g.title} · ${items.length}` }));
      if (!items.length) out.push(h('div', { class: 'placeholder', text: 'None.' }));
      else if (g.key === 'devices') out.push(h('div', { class: 'wall' }, items.map(d => deviceCard(d, inv, { open, on: sel, roleOf: id => s.roleOf(id) }))));
      else if (g.key === 'exfoliated') out.push(h('div', { class: 'wall' }, items.map(c => chipCard(c, inv, { open, on: sel }))));
      else if (g.key === 'materials') out.push(h('div', {class: 'wall'}, items.map(x => x.kind === 'carrier'
        ? motherTapeCard(x, inv, {open, on: sel})
        : h('button', {class: 'card mother-tape-card', onclick: () => open(x.id)}, h('b', {text: 'Daughter tape'}),
          h('span', {class: 'muted', text: x.props.material ? `Prepared with ${x.props.material}` : 'No material prepared yet'}),
          h('span', {text: whereText(x, inv)})))));
      else if (g.key === 'stamps') out.push(h('div', { class: 'pills' }, items.map(x => stampPill(x, { open, on: sel }))));
      else if (g.key === 'target') out.push(h('div', { class: 'wall' }, items.map(x => {
        const src = samplePhoto(x)?.src;
        return h('button', { class: 'card target-card', onclick: () => open(x.id) },
          h('b', { text: x.id }),
          src ? h('figure', { class: 'sample-photo' }, h('img', { src: imgUrl(src), alt: `${x.id} electrodes`, loading: 'lazy' })) : null,
          h('div', { class: 'muted small', text: x.props.electrodes || 'Target chip' }));
      })));
      else out.push(h('div', { class: 'pills' }, items.map(x => h('button', { class: 'stamp-pill' + (sel === x.id ? ' on' : ''), dataset: { id: x.id }, title: `${x.id} · details`, onclick: () => open(x.id) },
        h('span', { class: 'mono', text: x.id }), h('span', { class: 'muted', text: [x.props.material, x.kind === 'chip' && inv.of('device').some(d => d.location === x.id) ? inv.of('device').filter(d => d.location === x.id).map(d => d.id).join(', ') : null, whereText(x, inv)].filter(Boolean).join(' · ') })))));
    }
    if (!shown) out.push(h('div', { class: 'placeholder', text: q || this.mat ? 'Nothing matches.' : 'Nothing here.' }));
    const keep = body.scrollTop;
    body.replaceChildren(...out);
    body.scrollTop = keep;
  }

  // ------------------------------------------------------------ right
  detail(dctx) {
    const it = this.sel, s = this.ctx.s();
    if (!it) return null;
    if (it.t === 'sample') {
      const x = s.inv.samples.get(it.id);
      return x ? sampleDetail(x, dctx) : null;
    }
    if (it.t === 'device') {
      const device = (this.ctx.bench()?.devices || []).find(d => d.id === it.id);
      const g = device ? this.group(device.id, devName(device.id), [device]) : null;
      if (!g) return null;
      const node = h('div', { class: 'detail-body' }, g.parts.map(({ d, st }) => {
        const kv = [['State', h('span', { class: 'tag ' + (st[0] === 'ok' ? 'ok' : st[0] === 'info' ? 'running' : st[0]), text: st[1] })]];
        const pose = d.world_pose || d.pose;
        if (pose) kv.push(['Position', `x ${fmt(pose.x, 1)} · y ${fmt(pose.y, 1)} · z ${fmt(pose.z, 1)} mm${pose.rotation ? ` · ${fmt(pose.rotation, 1)}°` : ''}`]);
        if (d.positions) kv.push(['Position', Object.entries(d.positions).filter(([, v]) => typeof v === 'number').slice(0, 4).map(([k, v]) => `${k.replace(/_/g, ' ')} ${fmt(v, 1)}`).join(' · ')]);
        if (d.temperature != null) kv.push(['Temperature', `${fmt(d.temperature, 1)} °C${d.target != null ? ` · target ${d.target} °C` : ''}`]);
        if (d.flow_sccm != null) kv.push(['Gas flow', `${fmt(d.flow_sccm, 1)} sccm · target ${fmt(d.flow_target_sccm, 1)} sccm`]);
        if (d.vacuum != null) kv.push(['Vacuum', d.vacuum ? 'on' : 'off']);
        if (d.on != null) kv.push(['Switch', d.on ? 'on' : 'off']);
        if (d.objective != null) kv.push(['Objective', `${d.objective}×`]);
        if (d.roll_left_mm != null) kv.push(['Tape left', tapeText(d.roll_left_mm)]);
        if (d.homed != null) kv.push(['Homed', d.homed ? 'yes' : 'no']);
        const c = s.lastCommand(d.id);
        kv.push(['Last command', c ? `${c.payload.command.replace(/_/g, ' ')} ${argText(c.payload.params)}${c.kind === 'safety.violation' ? ' · refused' : ''}${s.when(c) ? ` · ${s.when(c)}` : ''}` : 'none in this run']);
        return h('div', { class: 'inst-part' }, h('div', { class: 'd-sec', text: devName(d.id) }),
          h('dl', { class: 'kv' }, kv.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', {}, v)])));
      }));
      return { title: g.name, sub: g.text, node };
    }
    return null;
  }
}
