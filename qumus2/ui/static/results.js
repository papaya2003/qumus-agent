// Results: a report of products and decisions, with index → entries → detail.
// Diagnostic job records remain reachable explicitly from Progress.
// Historical design submissions are retained, even for multiple devices.
import { h, label, jobTitle, RUN_STATUS, fmtDur, imgUrl, imageSource, stepName, short, VERDICT, linkIds, costText, markdown } from './ui.js';
import { renderDevicePlan, renderSelection, renderLayout, layoutDrawing, planThumbnail } from './planbook.js';
import { FlakeGrid, flakeCard, bestPhoto, hasPhoto, thickText, shortId } from './flakes.js';
import { chipCard, layerStack, whereText } from './inventory.js';
import { problemState } from './panel.js';
import { OUTCOME } from './session.js';
import { sampleDetail } from './details.js';
import { requestChart, requestSeries, requestInspector } from './requestchart.js';
import { materialMetalSummary } from './materialmetal.js';

const STEP_STATUS = { in_progress: 'in progress', done: 'done', failed: 'failed', skipped: 'skipped' };
const n2 = x => (typeof x === 'number' ? (Math.abs(x) >= 100 ? Math.round(x) : +x.toFixed(2)) : x);
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const messagePreview = text => {
  const node = markdown(text);
  // Keep code/sample identifiers, and separate Markdown blocks in plain text.
  node.querySelectorAll('p, li, th, td, h1, h2, h3, h4, h5, h6, pre, br').forEach(el => el.append(' '));
  return short(node.textContent.replace(/\s+/g, ' ').trim(), 320);
};

// a check's measurement against what is allowed, in a few words ("0.18 / 0.5 mm")
export function measureText(m = {}, e = {}) {
  m = m || {}; e = e || {};
  const pair = (v, tol, unit) => `${n2(v)} / ${n2(tol)} ${unit}`;
  if (m.error_mm != null && e.tolerance_mm != null) return pair(m.error_mm, e.tolerance_mm, 'mm');
  if (m.offset_mm != null && e.tolerance_mm != null) return pair(m.offset_mm, e.tolerance_mm, 'mm');
  if (m.offset_from_nominal_mm != null && e.tolerance_mm != null) return pair(m.offset_from_nominal_mm, e.tolerance_mm, 'mm');
  if (m.offset_um != null && e.centre_tolerance_um != null) return pair(m.offset_um, e.centre_tolerance_um, 'µm');
  if (m.offset_mm != null) return `off ${n2(m.offset_mm)} mm${m.drot_deg != null ? ` · ${n2(m.drot_deg)}°` : ''}`;
  if (m.rotation_deg != null) return `${n2(m.rotation_deg)}°${m.edge_mm != null ? ` · edge ${n2(m.edge_mm)} mm` : ''}`;
  if (m.tiles_scanned != null) return `${m.tiles_scanned} tiles · ${Array.isArray(m.candidates) ? m.candidates.length : m.candidates ?? '?'} candidates`;
  if (m.revisited != null) return `${m.found_again} / ${m.revisited} found again`;
  if (m.celsius != null) return `${n2(m.celsius)} °C${e.celsius != null ? ` / ${n2(e.celsius)} ± ${n2(e.band_c)} °C` : ''}`;
  if (m.reached_c != null) return `${n2(m.reached_c)} °C reached`;
  if (m.shift_mm != null && e.max_shift_mm != null) return pair(m.shift_mm, e.max_shift_mm, 'mm');
  if (m.flake_on_stamp != null) return m.flake_on_stamp ? 'flake on the stamp' : 'flake not on the stamp';
  return '';
}

// "Scanned 412 tiles of chip_001 and found 37 Gr candidates. ..."
function jobSummary(run, steps) {
  const p = run.params || {}, by = n => steps.find(st => st.name === n), fixes = steps.reduce((n, st) => n + (st.corrections || 0), 0);
  const out = [];
  if (run.flow === 'search_chip' || run.flow === 'revisit_chip') {
    const scan = by('scan_chip'), rev = steps.find(st => /^revisit/.test(st.name));
    if (scan) { const m = scan.measurements || {}; out.push(`Scanned ${m.tiles_scanned ?? 'the'} tiles of ${p.chip_id || 'the chip'} at 10× and found ${Array.isArray(m.candidates) ? m.candidates.length : m.candidates ?? 'some'} ${p.material || ''} candidates.`.replace(/ +/g, ' ')); }
    if (rev) { const m = rev.measurements || {}; out.push(`Went back to ${m.revisited} of them at 20× and 100×; ${m.found_again} found again${m.lost ? `, ${m.lost} lost` : ''}.`); }
  } else if (run.flow === 'stack_device') {
    out.push(`Stacked ${(p.flake_ids || []).join(', ') || 'the flakes'} onto ${p.target_chip_id || 'the target chip'}${p.stamp_id ? ` with ${p.stamp_id}` : ''}.`);
  } else if (run.flow === 'exfoliate_chips') {
    out.push(`Exfoliated ${p.material || ''} onto ${(p.chip_ids || []).join(', ') || 'the chips'}.`.replace(/ +/g, ' '));
  } else if (run.flow === 'place_chip_on_transfer_stage') {
    out.push(`Put ${p.chip_id || 'the chip'} on the transfer stage.`);
  }
  const done = steps.filter(st => st.status === 'done').length;
  out.push(`${done} of ${steps.length} steps done${fixes ? `, ${fixes} automatic correction${fixes === 1 ? '' : 's'}` : ''}${run.status === 'succeeded' ? '' : `; the job ${RUN_STATUS[run.status] || run.status}`}.`);
  if (run.error) out.push(typeof run.error === 'string' ? run.error : run.error.message || JSON.stringify(run.error));
  return out.join(' ');
}

