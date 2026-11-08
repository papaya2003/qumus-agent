// The plan book and the hardware jobs (data), and how the plans look.
//
// Only Qumus writes the step plan (update_plan). The subagents do not write
// step plans, but three of them hand in a plan of their own kind: the Advisor
// a device plan (layers, materials, thickness, minimum area), the Picker a
// flake selection, the Designer a layout. The Progress page shows the step
// plan (the progress tree, earlier revisions as "Plan 1", "Plan 2"); the
// other three are part of the Results. A step a subagent finished shows
// "returned": the agent came back, which is not yet the same as the goal being
// met (its report says that).
//
// The book is data only: the page keeps one for this run and builds others for
// runs it shows from History.

import { h, svg, avatar, label, short, imgUrl } from './ui.js';
import { bestPhoto, thickText, shortId } from './flakes.js';

const PLAN_STATUS = { pending: 'to do', running: 'in progress', failed: 'failed', skipped: 'skipped' };
const MAT_COLOR = { hbn: 'var(--m-hbn)', gr: 'var(--m-gr)', graphite: 'var(--m-gr)', graphene: 'var(--m-gr)', wte2: 'var(--m-tmd)',
  mos2: 'var(--m-tmd)', wse2: 'var(--m-tmd)', au: 'var(--m-metal)', 'cr/au': 'var(--m-metal)', pd: 'var(--m-metal)' };
export const matColor = m => MAT_COLOR[String(m || '').toLowerCase()] || 'var(--m-other)';
const LAYER_COLOR = l => (/metal|contact|electrode|^source$|^drain$|^voltage_/i.test(l) ? 'var(--m-metal)' : /hbn/i.test(l) ? 'var(--m-hbn)'
  : /gate|graphite|^gr$/i.test(l) ? 'var(--m-gr)' : /channel/i.test(l) ? 'var(--accent)' : 'var(--m-other)');
export const planName = rev => `Plan ${rev}`;

export class PlanBook {
  constructor() { this.reset(); }

  reset() {
    this.plans = [];
    this.device = null; this.selection = null; this.design = null;
    this.devicePlans = []; this.selections = []; this.designs = [];
    this.runs = new Map();
    this.version = 0;  // bumps on every change (the page redraws what reads it)
  }

  get latest() { return this.plans[this.plans.length - 1] || null; }

  // ---------------------------------------------------------------- data in
  addRevision(p, author) {
    if (!this.plans.some(x => x.revision === p.revision)) this.plans.push({ revision: p.revision, plan: p.plan || [], note: p.note, author });
    this.version++;
  }
  stepProgress(p) {
    const rev = this.plans.find(x => x.revision === p.revision);
    if (rev) { rev.plan = p.plan || rev.plan; this.version++; }
  }
  // an accepted submit_device_plan / submit_flake_selection / submit_design
  accepted(tool, args, agent, eventId) {
    const entry = { ...args, agent, recordId: String(eventId ?? `${tool}:${this.version}`) };
    if (tool === 'submit_device_plan') { this.device = entry; this.devicePlans.push(entry); }
    else if (tool === 'submit_flake_selection') { this.selection = entry; this.selections.push(entry); }
    else if (tool === 'submit_design' || tool === 'create_device_design') { this.design = entry; this.designs.push(entry); }
    else return;
    this.version++;
  }

