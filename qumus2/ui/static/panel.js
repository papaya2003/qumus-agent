// Problems: the log (data) and the full-width panel above the status bar (view).
//
// The panel (workbench review item 7, as changed by Jialei) shows Problems
// only. Every allowed device command is a count in the tab ("Safety: all 1,284
// device commands checked and allowed"); the rows are the exceptions: refusals
// by the safety layer, blocked tool calls, checks that failed, jobs that
// stopped or wait for a person, warnings. What one step of one job led to is
// one problem (review issue 12: an unclear check, the job that then waits for
// a person and Qumus's note about it are one problem): the row shows the first
// reason and the latest state, and opens to its parts. Each row has "↗ step"
// that jumps to the row in the progress tree. Always collapsed at first (also
// in a replay); a new problem while collapsed paints the top edge red until the
// panel is opened. The Events tab lists testbench faults and shows only with
// the testbench tools.
//
// The Results list the same problems (results.js), each with its details: the
// log keeps the job, the step and the events of every part.

import { $, h, label, flowLabel, stepName, VERDICT, RUN_STATUS } from './ui.js';
import { ToolAttempts } from './toolattempts.js';

const OK_VERDICTS = new Set(['ok', 'correctable', 'not_verifiable']);
// a grouped problem's latest state, on its row
export const PROBLEM_STATE = { wait: '⏸ waits for a person', bad: '✗ stopped', warn: '⚠ check not passed' };

export class ProblemLog {
  // hooks: when(e) -> time text; faultText(p)
  constructor(hooks) { this.hooks = hooks || {}; this.reset(); }

  reset() {
    this.problems = [];   // one per problem: { cls, icon, when, who, text, key, run, group, parts: [...] }
    this.groups = new Map();  // "run|seq" -> its problem
    this.events = [];     // testbench faults
    this.cmds = 0;
    this.refused = 0;
    this.added = 0;       // problems so far (the panel counts what it has not shown)
    this.curStep = {};    // run_id -> step key being run (for "↗ step")
    this.version = 0;
    this.tools = new ToolAttempts();
    this.budgetStops = []; this.lastBudgetNotice = null; this.lastBudgetFailure = null; this.lastUserMessage = null;
    this.modelStops = [];
  }

  // group: what makes two entries one problem (the job's step), null = its own row
  problem(e, cls, icon, who, text, key, group) {
    const part = { cls, icon, when: this.hooks.when ? this.hooks.when(e) : '', who, text, key: key || null, run: e.run_id || null,
      id: e.id, kind: e.kind, payload: e.payload || {} };
    const pr = group && this.groups.get(group);
    if (pr) {
      // the latest state shows on the row; the first reason stays its text
      pr.parts.push(part);
      Object.assign(pr, { cls, icon, resolved: false, key: pr.key || part.key });
    } else {
      const fresh = { ...part, group: group || null, parts: [part], n: this.problems.length };
      this.problems.push(fresh);
      if (group) this.groups.set(group, fresh);
      this.added++;
    }
    this.version++;
  }

  // the job went on (Continue, or it finished): its open problems are settled
  // warnOnly: only a failed check the job got past by itself (not one waiting for a person)
  settle(run, status, warnOnly = false) {
    for (const pr of this.problems) {
      if (pr.run !== run || pr.resolved || pr.cls === 'bad' || (warnOnly && pr.cls !== 'warn')) continue;
      pr.resolved = status === 'handled' ? 'handled by the job' : status === 'succeeded' ? 'job finished' : 'job went on';
      this.version++;
    }
  }

