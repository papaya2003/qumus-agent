// The sample ledger as lab state (materials, chips, flakes, devices, stamps),
// and the cards that show one chip or one device. Every ledger write is a
// `sample.updated` event (C7), so the same code rebuilds it live (seeded from
// /api/snapshot, then kept current by the events), for a run shown from
// History and in a replay (from the recorded events alone).
//
// A stamp is new (fresh PC film), on the holder, used (its film stayed on a
// device) or torn. A person who put a new PC film on a used or torn stamp says
// so in the stamp's details (live only): POST /api/stamps/{id}/film checks the
// stamp and records the report; the stamp is new again.

import { h, toast, imgUrl, placeName } from './ui.js';
import { THICKNESS, thickColor, thickText, bestPhoto, hasPhoto, shortId } from './flakes.js';

export const STAMP_WORD = { fresh: 'new', in_use: 'on the holder', used: 'used', torn: 'torn' };
export const STAMP_TAG = { fresh: 'fresh', in_use: 'running', used: 'used', torn: 'torn' };
export const REFILMABLE = new Set(['used', 'torn']);
// flakes still on their chip (not lost, vetoed, picked up or stacked)
export const ON_CHIP = new Set(['candidate', 'catalogued']);
const VIA = { 'ui:lab': 'Lab page', chat: 'chat' };

// where a sample is, in words: "right board, slot 1"; on the holder; in a device
// The one photo shown for a sample. A target chip shows the person's own 20x
// photo of the electrode site (user_reference_20x, the same picture the
// stacking flow refinds the electrodes with). The old purple 20x display
// thumbnail (props.images.electrodes, no calibration, often off the metal) is
// never shown, also not in older recordings that still carry it (Jialei,
// 2026-11-06).
export function samplePhoto(x) {
  const ref = x.kind === 'chip' && x.props.user_reference_20x;
  if (ref && ref.image) return { src: ref.image, caption: ref.source === 'simulated_user_photo'
    ? 'Electrode site, 20x reference photo (simulated)' : 'Electrode site, 20x reference photo' };
  const imgs = x.props.images;
  const list = Array.isArray(imgs) ? imgs
    : Object.entries(imgs || {}).filter(([k]) => !(x.kind === 'chip' && k === 'electrodes')).map(([, v]) => v);
  if (!list.length) return null;
  return { src: list[list.length - 1], caption: x.props.image_source === 'simulated' ? 'Simulated optical image' : 'Recorded image' };
}

export function whereText(x, inv) {
  if (!x) return '';
  if (x.kind === 'stamp' && x.status === 'in_use') return 'on the stamp holder';
  if (placeName(x.location)) return placeName(x.location);
  const host = inv && inv.samples.get(x.location);
  if (host && placeName(host.location)) return `on ${host.id} · ${placeName(host.location)}`;
  return x.location || '';
}

// Material identity comes from the inventory and a person's review, never image colour.
export function materialReview(x) {
  const r = (x.props.material_reviews || []).at(-1);
  return r?.via === 'ui:lab' && r.material === x.props.material ? r : {status: 'unreviewed'};
}
export function materialReviewText(x) {
  const r = materialReview(x);
  return r.status === 'confirmed' ? `Confirmed by ${r.by}` : r.status === 'uncertain' ? `Identity uncertain · ${r.by}` : 'Not yet confirmed by a person';
}
export function motherTapeCard(x, inv, {open, on}) {
  const r = materialReview(x);
  return h('button', {class: 'card mother-tape-card' + (on === x.id ? ' on' : ''), onclick: () => open(x.id)},
    h('div', {class: 'cc-h'}, h('b', {text: x.props.material || 'Unknown material'}), h('span', {class: 'muted', text: 'mother tape'})),
    h('span', {class: 'mono muted', text: x.id}),
    h('span', {text: whereText(x, inv)}),
    h('span', {class: r.status === 'confirmed' ? 'material-confirmed' : 'muted small', text: materialReviewText(x)}));
}

export class Inventory {
  constructor() { this.reset(); }