  // ---------------------------------------------------------------- jobs
  track(e) {
    const p = e.payload || {};
    if (!e.run_id) return null;
    let r = this.runs.get(e.run_id);
    // t0/t1: session clock; lab0/lab1: the lab's clock (how long the instruments took)
    if (!r) { r = { id: e.run_id, flow: p.flow || '?', params: p.params || {}, status: 'queued', steps: new Map(), agent: e.agent,
      t0: e.wall ?? e.ts, t1: e.wall ?? e.ts, lab0: e.ts, lab1: e.ts }; this.runs.set(e.run_id, r); }
    r.t1 = e.wall ?? e.ts; r.lab1 = e.ts;
    if (e.kind === 'job.submitted') { r.flow = p.flow; r.params = p.params; r.agent = e.agent; }
    else if (e.kind === 'job.status') { r.status = p.status; r.message = p.message; }
    else if (e.kind === 'step.started') r.steps.set(p.seq, { name: p.name, status: 'in_progress', corrections: 0 });
    else if (e.kind === 'step.finished') { const st = r.steps.get(p.seq) || { name: p.name }; st.status = p.status; st.corrections = p.corrections; r.steps.set(p.seq, st); }
    this.version++;
    return r;
  }
}

// ---------------------------------------------------------------- views
// An earlier revision of the step plan, with what changed against the one before.
export function renderRevision(book, shown) {
  const prev = book.plans.find(p => p.revision === shown.revision - 1);
  const note = shown.note ? h('p', { class: 'plan-note' }, h('b', { text: `${label(shown.author)}: ` }), shown.note,
    prev ? h('span', { class: 'muted', text: ` · changes against ${planName(prev.revision)} are marked` }) : null) : null;
  const before = new Map((prev ? prev.plan : []).map(s => [s.id, s]));
  // until Qumus marks progress on a step the plan only says what is planned;
  // the hardware jobs carry the real status
  const tracked = book.plans.some(p => p.plan.some(s => s.status && s.status !== 'pending'));
  const rows = shown.plan.map((s, i) => {
    const b = before.get(s.id);
    const cls = prev && !b ? 'added' : (b && (b.title !== s.title || b.owner !== s.owner) ? 'changed' : '');
    const done = s.status === 'done';
    const st = done ? (s.owner && s.owner !== 'orchestrator' ? 'returned' : 'done') : (PLAN_STATUS[s.status] || s.status || 'to do');
    return h('div', { class: 'pstep ' + cls },
      h('span', { class: 'num', text: i + 1 }), avatar(s.owner),
      h('span', { class: 'title' }, h('span', { class: 'tt', text: s.title, title: s.title }),
        h('small', { text: label(s.owner) + (s.note ? ` · ${s.note}` : '') })),
      cls === 'added' ? h('span', { class: 'tag new', text: 'new' }) : cls === 'changed' ? h('span', { class: 'tag new', text: 'changed' }) : null,
      tracked ? h('span', { class: 'tag ' + (done ? 'done' : s.status === 'running' ? 'running' : s.status || 'pending'), text: st,
        title: done ? 'The agent finished this step and reported back; its report says whether the goal was met.' : '' })
        : h('span', { class: 'tag', text: 'planned', title: 'Progress shows in the tree of the latest plan and in the conversation' }));
  });
  if (prev) {
    const now = new Set(shown.plan.map(s => s.id));
    for (const s of prev.plan) if (!now.has(s.id)) rows.push(h('div', { class: 'pstep removed' }, h('span', { class: 'num', text: '–' }),
      avatar(s.owner), h('span', { class: 'title' }, h('span', { class: 'tt', text: s.title })), h('span', { class: 'tag', text: 'removed' })));
  }
  return [note, h('div', { class: 'steps' }, rows)].filter(Boolean);
}

