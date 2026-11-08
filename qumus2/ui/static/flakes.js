// Flakes: a photo grid with filters and the details of one flake (usability
// redesign items 2 and 3, placed by the scenario redesign). The grid is a
// centre view (Results: the flakes this run found; Lab · Stock: every flake in
// the lab); clicking a flake shows its details in the right column: both
// photos, what each objective said, its area, residue, where it came from and
// where it is now (in words), and what happened to it.
//
// The data is the ledger (`sample.updated` events carry the flake fields since
// 2026-10-23), live and in a replay. Older recordings have no flake photos:
// their cards say so. "Don't use this flake" and "Ask Qumus about it" talk to
// Qumus, so they are live only.

import { h, fmt, imgUrl, placeName } from './ui.js';

// thickness words in the ledger's order (core/materials.py THICKNESS_LABELS)
export const THICKNESS = {
  hBN: ['0-10', '10-20', '20-30', '30-40', '>40'],
  Gr: ['monolayer', 'bilayer', 'trilayer', 'fewlayer', 'manylayer'],
};
const SHADES = {
  hBN: ['#cfe0f3', '#a3c4ea', '#6c9cd8', '#3c6dc0', '#1e3d85'],
  Gr: ['#e4e4ea', '#bdbdc6', '#8f8f99', '#5e5e67', '#2d2d33'],
};
export const thickColor = (material, label) => {
  const i = (THICKNESS[material] || []).indexOf(label);
  return i >= 0 ? SHADES[material][i] : 'var(--m-other)';
};
// hBN bins are nanometres; graphene's are words
export const thickText = (material, label) => (label == null ? 'thickness unknown'
  : material === 'hBN' || /^[<>]?\d/.test(label) ? `${String(label).replace('-', '–')} nm` : label);

const STATUS = {
  prepared: ['prepared reactor input', 'pending'], reactor_consumed: ['consumed in reaction', 'muted'],
  reactor_product: ['verified reactor product', 'ok'],
  candidate: ['on the chip · 10× only', 'pending'], catalogued: ['on the chip', 'ok'], lost: ['not found again', 'warn'],
  vetoed: ['excluded', 'danger'], on_stamp: ['on a stamp', 'running'], stacked: ['in a device', 'new'],
};
const GROUPS = { chip: new Set(['candidate', 'catalogued', 'prepared', 'reactor_product']), used: new Set(['on_stamp', 'stacked', 'reactor_consumed']), lost: new Set(['lost', 'vetoed']) };
const OBSERVATION_VIEWS = {
  reactor: { label: 'Reactor view', description: 'reactor observation', title: 'Saved reactor observation used for acceptance' },
  ordinary_microscope: { label: 'Ordinary microscope', description: 'ordinary microscope inspection', title: 'Saved ordinary microscope inspection' },
};
export const shortId = id => String(id).replace(/^.*_(f\d+)$/, '$1');
export const photos = f => (f.props && f.props.images && typeof f.props.images === 'object' && !Array.isArray(f.props.images) ? f.props.images : {});
export const bestPhoto = (f, prefer = '100x') => photos(f)[prefer] || photos(f)['100x'] || photos(f)['20x'] || photos(f).reactor || null;
export const hasPhoto = f => !!bestPhoto(f);

// a flake's place in words: "in device_001 · top hBN", "on stamp_01", "on the chip"
export function flakeWhere(f, roleOf) {
  const p = f.props || {};
  if (f.status === 'stacked') return `in ${p.device || f.location}${roleOf && roleOf(f.id) ? ` · ${roleOf(f.id)}` : ''}`;
  if (f.status === 'on_stamp') return `on ${f.location}`;
  return (STATUS[f.status] || [f.status || 'on the chip'])[0];
}