// how long the instruments worked on a job (the lab clock)
export const jobLabTime = r => (typeof r.lab1 === 'number' && typeof r.lab0 === 'number' && r.lab1 > r.lab0 ? r.lab1 - r.lab0 : 0);

export class Results {
  // s: the Session; hooks: live() -> may talk to Qumus; run(id) -> Promise of the
  // job's record; photo(src); ask(id); veto(id); changed() redraw; jump(key) the
  // tree row; showRight() (a phone shows the details column); costs() -> show cost
  constructor(s, hooks) {
    this.s = s;
    this.hooks = hooks;
    this.doc = 'overview';   // the centre: 'overview' | 'flakes' | 'chips' | 'job:<id>'
    this.sel = null;         // the right column: { t, id }
    this.jobNode = null;     // the job record being shown (built once per open)
    this.grid = new FlakeGrid({
      list: () => this.foundFlakes(),
      selected: () => (this.sel && this.sel.t === 'sample' ? this.sel.id : null),
      select: id => this.select({ t: 'sample', id }),
      roleOf: id => s.roleOf(id), scoreOf: id => s.scoreOf(id),
    });
  }

  reset() { this.doc = 'overview'; this.sel = null; this.jobNode = null; this.requestChartState = {}; }

  humanReviewForm(run) {
    if (!this.hooks.live() || run.status !== 'waiting_human' || !run.human_review?.failure_event_id || !this.hooks.humanReview) return null;
    const reviewer = h('input', { name: 'reviewer', 'aria-label': 'Reviewer', required: true, maxlength: 120 });
    const reason = h('textarea', { name: 'reason', 'aria-label': 'Review reason', required: true, maxlength: 2000, rows: 2 });
    const message = h('p', { class: 'muted small', role: 'status' });
    const button = h('button', { type: 'submit', class: 'inv-btn primary', text: 'Record human review' });
    return h('form', { class: 'jr-fold material-review-form', 'aria-label': 'Review job hold', onsubmit: async event => {
      event.preventDefault();
      if (!reviewer.value.trim() || !reason.value.trim()) { message.textContent = 'Enter your name and what you reviewed.'; return; }
      button.disabled = true;
      try {
        await this.hooks.humanReview(run.id, { failure_event_id: run.human_review.failure_event_id,
          reviewer: reviewer.value.trim(), reason: reason.value.trim() });
        await this.buildJob(run.id, this.jobNode);
      } catch (error) { message.textContent = error.message || 'The hold could not be reviewed. Reload the job record.'; }
      finally { button.disabled = false; }
    } }, h('div', { class: 'section-t', text: 'Review this job hold' }),
    h('p', { class: 'muted small', text: 'Record your review of this failure. Normal recovery remains a separate action.' }),
    h('label', {}, 'Reviewer', reviewer), h('label', {}, 'What you reviewed or fixed', reason), button, message);
  }

  // ------------------------------------------------------------ data
  foundFlakes() {
    const t = this.s.touched().flakes;
    return [...t].map(id => this.s.inv.samples.get(id)).filter(Boolean);
  }
  usedFlakes() {
    const b = this.s.book, chosen = b.selections.flatMap(s => Object.values(s.selected_by_role || {}).flat());
    const used = this.foundFlakes().filter(f => f.status === 'stacked' || f.status === 'on_stamp').map(f => f.id);
    return [...new Set([...chosen, ...used])];
  }
  devices() { return [...this.s.touched().devices].map(id => this.s.inv.samples.get(id)).filter(Boolean); }
  chips() { return [...this.s.touched().chips].map(id => this.s.inv.samples.get(id)).filter(x => x && !this.s.inv.isTarget(x) && !x.props.reactor_substrate); }
  reactorProducts() { return this.foundFlakes().filter(x => x.status === 'reactor_product'); }
  materialMetalCarriers() { return this.s.inv.of('chip').filter(x => x.props.material_metal_preparation?.scope === 'device_simulation'); }