// The Advisor's device plan: the layers top to bottom with what each must be.
export function renderDevicePlan(d) {
  const stack = (d.layer_stack || []).map(l => h('div', { class: 'layer', style: `--c:${matColor(l.material)}` },
    h('span', { class: 'swatch' }), h('span', { class: 'lrole', text: String(l.role || '').replace(/_/g, ' ') }),
    h('span', { class: 'lmat', text: l.material }), h('span', { class: 'muted', text: [l.thickness, (l.thickness_labels || []).join(', ')].filter(Boolean).join(' · ') }),
    l.min_area_um2 ? h('span', { class: 'muted', text: `≥ ${Math.round(l.min_area_um2)} µm²` }) : h('span'),
    l.flake === false ? h('span', { class: 'tag', text: 'not a flake' }) : h('span')));
  const kv = [['Channel', d.channel], ['Contacts', d.contact_scheme], ['Gate', d.gate_scheme]].filter(([, v]) => v);
  return h('div', { class: 'devplan' },
    h('div', { class: 'dp-head' }, avatar(d.agent || 'advisor'), h('b', { text: d.device_name || 'Device Guidance' }), h('span', { class: 'muted', text: 'by the Advisor · layers top to bottom' })),
    h('div', { class: 'stack' }, stack),
    kv.length ? h('dl', { class: 'kv' }, kv.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', { text: v })])) : null,
    (d.workflow_plan || []).length ? h('ol', { class: 'wf' }, d.workflow_plan.map(s => h('li', { text: s }))) : null,
    d.evidence ? h('p', { class: 'evidence' }, h('b', { text: 'Why: ' }), d.evidence) : null);
}

// The Picker's choice with the photos it judged (redesign item 4): one card per
// chosen flake (photo, thickness, score, why), the flakes not chosen folded.
// hooks: flake(id) -> the ledger's flake, open(id) -> its details
export function renderSelection(s, hooks) {
  const by = new Map((s.assessments || []).map(a => [a.flake_id, a]));
  const card = (id, chosen) => {
    const a = by.get(id), f = hooks.flake(id), src = f ? bestPhoto(f) : null, p = (f && f.props) || {};
    return h('button', { class: 'pflake' + (chosen ? '' : ' other'), dataset: { id }, title: `${id} · details`, onclick: () => hooks.open(id) },
      src ? h('img', { src: imgUrl(src), alt: id, loading: 'lazy' }) : h('span', { class: 'fnone', text: f ? 'no photo' : 'not in the ledger' }),
      h('span', { class: 'pf-t' },
        h('span', { class: 'pf-l' }, h('span', { class: 'mono', text: `${p.chip_id || id.replace(/_f\d+$/, '')} · ${shortId(id)}` }),
          a && a.overall_score != null ? h('span', { class: 'score', text: `${Math.round(a.overall_score)}`, title: 'overall score (0-100)' }) : null,
          a && a.feasible === false ? h('span', { class: 'tag warn', text: 'not feasible' }) : null),
        h('small', { text: [p.material, p.thickness_label ? thickText(p.material, p.thickness_label) : null,
          p.area_um2 != null ? `${Math.round(p.area_um2).toLocaleString()} µm²` : null, p.residue?.level ? `residue ${p.residue.level}` : null].filter(Boolean).join(' · ') }),
        a && a.reason ? h('span', { class: 'pf-r', text: short(a.reason, 200) }) : null));
  };
  const chosen = Object.values(s.selected_by_role || {}).flat();
  const roles = Object.entries(s.selected_by_role || {}).map(([role, ids]) => h('div', { class: 'role' },
    h('div', { class: 'rname', text: role.replace(/_/g, ' ') }),
    h('div', { class: 'flakes' }, (ids || []).map(id => card(id, true)))));
  const others = (s.assessments || []).filter(a => !chosen.includes(a.flake_id));
  return h('div', { class: 'selection' },
    h('div', { class: 'dp-head' }, avatar(s.agent || 'picker'), h('b', { text: 'Flake selection' }), h('span', { class: 'muted', text: `by the Picker · ${(s.assessments || []).length} candidates looked at` })),
    roles, s.reasons ? h('p', { class: 'evidence' }, h('b', { text: 'Why this combination: ' }), s.reasons) : null,
    others.length ? h('details', {}, h('summary', { text: `${others.length} not chosen` }),
      h('div', { class: 'flakes' }, others.map(a => card(a.flake_id, false)))) : null);
}

// Render the recorded geometry, not a new layout inferred from material labels.
export function layoutPolygons(d) {
  if (d.version !== 'device_design_v1') return (d.shapes || []).map(r => ({ layer: r.layer,
    points: [[r.x_um, r.y_um], [r.x_um + r.w_um, r.y_um],
      [r.x_um + r.w_um, r.y_um + r.h_um], [r.x_um, r.y_um + r.h_um]] }));
  const contacts = (d.target?.contacts || []).map(c => ({ layer: c.role,
    points: c.points_mm.map(([x, y]) => [x * 1000, y * 1000]) }));
  const flakes = (d.sources || []).map(source => {
    const angle = source.placement.rotation_deg * Math.PI / 180;
    const [tx, ty] = source.placement.translation_mm;
    return { layer: source.material, points: source.outline_mm.map(([x, y]) =>
      [(tx + x * Math.cos(angle) - y * Math.sin(angle)) * 1000,
        (ty + x * Math.sin(angle) + y * Math.cos(angle)) * 1000]) };
  });
  return [...contacts, ...flakes];
}

// The Designer's layout: complete polygons in µm, one colour per layer.
export function layoutDrawing(d) {
  const shapes = layoutPolygons(d);
  if (!shapes.length) return null;
  const xs = shapes.flatMap(r => r.points.map(p => p[0])), ys = shapes.flatMap(r => r.points.map(p => p[1]));
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = Math.max(x1 - x0, y1 - y0) * 0.06 || 5;
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs, parent) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); parent?.appendChild(n); return n; };
  const s = mk('svg', { viewBox: `${x0 - pad} ${-(y1 + pad)} ${x1 - x0 + 2 * pad} ${y1 - y0 + 2 * pad}`, class: 'layout-svg', preserveAspectRatio: 'xMidYMid meet' });
  for (const r of shapes) {
    const e = mk('polygon', { points: r.points.map(([x, y]) => `${x},${-y}`).join(' '), style: `fill:${LAYER_COLOR(r.layer)};fill-opacity:0.45`, class: 'lshape' }, s);
    mk('title', {}, e).textContent = `${r.layer} · recorded geometry in µm`;
  }
  return s;
}