  add(e, run) {
    const p = e.payload || {}, r = e.run_id;
    const changes = this.tools.add(e);
    for (const issue of changes.opened) {
      this.problem(e, 'bad', '✗', label(e.agent), `${p.tool} ${e.kind === 'tool.denied' ? 'blocked' : 'rejected'} · ${p.reason || p.summary || ''}`, null, `tool:${issue.id}`);
    }
    for (const issue of changes.resolved) {
      const pr = this.groups.get(`tool:${issue.id}`);
      if (!pr) continue;
      pr.resolved = issue.resolved;
      for (const proof of issue.resolutionEvidence || [e]) {
        if (pr.parts.some(part => part.id === proof.id)) continue;
        pr.parts.push({ cls: 'ok', icon: '✓', when: this.hooks.when?.(proof) || '', who: 'Qumus',
          text: proof === issue.resolution ? issue.resolved : 'linked recovery evidence',
          id: proof.id, kind: proof.kind, payload: proof.payload });
      }
      this.version++;
    }
    for (const issue of changes.recoveredJobs) for (const pr of this.problems) {
      if (pr.run !== issue.run || pr.resolved || pr.parts.some(part => part.kind === 'safety.violation')
        || !pr.parts.some(part => part.kind === 'job.status' && part.payload.status === 'failed')) continue;
      pr.resolved = issue.resolved;
      pr.parts.push({cls: 'ok', icon: '✓', when: this.hooks.when?.(e) || '', who: label(issue.call.event.agent),
        text: `${issue.resolved} · ${issue.retryJob}`, id: e.id, kind: e.kind, payload: p, run: e.run_id});
      this.version++;
    }
    if (e.kind === 'user.message') this.lastUserMessage = e;
    if (e.kind === 'agent.delegation_checked' && p.ok || e.kind === 'job.status' && p.status === 'succeeded') {
      for (const stop of this.budgetStops) if (this.lastUserMessage?.id > stop.event.id
          && stop.project === this.tools.project) stop.evidence = e;
    }
    if (e.kind === 'agent.turn_end' && e.agent === 'orchestrator') {
      if (p.reason === 'budget_exhausted') this.lastBudgetFailure = e;
      if (p.reason === 'budget_exhausted' && this.lastBudgetNotice && this.lastBudgetNotice.project === this.tools.project) {
        this.budgetStops.push({event: e, notice: this.lastBudgetNotice, project: this.tools.project,
          session: p.session_id, evidence: null});
        this.lastBudgetNotice = null;
      } else if (!p.is_error && p.reason === 'success' && p.session_id) {
        for (const stop of this.modelStops) {
          if (stop.payload.session_id !== p.session_id || stop.payload.project_id !== (p.project_id || this.tools.project) ||
              !(this.lastUserMessage?.id > stop.id)) continue;
          const problem = this.problems.find(pr => pr.id === stop.id);
          if (!problem || problem.resolved) continue;
          problem.resolved = 'a later requested model turn succeeded in the same session';
          problem.parts.push({ cls: 'ok', icon: '✓', who: 'Agent', text: problem.resolved,
            when: this.hooks.when?.(e) || '', id: e.id, kind: e.kind, payload: p });
          this.version++;
        }
        for (const stop of this.budgetStops) {
          if (stop.session !== p.session_id || stop.project !== this.tools.project || !stop.evidence) continue;
          const pr = this.problems.find(pr => pr.id === stop.notice.id);
          if (!pr || pr.resolved) continue;
          pr.resolved = 'saved session resumed successfully after the budget stop';
          for (const proof of [stop.event, this.lastUserMessage, stop.evidence, e]) pr.parts.push({cls: 'ok', icon: '✓',
            when: this.hooks.when?.(proof) || '', who: 'Qumus', text: proof === e ? pr.resolved : 'linked continuation evidence',
            id: proof.id, kind: proof.kind, payload: proof.payload});
          this.version++;
        }
      }
    }
    switch (e.kind) {
      case 'device.command': this.cmds++; this.version++; break;
      case 'step.started':
        // the job went on to another step by itself (e.g. it changed a torn stamp)
        if (this.curStep[r] && this.curStep[r] !== `${r}|${p.seq}`) this.settle(r, 'handled', true);
        this.curStep[r] = `${r}|${p.seq}`;
        break;
      case 'safety.violation':
        this.refused++;
        this.problem(e, 'bad', '✗', p.device || '', `${p.command || 'command'} refused · ${p.reason || ''}`, this.curStep[r], r && (this.curStep[r] || r));
        break;
      case 'step.verified':
        if (OK_VERDICTS.has(p.verdict)) break;
        this.problem(e, 'warn', '⚠', flowLabel(run?.flow), `${stepName(p.name)} · check ${VERDICT[p.verdict] || p.verdict}${p.message ? ` · ${p.message}` : ''}`, `${r}|${p.seq}`, r && `${r}|${p.seq}`);
        break;
      case 'job.status':
        if (p.status === 'failed') this.problem(e, 'bad', '✗', flowLabel(run?.flow), `job failed${p.message ? ` · ${p.message}` : ''}`, this.curStep[r] || r, r && (this.curStep[r] || r));
        else if (p.status === 'waiting_human' || p.status === 'parked') this.problem(e, 'wait', '⏸', flowLabel(run?.flow), `${RUN_STATUS[p.status]}${p.message ? ` · ${p.message}` : ''}`, this.curStep[r] || r, r && (this.curStep[r] || r));
        else if (p.status === 'running' || p.status === 'succeeded') this.settle(r, p.status);
        break;
      case 'notification': {
        if (p.reason === 'lab_restarted' && p.restart_id) {
          for (const problem of this.problems) {
            if (problem.payload?.reason !== 'lab_restarting' || problem.payload.restart_id !== p.restart_id || problem.resolved) continue;
            problem.resolved = 'lab process restarted; startup recovery completed';
            problem.parts.push({ cls: 'ok', icon: '✓', who: 'Qumus', text: problem.resolved,
              when: this.hooks.when?.(e) || '', id: e.id, kind: e.kind, payload: p });
            this.version++;
          }
        }
        // a note about a job (it carries the job's run id) joins that job's problem
        const g = r && (this.curStep[r] || r);
        if (!r && p.level === 'danger' && (p.reason === 'budget_exhausted' || /budget.*(?:exhaust|cannot cover)/i.test(p.text || ''))) {
          const prior = this.lastBudgetFailure;
          if (prior && p.session_id && prior.payload.session_id === p.session_id
              && p.project_id === this.tools.project && (!this.lastUserMessage || this.lastUserMessage.id < prior.id)) {
            this.budgetStops.push({event: prior, notice: e, project: this.tools.project, session: p.session_id, evidence: null});
          } else this.lastBudgetNotice = {...e, project: this.tools.project};
        }
        if (p.level === 'needs_human') this.problem(e, 'wait', '⏸', 'Qumus', p.text || 'needs a person', null, g);
        else if (p.level === 'danger') this.problem(e, 'bad', '✗', 'Qumus', p.text || '', null, g);
        else if (p.level === 'warn') this.problem(e, 'warn', '⚠', 'Qumus', p.text || '', null, g);
        break;
      }
      case 'agent.error':
        this.problem(e, 'bad', '✗', 'Agent', `stopped with an error · ${p.error || p.type || ''}`);
        if (p.session_id && p.project_id) this.modelStops.push(e);
        break;
      case 'fault.injected':
        this.events.push({ when: this.hooks.when ? this.hooks.when(e) : '', text: this.hooks.faultText ? this.hooks.faultText(p) : JSON.stringify(p) });
        this.version++;
        break;
      default: break;
    }
  }
}

