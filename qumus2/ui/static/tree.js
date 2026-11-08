// Task Plan -> individual tool call -> originating job -> lab action -> command.
// Queries and wait/status calls never create duplicate jobs. Jobs without an
// observed submitting tool (older recordings/future events) retain a fallback
// under their plan step. Rebuild from event prefixes on replay/seek.

import { h, avatar, label, flowLabel, RUN_STATUS, fmtDur, short } from './ui.js';
import { actionText, checkState } from './actions.js';
import { ToolAttempts } from './toolattempts.js';

const ICON = { todo: '○', going: '▶', ok: '✓', fix: '↻', bad: '✗', skip: '–', wait: '⏸' };
const SUBMITS = { submit_device_plan: ['device', 'Device Guidance'], submit_flake_selection: ['flakes', 'Flake selection'], submit_design: ['layout', 'Layout'], create_device_design: ['layout', 'KLayout design'] };
// "x 123.6 y 155 z 5 · speed 20": one level of nesting flattened, numbers rounded
function argText(params) {
  const num = v => (Number.isInteger(v) ? String(v) : +v.toFixed(1));
  const one = (k, v) => (typeof v === 'number' ? `${k} ${num(v)}` : typeof v === 'boolean' ? `${k} ${v ? 'on' : 'off'}`
    : v && typeof v === 'object' && !Array.isArray(v) ? Object.entries(v).map(([k2, v2]) => one(k2, v2)).join(' ') : `${k} ${JSON.stringify(v)}`);
  return Object.entries(params || {}).map(([k, v]) => one(k, v)).join(' · ');
}

const STEP_ICON = { done: 'ok', running: 'going', failed: 'bad', skipped: 'skip', pending: 'todo' };

export class ProgressTree {
  // book: the PlanBook (plan revisions); hooks: seek(key | eventId) in a replay,
  // openRun(id), openTab(name) for a submitted plan / selection / layout
  constructor(book, hooks) {
    this.book = book;
    this.hooks = hooks || {};
    this.root = h('div', { class: 'tree' });
    this.reset();
  }

  reset() {
    this.openAll = false;        // a replay: every step and job open unless closed by hand
    this.jobs = new Map();       // run_id -> job
    this.work = new Map();       // plan step id -> { tools: [], results: [] }
    this.jobStep = new Map();    // run_id -> plan step id
    this.open = new Set();       // open rows (plan step ids, run ids, step keys)
    this.closed = new Set();     // rows the person closed
    this.cur = null;             // step key the replay shows
    this.highlight = null;       // step key the Problems panel pointed at
    this.cmds = 0;               // device commands so far ("allowed")
    this.dirty = true;
    this.attempts = new ToolAttempts();
  }

  // A replay lists every job and lab action from the start (to seek to), as
  // "to come": scan the whole recording once for the jobs, their steps and the
  // plan step each job belongs to; playback then fills in what happened.
  preload(events) {
    let plan = [];
    const stepFor = agent => {
      const running = plan.filter(s => s.status === 'running');
      const mine = running.find(s => s.owner === agent) || running[0];
      return mine ? mine.id : null;
    };
    for (const e of events) {
      const p = e.payload || {}, r = e.run_id;
      if (e.kind === 'plan.updated' || e.kind === 'plan.step') { plan = p.plan || plan; continue; }
      if (!r) continue;
      if (e.kind === 'job.submitted') {
        const j = this.job(r, e); j.flow = p.flow || j.flow; j.agent = e.agent || j.agent; j.status = 'todo';
        if (!this.jobStep.has(r)) { const sid = stepFor(e.agent); if (sid != null) this.jobStep.set(r, sid); }
      } else if (e.kind === 'step.started') {
        const j = this.job(r, e);
        if (!j.steps.has(p.seq)) j.steps.set(p.seq, { key: `${r}|${p.seq}`, seq: p.seq, name: p.name || '', status: 'todo', verdict: null, corrections: 0, t0: e.ts, t1: e.ts, cmds: [], later: true });
      } else if (e.kind === 'step.finished') {
        const j = this.jobs.get(r), st = j && j.steps.get(p.seq);
        if (st && st.later) st.t1 = e.ts;
      }
    }
    this.openAll = true;
    this.dirty = true;
  }

