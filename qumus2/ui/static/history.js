// History · Runs: Demonstrations, Simulated lab and Real instruments
// (scenario 4; the demo of scenario 5 starts here, no command line).
//
//   left    three groups. "Demonstrations": the recordings named in demoguide
//           (CURRENT, in DEMOS order) found in recordings/. "Simulated lab":
//           the current run, then every earlier `serve` of this run store that
//           was asked something (GET /api/history; the live page only). Other
//           recordings stay on disk but are listed only while selected or
//           playing. "Real instruments": empty until real hardware runs.
//   centre  the picked run, read-only: what it was asked and how it ended,
//           "▶ Play recording", and its Results, Progress tree or
//           Conversation (the same views as during the run).
//   right   its key moments; a click plays the run from there. A thing
//           clicked in its Results shows its details instead.
//
// Playing reloads the page on the recording (?rec=<name> or ?run=<id>,
// &at=<event index>): the replay takes the whole page, with "Back to live".

import { h, api, fetchRecording, fmtDur, label, linkIds, modelLabel } from './ui.js';
import { CURRENT, DEMOS, DIAGNOSTICS, order } from './demoguide.js';
import { Session, OUTCOME } from './session.js';
import { Results } from './results.js';
import { ProgressTree } from './tree.js';
import { Feed } from './feed.js';
import { inspectionPreview } from './inspection.js';
import { Replay, eventTimes, eventTimeLabel } from './replay.js';
import { ExperienceView } from './experience.js';

const when = iso => (iso ? new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '');
const whenS = t => (t ? when(new Date(t * 1000).toISOString()) : '');

// A diagnostic description cannot relabel a model-driven event history.
export function diagnosticPresentation(rec) {
  const m = rec?.meta || {}, p = m.presentation;
  if (m.agents !== 'scripted' || m.models?.length || p?.version !== 1
      || p.kind !== 'scripted_diagnostic' || p.model_calls !== 0 || p.stack_jobs !== 0
      || Object.values(rec.runs || {}).some(run => run.flow === 'stack_device')
      || (rec.events || []).some(e => ['agent.request', 'agent.request_usage', 'agent.usage', 'agent.turn_end'].includes(e.kind)
        || (e.kind === 'job.submitted' && e.payload?.flow === 'stack_device'))) return null;
  return p;
}

export class HistoryView {
  // ctx: { live() serve (it has a run store); playing() -> {rec, run} on screen;
  // play(target) reload on a run; photo(src); faultText(p); changed(part); showRight(); dctx() }
  constructor(ctx) {
    this.ctx = ctx;
    this.runs = null;        // this computer: [] or null (no run store: the replay server)
    this.recs = null;        // demo recordings
    this.sel = null;         // { kind: 'run' | 'rec', id }
    this.pv = null;          // the picked run, loaded: { key, rec, s, results, tree, feed, moments }
    this.sub = 'results';    // the centre: results | progress | conversation | experience | context
    this.loading = null;
    this.loaded = 0;
  }

  async load(force) {
    // A restored History pane may render before health identifies the server.
    // Do not request live-only endpoints or start the cache timer until then.
    if (this.ctx.ready && !this.ctx.ready()) return;
    if (!force && Date.now() - this.loaded < 15000) return;
    this.loaded = Date.now();
    const [runs, recs] = await Promise.all([
      this.ctx.live() ? api('/api/history').catch(() => null) : Promise.resolve(null),
      api('/api/recordings').catch(() => []),
    ]);
    this.runs = runs; this.recs = recs;
    if (!this.sel) {
      const cur = this.ctx.playing();
      const pick = (this.ctx.current?.() && { kind: 'current', id: 'live' }) || (cur.run && { kind: 'run', id: String(cur.run) }) || (cur.rec && { kind: 'rec', id: cur.rec })
        || (runs && runs[0] && { kind: 'run', id: String(runs[0].id) })
        || ((recs || []).find(r => CURRENT.has(r.name)) && { kind: 'rec', id: recs.find(r => CURRENT.has(r.name)).name })
        || ((recs || []).find(r => r.default) && { kind: 'rec', id: recs.find(r => r.default).name });
      if (pick) this.pick(pick, true);
    }
    this.ctx.changed('all');
  }