  // what a person still has to do, from the records (and what Qumus asked, labelled)
  todos() {
    const out = [], s = this.s;
    for (const r of s.book.runs.values()) {
      if (r.status === 'waiting_human' || r.status === 'parked') out.push({ cls: 'warn', text: `${jobTitle(r.flow, r.params)}: ${RUN_STATUS[r.status]}${r.message ? ` · ${r.message}` : ''}` });
      else if (r.status === 'paused') out.push({ cls: 'warn', text: `${jobTitle(r.flow, r.params)} is paused: press Continue to go on` });
    }
    const st = summaryOf(s);
    if (st.outcome === 'stopped') out.push({ cls: 'warn', text: 'The run was stopped: press Continue so the paused jobs go on' });
    if (st.outcome === 'error') out.push({ cls: 'bad', text: 'Agent stopped with an error: send a message to try again' });
    for (const step of s.book.latest?.plan || []) {
      if (step.owner === 'human' && ['running', 'in_progress'].includes(step.status)) {
        out.push({ cls: 'warn', text: `${step.title}${step.note ? `: ${step.note}` : ''}` });
      }
    }
    for (const x of s.inv.of('stamp')) if (x.status === 'torn') out.push({ cls: 'warn', text: `${x.id}: the PC film is torn (${whereText(x, s.inv)}). Put a new film on it, then say so in Lab › Stock` });
    const ask = s.asks[s.asks.length - 1];
    if (ask && out.length) out.push({ cls: 'ai', text: ask.payload.text, ai: 'Qumus asked' });
    return out;
  }

  // ------------------------------------------------------------ left: the index
  renderIndex(box) {
    const b = this.s.book, rows = [];
    const row = (key, title, count) => h('button', { class: `drow${this.doc === key ? ' on' : ''}`,
      dataset: { key }, title, onclick: () => this.openDoc(key) },
      h('span', { class: 'dt', text: title }), h('span', { class: 'dn', text: count ?? '' }));
    rows.push(row('overview', 'Overview', OUTCOME[summaryOf(this.s).outcome]?.[0]));
    const products = [['devices', 'Devices', this.devices().length],
      ['plans', 'Device Guidance', b.devicePlans.length], ['layouts', 'Layouts', b.designs.length]];
    if (products.some(x => x[2])) rows.push(h('div', { class: 'sec-t', text: 'Device reports' }),
      ...products.filter(x => x[2]).map(x => row(...x)));
    const materials = [['used', 'Selected flakes', this.usedFlakes().length],
      ['reactor', 'Reactor products', this.reactorProducts().length],
      ['material-metal', 'Material-metal simulations', this.materialMetalCarriers().length],
      ['flakes', 'All flakes found', this.foundFlakes().length], ['chips', 'Exfoliated chips', this.chips().length]];
    if (materials.some(x => x[2])) rows.push(h('div', { class: 'sec-t', text: 'Materials' }),
      ...materials.filter(x => x[2]).map(x => row(...x)));
    if (!this.s.request) rows.push(h('div', { class: 'placeholder', text: 'Products, images and accepted designs will appear here.' }));
    const keep = box.scrollTop;
    box.replaceChildren(...rows); box.scrollTop = keep;
  }
  isSel(t, id) { return !!this.sel && this.sel.t === t && (id === undefined || this.sel.id === id); }

  openDoc(doc) {
    this.doc = doc === 'context' ? 'overview' : doc;
    this.sel = null;
    if (!doc.startsWith('job:')) this.jobNode = null;
    this.hooks.changed('centre');
    this.hooks.showCentre?.();
  }
  select(item) { this.sel = item; this.hooks.changed('right'); this.hooks.showRight?.(); }

  contextView(state, view = 'recording') {
    this.activeRequestChartState = state;
    const revision = this.s.events.findLast(e => ['agent.request', 'agent.request_usage', 'agent.request_failure', 'agent.usage', 'notification'].includes(e.kind))?.id ?? 0;
    if (this.contextChart?.state === state && this.contextChart.view === view && this.contextChart.revision === revision) {
      this.sel = state.current == null ? null : { t: 'request', id: state.current };
      return this.contextChart.node;
    }
    const node = requestChart(this.s.events, state, { view, onSelect: (point, reveal) => {
      this.sel = point ? { t: 'request', id: point.key } : null;
      this.hooks.changed('right');
      if (reveal) this.hooks.showRight?.();
    } });
    this.sel = state.current === null ? null : { t: 'request', id: state.current };
    this.contextChart = { state, view, revision, node };
    return node;
  }

