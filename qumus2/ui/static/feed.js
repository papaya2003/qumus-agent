// The conversation, as a group chat (Jialei, workbench review item 4).
//
// You are on the right in a dark bubble. Every team member is on the left with
// its avatar and name. A hand-off is a message from Qumus to the member
// ("Qumus → Hands · the task"); the member's report is the member's own
// message. Everything a member does on the way (its tool calls, its interim
// notes, its hardware jobs) sits at the bottom of that member's bubble: open
// while it works (the last few rows), folded to one line once it has reported.
// Qumus's own tool calls fold the same way under Qumus's next message.
//
// Shown in full besides messages: problems that need a person, errors, plan
// changes, hardware job results, Stop / Continue. A verification within
// tolerance is not listed (the camera card shows it); an automatic correction
// is one line. Focus mode hides everything that is not a message.
//
// Tool results pair with their calls by tool_use_id (by name only when an
// older recording has no ids).

import { h, avatar, label, markdown, short, flowLabel, RUN_STATUS, VERDICT, stepName, fmt, fmtDur, imgUrl, imageSource } from './ui.js';
import { eventTimeLabel } from './replay.js';

const TOOL_STATUS = { ok: 'done', corrected: 'corrected', needs_attention: 'needs attention', denied: 'blocked',
  error: 'error', pending: 'submitted', paused: 'paused' };
const TOOL_CLASS = { ok: 'ok', corrected: 'fix', needs_attention: 'warn', denied: 'warn', error: 'bad', pending: 'ok', paused: 'warn' };
const JOB_END = new Set(['succeeded', 'failed', 'cancelled', 'parked', 'waiting_human', 'paused']);
const SHOW_WHILE_WORKING = 3;   // tool rows visible under a working member
const END_TAG = { returned: null, refused: ['warn', 'not sent'], stopped: ['paused', 'stopped'], replaced: ['paused', 'replaced'] };

export class Feed {
  // hooks: openRun(id), openPlan(rev), photo(src) -> lightbox, faultText(p)
  constructor(scroller, hooks) {
    this.scroller = scroller;
    this.hooks = hooks;
    this.list = h('div', { class: 'feed-list' });
    this.working = h('div', { class: 'working', hidden: true });
    this.jump = h('button', { class: 'jump', hidden: true, onclick: () => this.toBottom(true) });
    scroller.replaceChildren(this.list, this.working);
    scroller.parentNode.appendChild(this.jump);
    this.pinned = true;
    this._auto = 0;
    this.newCount = 0;
    this.clock = 'wall';        // 'wall' = time of day (live), 'elapsed' = since the first message (replay)
    this.stoppable = true;      // the working line says "Esc to stop" (not in a replay)
    this.focus = false;
    try { this.setFocus(localStorage.getItem('qumus.feed') === 'focus'); } catch { /* private window */ }
    scroller.addEventListener('scroll', () => {
      if (this._auto) return;  // our own scrolling
      this.pinned = this.distance() < 48;
      if (this.pinned) { this.newCount = 0; this.jump.hidden = true; }
    }, { passive: true });
    // pictures load and cards open after they were added: keep the bottom in view
    new ResizeObserver(() => { this.reportControls(); if (this.pinned) this.toBottom(); }).observe(this.list);
    this.reset();
  }

  reset(intro) {
    this.list.replaceChildren(...(intro ? [intro] : []));
    this.cards = new Map();      // agent -> the member's open bubble (working)
    this.cardById = new Map();   // delegate tool_use_id -> bubble
    this.calls = new Map();      // tool_use_id -> tool row
    this.pending = [];           // tool rows without an id, oldest first
    this.bubbles = new Map();    // instruction_id -> your bubble
    this.jobAgent = new Map();   // run_id -> agent that submitted it
    this.lastQumus = null;
    this.t0 = null;
    this.workTools = 0;          // tool calls since the working line last changed
    this.pinned = true;
    this.newCount = 0;
    this.jump.hidden = true;
  }

  // Full (everything) / Focus (messages only)
  setFocus(on) {
    this.focus = !!on;
    this.scroller.classList.toggle('focus', this.focus);
    if (this.scroller.id === 'feed') document.querySelectorAll('#feed-mode button').forEach(b => b.classList.toggle('on', (b.dataset.m === 'focus') === this.focus));
    try { localStorage.setItem('qumus.feed', this.focus ? 'focus' : 'full'); } catch { /* private window */ }
    if (this.pinned) this.toBottom();
  }