// a grouped problem's latest state in words ("✓ handled by the job")
export const problemState = pr => (pr.resolved ? `✓ ${pr.resolved}` : pr.parts.length > 1 ? PROBLEM_STATE[pr.cls] || '' : '');

export class Panel {
  // hooks: jump(key | run_id) -> reveal the tree row
  constructor(log, hooks) {
    this.log = log;
    this.hooks = hooks || {};
    this.el = $('panel');
    this.list = $('plist');
    this.tab = 'problems';
    this.open = false;
    this.reset();
    document.querySelectorAll('#panel .ptab').forEach(b => {
      b.onclick = () => { if (this.open && this.tab === b.dataset.t) this.show(false); else { this.tab = b.dataset.t; this.show(true); } };
    });
    $('panel-toggle').onclick = () => this.show(!this.open);
  }

  reset() {
    this.recoveredOpen = false;
    this.expanded = new Set();  // problems opened to their parts (index)
    this.seen = 0;              // problems already shown (or there while the panel was open)
    this.drawn = -1;
    this.el.classList.remove('alert');
  }

  show(on) {
    this.open = on;
    this.el.classList.toggle('open', on);
    this.list.hidden = !on;
    $('panel-toggle').textContent = on ? '▾' : '▴';
    $('panel-toggle').title = on ? 'Collapse the panel' : 'Open the panel';
    this.drawn = -1; this.render();
  }