  key(it) { return it ? `${it.kind}:${it.id}` : ''; }

  pick(it, quiet) {
    this.sel = it;
    this.sub = it.kind === 'rec' && DIAGNOSTICS[it.id] ? 'evidence' : 'results';
    if (it.kind !== 'current' && (!this.pv || this.pv.key !== this.key(it))) this.open(it);
    if (!quiet) { this.ctx.changed('all'); this.ctx.showCentre?.(); }
  }

  // ------------------------------------------------------------ the picked run
  async open(it) {
    const key = this.key(it);
    this.pv = { key, rec: null, error: null };
    let rec;
    try {
      rec = it.kind === 'rec' ? await fetchRecording(`/api/recording?name=${encodeURIComponent(it.id)}`, it.id)
        : await fetchRecording(`/api/history/${encodeURIComponent(it.id)}/recording`);
    } catch (err) {
      if (this.pv.key === key) { this.pv.error = String(err.message || err); this.ctx.changed('all'); }
      return;
    }
    if (this.pv.key !== key) return;
    const events = [...(rec.events || [])].sort((a, b) => a.id - b.id);
    // read-only views of the run, built from its events alone
    const feedBox = h('div', { class: 'feed' }), wrap = h('div', { class: 'feed-wrap pv-feed' }, feedBox);
    const feed = new Feed(feedBox, { openRun: id => { results.openDoc(`job:${id}`); this.sub = 'results'; this.ctx.changed('all'); },
      ids: node => linkIds(node, id => results.select({ t: 'sample', id }), id => s.inv.samples.has(id)),
      openPlan: () => {}, photo: src => this.ctx.photo(src), faultText: p => this.ctx.faultText(p),
      inspection: e => inspectionPreview(replay, e, src => this.ctx.photo(src)) });
    feed.clock = 'elapsed'; feed.stoppable = false;
    feed.recordedTimes = eventTimes(events);
    const s = new Session({ when: e => feed.when(e), faultText: p => this.ctx.faultText(p) });
    const replay = new Replay(rec, { deliver() {}, apply() {}, skipped() {}, reset() {}, settle() {}, progress() {} });
    const results = new Results(s, {
      live: () => false, run: async id => replay.run(id, Infinity), photo: src => this.ctx.photo(src),
      changed: part => this.ctx.changed(part === 'right' ? 'right' : 'centre'),
      showRight: () => this.ctx.showRight(), showCentre: () => this.ctx.showCentre?.(), costs: () => false,
      subtitle: () => 'read-only',
    });
    const tree = new ProgressTree(s.book, { openRun: id => { results.openDoc(`job:${id}`); this.sub = 'results'; this.ctx.changed('all'); },
      openTab: tab => { results.openDoc(tab === 'device' ? 'plans' : tab === 'flakes' ? 'used' : 'layouts'); this.sub = 'results'; this.ctx.changed('all'); } });
    feed.reset(null);
    for (const e of events) {
      const run = s.add(e);
      feed.add(e, run);
      tree.add(e);
    }
    feed.setWorking(false);
    feed.finishRecordedActivity(s, {modelDriven: rec.meta?.agents === 'sdk'});
    const experience = new ExperienceView({ changed: () => this.ctx.changed('all'),
      showRight: () => this.ctx.showRight() });
    experience.data = rec.experience ?? null;
    this.pv = { key, it, rec, s, results, tree, feed, wrap, moments: moments(events), replay, experience,
      diagnostic: diagnosticPresentation(rec) };
    this.ctx.changed('all');
  }