  distance() { const s = this.scroller; return s.scrollHeight - s.scrollTop - s.clientHeight; }
  toBottom(force) {
    if (force) this.pinned = true;
    this._auto++;
    this.scroller.scrollTop = this.scroller.scrollHeight;
    requestAnimationFrame(() => { this._auto = Math.max(0, this._auto - 1); });
    this.newCount = 0;
    this.jump.hidden = true;
  }
  unread() {
    this.newCount++;
    this.jump.replaceChildren(h('span', { text: '↓' }), ` ${this.newCount} new`);
    this.jump.hidden = false;
  }

  append(node) {
    const empty = this.list.querySelector('.empty');
    if (empty) empty.remove();
    this.list.appendChild(node);
    if (this.pinned) this.toBottom(); else this.unread();
    return node;
  }
  touched() { if (this.pinned) this.toBottom(); }

  // the wall clock of the event (`wall`; `ts` is the testbench's clock, which stands still while the model thinks)
  when(e) {
    if (this.clock === 'elapsed' && this.recordedTimes?.has(e.id)) return eventTimeLabel(this.recordedTimes.get(e.id));
    const t = typeof e.wall === 'number' ? e.wall : e.ts;
    if (t == null) return '';
    if (this.t0 == null) this.t0 = t;
    if (this.clock === 'elapsed') {
      const s = Math.max(0, Math.round(t - this.t0)), m = Math.floor(s / 60);
      return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : `${m}:${String(s % 60).padStart(2, '0')}`;
    }
    const d = new Date(t * 1000);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }

  // a replay cut a pause short (replay.js MAX_GAP_S)
  skipped(seconds, busy) {
    if (seconds < 5) return;  // a few seconds cut from a pause are not worth a line
    const why = busy ? 'Qumus was thinking' : 'nothing happened';
    this.append(h('div', { class: 'skipline item', title: 'The replay plays pauses longer than 5 s for 5 s and skips the rest' },
      h('span', {}, h('b', { text: `${Math.round(seconds)} s skipped` }), ` · ${why}`)));
  }

  // "✻ Picker is working… · 2 min 10 s · 8 tool calls · Esc to stop"
  setWorking(busy, who, text) {
    this.working.hidden = !busy;
    this.workWho = who; this.workText = text; this.workTools = 0;
    if (busy) this.renderWorking(this._elapsed);
    if (this.pinned) this.toBottom();
  }

  // Read-only projection of a complete recording, never live idle or a prefix.
  // Closing the file is not evidence that an agent, tool or physical job succeeded.
  finishRecordedActivity(session, {modelDriven = false} = {}) {
    if (!session?.log?.tools?.calls || !session?.book?.runs) return;
    const explicit = new Set();
    for (const event of session.events) {
      const payload = event.payload || {}, agent = event.agent || 'orchestrator';
      if (event.kind === 'agent.delegate' && payload.to) explicit.add(payload.to);
      if (event.kind === 'agent.request') explicit.add(agent);
      if (event.kind === 'agent.done' || event.kind === 'agent.turn_end') explicit.delete(agent);
      if (event.kind === 'agent.error' || event.kind === 'agent.interrupted') explicit.clear();
    }
    const settled = new Set(['succeeded', 'failed', 'cancelled']);
    const staleActivity = new Set(['working', 'Recorded activity', 'Incomplete in recording']);
    const unknownJob = [...session.book.runs.values()].some(run => !settled.has(run.status) && !this.jobAgent.get(run.id));
    for (const member of this.cards.values()) {
      if (!staleActivity.has(member._tag.textContent)) continue;
      const unmatched = [...session.log.tools.calls.values()].some(call => (call.event.agent || 'orchestrator') === member._agent);
      const unfinishedJob = [...this.jobAgent].some(([id, agent]) => agent === member._agent
        && !settled.has(session.book.runs.get(id)?.status));
      const incomplete = modelDriven || explicit.has(member._agent) || unmatched || unfinishedJob || unknownJob;
      member._tag.hidden = false;
      member._tag.className = incomplete ? 'tag warn' : 'tag';
      member._tag.textContent = incomplete ? 'Incomplete in recording' : 'Recorded activity';
      if (!incomplete) {
        member.classList.remove('busy', 'open');
        member.classList.add('recorded');
        this.foldText(member, member._end);
        this.trim(member);
      }
    }
  }
  tickWorking(elapsedS) {
    this._elapsed = elapsedS;
    if (!this.working.hidden) this.renderWorking(elapsedS);
  }
  renderWorking(elapsedS) {
    const who = this.workWho || 'orchestrator';
    const bits = [];
    if (elapsedS != null && elapsedS >= 1) bits.push(fmtDur(elapsedS));
    if (this.workTools) bits.push(`${this.workTools} tool call${this.workTools > 1 ? 's' : ''}`);
    if (this.stoppable) bits.push('Esc to stop');
    this.working.replaceChildren(h('i', { class: 'spark', text: '✻' }), h('b', { text: label(who) }),
      h('span', { text: ` ${this.workText || (who === 'orchestrator' ? 'is thinking' : 'is working')}…` }),
      ...(bits.length ? [h('span', { class: 'wmeta', text: bits.join(' · ') })] : []));
  }