// One flake as a card (photo, id, thickness, area).
export function flakeCard(x, { on, select, roleOf, short }) {
  const p = x.props, src = bestPhoto(x);
  const where = ['stacked', 'on_stamp', 'reactor_consumed', 'reactor_product', 'prepared'].includes(x.status) ? flakeWhere(x, roleOf) : null;
  const res = p.residue && p.residue.level ? `residue ${p.residue.level}` : '';
  return h('button', { class: 'fcard' + (on ? ' on' : '') + (x.status === 'lost' || x.status === 'vetoed' ? ' dim' : ''),
    dataset: { id: x.id }, title: `${x.id} · ${thickText(p.material, p.thickness_label)}`, onclick: () => select(x.id) },
  h('div', { class: 'fimg' }, src ? h('img', { src: imgUrl(src), alt: photos(x).reactor ? `${x.id}: reactor observation` : `${x.id} at ${photos(x)['100x'] ? 100 : 20}×`, loading: 'lazy' })
    : h('span', { class: 'fnone', text: x.status === 'candidate' ? '10× scan only · no photo' : 'No saved photo' }), where ? h('span', { class: 'fwhere', text: where }) : null),
  h('div', { class: 'fmeta' },
    h('span', { class: 'mono', text: short ? shortId(x.id) : x.id.replace(/^chip_/, '') }),
    h('span', { class: 'fth' }, h('i', { style: `background:${thickColor(p.material, p.thickness_label)}` }), thickText(p.material, p.thickness_label))),
  h('div', { class: 'fmeta muted' }, h('span', { text: p.area_um2 != null ? `${Math.round(p.area_um2).toLocaleString()} µm²` : '' }),
    h('span', { text: x.status === 'lost' ? 'not found again' : x.status === 'vetoed' ? 'excluded' : res })));
}

export class FlakeGrid {
  // hooks: list() -> the flakes to show; selected() -> id; select(id); roleOf(id); scoreOf(id)
  constructor(hooks) {
    this.hooks = hooks;
    this.f = { material: null, thick: new Set(), status: 'all', chip: null, sort: 'area' };
    this.box = null;
  }

  filter(patch) { Object.assign(this.f, { material: null, thick: new Set(), status: 'all', chip: null }, patch); }

  shown() {
    const f = this.f;
    const list = this.hooks.list().filter(x => (!f.material || x.props.material === f.material)
      && (!f.thick.size || f.thick.has(x.props.thickness_label))
      && (f.status === 'all' || GROUPS[f.status].has(x.status))
      && (!f.chip || (x.props.chip_id || x.location) === f.chip));
    const score = x => (this.hooks.scoreOf ? this.hooks.scoreOf(x.id) : null);
    const by = {
      area: (a, b) => (b.props.area_um2 || 0) - (a.props.area_um2 || 0),
      score: (a, b) => (score(b) ?? -1) - (score(a) ?? -1) || (b.props.area_um2 || 0) - (a.props.area_um2 || 0),
      id: (a, b) => a.id.localeCompare(b.id),
    }[f.sort];
    // photographed flakes first: the 10x-only candidates have nothing to look at
    return list.sort((a, b) => (hasPhoto(b) - hasPhoto(a)) || by(a, b));
  }

  // the filters and the grid into `box` (keeps its scroll position)
  render(box) {
    this.box = box;
    const all = this.hooks.list(), list = this.shown(), sel = this.hooks.selected();
    const withPhotos = all.filter(hasPhoto).length;
    const keep = box.querySelector('.fl-grid')?.scrollTop || 0;
    box.replaceChildren(
      h('div', { class: 'fl-count muted', text: `${list.length} of ${all.length} shown · ${withPhotos} with saved photos` }),
      this.filters(all),
      h('div', { class: 'fl-grid' }, list.length ? list.map(x => flakeCard(x, { on: x.id === sel, select: this.hooks.select, roleOf: this.hooks.roleOf, short: !!this.f.chip }))
        : [h('div', { class: 'placeholder', text: all.length ? 'No flake matches these filters.' : 'No flakes yet. A chip search lists every flake it finds here, with its photos.' })]));
    const g = box.querySelector('.fl-grid');
    if (g) g.scrollTop = keep;
  }