  // ------------------------------------------------------------ left
  renderIndex(box) {
    const rows = [], playing = this.key(this.playingItem());
    const tag = o => { const [t, c] = OUTCOME[o] || [o, '']; return h('span', { class: 'tag ' + c, text: t }); };
    const row = (it, title, facts, outcome, extra) => h('button', { dataset: { kind: it.kind }, class: 'hrow' + (this.key(it) === this.key(this.sel) ? ' on' : ''), title, onclick: () => this.pick(it) },
      h('span', { class: 'hr-l' }, h('b', { text: title }), extra || null, outcome ? tag(outcome) : null),
      h('small', { text: facts.filter(Boolean).join(' · ') }),
      this.key(it) === playing ? h('span', { class: 'hr-now', text: '▶ playing now' }) : null);
    // Two groups (owner, 2026-11-06): the simulated lab and real instruments.
    // The simulated group lists only the current Hall-bar recording(s), the
    // runs on this computer and whatever is selected or playing; other
    // recordings stay on disk but are not listed.
    // Demonstrations (owner, 2026-11-13): the recordings to show and replay,
    // named in demoguide; then this computer's own simulated runs.
    const recs = this.recs || [];
    const demoRow = r => {
      const guide = DEMOS[r.name];
      const title = guide?.[0] || String(r.title || '').replace(/\(.*?\)\s*$/, '').trim() || r.request || r.name;
      const el = row({ kind: 'rec', id: r.name }, title,
        [r.agents === 'sdk' ? `Recorded ${modelLabel(r)}` : 'Scripted', r.agents !== 'sdk' && r.lab_s > 0 ? `${fmtDur(r.lab_s)} lab time` : r.duration_s ? fmtDur(r.duration_s) : null],
        DIAGNOSTICS[r.name] ? null : r.outcome);
      if (guide && this.key(this.sel) === `rec:${r.name}`) el.append(h('span', { class: 'demo-purpose', text: guide[1] }));
      return el;
    };
    const shown = recs.filter(r => CURRENT.has(r.name) || this.key(this.sel) === `rec:${r.name}` || this.key(this.playingItem()) === `rec:${r.name}`);
    shown.sort((a, b) => order(a.name) - order(b.name));
    rows.push(h('div', { class: 'sec-t', text: 'Demonstrations' }));
    rows.push(...(shown.length ? shown.map(demoRow)
      : [h('div', { class: 'placeholder', text: this.recs ? 'No demonstration recording in recordings/ on this computer.' : 'Loading…' })]));
    rows.push(h('div', { class: 'sec-t', text: 'Simulated lab' }));
    if (this.ctx.current?.()) rows.push(row({ kind: 'current', id: 'live' }, 'Current run', ['Live context']));
    for (const r of this.runs || []) {
      rows.push(row({ kind: 'run', id: String(r.id) }, r.request || 'Run', [whenS(r.started), r.agents === 'sdk' ? modelLabel(r) : 'scripted',
        r.devices && r.devices.length ? r.devices.join(', ') : null, r.lab_s >= 1 ? `${fmtDur(r.lab_s)} lab` : null], r.outcome));
    }
    if (!(this.runs || []).length && !this.ctx.current?.()) {
      rows.push(h('div', { class: 'placeholder', text: this.recs ? 'No run on this computer yet.' : 'Loading…' }));
    }
    rows.push(h('div', { class: 'sec-t', text: 'Real instruments' }));
    rows.push(h('div', { class: 'placeholder', text: 'No run on real instruments yet.' }));
    const keep = box.scrollTop;
    box.replaceChildren(...rows);
    box.scrollTop = keep;
  }
  playingItem() { const c = this.ctx.playing(); return c.run ? { kind: 'run', id: String(c.run) } : c.rec ? { kind: 'rec', id: c.rec } : null; }

  // ------------------------------------------------------------ centre
  currentContext() {
    const s = this.ctx.current?.();
    if (!s) return null;
    if (!this.currentResults || this.currentResults.s !== s) this.currentResults = new Results(s, {
      changed: part => this.ctx.changed(part), showRight: () => this.ctx.showRight(),
    });
    return this.currentResults;
  }