  // ------------------------------------------------------------ centre
  // into: { title, sub, home (button), body }
  renderCentre(into) {
    const d = this.doc;
    into.home.hidden = d === 'overview';
    if (d === 'flakes') {
      into.title.textContent = 'All flakes found';
      into.sub.textContent = 'click a flake for its details';
      if (!into.body.querySelector(':scope > .fl-box')) into.body.replaceChildren(h('div', { class: 'fl-box' }));
      this.grid.render(into.body.querySelector('.fl-box'));
      return;
    }
    if (d === 'material-metal') {
      into.title.textContent = 'Material-metal simulations';
      into.sub.textContent = 'observed device simulation; real chemistry remains unverified';
      into.body.replaceChildren(h('div', { class: 'fl-grid' }, this.materialMetalCarriers().map(x => {
        const m = materialMetalSummary(x, this.s.events);
        return this.entry(x.id, m.layers.join(' / '), `${m.status}${m.progress == null ? '' : ` · qualitative progress ${(m.progress * 100).toFixed(0)}%`}`,
          m.image ? h('img', {src: imgUrl(m.image), alt: `${x.id} simulated material-metal observation`, loading: 'lazy'}) : null,
          { t: 'sample', id: x.id });
      })));
      return;
    }
    if (d === 'reactor') {
      into.title.textContent = 'Reactor products';
      into.sub.textContent = 'one accepted product per prepared pair; select for sources and evidence';
      into.body.replaceChildren(h('div', { class: 'fl-grid' }, this.reactorProducts().map(x =>
        flakeCard(x, { on: this.isSel('sample', x.id), select: id => this.select({ t: 'sample', id }) }))));
      return;
    }
    if (d === 'devices') {
      into.title.textContent = 'Devices'; into.sub.textContent = 'choose a device to see its report on the right';
      into.body.replaceChildren(h('div', { class: 'fl-grid' }, this.devices().map(dev => {
        const photos = Array.isArray(dev.props.images) ? dev.props.images : Object.values(dev.props.images || {});
        const src = photos.at(-1);
        return this.entry(dev.id, dev.location, `${(dev.props.layers_top_first || []).length} layer(s)`,
          src ? h('img', {src: imgUrl(src), alt: `${dev.id} after stacking`, loading: 'lazy'}) : layerStack(dev, this.s.inv, id => this.s.roleOf(id)),
          { t: 'sample', id: dev.id });
      })));
      return;
    }
    if (d === 'plans' || d === 'layouts') {
      const layouts = d === 'layouts', entries = layouts ? this.s.book.designs : this.s.book.devicePlans;
      into.title.textContent = layouts ? 'Layouts' : 'Device Guidance';
      into.sub.textContent = `${entries.length} accepted · choose an entry to see its contents on the right`;
      into.body.replaceChildren(h('div', { class: 'fl-grid' }, entries.map((item, i) =>
        this.entry(layouts ? `Layout ${i + 1}` : `Device Guidance ${i + 1}`,
          layouts ? (item.flake_ids || []).join(', ') : item.device_name || 'Layer specification',
          layouts ? `${(item.shapes || []).length} shapes` : `${(item.layer_stack || []).length} layers`,
          layouts ? layoutDrawing(item) : planThumbnail(item),
          { t: layouts ? 'layout' : 'plan', id: item.recordId }))));
      return;
    }
    if (d === 'used') {
      into.title.textContent = 'Selected flakes'; into.sub.textContent = 'choose a flake to see its images and assessment';
      into.body.replaceChildren(h('div', { class: 'fl-grid' }, this.usedFlakes().map(id => this.s.inv.samples.get(id)).filter(Boolean).map(f =>
        flakeCard(f, {on: this.isSel('sample', f.id), select: id => this.select({t: 'sample', id}), roleOf: id => this.s.roleOf(id)}))));
      return;
    }
    if (d === 'chips') {
      into.title.textContent = 'Exfoliated chips';
      into.sub.textContent = 'made in this run';
      const sel = this.sel && this.sel.t === 'sample' ? this.sel.id : null;
      into.body.replaceChildren(h('div', { class: 'wall' }, this.chips().map(c => chipCard(c, this.s.inv, { open: id => this.select({ t: 'sample', id }), on: sel }))));
      return;
    }
    if (d.startsWith('job:')) {
      const id = d.slice(4), r = this.s.book.runs.get(id);
      into.title.textContent = r ? jobTitle(r.flow, r.params) : 'Job record';
      into.sub.textContent = 'job record';
      if (!this.jobNode || this.jobNode.dataset.id !== id) {
        this.jobNode = h('div', { class: 'jobrec', dataset: { id } }, h('div', { class: 'muted', text: 'Loading…' }));
        this.buildJob(id, this.jobNode);
      }
      if (into.body.firstChild !== this.jobNode) into.body.replaceChildren(this.jobNode);
      return;
    }
    into.title.textContent = 'Results';
    into.sub.textContent = this.hooks.subtitle ? this.hooks.subtitle() : 'of this run';
    const keep = into.body.scrollTop;
    into.body.replaceChildren(this.overview());
    into.body.scrollTop = keep;
  }