// The same recorded geometry supplies both the thumbnail and full detail.
export function renderLayout(d) {
  const s = layoutDrawing(d);
  if (!s) return h('div', { class: 'placeholder', text: 'The layout has no shapes.' });
  const layers = [...new Set(layoutPolygons(d).map(r => r.layer))];
  return h('div', { class: 'layout' },
    h('div', { class: 'dp-head' }, avatar(d.agent || 'designer'), h('b', { text: 'Layout' }), h('span', { class: 'muted', text: `by the Designer · for ${(d.pickup_order || d.flake_ids || []).join(', ')} · µm` })),
    d.artifact ? h('p', { class: 'small', text: `Verified ${d.artifact.backend} artifact · revision ${d.revision} · ${d.artifact.sha256}` }) : null,
    h('div', { class: 'layout-row' }, s, h('div', { class: 'legend' }, layers.map(l => h('span', {}, h('i', { style: `background:${LAYER_COLOR(l)}` }), String(l).replace(/_/g, ' '))))),
    d.notes ? h('p', { class: 'evidence', text: d.notes }) : null);
}

// Compact layer order for the plan card; the complete specification opens on the right.
export function planThumbnail(d) {
  const layers = d.layer_stack || [];
  const height = Math.max(160, 24 + layers.length * 28);
  const s = svg('svg', {viewBox: `0 0 240 ${height}`, role: 'img', 'aria-label': 'Planned layers, top first'});
  layers.forEach((layer, i) => {
    const y = (height - layers.length * 28) / 2 + i * 28;
    svg('rect', {x: 20, y, width: 200, height: 23, rx: 4, style: `fill:${matColor(layer.material)}`}, s);
    svg('text', {x: 32, y: y + 16, fill: /^(gr|graphite|graphene|wte2|mos2|wse2)$/i.test(layer.material) ? '#fff' : '#20212a', 'font-size': 14, 'font-family': 'system-ui, sans-serif'}, s).textContent = layer.material || layer.role;
  });
  return s;
}