  // ------------------------------------------------------------ bubbles
  // your message, on the right
  me(e, text) {
    const b = h('div', { class: 'bub' }, h('small', { text: `You · ${this.when(e)}` }), h('div', { class: 'btext', text }));
    return h('div', { class: 'msg me item' }, b);
  }

  // a team member's message, on the left: name line, text, jobs, tools at the bottom
  member(agent, e, { to, text, md, open } = {}) {
    const tag = h('span', { class: 'tag running', text: 'working', hidden: !open });
    const nm = h('div', { class: 'nm' }, h('b', { text: label(agent), style: `color: var(--a-${agent})` }),
      to ? h('span', { class: 'to', text: `→ ${label(to)}` }) : null, tag, h('span', { class: 'when', text: this.when(e) }));
    const body = h('div', { class: 'btext' });
    if (text != null) body.appendChild(md ? markdown(text) : h('div', { text }));
    this.hooks.ids?.(body);
    const jobs = h('div', { class: 'card-jobs' });
    const tbody = h('div', { class: 'tbody' });
    const fold = h('button', { class: 'fold', onclick: ev => { ev.stopPropagation(); this.toggleTools(m); } });
    const tools = h('div', { class: 'tools', hidden: true }, fold, tbody);
    const bub = h('div', { class: 'bub mb' }, nm, body, jobs, tools);
    const m = h('div', { class: 'msg member item' + (open ? ' open busy' : ''), dataset: { a: agent } }, avatar(agent), bub);
    Object.assign(m, { _agent: agent, _tag: tag, _body: body, _jobs: jobs, _tools: tools, _tbody: tbody, _fold: fold,
      _n: 0, _warn: 0, _jobN: 0, _t0: typeof e.wall === 'number' ? e.wall : e.ts, _expanded: false });
    return m;
  }