  overview() {
    const s = this.s, st = summaryOf(s), out = [];
    // the verdict: the request in the person's words, then the facts from the records
    const [otext, ocls] = OUTCOME[st.outcome] || [st.outcome, ''];
    const devs = this.devices();
    const tags = [
      h('span', { class: 'tag ' + ocls, text: `${st.outcome === 'done' ? '✓ ' : ''}${otext}${st.outcome === 'done' && devs.length ? ` · ${devs.map(d => d.id).join(', ')}` : ''}` }),
      st.lab_s >= 1 ? h('span', { class: 'tag', text: `${fmtDur(st.lab_s)} lab time`, title: 'How long the instruments worked (the lab clock)' }) : null,
      this.hooks.costs?.() && st.cost_usd ? h('span', { class: 'tag', text: costText(st.cost_usd, st.cost_basis) }) : null,
      ...st.faults.map(f => h('span', { class: 'tag warn', text: `testbench fault: ${f.replace(/_/g, ' ')}` })),
    ];
    const w = s.words;
    out.push(h('section', { class: 'verdict' },
      h('h3', { text: st.request ? `“${st.request}”` : 'No request yet' }),
      h('div', { class: 'tags' }, tags),
      w ? h('p', { class: 'ai-words' }, h('span', { class: 'ai-k', text: 'Qumus reported' }), messagePreview(w.payload.text)) : null));

    // products: devices first, then the flakes used, then what was found
    if (devs.length) {
      out.push(h('div', { class: 'o-h', text: devs.length > 1 ? `Devices · ${devs.length}` : 'Device' }));
      for (const d of devs) out.push(this.deviceBlock(d));
    }
    const used = this.usedFlakes();
    if (used.length && !devs.length) {
      out.push(h('div', { class: 'o-h' }, h('span', { text: `Flakes used · ${used.length}` }), h('button', { class: 'lnk-btn', text: 'details ›', onclick: () => this.openDoc('used') })));
      out.push(this.strip(used.map(id => s.inv.samples.get(id)).filter(Boolean), x => s.roleOf(x.id)));
    }
    const found = this.foundFlakes();
    if (found.length) {
      const chips = new Set(found.map(f => f.props.chip_id || f.location));
      out.push(h('div', { class: 'o-h' }, h('span', { text: `All flakes found · ${found.length} on ${plural(chips.size, 'chip')}` }),
        h('button', { class: 'lnk-btn', text: 'open all ›', onclick: () => this.openDoc('flakes') })));
      const shot = found.filter(hasPhoto).sort((a, b) => (b.props.area_um2 || 0) - (a.props.area_um2 || 0)).slice(0, 10);
      out.push(shot.length ? this.strip(shot, x => (x.status === 'stacked' || x.status === 'on_stamp' ? 'used' : null), found.length)
        : h('div', { class: 'muted small', text: 'No flake photos in this run (recorded before flake photos were kept, or no flake was photographed at 20× or 100×). “open all” lists them.' }));
    }
    const chips = this.chips();
    if (chips.length && !found.length) {
      out.push(h('div', { class: 'o-h' }, h('span', { text: `Exfoliated chips · ${chips.length}` }), h('button', { class: 'lnk-btn', text: 'open ›', onclick: () => this.openDoc('chips') })));
    }

    // Accepted documents are report products. Hardware diagnostics belong in Progress.
    for (const [doc, title, entries] of [['plans', 'Device Guidance', s.book.devicePlans], ['layouts', 'Layouts', s.book.designs]]) {
      if (entries.length) out.push(h('div', { class: 'o-h' }, h('span', { text: `${title} · ${entries.length}` }),
        h('button', { class: 'lnk-btn', text: `Open ${title.toLowerCase()} ›`, onclick: () => this.openDoc(doc) })));
    }

    // what is left for a person
    const todo = this.todos();
    out.push(h('div', { class: 'o-h', text: 'Left for a person' }));
    out.push(todo.length ? h('ul', { class: 'todo-list' }, todo.map(t => h('li', { class: t.cls }, t.ai ? h('span', { class: 'ai-k', text: `${t.ai} (AI)` }) : null, t.text)))
      : h('div', { class: 'nothing', text: st.request ? 'Nothing left for a person.' : 'Nothing yet.' }));
    const node = h('div', { class: 'overview' }, out);
    linkIds(node, id => this.select({ t: 'sample', id }), id => s.inv.samples.has(id));
    return node;
  }

  entry(title, subtitle, facts, preview, item) {
    return h('button', { class: 'fcard report-card' + (this.isSel(item.t, item.id) ? ' on' : ''),
      title: `${title} · ${subtitle}`, dataset: {id: item.id}, onclick: () => this.select(item) },
      h('div', {class: 'fimg report-preview'}, preview || h('span', {class: 'fnone', text: 'No preview'})),
      h('div', {class: 'fmeta'}, h('span', {text: title}), h('span', {class: 'muted', text: facts})),
      h('div', {class: 'fmeta muted'}, h('span', {text: subtitle, title: subtitle})));
  }

  deviceBlock(d) {
    const s = this.s, layers = d.props.layers_top_first || [];
    const photos = Array.isArray(d.props.images) ? d.props.images : Object.values(d.props.images || {});
    const src = photos.at(-1);
    return h('div', { class: 'devblock' },
      h('div', { class: 'db-l' }, layerStack(d, s.inv, id => s.roleOf(id)),
        src ? h('figure', { class: 'sample-photo' }, h('img', { src: imgUrl(src), alt: `${d.id} after stacking`, onclick: () => this.hooks.photo(src) }),
          h('figcaption', { text: 'Recorded view after stacking' })) : null,
        h('div', { class: 'muted small', text: `on ${d.location} · ${whereText(s.inv.samples.get(d.location), s.inv) || ''}` }),
        h('button', { class: 'lnk-btn', text: `${d.id} details ›`, onclick: () => this.select({ t: 'sample', id: d.id }) })),
      h('div', { class: 'db-r' }, layers.map(id => this.flakeTile(s.inv.samples.get(id), id, s.roleOf(id)))));
  }

  flakeTile(f, id, note) {
    const p = (f && f.props) || {}, src = f ? bestPhoto(f) : null;
    return h('button', { class: 'ftile' + (this.isSel('sample', id) ? ' on' : ''), title: `${id} · details`, onclick: () => this.select({ t: 'sample', id }) },
      src ? h('img', { src: imgUrl(src), alt: id, loading: 'lazy' }) : h('span', { class: 'fnone', text: 'no photo' }),
      h('b', { class: 'mono', text: id.replace(/^chip_/, '') }),
      h('small', { text: [note, p.thickness_label ? thickText(p.material, p.thickness_label) : null, p.area_um2 != null ? `${Math.round(p.area_um2).toLocaleString()} µm²` : null].filter(Boolean).join(' · ') }));
  }