  renderCentre(body) {
    if (this.sel?.kind === 'current') {
      const results = this.currentContext();
      if (!results) { body.replaceChildren(h('p', { class: 'placeholder', text: 'No live run is connected.' })); return; }
      const node = results.contextView(this.currentChartState ||= {}, 'live');
      if (body.contains(node)) { this.currentChartState.restoreViewport?.(); return; }
      body.replaceChildren(h('div', { class: 'hist-head' }, h('h3', { text: 'Current run' })),
        h('div', { class: 'hist-sub' }, h('div', { class: 'seg small' },
          h('button', { class: 'on', text: 'Context', 'aria-pressed': 'true' }))),
        node);
      return;
    }
    if (!this.sel) { body.replaceChildren(h('div', { class: 'placeholder pad', text: this.recs ? 'Pick a run on the left.' : 'Loading…' })); return; }
    const pv = this.pv;
    if (this.sub === 'context' && pv?.contextNode && body.contains(pv.contextNode)) { pv.requestChartState.restoreViewport?.(); return; }
    // Immutable evidence and its GIF should not restart on every bench poll.
    if (this.sub === 'evidence' && pv?.evidenceNode && body.contains(pv.evidenceNode)) return;
    if (!pv || !pv.s) {
      body.replaceChildren(h('div', { class: 'placeholder pad', text: pv && pv.error ? `Could not load this run: ${pv.error}` : 'Loading the run…' }));
      return;
    }
    const m = pv.rec.meta || {}, sum = pv.s.summary, row = this.rowOf(this.sel), diagnostic = pv.diagnostic;
    if (this.sub === 'evidence' && !diagnostic) this.sub = 'results';
    const [ot, oc] = OUTCOME[sum.outcome] || [sum.outcome, ''];
    const facts = [this.sel.kind === 'rec' ? 'Demo recording' : 'This computer', m.agents === 'sdk' ? (m.models?.length ? m.models.join(', ') : modelLabel(m)) : 'scripted agents',
      this.sel.kind === 'rec' ? 'testbench' : null, m.recorded_at ? when(m.recorded_at) : null,
      ...(sum.faults || []).map(f => `injected fault: ${f.replace(/_/g, ' ')}`)].filter(Boolean);
    const head = h('div', { class: 'hist-head' },
      h('div', { class: 'hh-t' }, h('h3', { text: (this.sel.kind === 'rec' && DEMOS[this.sel.id]?.[0]) || (row && (row.title || row.request)) || m.title || sum.request || 'Run' }),
        h('button', { class: 'play-btn', title: 'Play this run in the page from the start', onclick: () => this.ctx.play(this.sel) }, h('i'), 'Play recording')),
      diagnostic ? null : h('div', { class: 'muted small', text: facts.join(' · ') }),
      h('div', { class: 'tags' }, diagnostic ? null : h('span', { class: 'tag ' + oc, text: ot }),
        sum.lab_s >= 1 ? h('span', { class: 'tag', text: `${fmtDur(sum.lab_s)} lab time` }) : null,
        h('span', { class: 'tag', text: 'Free playback' })));
    const tabs = [...(diagnostic ? [['evidence', 'Evidence']] : []), ['results', 'Results'], ['progress', 'Progress'], ['conversation', 'Conversation'], ['experience', 'Experience'], ['context', 'Context']];
    const seg = h('div', { class: 'seg small' }, ...tabs.map(([k, t]) =>
      h('button', { class: this.sub === k ? 'on' : '', text: t, onclick: () => {
        this.sub = k; this.ctx.showCentre?.(); this.ctx.changed('all');
      } })));
    const bar = h('div', { class: 'hist-sub' }, seg, diagnostic ? null : h('span', { class: 'muted small', text: 'read-only · the same views as during the run' }),
      !['experience', 'context'].includes(this.sub) ? h('button', { class: 'lnk-btn phone-show', text: 'Key moments ›', onclick: () => {
        pv.results.sel = null; this.ctx.changed('right'); this.ctx.showRight();
      } }) : null);
    let content;
    if (this.sub === 'evidence') {
      content = pv.evidenceNode ||= this.diagnosticEvidence(pv);
    } else if (this.sub === 'context') {
      content = pv.contextNode = pv.results.contextView(pv.requestChartState ||= {});
    } else if (this.sub === 'experience') {
      content = h('div', { class: 'pad' });
      pv.experience.renderCentre(content, true);
      content.prepend(h('p', { class: 'muted small', text: 'Read-only experience snapshot saved with this recording.' }));
    } else if (this.sub === 'progress') {
      pv.tree.dirty = true; pv.tree.render();
      content = h('div', { class: 'pv-tree' }, pv.tree.root);
    } else if (this.sub === 'conversation') {
      const modes = h('div', { class: 'seg small', role: 'group', 'aria-label': 'Conversation detail' });
      for (const [focus, text] of [[false, 'Full'], [true, 'Focus']]) {
        modes.appendChild(h('button', { class: pv.feed.focus === focus ? 'on' : '', text,
          title: focus ? 'Show only messages' : 'Show messages and tool calls', onclick: () => {
            pv.feed.setFocus(focus);
            [...modes.children].forEach(b => b.classList.toggle('on', b.textContent === text));
          } }));
      }
      content = h('div', {}, h('div', { class: 'pv-feed-mode' }, modes), pv.wrap);
    } else {
      const into = { title: h('span'), sub: h('span'), home: h('button', { class: 'lnk-btn', text: '← Overview', onclick: () => pv.results.openDoc('overview') }), body: h('div', { class: 'pv-res' }) };
      pv.results.renderCentre(into);
      content = h('div', {}, into.home.hidden ? null : h('div', { class: 'pv-doc' }, into.home, h('b', { text: into.title.textContent })), into.body);
    }
    const guide = this.sel.kind === 'rec' && DEMOS[this.sel.id];
    const notice = diagnostic ? h('section', { class: 'verdict diagnostic-notice', role: 'note', style: {margin: '8px 20px', border: '2px solid #b57824', padding: '10px 12px'} },
      h('b', { text: 'Failed scripted diagnostic · Not an accepted Astra recording' }),
      h('div', { class: 'small', text: '0 stack jobs · 0 model calls · virtual origin · experimental learning disabled' })) : null;
    body.replaceChildren(head, notice, ...(guide ? [h('p', { class: 'hist-description demo-guide', text: guide[1] })] : []), ...(!diagnostic && m.description ? [h('p', { class: 'hist-description muted small', text: m.description })] : []), bar, content);
    if (this.sub === 'conversation') { pv.feed.reportControls(); pv.feed.toBottom(true); }
  }