  // ---------------------------------------------------------------- data in
  latestPlan() { const p = this.book.plans[this.book.plans.length - 1]; return p ? p.plan : []; }
  // the plan step an agent's work belongs to right now
  stepFor(agent) {
    const plan = this.latestPlan();
    const running = plan.filter(s => s.status === 'running');
    const mine = running.find(s => s.owner === agent) || running[0];
    if (mine) return mine.id;
    const owned = plan.filter(s => s.owner === agent && s.status !== 'done');
    return owned.length ? owned[0].id : null;
  }
  workOf(stepId) {
    if (!this.work.has(stepId)) this.work.set(stepId, { tools: [], results: [] });
    return this.work.get(stepId);
  }

  add(e) {
    const changed = this.attempts.add(e);
    if (changed.opened.length || changed.resolved.length || changed.recoveredJobs.length) this.dirty = true;
    const p = e.payload || {}, t = e.ts;   // lab time, like the steps (the testbench's clock stands still while the model thinks)
    const r = e.run_id;
    switch (e.kind) {
      case 'plan.updated': case 'plan.step': this.dirty = true; return;
      case 'job.submitted': {
        const j = this.job(r, e);
        j.flow = p.flow || j.flow; j.params = p.params || {}; j.agent = e.agent || j.agent; j.t0 = t;
        if (!this.jobStep.has(r)) this.jobStep.set(r, this.stepFor(e.agent));
        const tools = this.work.get(this.jobStep.get(r))?.tools || [];
        const origin = [...tools].reverse().find(t => t.agent === e.agent && t.status === 'running' &&
          (t.tool === p.flow || (t.tool === 'search_chip' && p.flow === 'revisit_chip')));
        if (origin) j.toolEvent = origin.eventId;
        break;
      }
      case 'job.status': { const j = this.job(r, e); j.status = p.status; j.message = p.message; j.t1 = t; break; }
      case 'step.started': {
        const j = this.job(r, e);
        const st = j.steps.get(p.seq);
        if (st && st.later) Object.assign(st, { status: 'in_progress', later: false, t0: e.ts, t1: e.ts });
        else j.steps.set(p.seq, { key: `${r}|${p.seq}`, seq: p.seq, name: p.name || '', status: 'in_progress', verdict: null, corrections: 0, t0: e.ts, t1: e.ts, cmds: [] });
        j.curSeq = p.seq; j.t1 = t;
        if (j.status === 'todo') j.status = 'running';
        break;
      }
      case 'device.command': {
        this.cmds++;
        const j = this.jobs.get(r), st = j && j.steps.get(j.curSeq);
        if (st) { st.cmds.push({ id: e.id, device: p.device, command: p.command, params: p.params || {} }); st.t1 = e.ts; }
        break;
      }
      case 'safety.violation': {
        const j = this.jobs.get(r), st = j && j.steps.get(j.curSeq);
        if (st) st.cmds.push({ id: e.id, device: p.device, command: p.command, params: p.params || {}, refused: p.reason || 'refused' });
        break;
      }
      case 'step.verified': case 'step.corrected': case 'step.finished': {
        const j = this.jobs.get(r), st = j && j.steps.get(p.seq);
        if (!st) return;
        if (e.kind === 'step.verified') st.verdict = p.verdict;
        else if (e.kind === 'step.corrected') st.corrections += 1;
        else { st.status = p.status || 'done'; if (p.corrections != null) st.corrections = p.corrections; }
        st.t1 = e.ts; j.t1 = t;
        break;
      }
      case 'tool.call': {
        const a = e.agent || 'orchestrator';
        if (a === 'orchestrator' || p.tool === 'delegate') return;
        const sid = this.stepFor(a);
        if (sid == null) return;
        this.workOf(sid).tools.push({ id: p.tool_use_id, eventId: e.id, tool: p.tool, status: 'running', agent: a });
        break;
      }
      case 'tool.result': case 'tool.denied': {
        const a = e.agent || 'orchestrator';
        if (a === 'orchestrator') return;
        let hit = null;
        for (const w of this.work.values()) {
          hit = w.tools.find(x => (p.tool_use_id && x.id === p.tool_use_id) || (!p.tool_use_id && x.tool === p.tool && x.status === 'running'));
          if (hit) {
            hit.status = e.kind === 'tool.denied' ? 'denied' : p.status || 'ok';
            hit.summary = e.kind === 'tool.denied' ? p.reason : p.summary;
            hit.resultId = e.id;
            hit.resultJob = p.data?.run_id || p.data?.job_id;
            if (e.kind === 'tool.result' && SUBMITS[p.tool] && p.status === 'ok') w.results.push({ tab: SUBMITS[p.tool][0], text: SUBMITS[p.tool][1], summary: p.summary || '' });
            break;
          }
        }
        if (!hit && e.kind === 'tool.denied') {
          const sid = this.stepFor(a);
          if (sid != null) this.workOf(sid).tools.push({ id: p.tool_use_id, tool: p.tool, status: 'denied', agent: a, resultId: e.id, summary: p.reason });
        } else if (!hit) return;
        break;
      }
      default: return;
    }
    this.dirty = true;
  }
  job(r, e) {
    let j = this.jobs.get(r);
    if (!j) {
      j = { id: r, flow: (e.payload || {}).flow || '?', status: 'queued', agent: e.agent, steps: new Map(), curSeq: null, t0: null, t1: null };
      this.jobs.set(r, j);
    }
    return j;
  }