  // a row of flake photos; `more`: how many there are in all
  strip(list, tagOf, more) {
    return h('div', { class: 'strip' }, list.map(f => h('button', { class: 'st-f' + (this.isSel('sample', f.id) ? ' on' : ''), title: `${f.id} · ${thickText(f.props.material, f.props.thickness_label)}`,
      onclick: () => this.select({ t: 'sample', id: f.id }) },
    bestPhoto(f) ? h('img', { src: imgUrl(bestPhoto(f)), alt: f.id, loading: 'lazy' }) : h('span', { class: 'fnone', text: shortId(f.id) }),
    tagOf(f) ? h('span', { class: 'st-tag', text: tagOf(f) }) : null)),
    more > list.length ? h('button', { class: 'st-f more', text: `all ${more} ›`, onclick: () => this.openDoc('flakes') }) : null);
  }

  // one job: status, steps, its last check measured against allowed, photos, the record
  jobRow(r) {
    const s = this.s, steps = [...r.steps.values()];
    const fixes = steps.reduce((n, st) => n + (st.corrections || 0), 0);
    let check = null;
    const imgs = [];
    for (const e of s.events) {
      if (e.run_id !== r.id || e.kind !== 'step.verified') continue;
      const p = e.payload || {}, t = measureText(p.measurements, p.expected);
      if (t) check = { text: t, verdict: p.verdict, name: p.name };
      for (const src of p.images || []) imgs.push(src);
    }
    const ok = r.status === 'succeeded';
    const lab = jobLabTime(r);
    return h('div', { class: 'jrow ' + (r.status || '') },
      h('span', { class: 'st ' + (ok ? (fixes ? 'fix' : 'ok') : r.status === 'failed' ? 'bad' : r.status === 'running' ? 'going' : 'wait'), text: ok ? (fixes ? '↻' : '✓') : r.status === 'failed' ? '✗' : r.status === 'running' ? '▶' : '⏸' }),
      h('div', { class: 'jr-m' },
        h('b', { text: jobTitle(r.flow, r.params) }),
        h('small', { text: [`${steps.filter(st => st.status === 'done').length}/${steps.length} steps`, fixes ? plural(fixes, 'correction') : null,
          lab >= 1 ? `${fmtDur(lab)} lab time` : null, ok ? null : RUN_STATUS[r.status] || r.status].filter(Boolean).join(' · ') }),
        check ? h('small', { class: 'jr-check ' + (check.verdict === 'ok' ? 'ok' : 'warn') }, `last check · ${stepName(check.name)}: `, h('span', { class: 'mono', text: check.text }),
          ` · ${VERDICT[check.verdict] || check.verdict}`) : null),
      imgs.length ? h('div', { class: 'jr-ph' }, imgs.slice(-3).map(src => h('img', { src: imgUrl(src), alt: imageSource(src), title: imageSource(src), loading: 'lazy',
        onclick: ev => { ev.stopPropagation(); this.hooks.photo(src); } }))) : null,
      h('button', { class: 'lnk-btn', text: 'record ›', title: 'Open the job record', onclick: () => this.openDoc(`job:${r.id}`) }));
  }