  render() {
    const L = this.log;
    if (this.open) this.seen = L.added;
    const unseen = L.problems.filter(p => !p.resolved && p.n >= this.seen).length;
    this.el.classList.toggle('alert', unseen > 0);
    const key = `${L.version}|${this.open}|${this.tab}|${unseen > 0}`;
    if (key === this.drawn) return;
    this.drawn = key;
    const active = L.problems.filter(p => !p.resolved), recovered = L.problems.filter(p => p.resolved);
    const n = active.length;
    $('pb-n').textContent = String(n);
    $('pb-n').classList.toggle('bad', unseen > 0);
    // the safety count apart from the problem count, so it is not read as problems
    const n0 = L.cmds.toLocaleString(), one = L.cmds === 1;
    $('pb-ok').textContent = L.refused
      ? `Safety: ${n0} device command${one ? '' : 's'} allowed, ${L.refused} refused`
      : L.cmds ? `Safety: ${one ? 'the 1 device command' : `all ${n0} device commands`} checked and allowed` : 'Safety: no device command yet';
    $('pb-ev').textContent = String(L.events.length);
    document.querySelectorAll('#panel .ptab').forEach(b => b.classList.toggle('on', this.open && b.dataset.t === this.tab));
    if (!this.open) return;
    if (this.tab === 'events') {
      this.list.replaceChildren(...(L.events.length ? L.events.map(ev => h('div', { class: 'ar' }, h('span', { class: 'st', text: '⚡' }),
        h('span', { class: 'n', text: ev.when }), h('b', { text: 'testbench' }), h('span', { class: 'x', text: ev.text })))
        : [h('div', { class: 'placeholder', text: 'No testbench faults were injected.' })]));
      return;
    }
    const rows = n ? [] : [h('div', { class: 'placeholder', text: 'No unresolved problems.' })];
    const recoveredRows = [];
    L.problems.forEach(pr => {
      const i = pr.n, into = pr.resolved ? recoveredRows : rows;
      const many = pr.parts.length > 1, open = many && this.expanded.has(i);
      // the latest state, when it differs from the first reason ("⏸ Waiting for a person")
      const state = problemState(pr);
      into.push(h('div', { class: `ar ${pr.resolved ? 'done' : pr.cls}${many ? ' grp' : ''}`, title: pr.parts.map(x => `${x.when} ${x.who}: ${x.text}`).join('\n'),
        onclick: many ? () => { if (open) this.expanded.delete(i); else this.expanded.add(i); this.drawn = -1; this.render(); } : null },
        h('span', { class: 'st', text: pr.resolved ? '✓' : pr.icon }), h('span', { class: 'n', text: pr.when }), h('b', { text: pr.who }), h('span', { class: 'x', text: pr.text }),
        ...(state ? [h('span', { class: 'ar-state', text: state })] : []),
        ...(many ? [h('span', { class: 'ar-n', text: `${open ? '▾' : '▸'} ${pr.parts.length}`, title: 'What this problem led to, step by step' })] : []),
        ...(pr.key ? [h('button', { class: 'ar-jump', text: '↗ step', title: 'Show this step in the progress tree', onclick: ev => { ev.stopPropagation(); this.hooks.jump?.(pr.key); } })] : [])));
      if (open) for (const x of pr.parts) into.push(h('div', { class: 'ar sub ' + x.cls },
        h('span', { class: 'st', text: x.icon }), h('span', { class: 'n', text: x.when }), h('b', { text: x.who }), h('span', { class: 'x', text: x.text })));
    });
    if (recovered.length) rows.push(h('details', { class: 'recovered-problems', open: this.recoveredOpen,
      ontoggle: ev => { this.recoveredOpen = ev.target.open; } },
      h('summary', { text: `Recovered earlier · ${recovered.length}` }), recoveredRows));
    this.list.replaceChildren(...rows);
    if (!this.expanded.size) this.list.scrollTop = this.list.scrollHeight;
  }
}