  diagnosticEvidence(pv) {
    const p = pv.diagnostic, target = p.target || {}, source = p.sources || {}, optics = p.optics || {};
    const section = (title, ...nodes) => h('section', {class: 'card', style: {margin: '14px 0', padding: '14px'}}, h('h3', {text: title}), ...nodes);
    const photo = (src, alt) => src ? h('button', {class: 'lnk-btn', title: 'Open image', style: {display: 'block', width: '100%'}, onclick: () => this.ctx.photo(src)},
      h('img', {src, alt, loading: 'lazy', style: {display: 'block', maxWidth: '100%', maxHeight: '440px', margin: '8px auto', objectFit: 'contain'}})) : null;
    const openJob = id => { pv.results.openDoc(`job:${id}`); this.sub = 'results'; this.ctx.changed('all'); this.ctx.showCentre?.(); };
    const completed = (p.verified_components || []).map(text => h('li', {text}));
    const incomplete = (p.not_established || []).map(text => h('li', {text}));
    return h('div', {class: 'pad diagnostic-evidence'},
      h('div', {class: 'muted small', text: 'Recorded lab clock; session timestamps unavailable.'}),
      section('1 · Supplied electrode target',
        photo(target.image, 'Owner electrode layout with all leads/pads and the eight fixed contact-end regions'),
        h('p', {class: 'small', text: '34 metal primitives · eight complete ends: full source/drain boxes + first 2 µm of each 1 µm side lead. Gr must contain all ends; hBN must contain Gr.'})),
      section('2 · Actual source preparation and search',
        h('p', {text: `Six raw chips · seed 8 · eligible sources: ${source.hbn_count ?? 'unknown'} hBN / ${source.gr_count ?? 'unknown'} Gr · ${source.contact_spanning_gr_count ?? 'unknown'} contact-spanning Gr.`}),
        ...(source.candidates || []).map(candidate => h('article', {class: 'card', style: {padding: '10px', margin: '10px 0'}},
          h('b', {text: `${candidate.material} · ${candidate.id} · ${Number(candidate.area_um2).toFixed(3)} µm²`}),
          photo(candidate.image, `${candidate.material} ${candidate.id}: recorded ${candidate.objective || 'microscope'} source image`),
          h('p', {class: 'small', text: `${candidate.objective || 'Microscope'} · SIM metadata geometry; not RGB reanalysis.`}),
          candidate.job_id ? h('button', {class: 'lnk-btn', text: 'Open original search-job evidence →', onclick: () => openJob(candidate.job_id)}) : null))),
      section('3 · Why no stack was submitted',
        h('p', {text: p.reason}),
        photo(p.strict_screen_image, 'Same-scale source contours and the existing strict screen necessary area bound; no proposed device placement'),
        h('button', {class: 'lnk-btn', text: 'Inspect the recorded preparation/search progress →', onclick: () => { this.sub = 'progress'; this.ctx.changed('all'); }})),
      section('4 · Optical illustration',
        h('div', {class: 'tag warn', text: 'Illustration only · no observed pickup, release or transfer'}),
        photo(optics.image, 'Optical forward illustration only; not an observed pickup, release or material transfer'),
        h('p', {class: 'small', text: 'White-light approach → contact-front growth → withdrawal fringes.'})),
      h('details', {class: 'card', style: {padding: '14px', margin: '14px 0', overflowWrap: 'anywhere'}},
        h('summary', {text: 'Evidence details'}),
        h('p', {class: 'small mono', text: `Application SHA-256: ${p.application_source_sha256 || 'unknown'}`}),
        h('p', {class: 'small mono', text: `Original GDS SHA-256: ${target.source_sha256 || 'unknown'}`}),
        h('p', {class: 'small', text: `${p.source_jobs} preparation/search jobs. Safe closure is not experimental success. Original event order and lab clock are retained. No wall-time timeline was recorded.`}),
        h('p', {class: 'small', text: `The harness recorded ${Number(p.harness_wall_seconds).toFixed(3)} seconds for the complete diagnostic. The displayed lab clock preserves recorded time. Session timestamps were not recorded; playback pacing and compression do not add laboratory elapsed time.`}),
        h('p', {class: 'small', text: pv.rec.meta.description}),
        h('p', {class: 'small', text: p.allocation_note}),
        p.strict_screen_note ? h('p', {class: 'small', text: p.strict_screen_note}) : null,
        h('p', {class: 'small', text: 'No compatible pair was accepted; no selection, design-driven stack or finished Hall device is claimed. The finite search does not prove that all possible source material is infeasible.'}),
        h('p', {class: 'small', text: 'Native Designer/MCP round-trip evidence used analytic adapter sources, not a device made in these diagnostics.'}),
        h('p', {class: 'small', text: 'The optical illustration used no controller, laboratory job or model. Its fixed historical pose was not admitted for safe discovery. Material transfer and withdrawal-front detachment are not modeled.'}),
        h('b', {text: 'Component evidence available'}), h('ul', {}, ...completed),
        h('b', {text: 'Not established by this package'}), h('ul', {}, ...incomplete)));
  }
  rowOf(it) {
    if (!it) return null;
    if (it.kind === 'rec') return (this.recs || []).find(r => r.name === it.id) || null;
    return (this.runs || []).find(r => String(r.id) === it.id) || null;
  }