  // ------------------------------------------------------------ the job record
  async buildJob(id, box) {
    let run;
    try { run = await this.hooks.run(id); } catch { run = null; }
    if (box !== this.jobNode) return;
    if (!run) { box.replaceChildren(h('div', { class: 'placeholder', text: 'Could not load this job.' })); return; }
    const s = this.s, r = s.book.runs.get(id);
    const evs = s.events.filter(e => e.run_id === id);
    const cmds = evs.filter(e => e.kind === 'device.command' || e.kind === 'safety.violation');
    const refused = cmds.filter(e => e.kind === 'safety.violation').length;
    // each step's photos: the job's results in order, matched to its steps by name
    const results = (Array.isArray(run.result) ? run.result : []).slice();
    const stepImgs = run.steps.map(st => {
      const k = results.findIndex(x => x && x.step === st.name);
      const res = k >= 0 ? results.splice(k, 1)[0] : null;
      return ((res && res.observations) || []).flatMap(o => o.images || []);
    });
    const imgs = stepImgs.flat();
    const lab = r ? jobLabTime(r) : 0;
    const sum = h('div', { text: jobSummary(run, run.steps) });
    const head = h('div', { class: 'jr-sum' }, sum,
      h('div', { class: 'muted small', text: [lab ? `${fmtDur(lab)} lab time` : null, `asked by ${label(run.requested_by)}`, `job ${run.id}`].filter(Boolean).join(' · ') }));
    let found = null;
    if ((run.flow === 'search_chip' || run.flow === 'revisit_chip') && run.params?.chip_id) {
      const fl = s.inv.flakesOf(run.params.chip_id).sort((a, b) => (hasPhoto(b) - hasPhoto(a)) || (b.props.area_um2 || 0) - (a.props.area_um2 || 0));
      const shot = fl.filter(hasPhoto);
      found = fl.length ? h('div', {},
        h('div', { class: 'section-t', text: `Found flakes · ${fl.length}${shot.length < fl.length ? ` (${shot.length} photographed)` : ''} · click one for its details` }),
        h('div', { class: 'jr-found' }, shot.slice(0, 24).map(f => h('button', { class: 'jr-f', title: `${f.id} · ${thickText(f.props.material, f.props.thickness_label)}`,
          onclick: () => this.select({ t: 'sample', id: f.id }) }, h('img', { src: imgUrl(bestPhoto(f)), alt: f.id, loading: 'lazy' }), h('small', { class: 'mono', text: shortId(f.id) }))),
        fl.length > Math.min(24, shot.length) ? h('button', { class: 'jr-f more', text: `all ${fl.length} ›`, onclick: () => { this.grid.filter({ chip: run.params.chip_id }); this.openDoc('flakes'); } }) : null))
        : h('div', { class: 'section-t', text: 'Found flakes · none in the ledger for this chip' });
    }
    const steps = h('ul', { class: 'jr-steps' }, run.steps.map((st, i) => {
      const ok = st.status === 'done', icon = ok ? (st.corrections ? '↻' : '✓') : st.status === 'in_progress' ? '…' : st.status === 'skipped' ? '–' : '✗';
      const n = stepImgs[i].length;
      return h('li', { class: ok ? (st.corrections ? 'fix' : 'ok') : st.status },
        h('span', { class: 'ck', text: icon }),
        h('span', {}, stepName(st.name).replace(/^./, c => c.toUpperCase()),
          h('small', { text: [st.corrections ? plural(st.corrections, 'correction') : null, st.verifiable === false ? 'not verifiable (no sensor)' : null,
            n ? plural(n, 'photo') : null, st.note || null].filter(Boolean).join(' · ') })),
        h('span', { class: 'v', text: measureText(st.measurements, st.expected) }));
    }));
    const raw = h('details', { class: 'jr-fold' }, h('summary', { text: 'Raw measurements and settings' }),
      h('dl', { class: 'kv' },
        h('dt', { text: 'Job' }), h('dd', { class: 'mono', text: run.id }),
        h('dt', { text: 'Flow' }), h('dd', { class: 'mono', text: run.flow }),
        h('dt', { text: 'Params' }), h('dd', { class: 'mono', text: JSON.stringify(run.params) }),
        h('dt', { text: 'Status' }), h('dd', {}, h('span', { class: 'tag ' + run.status, text: RUN_STATUS[run.status] || run.status })),
        run.error ? h('dt', { text: 'Error' }) : null, run.error ? h('dd', { class: 'mono', text: JSON.stringify(run.error) }) : null),
      ...run.steps.map(st => h('div', { class: 'dstep' },
        h('h4', {}, h('span', { class: 'muted', text: `#${st.seq}` }), st.name, h('span', { class: 'tag ' + st.status, text: STEP_STATUS[st.status] || st.status })),
        h('dl', { class: 'kv', style: 'margin:0' },
          h('dt', { text: 'Expected' }), h('dd', { class: 'mono', text: short(st.expected, 400) }),
          h('dt', { text: 'Measured' }), h('dd', { class: 'mono', text: short(st.measurements, 600) })))));
    // thousands of commands: built only when opened
    const cmdBox = h('div', { class: 'cmds' });
    const cmdFold = h('details', { class: 'jr-fold', ontoggle: ev => {
      if (!ev.target.open || cmdBox.childElementCount) return;
      cmdBox.replaceChildren(...cmds.map(e => h('div', { class: e.kind === 'safety.violation' ? 'refused' : '' },
        `${e.payload.device}.${e.payload.command} ${short(e.payload.params || {}, 110)}${e.kind === 'safety.violation' ? '  ✗ ' + e.payload.reason : ''}`)));
    } }, h('summary', { text: `${cmds.length.toLocaleString()} device commands · ${refused ? `${refused} refused by the safety check` : 'all passed the safety check'}` }), cmdBox);
    box.replaceChildren(...[
      head, this.humanReviewForm(run), found,
      h('div', { class: 'section-t', text: 'Each step is checked before the next; progress follows the saved checkpoints.' }),
      steps,
      h('div', { class: 'section-t', text: `Photos · ${imgs.length}` }),
      imgs.length ? h('div', { class: 'thumbs' }, imgs.slice(0, 30).map(src => h('img', { src: imgUrl(src), alt: imageSource(src), title: imageSource(src),
        loading: 'lazy', onclick: ev => { ev.stopPropagation(); this.hooks.photo(src); } })))
        : h('div', { class: 'placeholder', text: 'No photos for this job.' }),
      raw, cmdFold].filter(Boolean));
    linkIds(sum, x => this.select({ t: 'sample', id: x }), x => s.inv.samples.has(x));
  }