  filters(all) {
    const f = this.f;
    const chip = (text, on, onclick, title) => h('button', { class: 'fchip' + (on ? ' on' : ''), text, title: title || null, onclick });
    const set = patch => { Object.assign(this.f, patch); if (this.box) this.render(this.box); };
    const mats = [...new Set(all.map(x => x.props.material).filter(Boolean))].sort();
    const present = new Set(all.filter(x => !f.material || x.props.material === f.material).map(x => x.props.thickness_label).filter(Boolean));
    const vocab = f.material ? THICKNESS[f.material] || [] : Object.values(THICKNESS).flat();
    const labels = [...vocab.filter(l => present.has(l)), ...[...present].filter(l => !vocab.includes(l))];
    const chips = [...new Set(all.map(x => x.props.chip_id || x.location).filter(Boolean))].sort();
    const group = (name, ...kids) => h('div', { class: 'fl-fg' }, h('span', { class: 'fl-fl', text: name }), ...kids);
    return h('div', { class: 'fl-filters' },
      mats.length > 1 ? group('Material', chip('All', !f.material, () => set({ material: null, thick: new Set() })),
        ...mats.map(m => chip(m, f.material === m, () => set({ material: f.material === m ? null : m, thick: new Set() })))) : null,
      labels.length ? group('Thickness', ...labels.map(l => chip(thickText(f.material || (THICKNESS.hBN.includes(l) ? 'hBN' : 'Gr'), l).replace(' nm', ''),
        f.thick.has(l), () => { const t = new Set(f.thick); if (t.has(l)) t.delete(l); else t.add(l); set({ thick: t }); }))) : null,
      group('Status', ...[['all', 'All'], ['chip', 'On a chip'], ['used', 'Used or consumed'], ['lost', 'Lost or excluded']]
        .map(([k, t]) => chip(t, f.status === k, () => set({ status: k })))),
      chips.length > 1 ? group('Chip', chip('All', !f.chip, () => set({ chip: null })),
        ...chips.map(c => chip(c, f.chip === c, () => set({ chip: f.chip === c ? null : c })))) : null,
      h('span', { class: 'grow' }),
      group('Sort', h('select', { class: 'fl-sort', 'aria-label': 'Sort', onchange: ev => set({ sort: ev.target.value }) },
        ...[['area', 'Area'], ['score', 'Picker score'], ['id', 'Name']].map(([v, t]) => h('option', { value: v, text: t, selected: f.sort === v || null })))));
  }
}

// The details of one flake, for the right column. Keeps which photo is shown
// and whether "Don't use" waits for its confirmation.
export class FlakeDetail {
  // hooks: inventory, history(id) -> [{when, text, cls}], roleOf(id), live() -> bool,
  // photo(src), ask(id), veto(id) -> Promise, redraw()
  constructor(hooks) { this.hooks = hooks; this.id = null; this.obj = '100x'; this.confirm = false; }