  // ------------------------------------------------------------ right
  detail(dctx) {
    if (this.sel?.kind === 'current') return this.currentContext()?.detail(dctx) || null;
    const pv = this.pv;
    if (!pv || !pv.s) return null;
    if (this.sub === 'context') return pv.results.detail({ ...dctx, s: pv.s, live: () => false });
    if (this.sub === 'experience') return pv.experience.detail() || {
      title: 'Experience', sub: 'recorded snapshot',
      node: h('div', { class: 'placeholder pad', text: 'Select an entry to see its source and votes.' }),
    };
    if (pv.results.sel) {
      const d = pv.results.detail({ ...dctx, s: pv.s, live: () => false });
      if (d) return { ...d, back: { text: '← Key moments', fn: () => { pv.results.sel = null; this.ctx.changed('right'); } } };
    }
    const node = h('div', { class: 'detail-body' },
      h('p', { class: 'muted small', text: 'Lab time includes instrument actions and thinking. Session time is the original wall time.' }),
      pv.moments.length ? h('ol', { class: 'moments' }, pv.moments.map(k => h('li', {}, h('button', { class: 'mom ' + k.kind, title: 'Play the run from here', onclick: () => this.ctx.play(this.sel, k.i) },
        h('span', { class: 'mi', text: k.icon }), h('span', { class: 'mt', text: k.text }), h('span', { class: 'mw mono', text: k.when })))))
        : h('div', { class: 'placeholder', text: 'Nothing happened in this run.' }),
      h('p', { class: 'muted small', text: 'A recording takes the whole page while it plays; the run on this computer goes on, and "Back to live" returns to it.' }));
    return { title: 'Key moments', sub: 'lab · session', node };
  }
}