  // the step a replay shows right now (the caption's step)
  setCurrent(key) { if (key !== this.cur) { this.cur = key; this.dirty = true; } }
  // open every parent of a step key (or a run id) and light the row
  reveal(key) {
    const run = String(key).split('|')[0];
    const sid = this.jobStep.get(run);
    if (sid != null) { this.open.add(`s${sid}`); this.closed.delete(`s${sid}`); }
    this.open.add(run); this.closed.delete(run);
    const toolEvent = this.jobs.get(run)?.toolEvent;
    if (toolEvent != null) { this.open.add(`tool:${toolEvent}`); this.closed.delete(`tool:${toolEvent}`); }
    this.open.add(key); this.closed.delete(key);
    this.highlight = key; this.dirty = true;
    this.render();
    const row = this.root.querySelector('.tr.lit');
    if (row) row.scrollIntoView({ block: 'center' });
  }
  // cur: whether the row is open right now (its default may say so without a click)
  toggle(id, cur) {
    if (cur) { this.open.delete(id); this.closed.add(id); } else { this.open.add(id); this.closed.delete(id); }
    this.dirty = true; this.render();
  }
  isOpen(id, dflt) { return this.open.has(id) || (dflt && !this.closed.has(id)); }

  // ---------------------------------------------------------------- rows
  row(level, cls, icon, title, right, opts = {}) {
    const el = h('div', { class: `tr l${level} ${cls}${opts.extra ? ' ' + opts.extra : ''}`, style: `--ind:${8 + (level - 1) * 16}px`, title: opts.title || '' },
      opts.chev !== undefined ? h('span', { class: 'chev' + (opts.chev ? ' open' : ''), onclick: opts.onchev || null, title: opts.onchev ? (opts.chev ? 'Fold' : 'Unfold') : null }) : h('span', { class: 'nochev' }),
      h('span', { class: 'st ' + cls, text: icon }),
      ...(opts.avatar ? [avatar(opts.avatar)] : []),
      ...(opts.seq != null ? [h('span', { class: 'seq', text: opts.seq })] : []),
      h('span', { class: 't', text: title, title: title }),
      ...(opts.who ? [h('span', { class: 'who', text: opts.who })] : []),
      ...(opts.note ? [h('span', { class: 'note', text: opts.note })] : []),
      h('span', { class: 'tm', text: right || '' }),
      ...(opts.action ? [opts.action] : []));
    if (opts.onclick) el.onclick = opts.onclick;
    return el;
  }