  // the bubble a member's activity goes into: its open bubble, else a new one
  host(agent, e) {
    let m = this.cards.get(agent);
    if (!m) {
      m = this.member(agent, e, { open: true });
      m.classList.add('activity');
      this.cards.set(agent, m);
      this.append(m);
    }
    return m;
  }
  addTool(m, row) {
    m._tbody.appendChild(row);
    m._n++;
    m._tools.hidden = false;
    this.foldText(m);
    this.trim(m);
    this.touched();
  }
  // while working only the newest rows show; once reported, the list is folded
  trim(m) {
    const rows = [...m._tbody.children];
    rows.forEach((r, i) => r.classList.toggle('older', !m._expanded && m.classList.contains('busy') && i < rows.length - SHOW_WHILE_WORKING));
  }
  foldText(m, e) {
    const parts = [`${m._n} tool call${m._n > 1 ? 's' : ''}`];
    if (m._warn) parts.push(`${m._warn} blocked`);
    const t = e ? (typeof e.wall === 'number' ? e.wall : e.ts) : null;
    if (t != null && m._t0 != null && t - m._t0 >= 1) parts.push(fmtDur(t - m._t0));
    const older = m.classList.contains('busy') && !m._expanded ? Math.max(0, m._n - SHOW_WHILE_WORKING) : 0;
    m._fold.replaceChildren(h('span', { class: 'chev' }), h('span', { text: parts.join(' · ') }),
      ...(older ? [h('span', { class: 'muted', text: ` · ${older} earlier` })] : []));
  }
  toggleTools(m) {
    m._expanded = !m._expanded;
    m.classList.toggle('expanded', m._expanded);
    this.foldText(m, m._end);
    this.trim(m);
    this.touched();
  }
  // the member has reported (or was stopped): fold the tools, show the state
  close(m, state, e) {
    m.classList.remove('busy', 'open');
    m.classList.add(state);
    m._end = e;
    const t = END_TAG[state];
    m._tag.hidden = !t;
    if (t) { m._tag.className = 'tag ' + t[0]; m._tag.textContent = t[1]; }
    m._expanded = false; m.classList.remove('expanded');
    this.foldText(m, e);
    this.trim(m);
    if (this.cards.get(m._agent) === m) this.cards.delete(m._agent);
  }
  // text into a member's bubble: a working bubble's interim note, or its report
  say(m, text, { report } = {}) {
    const md = markdown(text);
    this.hooks.ids?.(md);
    if (report) {
      // SDKs can emit the final text as both agent.message and agent.done.
      // Coalesce only exact duplicate text in this delegation's bubble;
      // distinct interim notes and the original events remain intact.
      m._body.querySelectorAll('.interim').forEach(n => {
        if (n._text === text) n.remove(); else n.classList.add('dim');
      });
      const wrap = h('div', { class: 'report' }, md);
      const more = h('button', { class: 'more', text: 'Show all', onclick: ev => { ev.stopPropagation(); wrap.classList.toggle('full'); more.textContent = wrap.classList.contains('full') ? 'Show less' : 'Show all'; this.touched(); } });
      m._body.append(wrap, more);
      requestAnimationFrame(() => this.reportControls());
    } else {
      const interim = h('div', { class: 'interim' }, md);
      interim._text = text;
      m._body.appendChild(interim);
    }
    m.classList.add('has-text');
    this.touched();
  }

  reportControls() {
    // History builds its feed off screen. Measure again when it becomes
    // visible; a hidden report's zero height cannot decide "Show all".
    this.list.querySelectorAll('.report').forEach(wrap => {
      const md = wrap.firstElementChild, more = wrap.nextElementSibling;
      if (md?.clientHeight && more?.classList.contains('more')) {
        more.hidden = !wrap.classList.contains('full') && md.scrollHeight <= md.clientHeight + 2;
      }
    });
  }

  // ------------------------------------------------------------ rows
  // ⏺ tool · args  /  ⎿ result
  toolRow(p, agent) {
    const dot = h('span', { class: 'tool-state run', text: '…', title: 'Running', 'aria-label': 'Running' });
    const sum = h('span', { class: 'sum', text: short(p.args || {}, 110) });
    const res = h('div', { class: 'res', hidden: true }, h('i', { text: '⎿' }), h('span', { class: 'rsum' }));
    const detail = h('pre', { hidden: true });
    detail.textContent = 'args ' + JSON.stringify(p.args || {}, null, 1);
    const row = h('div', { class: 'tc', title: 'Click for the arguments and the result',
      onclick: ev => { ev.stopPropagation(); detail.hidden = !detail.hidden; this.touched(); } },
    dot, h('div', { class: 'head' }, h('span', { class: 'tool', text: p.tool }), sum), res, detail);
    Object.assign(row, { _dot: dot, _sum: sum, _res: res, _detail: detail, _key: `${agent}|${p.tool}` });
    return row;
  }

  jobLine(run, status, extra) {
    const st = RUN_STATUS[status] || status;
    return h('div', { class: `jline ${status}`, title: 'Open the job record', onclick: () => this.hooks.openRun(run.id) },
      h('span', { class: 'jicon' }), h('span', { class: 'jflow', text: flowLabel(run.flow) }),
      h('span', { class: 'tag ' + status, text: st }), extra ? h('span', { class: 'sum', text: extra }) : null);
  }

  photos(images) {
    return h('div', { class: 'thumbs inline' }, [...new Set(images)].slice(0, 6).map(src => h('img', {
      src: imgUrl(src), alt: imageSource(src), title: `${imageSource(src)} · click to enlarge`, loading: 'lazy',
      onclick: ev => { ev.stopPropagation(); this.hooks.photo(src); } })));
  }