  reset() {
    this.samples = new Map();
    this.refs = new Map();
    this.known = false;   // anything seen (old recordings carry no ledger events)
    this.version = 0;
  }

  // the ledger as GET /api/snapshot returns it (live)
  seed(list) {
    for (const x of list || []) this.samples.set(x.id, { id: x.id, kind: x.kind, location: x.location, status: x.status, props: x.props || {} });
    this.known = true;
    this.version++;
  }

  // one event; true when the inventory changed
  add(e) {
    const p = e.payload || {};
    if (e.kind === 'sample.removed' && p.id) {
      // a new testbench in a kept run store: what the earlier bench made is gone
      if (!this.samples.delete(p.id)) return false;
      this.version++;
      return true;
    }
    if (e.kind === 'tool.result') return this.noteReferences(p.data);
    if (e.kind !== 'sample.updated') return false;
    if (!p.id) return false;
    // the event carries the sample's whole state (its shown props), not a patch
    const props = p.props || {}, ref = this.refs.get(p.id);
    this.samples.set(p.id, { id: p.id, kind: p.kind, location: p.location, status: p.status,
      props: ref && !props.user_reference_20x ? { ...props, user_reference_20x: ref } : props });
    this.known = true;
    this.version++;
    return true;
  }

  // A target chip's 20x electrode-site photo (user_reference_20x). Recordings
  // made before it rode on sample.updated (e.g. hallbar seed 3 r13, 11-05)
  // carry it only in the samples a list_samples/get_sample result returned;
  // take it from there so the chip details can show it.
  noteReferences(data) {
    const rows = Array.isArray(data?.samples) ? data.samples : data?.sample ? [data.sample] : [];
    let changed = false;
    for (const r of rows) {
      const ref = r && r.kind === 'chip' && r.props?.user_reference_20x;
      if (!ref?.image || !r.id) continue;
      this.refs.set(r.id, ref);
      const x = this.samples.get(r.id);
      if (x && !x.props.user_reference_20x) { x.props = { ...x.props, user_reference_20x: ref }; changed = true; }
    }
    if (changed) this.version++;
    return changed;
  }

  of(kind) { return [...this.samples.values()].filter(x => x.kind === kind).sort((a, b) => a.id.localeCompare(b.id)); }

  // every flake found on a chip (wherever it is now: on a stamp, in a device)
  flakesOf(chipId) {
    return [...this.samples.values()].filter(x => x.kind === 'flake' && (x.props.chip_id || x.location) === chipId);
  }

  stampStatus(id) {
    const x = this.samples.get(id);
    return x && x.kind === 'stamp' ? x.status : undefined;
  }

  // a chip that carries (or will carry) a device: pre-patterned electrodes
  isTarget(c) {
    return c.status === 'patterned' || !!c.props.electrodes || this.of('device').some(d => d.location === c.id);
  }

  // The stock in groups, in the order the Lab shows them
  groups() {
    const chips = this.of('chip');
    const flakes = this.of('flake');
    return [
      { key: 'devices', title: 'Finished devices', items: this.of('device') },
      { key: 'exfoliated', title: 'Exfoliated chips', items: chips.filter(c => !this.isTarget(c) && c.status !== 'bare') },
      { key: 'flakes', title: 'Flakes', items: flakes, note: `${flakes.filter(hasPhoto).length} with photos` },
      { key: 'materials', title: 'Mother tapes & daughter tape', items: [...this.of('carrier'), ...this.of('tape')] },
      { key: 'bare', title: 'Blank chips', items: chips.filter(c => !this.isTarget(c) && c.status === 'bare') },
      { key: 'target', title: 'Target chips (electrodes)', items: chips.filter(c => this.isTarget(c)) },
      { key: 'stamps', title: 'Stamps', items: this.of('stamp'), note: `${this.of('stamp').filter(x => x.status === 'fresh').length} new` },
    ];
  }