  render() {
    if (!this.dirty) return;
    this.dirty = false;
    const plan = this.latestPlan();
    const rows = [];
    const byStep = new Map();
    for (const [run, sid] of this.jobStep) { if (!byStep.has(sid)) byStep.set(sid, []); byStep.get(sid).push(run); }
    const todo = r => this.jobs.get(r)?.status === 'todo';
    const loose = [...this.jobs.keys()].filter(r => this.jobStep.get(r) == null);
    const orphan = loose.filter(r => !todo(r));
    if (!plan.length && !this.jobs.size) {
      this.root.replaceChildren(h('div', { class: 'placeholder', text: 'Qumus writes its plan here before it hands out work; every job and lab action hangs under its step.' }));
      return;
    }
    plan.forEach((s, i) => {
      const id = `s${s.id}`;
      const jobs = (byStep.get(s.id) || []).map(r => this.jobs.get(r)).filter(Boolean);
      const w = this.work.get(s.id);
      const kids = jobs.length + (w ? w.tools.length + w.results.length : 0);
      const open = kids > 0 && this.isOpen(id, this.openAll || (s.status && s.status !== 'pending') || jobs.some(j => j.status === 'running' || j.status === 'paused' || j.status === 'waiting_human'));
      let cls = STEP_ICON[s.status] || 'todo';
      if (jobs.some(j => j.status === 'waiting_human' || j.status === 'parked')) cls = 'wait';
      else if (jobs.some(j => j.status === 'failed' && !this.attempts.jobIssues.get(j.id)?.resolved)) cls = 'bad';
      else if (cls === 'ok' && jobs.some(j => this.attempts.jobIssues.get(j.id)?.resolved || [...j.steps.values()].some(st => st.corrections))) cls = 'fix';
      const dur = this.stepDuration(jobs, w);
      rows.push(this.row(1, cls, ICON[cls], `${i + 1} ${s.title}`, dur, { chev: kids > 0 ? open : undefined, who: label(s.owner), extra: (s.status === 'running' ? 'cur' : '') + (kids > 0 ? ' has-kids' : ''),
        title: `${s.title} · ${label(s.owner)} · ${s.status || 'to do'}`, onclick: kids > 0 ? () => this.toggle(id, open) : null }));
      if (!open) return;
      if (w && w.tools.length) {
        const issueFor = t => this.attempts.issues.get(t.resultId) || this.attempts.jobIssues.get(t.resultJob);
        for (const tool of w.tools) {
          const issue = issueFor(tool), children = jobs.filter(j => j.toolEvent === tool.eventId && tool.eventId != null);
          const key = `tool:${tool.eventId || tool.id}`, expanded = this.isOpen(key, true);
          const cls = issue?.resolved ? 'fix' : ['denied', 'error', 'needs_attention'].includes(tool.status) || issue ? 'bad'
            : tool.status === 'running' ? 'going' : 'ok';
          const status = issue?.resolved ? 'recovered' : tool.status === 'pending' ? 'submitted' : tool.status === 'ok' ? 'done' : tool.status;
          const row = this.row(2, cls, ICON[cls], tool.tool, status, {extra: 'tool' + (cls === 'going' ? ' current-tool' : ''),
            chev: children.length ? expanded : undefined,
            title: `${tool.tool} · ${status}${tool.summary ? ` · ${tool.summary}` : ''}`,
            onchev: children.length ? ev => { ev.stopPropagation(); this.toggle(key, expanded); } : null,
            onclick: () => { if (this.hooks.seek) this.hooks.seek(tool.resultId || tool.eventId); else if (children[0]) this.hooks.openRun?.(children[0].id); }});
          row.dataset.tool = tool.tool; row.dataset.event = tool.eventId || tool.resultId || '';
          rows.push(row);
          if (expanded) for (const j of children) rows.push(...this.jobRows(j, 3));
        }
      }
      if (w) for (const res of w.results) {
        rows.push(this.row(2, 'ok', '▪', `${res.text}: ${short(res.summary, 60)}`, '▸', { extra: 'job link', title: `Open the ${res.text.toLowerCase()}`, onclick: () => this.hooks.openTab?.(res.tab) }));
      }
      for (const j of jobs) if (!w?.tools.some(t => t.eventId != null && t.eventId === j.toolEvent)) rows.push(...this.jobRows(j));
    });
    // a job whose plan step is not in the latest plan: still to come in a replay,
    // or it ran under a step a newer plan revision dropped
    const unplaced = [...this.jobStep.entries()].filter(([, sid]) => sid != null && !plan.some(s => s.id === sid)).map(([r]) => r);
    const later = [...unplaced, ...loose].filter(todo);
    const earlier = unplaced.filter(r => !todo(r));
    if (!plan.length) rows.push(h('div', { class: 'placeholder tr-ph', text: 'Qumus has not written a plan yet. It writes one before it hands out work; every job then hangs under its step.' }));
    for (const [title, runs, dflt] of [['Under an earlier plan', earlier, true], ['Later in this recording', later, false], ['Jobs outside the plan', orphan, true]]) {
      if (!runs.length) continue;
      const open = this.isOpen(title, dflt);
      rows.push(this.row(1, 'todo', ICON.todo, title, `${runs.length}`, { chev: open, onclick: () => this.toggle(title, open) }));
      if (open) for (const r of runs) rows.push(...this.jobRows(this.jobs.get(r)));
    }
    const top = this.root.scrollTop;
    this.root.replaceChildren(...rows);
    this.root.scrollTop = top;
    const cur = this.root.querySelector('.tr.cur3');
    if (cur && this.cur !== this._revealed) { this._revealed = this.cur; this.scrollTo(cur); }
  }