  // a line under a member's bubble (job result, correction) or on its own
  under(agent, node, where) {
    const m = this.cards.get(agent);
    if (m) { m[where === 'jobs' ? '_jobs' : '_jobs'].appendChild(node); this.touched(); } else this.append(h('div', { class: 'item jwrap' }, node));
  }

  // ------------------------------------------------------------ events
  // run: the page's record of a job ({id, flow, steps, ...}) for job events
  add(e, run) {
    const p = e.payload || {};
    switch (e.kind) {
      case 'user.message': {
        if (p.kind === 'stop' || p.kind === 'continue') break;  // shown as a divider when handled
        const row = this.append(this.me(e, p.text));
        if (p.instruction_id) this.bubbles.set(p.instruction_id, row);
        break;
      }
      case 'user.dropped': {
        const row = this.bubbles.get(p.instruction_id);
        if (row) {
          row.classList.add('dropped');
          row.appendChild(h('div', { class: 'note', text: 'Not delivered: Stop was pressed before Qumus read it' }));
        }
        break;
      }
      case 'agent.message': {
        const a = e.agent || 'orchestrator';
        const m = this.cards.get(a);
        if (m && a === 'orchestrator') {  // Qumus's tool calls fold under what Qumus says next
          this.say(m, p.text, { report: true });
          this.close(m, 'returned', e);
          this.lastQumus = m;
          break;
        }
        if (m) { this.say(m, p.text); break; }
        const row = this.append(this.member(a, e, { text: p.text, md: true }));
        row.classList.add('has-text');
        if (a === 'orchestrator') this.lastQumus = row;
        break;
      }
      case 'agent.delegate': {
        const q = this.cards.get('orchestrator');
        if (q) this.close(q, 'returned', e);   // Qumus's tools so far fold under the hand-off
        const hand = this.append(this.member('orchestrator', e, { to: p.to, text: p.task || '' }));
        hand.classList.add('has-text', 'handoff');
        const old = this.cards.get(p.to);
        if (old) this.close(old, 'replaced', e);
        const m = this.member(p.to, e, { open: true });
        m.classList.add('activity');
        m._body.appendChild(h('div', { class: 'interim wait', text: 'On it…' }));
        this.cards.set(p.to, m);
        if (p.tool_use_id) this.cardById.set(p.tool_use_id, m);
        this.append(m);
        break;
      }
      case 'agent.done': {
        let m = this.cards.get(e.agent);
        if (!m) { m = this.member(e.agent, e, { open: true }); this.append(m); }
        const wait = m._body.querySelector('.interim.wait');
        if (wait) wait.remove();
        if (p.summary) this.say(m, p.summary, { report: true });
        this.close(m, 'returned', e);
        break;
      }
      case 'agent.interrupted':
        for (const m of [...this.cards.values()]) this.close(m, 'stopped', e);
        break;
      case 'tool.call': {
        if (p.tool === 'delegate') break;   // the hand-off bubble is that call
        const a = e.agent || 'orchestrator';
        const m = this.host(a, e);
        const row = this.toolRow(p, a);
        row._box = m;
        this.addTool(m, row);
        if (p.tool_use_id) this.calls.set(p.tool_use_id, row); else this.pending.push(row);
        this.workTools++;
        if (!this.working.hidden) this.renderWorking(this._elapsed);
        break;
      }
      case 'tool.result': {
        if (p.tool === 'delegate') break;
        let row = p.tool_use_id ? this.calls.get(p.tool_use_id) : null;
        if (!row) {
          const key = `${e.agent}|${p.tool}`;
          const i = this.pending.findIndex(l => l._key === key);
          if (i >= 0) row = this.pending.splice(i, 1)[0];
        }
        if (!row) {  // a result without its call (an older recording)
          const m = this.host(e.agent || 'orchestrator', e);
          row = this.toolRow({ tool: p.tool, args: {} }, e.agent);
          row._box = m; this.addTool(m, row);
        }
        if (p.tool_use_id) this.calls.delete(p.tool_use_id);
        const cls = TOOL_CLASS[p.status] || 'ok';
        row._dot.className = 'tool-state ' + cls;
        row._dot.textContent = cls === 'ok' ? '✓' : cls === 'fix' ? '↻' : '!';
        row._dot.title = TOOL_STATUS[p.status] || p.status || 'Done';
        row._dot.setAttribute('aria-label', row._dot.title);
        row.classList.add(cls);
        row._res.hidden = false;
        row._res.querySelector('.rsum').textContent = (p.summary || TOOL_STATUS[p.status] || p.status || '').trim();
        row._detail.textContent += '\n\nresult ' + JSON.stringify(p.data || {}, null, 1).slice(0, 4000);
        if ((p.images || []).length && !row._thumbs) {
          row._thumbs = this.hooks.inspection?.(e) || this.photos(p.images);
          row.appendChild(row._thumbs);
        }
        if (cls === 'warn' && row._box) { row._box._warn++; this.foldText(row._box); }
        this.touched();
        break;
      }
      case 'tool.denied': {
        if (p.tool === 'delegate' && p.tool_use_id && this.cardById.has(p.tool_use_id)) {
          const m = this.cardById.get(p.tool_use_id);
          const wait = m._body.querySelector('.interim.wait');
          if (wait) wait.remove();
          m._body.appendChild(h('div', { class: 'refused' }, h('b', { text: 'Refused by a rule' + (p.rule ? ` (${p.rule})` : '') }), h('div', { text: p.reason || '' })));
          m.classList.add('has-text');
          this.close(m, 'refused', e);
          this.touched();
          break;
        }
        const a = e.agent || 'orchestrator';
        const m = this.host(a, e);
        const i = p.tool_use_id ? -1 : this.pending.findIndex(l => l._key === `${a}|${p.tool}`);
        if (i >= 0) this.pending.splice(i, 1)[0].remove();
        const row = h('div', { class: 'tc warn' }, h('span', { class: 'tool-state warn', text: '!', title: 'Blocked', 'aria-label': 'Blocked' }),
          h('div', { class: 'head' }, h('span', { class: 'tool', text: p.tool }), h('span', { class: 'tag warn', text: 'blocked' })),
          h('div', { class: 'res' }, h('i', { text: '⎿' }), h('span', { class: 'rsum wrap', text: `${p.reason || ''}${p.rule ? ` (rule ${p.rule})` : ''}` })));
        m._warn++;
        this.addTool(m, row);
        break;
      }
      case 'plan.updated':
        this.append(h('button', { class: 'planline item', onclick: () => this.hooks.openPlan(p.revision) },
          h('span', { class: 'pl-icon' }), h('span', {}, h('b', { text: `${label(e.agent)} ${p.revision > 1 ? 'changed' : 'wrote'} the plan` }),
            ` · revision ${p.revision} · ${(p.plan || []).length} steps`), p.note ? h('span', { class: 'muted', text: p.note }) : null));
        break;
      case 'job.submitted':
        if (e.run_id) this.jobAgent.set(e.run_id, e.agent);
        break;
      case 'job.status': {
        if (!run) break;
        if (/^recovered/.test(p.message || '')) {
          this.append(h('div', { class: 'banner item recover' }, h('b', { text: 'Job process restarted. ' }),
            `The job goes on from its last checkpoint (${p.message.replace(/^recovered:\s*/, '')}).`));
          break;
        }
        if (!JOB_END.has(p.status)) break;
        const corr = [...run.steps.values()].reduce((n, s) => n + (s.corrections || 0), 0);
        let extra = corr ? `${corr} automatic correction${corr > 1 ? 's' : ''}` : '';
        if (p.status !== 'succeeded' && p.message) extra = p.message;
        this.under(this.jobAgent.get(e.run_id), this.jobLine(run, p.status, extra));
        break;
      }
      case 'step.corrected': {
        if (!run) break;
        const st = run.steps.get(p.seq);
        this.under(this.jobAgent.get(e.run_id), h('div', { class: 'vline' }, h('span', { class: 'tag corrected', text: 'auto-corrected' }),
          h('span', { class: 'sum', text: `${stepName(p.name || st?.name)}: ${p.action || 'correction'}${run.lastOffset ? ` (was off by ${run.lastOffset})` : ''}` })));
        break;
      }
      case 'step.verified': {
        if (p.verdict === 'ok' || p.verdict === 'correctable') break;
        this.under(this.jobAgent.get(e.run_id), h('div', { class: 'vline warn' }, h('span', { class: 'tag ' + p.verdict, text: VERDICT[p.verdict] || p.verdict }),
          h('span', { class: 'sum', text: `${stepName(p.name)}${p.message ? `: ${p.message}` : ''}` })));
        break;
      }
      case 'safety.violation':
        this.append(h('div', { class: 'banner item danger' }, h('b', { text: 'Refused by the safety layer. ' }),
          `${p.device}.${p.command}: ${p.reason || ''}`));
        break;
      case 'notification':
        if (p.level === 'info' && /^agents:/.test(p.text || '')) break;  // shown in the title bar
        if (p.level === 'needs_human' || p.level === 'danger' || p.level === 'warn') {
          this.append(h('div', { class: 'banner item' + (p.level === 'danger' ? ' danger' : '') },
            p.level === 'needs_human' ? h('b', { text: 'Needs a person. ' }) : null, p.text,
            p.level === 'needs_human' ? h('div', { class: 'note', text: 'Reply here to go on.' }) : null));
        } else this.append(h('div', { class: 'sys item', text: p.text }));
        break;
      case 'fault.injected':
        this.append(h('div', { class: 'sys item fault' }, h('b', { text: 'Testbench fault' }), ` · ${this.hooks.faultText(p)}`));
        break;
      case 'agent.error':
        for (const m of [...this.cards.values()]) this.close(m, 'stopped', e);
        this.append(h('div', { class: 'banner item danger' }, h('b', { text: 'Agent stopped with an error. ' }),
          p.error || p.type || 'unknown error', h('div', { class: 'note', text: p.outage
            ? 'The model service is unavailable; Qumus waits and continues by itself.'
            : 'Nothing else was sent. Send a message to try again.' })));
        break;
      case 'agent.outage_wait':
        this.append(h('div', { class: 'sys item', text: p.text }));
        break;
      case 'agent.outage_cancelled':
        this.append(h('div', { class: 'sys item', text: 'Automatic continue cancelled (Stop pressed).' }));
        break;
      case 'agent.outage_gave_up':
        this.append(h('div', { class: 'banner item danger' }, p.text));
        break;
      case 'agent.turn_end': {
        const q = this.cards.get('orchestrator');
        if (q) this.close(q, 'returned', e);
        const bits = [];
        if (p.reason === 'interrupted') bits.push('paused by you');
        else if (p.is_error) bits.push(p.reason === 'budget_exhausted' ? 'stopped: budget exhausted' : `ended with an error${p.reason ? ` (${p.reason})` : ''}`);
        else bits.push('turn finished');
        if (p.num_turns) bits.push(`${p.num_turns} model call${p.num_turns > 1 ? 's' : ''}`);
        if (p.model) bits.push(p.model);
        this.append(h('div', { class: 'turnend item' + (p.is_error && p.reason !== 'interrupted' ? ' bad' : '') }, h('span', { text: bits.join(' · ') })));
        break;
      }
      case 'model.changed':
        this.append(h('div', { class: 'divider item' }, h('span', { text: `Next task model: ${p.model} · fresh conversation context` })));
        break;
      case 'system.stopped': {
        const n = (p.dropped || []).length;
        this.append(h('div', { class: 'divider item stop' }, h('span', {},
          h('b', { text: 'Stopped' }), ' · Agents interrupted; every job pauses at its next safe point',
          n ? ` · ${n} message${n > 1 ? 's' : ''} not delivered` : '')));
        for (const m of this.cards.values()) { m._tag.hidden = false; m._tag.className = 'tag paused'; m._tag.textContent = 'stopped'; }
        break;
      }
      case 'system.continued': {
        const res = p.resumed || [], wait = p.waiting_human || [];
        const lines = [...res.map(r => `${flowLabel(r.flow)} continues from step ${r.from_step}${r.step ? ` (${stepName(r.step)})` : ''}`),
          ...wait.map(w => `${flowLabel(w.flow)} waits for a person: ${w.reason}`)];
        this.append(h('div', { class: 'divider item go' }, h('span', {}, h('b', { text: 'Continued' }),
          lines.length ? '' : ' · no job was paused'), lines.length ? h('ul', {}, lines.map(t => h('li', { text: t }))) : null));
        for (const m of this.cards.values()) { m._tag.className = 'tag running'; m._tag.textContent = 'working'; }
        break;
      }
      default:
        break;
    }
  }
}

export function offsetText(m) {
  return m && m.offset_mm != null ? `${fmt(m.offset_mm)} mm / ${fmt(m.drot_deg ?? 0, 1)}°` : null;
}