  stampFacts(x) {
    const pr = x.props, stack = pr.stack || [];
    const last = (pr.film_reports || []).at(-1);
    return [
      pr.support?.film_type ? `Known contact film: ${pr.support.film_type} (operator report)` : null,
      x.status === 'in_use' ? null : x.status === 'torn' ? 'support torn' : pr.support?.film_type ? `support ${x.status}` : pr.film ? `${pr.film} film` : 'no film',
      stack.length ? `carries ${stack.join(', ')}` : null,
      last ? `new film reported by ${last.by || 'a person'} (${VIA[last.via] || last.via})` : null,
    ].filter(Boolean);
  }

  async reviewMaterial(id, by, status, note) {
    const x = this.samples.get(id);
    if (!x) return false;
    try {
      const r = await fetch(`/api/carriers/${encodeURIComponent(id)}/material-review`, {method: 'POST',
        headers: {'content-type': 'application/json'}, body: JSON.stringify({expected_material: x.props.material, by, status, note})});
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { toast(`Not recorded: ${body.detail || r.status}`); return false; }
      this.samples.set(id, {...x, props: {...x.props, material_reviews: [...(x.props.material_reviews || []), body]}});
      this.version++;
      toast(status === 'confirmed' ? 'Human material review recorded' : 'Material marked uncertain; excluded from exfoliation');
      return true;
    } catch { toast('Not recorded: the server did not answer'); return false; }
  }

  // A person put a new PC film on a used or torn stamp (live only). Resolves
  // true when recorded; the sample.updated event follows, the stamp shows new at once.
  async newFilm(id, by) {
    try {
      const r = await fetch(`/api/stamps/${encodeURIComponent(id)}/film`, { method: 'POST',
        headers: { 'content-type': 'application/json' }, body: JSON.stringify({ by: (by || '').trim() }) });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { toast(`Not recorded: ${body.detail || r.status}`); return false; }
      const x = this.samples.get(id);
      if (x) this.samples.set(id, { ...x, status: body.status, props: { ...x.props, film: 'PC', stack: [],
        film_reports: [...(x.props.film_reports || []), body] } });
      this.version++;
      toast(`${id} is new again · film report recorded`);
      return true;
    } catch {
      toast('Not recorded: the server did not answer');
      return false;
    }
  }
}

// ---------------------------------------------------------------- cards
// One exfoliated chip: its best flake photos, the flakes by thickness.
// hooks: open(id) a flake or the chip itself; on: the selected id
export function chipCard(c, inv, hooks) {
  const fl = inv.flakesOf(c.id), shot = fl.filter(hasPhoto).sort((a, b) => (b.props.area_um2 || 0) - (a.props.area_um2 || 0));
  const mat = c.props.material, labels = THICKNESS[mat] || [...new Set(fl.map(f => f.props.thickness_label).filter(Boolean))];
  const n = l => fl.filter(f => f.props.thickness_label === l).length;
  const used = fl.filter(f => !ON_CHIP.has(f.status)).length;
  const thumbs = shot.slice(0, 4).map(f => h('button', { class: 'cc-th', title: `${f.id} · ${thickText(mat, f.props.thickness_label)}`,
    onclick: ev => { ev.stopPropagation(); hooks.open(f.id); } },
  h('img', { src: imgUrl(bestPhoto(f)), alt: f.id, loading: 'lazy' }),
  !ON_CHIP.has(f.status) ? h('span', { class: 'cc-used', text: 'used' }) : null));
  if (fl.length > thumbs.length) thumbs.push(h('span', { class: 'cc-th more', text: `+${fl.length - thumbs.length}` }));
  const bar = fl.length ? h('div', { class: 'cc-bar', title: 'Flakes by thickness' }, labels.filter(n).map(l => h('i', { style: `flex:${n(l)};background:${thickColor(mat, l)}` }))) : null;
  const facts = !fl.length ? (c.status === 'searched' ? 'searched · no flake found' : 'not searched yet')
    : `${fl.length} flake${fl.length === 1 ? '' : 's'} · ${shot.length} photographed${used ? ` · ${used} used or lost` : ''}`;
  return h('div', { class: 'card chipcard' + (hooks.on === c.id ? ' on' : ''), dataset: { id: c.id }, role: 'button', tabindex: '0',
    title: `${c.id} · details`, onclick: () => hooks.open(c.id), onkeydown: ev => { if (ev.key === 'Enter') hooks.open(c.id); } },
  h('div', { class: 'cc-h' }, h('b', { class: 'mono', text: c.id }), mat ? h('span', { class: 'cc-mat', text: mat }) : null),
  thumbs.length ? h('div', { class: 'cc-thumbs' }, thumbs) : h('div', { class: 'cc-none', text: 'no flake photos' }),
  bar, h('div', { class: 'cc-f muted', text: facts }));
}