  render(x) {
    if (x.id !== this.id) { this.id = x.id; this.obj = '100x'; this.confirm = false; }
    const p = x.props, ph = photos(x), objs = ['ordinary_microscope', '20x', '100x', 'reactor'].filter(o => ph[o]);
    if (!ph[this.obj]) this.obj = objs[objs.length - 1] || '100x';
    const observations = Object.keys(OBSERVATION_VIEWS).filter(o => ph[o]);
    const views = [...['10x', '20x', '100x'].filter(o => !observations.length || ph[o]), ...observations];
    const st = STATUS[x.status] || [x.status, ''];
    const redraw = () => this.hooks.redraw();
    const big = ph[this.obj]
      ? h('div', { class: 'fl-big' }, h('img', { src: imgUrl(ph[this.obj]), alt: OBSERVATION_VIEWS[this.obj] ? `${x.id}: ${OBSERVATION_VIEWS[this.obj].description}` : `${x.id} at ${this.obj.replace('x', '×')}`, title: 'Click to enlarge',
        onclick: () => this.hooks.photo(ph[this.obj]) }),
        h('div', { class: 'seg fl-obj' }, ...views.map(o => h('button', { text: OBSERVATION_VIEWS[o]?.label || o.replace('x', '×'), class: this.obj === o ? 'on' : '',
          disabled: !ph[o] || null, title: ph[o] ? (OBSERVATION_VIEWS[o]?.title || `The ${o.replace('x', '×')} photo`) : o === '10x' ? 'The 10× scan keeps no photo per flake' : 'No photo at this objective',
          onclick: ev => { ev.stopPropagation(); this.obj = o; redraw(); } }))))
      : h('div', { class: 'fl-big none', text: p.images && !x.status?.startsWith('reactor_') ? 'Only seen in the 10× scan: no photo was taken (only the most promising candidates are photographed at 20× and 100×).'
        : 'No saved photo is available for this flake.' });
    const labels = p.labels || {}, ks = ['10x', '20x', '100x'].filter(k => labels[k]);
    const agree = ks.length > 1 && ks.every(k => labels[k] === labels[ks[0]]);
    const inv = this.hooks.inventory, chip = inv.samples.get(p.chip_id || x.location);
    const rel = p.rel_corner_mm;
    const kv = [
      ['Thickness', h('span', {}, h('b', { text: thickText(p.material, p.thickness_label) }),
        ks.length ? h('span', { class: 'muted', text: ` · ${ks.map(k => `${k.replace('x', '×')} ${labels[k]}`).join(' · ')}${agree ? ' (all agree)' : ''}` }) : null)],
      ['Area', p.area_um2 != null ? h('span', {}, h('b', { text: `${Math.round(p.area_um2).toLocaleString()} µm²` }),
        p.confidence != null ? h('span', { class: 'muted', text: ` · detector confidence ${Math.round(p.confidence * 100)}%` }) : null) : null],
      ['Residue', p.residue && p.residue.level ? h('span', {}, h('b', { text: p.residue.level }),
        h('span', { class: 'muted', text: ` · ${p.residue.by === 'picker' ? 'Picker' : p.residue.by || ''}${p.residue.evidence ? `: ${p.residue.evidence}` : ''}` }))
        : h('span', { class: 'muted', text: 'not judged yet (the Picker judges it from the photos)' })],
      ['Came from', h('span', {}, `${p.chip_id || x.location}${chip && chip.props.material ? ` (${chip.props.material})` : ''}`,
        rel ? ` · ${fmt(rel[0])}, ${fmt(rel[1])} mm from the chip's corner` : '')],
      ['Source flakes', p.source_flake_ids?.length ? h('span', { text: p.source_flake_ids.join(' + ') }) : null],
      ['Preparation', p.reactor_preparation_id ? h('span', { text: p.reactor_preparation_id }) : null],
      ['Evidence job', p.reactor_evidence_job_id ? h('span', { text: p.reactor_evidence_job_id }) : null],
      ['Now', h('span', { text: nowText(x, inv, this.hooks.roleOf) })],
    ].filter(([, v]) => v);
    const live = this.hooks.live();
    const actions = h('div', { class: 'fl-actions' },
      this.confirm
        ? h('span', { class: 'fl-confirm' }, `Tell Qumus not to use ${shortId(x.id)}?`,
          h('button', { class: 'inv-btn primary danger', text: 'Yes, exclude it', onclick: async () => {
            this.confirm = false; redraw(); await this.hooks.veto(x.id); } }),
          h('button', { class: 'inv-btn', text: 'Cancel', onclick: () => { this.confirm = false; redraw(); } }))
        : h('button', { class: 'inv-btn', text: 'Don\'t use this flake', disabled: !live || x.status === 'vetoed' || x.status === 'stacked' || x.status === 'reactor_consumed' || null,
          title: live ? 'Qumus records it and never picks this flake' : 'Live only: a recording cannot be changed',
          onclick: () => { this.confirm = true; redraw(); } }),
      h('button', { class: 'inv-btn', text: 'Ask Qumus about it', disabled: !live || null,
        title: live ? 'Starts a message to Qumus about this flake' : 'Live only', onclick: () => this.hooks.ask(x.id) }),
      live ? null : h('span', { class: 'muted small', text: 'live only' }));
    const hist = this.hooks.history(x.id);
    return h('div', { class: 'fl-detail', dataset: { id: x.id } },
      big,
      h('div', { class: 'fl-dh' }, h('b', { text: `${p.material || ''} flake ${shortId(x.id)}` }), h('span', { class: 'grow' }),
        h('span', { class: 'tag ' + st[1], text: flakeWhere(x, this.hooks.roleOf) })),
      h('dl', { class: 'kv fl-kv' }, kv.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', {}, v)])),
      h('div', { class: 'd-sec', text: 'History in the lab' }),
      h('ol', { class: 'fl-hist' }, hist.length ? hist.map(e => h('li', {}, h('span', { class: 'n mono', text: e.when || '' }), h('i', { class: e.cls || '' }), h('span', { text: e.text })))
        : [h('li', { class: 'muted', text: 'No history recorded for this flake.' })]),
      actions);
  }
}

// where a flake is now, in words: "top hBN of device_001 on chip_e01 · right board, slot 1"
function nowText(x, inv, roleOf) {
  const p = x.props || {};
  if (x.status === 'stacked') {
    const dev = inv.samples.get(p.device || x.location), chip = dev && inv.samples.get(dev.location);
    const where = chip && placeName(chip.location) ? ` · ${placeName(chip.location)}` : '';
    return `${roleOf && roleOf(x.id) ? `${roleOf(x.id)} of ` : 'in '}${p.device || x.location}${dev ? ` on ${dev.location}` : ''}${where}`;
  }
  if (x.status === 'on_stamp') return `on ${x.location}`;
  const chip = inv.samples.get(p.chip_id || x.location);
  const word = (STATUS[x.status] || [x.status || 'on the chip'])[0];
  return chip && placeName(chip.location) ? `${word} · ${chip.id} in ${placeName(chip.location)}` : word;
}