  // ------------------------------------------------------------ right: details
  // ctx: the details context (details.js); returns { title, sub, node } or null
  detail(ctx) {
    const s = this.s, b = s.book, it = this.sel;
    if (!it) return null;
    if (it.t === 'request') {
      // Resolve against the current prefix; never leak a later replay response.
      const p = requestSeries(s.events).find(p => p.key === it.id);
      return p ? { title: `Request ${p.number} · ${label(p.agent)}`, sub: p.model || 'Unknown model', node: requestInspector(p, false, key => {
        if (this.activeRequestChartState) {
          this.activeRequestChartState.selectedKey = key;
          this.activeRequestChartState.selectKey?.(key);
        }
        this.select({ t: 'request', id: key });
      }) } : null;
    }
    if (it.t === 'sample') {
      const x = s.inv.samples.get(it.id);
      return x ? sampleDetail(x, ctx) : { title: it.id, sub: '', node: h('div', { class: 'placeholder', text: 'Not in the sample ledger of this run.' }) };
    }
    if (it.t === 'plan') {
      const item = b.devicePlans.find(x => x.recordId === it.id);
      return item ? { title: item.device_name || 'Device Guidance', sub: `by ${label(item.agent || 'advisor')}`, node: renderDevicePlan(item) } : null;
    }
    if (it.t === 'layout') {
      const i = b.designs.findIndex(x => x.recordId === it.id), item = b.designs[i];
      return item ? { title: `Layout ${i + 1}`, sub: `by ${label(item.agent || 'designer')}`, node: renderLayout(item) } : null;
    }
    if (it.t === 'used') {
      if (b.selection) return { title: 'Flakes used', sub: `chosen by the ${label(b.selection.agent || 'picker')}`, node: renderSelection(b.selection, { flake: id => s.inv.samples.get(id), open: id => this.select({ t: 'sample', id }) }) };
      return { title: 'Flakes used', sub: '', node: h('div', { class: 'thumbgrid wide' }, this.usedFlakes().map(id => this.flakeTile(s.inv.samples.get(id), id, s.roleOf(id)))) };
    }
    if (it.t === 'problem') {
      const pr = s.log.problems[it.id];
      return pr ? this.problemDetail(pr) : null;
    }
    return null;
  }

  problemDetail(pr) {
    const s = this.s, r = pr.run ? s.book.runs.get(pr.run) : null;
    const seq = pr.key && String(pr.key).includes('|') ? +String(pr.key).split('|')[1] : null;
    const step = r && seq != null ? r.steps.get(seq) : null;
    const checks = pr.parts.filter(x => x.kind === 'step.verified').map(x => ({ text: measureText(x.payload.measurements, x.payload.expected), v: x.payload.verdict })).filter(x => x.text);
    // the check of that step, also when the problem started elsewhere (a refusal, a stop)
    if (!checks.length && r && seq != null) {
      for (const e of s.events) {
        if (e.run_id === pr.run && e.kind === 'step.verified' && e.payload.seq === seq) {
          const t = measureText(e.payload.measurements, e.payload.expected);
          if (t) checks.push({ text: t, v: e.payload.verdict });
        }
      }
    }
    const photos = pr.parts.flatMap(x => x.payload.images || []);
    // a refusal is handled by being refused: nothing ran
    const first = pr.parts[0] || {};
    const refused = first.kind === 'tool.denied' ? 'refused before it ran; nothing happened in the lab'
      : first.kind === 'safety.violation' ? 'the safety layer refused the command; the device did not move' : null;
    const state = pr.resolved ? `✓ ${pr.resolved}` : problemState(pr) || refused || { bad: '✗ not resolved', warn: '⚠ open', wait: '⏸ waits for a person' }[pr.cls] || 'open';
    const kv = [
      ['Job', r ? jobTitle(r.flow, r.params) : pr.run ? pr.run : 'none (an agent\'s tool call)'],
      ['Action', step ? stepName(step.name) : pr.who || '–'],
      ['Measured', checks.length ? h('span', {}, ...checks.map((c, i) => h('span', { class: 'mono', text: `${i ? ' → ' : ''}${c.text} (${VERDICT[c.v] || c.v})` }))) : 'no measurement'],
      ['Handled', state],
    ];
    const node = h('div', { class: 'detail-body' },
      h('div', { class: 'd-sec', text: 'What happened' }),
      h('ol', { class: 'fl-hist' }, pr.parts.map(x => h('li', {}, h('span', { class: 'n mono', text: x.when || '' }), h('i', { class: x.cls === 'bad' ? 'bad' : x.cls === 'warn' ? 'found' : 'pick' }),
        h('span', {}, h('b', { text: `${x.who}: ` }), x.text)))),
      h('dl', { class: 'kv' }, kv.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', {}, v)])),
      photos.length ? h('div', { class: 'thumbs' }, photos.slice(0, 6).map(src => h('img', { src: imgUrl(src), alt: imageSource(src), title: imageSource(src), loading: 'lazy', onclick: () => this.hooks.photo(src) }))) : null,
      h('div', { class: 'd-acts' },
        pr.run && r ? h('button', { class: 'inv-btn', text: 'Open the job record ›', onclick: () => this.openDoc(`job:${pr.run}`) }) : null,
        pr.key && this.hooks.jump ? h('button', { class: 'inv-btn', text: 'Show it in Progress ›', onclick: () => this.hooks.jump(pr.key) }) : null));
    linkIds(node, id => this.select({ t: 'sample', id }), id => s.inv.samples.has(id));
    return { title: `Problem ${pr.n + 1}`, sub: pr.resolved || refused ? 'handled' : 'open', node };
  }
}

export const summaryOf = s => s.summary;