// One finished device: its layers top first, each with its flake's photo.
export function deviceCard(d, inv, hooks) {
  const layers = d.props.layers_top_first || [], mats = d.props.materials || [];
  const roleOf = hooks.roleOf || (() => null);
  const photos = Array.isArray(d.props.images) ? d.props.images : Object.values(d.props.images || {});
  return h('div', { class: 'card devcard' + (hooks.on === d.id ? ' on' : ''), dataset: { id: d.id }, role: 'button', tabindex: '0',
    title: `${d.id} · details`, onclick: () => hooks.open(d.id), onkeydown: ev => { if (ev.key === 'Enter') hooks.open(d.id); } },
  h('div', { class: 'cc-h' }, h('b', { class: 'mono', text: d.id }), h('span', { class: 'muted', text: `${layers.length} layers` })),
  photos.length ? h('figure', { class: 'sample-photo' },
    h('img', { src: imgUrl(photos.at(-1)), alt: `${d.id} after stacking`, loading: 'lazy' })) : null,
  layerStack(d, inv, roleOf),
  h('div', { class: 'dc-layers' }, layers.map((id, i) => {
    const f = inv.samples.get(id), src = f ? bestPhoto(f) : null, mat = (f && f.props.material) || mats[i] || '';
    return h('button', { class: 'dc-l', title: `${id} · details`, onclick: ev => { ev.stopPropagation(); hooks.open(id); } },
      src ? h('img', { src: imgUrl(src), alt: id, loading: 'lazy' }) : h('span', { class: 'fnone', text: 'no photo' }),
      h('small', {}, h('span', { class: 'mono', text: shortId(id) }), ` ${roleOf(id) || mat}`));
  })),
  h('div', { class: 'cc-f muted', text: `on ${d.location}${whereText(inv.samples.get(d.location)) ? ` · ${whereText(inv.samples.get(d.location))}` : ''}` }));
}

// The layers as coloured bars, top first ("top hBN · 0–10 nm")
export function layerStack(d, inv, roleOf = () => null) {
  const layers = d.props.layers_top_first || [], mats = d.props.materials || [];
  return h('div', { class: 'lstack' }, layers.map((id, i) => {
    const f = inv.samples.get(id), p = (f && f.props) || {}, mat = p.material || mats[i] || '';
    const dark = (THICKNESS[mat] || []).indexOf(p.thickness_label) >= 2 || (!p.thickness_label && /gr|graph/i.test(mat));
    return h('div', { class: 'lbar' + (dark ? ' dark' : ''), style: `--c:${p.thickness_label ? thickColor(mat, p.thickness_label) : /hbn/i.test(mat) ? 'var(--m-hbn)' : 'var(--m-gr)'}` },
      h('span', { text: [roleOf(id) || mat, p.thickness_label ? thickText(mat, p.thickness_label) : null].filter(Boolean).join(' · ') }));
  }));
}

// One stamp as a pill (Stock): its id and state
export function stampPill(x, hooks) {
  return h('button', { class: `stamp-pill ${x.status || ''}${hooks.on === x.id ? ' on' : ''}`, dataset: { id: x.id }, title: `${x.id} · details`,
    onclick: () => hooks.open(x.id) },
  h('span', { class: 'mono', text: x.id }), h('span', { class: 'tag ' + (STAMP_TAG[x.status] || ''), text: STAMP_WORD[x.status] || x.status }));
}