const shortTitle = r => {
  const t = String(r.title || '');
  const i = t.indexOf(':');
  return i > 0 ? t.slice(i + 1).replace(/\(.*?\)\s*$/, '').trim() : '';
};

// The moments worth starting a replay at: the request, every hand-off, faults,
// problems, corrections, Stop / Continue and what was made.
export function moments(events) {
  const out = [], times = eventTimes(events);
  const at = e => eventTimeLabel(times.get(e.id));
  let lastFix = -1;
  events.forEach((e, i) => {
    const p = e.payload || {};
    let m = null;
    if (e.kind === 'user.message' && (p.kind || 'message') === 'message') m = ['user', '›', `You: ${p.text || ''}`];
    else if (e.kind === 'user.message' && p.kind === 'veto') m = ['user', '›', `You: ${p.text || ''}`];
    else if (e.kind === 'agent.delegate') m = [p.to || 'agent', label(p.to)[0], `${label(e.agent || 'orchestrator')} → ${label(p.to)}: ${p.task || ''}`];
    else if (e.kind === 'fault.injected') m = ['fault', '⚡', `Testbench fault: ${String(p.fault || '').replace(/_/g, ' ')}`];
    else if (e.kind === 'tool.denied') m = ['fault', '!', `Blocked: ${p.tool} · ${p.reason || ''}`];
    else if (e.kind === 'safety.violation') m = ['fault', '!', `Refused: ${p.device}.${p.command} · ${p.reason || ''}`];
    else if (e.kind === 'step.corrected' && i - lastFix > 40) { m = ['fix', '↻', `Auto-correct: ${String(p.name || '').replace(/_/g, ' ')}`]; lastFix = i; }
    else if (e.kind === 'system.stopped') m = ['fault', '■', 'Stop pressed'];
    else if (e.kind === 'system.continued') m = ['fix', '▶', 'Continue pressed'];
    else if (e.kind === 'agent.error') m = ['fault', '!', `Agent error: ${p.error || p.type || ''}`];
    else if (e.kind === 'job.status' && ['parked', 'failed', 'waiting_human'].includes(p.status)) m = ['fault', '⏸', `Job ${String(p.status).replace('_', ' ')}: ${p.message || ''}`];
    else if (e.kind === 'job.submitted' && p.flow === 'reactor_reaction') m = ['reactor', '◉', 'Two-flake reaction started'];
    else if (e.kind === 'job.submitted' && p.flow === 'reactor_unload') m = ['fix', '↻', 'Reactor unloading started (waits until cold)'];
    else if (e.kind === 'reactor.process') m = ['fix', '◉', `Reactor ${p.chip_id || ''}: ${p.disposition || p.phase || 'process state checked'}`];
    else if (e.kind === 'reactor.observation' && !out.some(x => x.reactorJob === e.run_id)) m = ['fix', '◉', `Reactor observation: ${p.chip_id || ''}`];
    else if (e.kind === 'sample.updated' && p.status === 'reactor_pair_prepared' && !out.some(x => x.pair === p.id)) m = ['done', '✓', `Two-flake pair prepared: ${p.id}`];
    else if (e.kind === 'sample.updated' && p.kind === 'flake' && p.status === 'reactor_product' && !out.some(x => x.dev === p.id)) m = ['done', '✓', `Single reactor product accepted: ${p.id}`];
    else if (e.kind === 'sample.updated' && p.kind === 'device' && !out.some(x => x.dev === p.id)) { m = ['done', '✓', `${p.id} is built`]; }
    if (!m) return;
    const text = m[2].length > 120 ? m[2].slice(0, 120) + '…' : m[2];
    out.push({ i, kind: m[0], icon: m[1], text, when: at(e), dev: e.kind === 'sample.updated' ? p.id : undefined,
      reactorJob: e.kind === 'reactor.observation' ? e.run_id : undefined,
      pair: p.status === 'reactor_pair_prepared' ? p.id : undefined });
  });
  return out;
}