  stepDuration(jobs, w) {
    const t0 = Math.min(...jobs.map(j => j.t0 ?? Infinity)), t1 = Math.max(...jobs.map(j => j.t1 ?? -Infinity));
    if (jobs.length && Number.isFinite(t0) && Number.isFinite(t1) && t1 - t0 >= 1) return fmtDur(t1 - t0);
    return '';
  }

  jobRows(j, level = 2) {
    const steps = [...j.steps.values()].sort((a, b) => a.seq - b.seq);
    const done = steps.filter(s => s.status !== 'in_progress' && !s.later).length;
    const fixes = steps.reduce((n, s) => n + (s.corrections || 0), 0);
    const recovery = this.attempts.jobIssues.get(j.id);
    const live = j.status === 'running' || j.status === 'paused';
    const cls = recovery?.resolved ? 'fix' : j.status === 'succeeded' ? (fixes ? 'fix' : 'ok') : j.status === 'failed' || j.status === 'cancelled' ? 'bad'
      : j.status === 'waiting_human' || j.status === 'parked' ? 'wait' : live ? 'going' : 'todo';
    const stat = recovery?.resolved ? 'recovered' : j.status === 'todo' ? 'to come' : (RUN_STATUS[j.status] || j.status);
    // level 2: a job opens on a click; a replay unfolds the job being played
    const open = steps.length > 0 && this.isOpen(j.id, this.openAll && live);
    const lab = steps.length ? Math.max(0, (steps[steps.length - 1].t1 ?? 0) - (steps[0].t0 ?? 0)) : 0;
    const right = live && steps.length ? `${done}/${steps.length}` : j.status !== 'todo' && lab >= 1 ? fmtDur(lab) : stat;
    const curSt = live && !open ? steps.find(s => s.seq === j.curSeq && s.status === 'in_progress') : null;
    const note = recovery?.resolved ? (lab >= 1 ? 'recovered' : '') : j.status === 'waiting_human' || j.status === 'parked' || j.status === 'failed' ? (RUN_STATUS[j.status] || j.status)
      : curSt ? actionText(curSt.name) : fixes ? `${fixes} correction${fixes > 1 ? 's' : ''}` : '';
    const rec = h('button', { class: 'tr-act', text: '↗', title: 'Open this job\'s record', onclick: ev => { ev.stopPropagation(); this.hooks.openRun?.(j.id); } });
    const rows = [this.row(level, cls, ICON[cls], j.flow === 'inspect_bench' ? 'Observation job' : flowLabel(j.flow), right, { chev: steps.length ? open : undefined, extra: 'job hw' + (live ? ' cur2' : '') + (curSt ? ' acting' : ''), note, action: rec,
      title: `${flowLabel(j.flow)} · ${stat}${recovery?.resolved ? ` by successful retry ${recovery.retryJob}` : ''}${j.message ? ` · ${j.message}` : ''}`, onclick: () => (steps.length ? this.toggle(j.id, open) : this.hooks.openRun?.(j.id)) })];
    if (!open) return rows;
    for (const st of steps) {
      const [c, text, title] = st.later ? ['todo', '', 'Not yet · click to watch it']
        : st.status === 'in_progress' && ['failed', 'cancelled'].includes(j.status)
          ? ['bad', 'stopped', 'This attempt stopped before its check completed. The original failure is preserved.'] : checkState(st);
      const isCur = st.key === this.cur && live;
      // device commands stay folded until the step's chevron is clicked
      const showCmds = st.cmds.length && this.isOpen(st.key);
      const labS = Math.max(0, (st.t1 ?? st.t0) - st.t0);
      const el = this.row(level + 1, c, ICON[c], j.flow === 'inspect_bench' ? 'Observe bench objects' : actionText(st.name), labS >= 1 ? fmtDur(labS) : '',
        { seq: st.seq, extra: `ac-row ${c}${isCur ? ' cur cur3' : ''}${st.key === this.highlight ? ' lit' : ''}`, chev: st.cmds.length ? !!showCmds : undefined,
          // in a replay the row seeks; its chevron folds
          onchev: this.hooks.seek && st.cmds.length ? ev => { ev.stopPropagation(); this.toggle(st.key, !!showCmds); } : null,
          note: c === 'fix' ? `${st.corrections} fix${st.corrections > 1 ? 'es' : ''}` : c === 'bad' ? text : '', title: `${title}${this.hooks.seek ? ' · click to watch it from its start' : ' · click for its device commands'}`,
          onclick: () => { if (this.hooks.seek) this.hooks.seek(st.key); else this.toggle(st.key, !!showCmds); } });
      el.querySelector('.t').classList.add('ac-name');
      rows.push(el);
      if (!showCmds) continue;
      for (const c4 of st.cmds) {
        const args = argText(c4.params);
        rows.push(this.row(level + 2, c4.refused ? 'bad' : 'ok', c4.refused ? ICON.bad : '·', `${c4.device} ${c4.command}`, '', { extra: 'cmd' + (c4.refused ? ' refused' : ''),
          note: c4.refused ? 'refused' : short(args, 60), title: c4.refused ? `Refused by the safety layer: ${c4.refused}` : args,
          onclick: this.hooks.seek ? () => this.hooks.seek(c4.id) : null }));
      }
    }
    return rows;
  }

  // scroll the tree (only the tree) so the row is in view
  scrollTo(el) {
    const box = this.root;
    const top = el.offsetTop, bottom = top + el.offsetHeight;
    if (top < box.scrollTop + 24 || bottom > box.scrollTop + box.clientHeight - 24) box.scrollTop = Math.max(0, top - box.clientHeight / 3);
  }
}
