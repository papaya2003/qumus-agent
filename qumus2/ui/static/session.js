// One run as data: the plan book and its jobs, the sample ledger, the problems,
// the request, what it cost, and what it made. The page keeps one Session for
// the run on screen ("This run": live, or the recording that plays) and builds
// another for a run it shows from History. Everything here comes from the
// events (the records); nothing is judged by an AI.
//
// summarize() says how a run ended with the same rules as the History list
// on the server (ui/recording.py summarize_events): keep the two in step.

import { label, placeName } from './ui.js';
import { PlanBook } from './planbook.js';
import { Inventory } from './inventory.js';
import { ProblemLog } from './panel.js';
import { shortId } from './flakes.js';

// a sample's state in its history, in words
const SAMPLE_WORD = { bare: 'blank', fresh: 'new film', in_use: 'on the stamp holder', used: 'used', torn: 'film torn' };
const cap = t => (t ? t[0].toUpperCase() + t.slice(1) : t);

const SUBMITS = new Set(['submit_device_plan', 'submit_flake_selection', 'submit_design', 'create_device_design']);
const WAITS = new Set(['waiting_human', 'parked']);

export class Session {
  // hooks: when(e) -> time text; faultText(p)
  constructor(hooks = {}) {
    this.hooks = hooks;
    this.book = new PlanBook();
    this.inv = new Inventory();
    this.log = new ProblemLog({ when: e => this.when(e), faultText: hooks.faultText });
    this.reset();
  }

  reset() {
    this.book.reset(); this.inv.reset(); this.log.reset();
    this.events = [];
    this.submits = new Map();
    this.request = null;      // the first message a person wrote (its event)
    this.words = null;        // Qumus's last message (its event)
    this.asks = [];           // what Qumus asked a person for (needs_human notes)
    this.version = 0;
    this._sum = null;
  }

  when(e) { return this.hooks.when ? this.hooks.when(e) : ''; }

  // one event; returns its job (the plan book's) when it has one
  add(e) {
    const p = e.payload || {};
    this.events.push(e);
    const run = e.run_id ? this.book.track(e) : null;
    switch (e.kind) {
      case 'plan.updated': this.book.addRevision(p, e.agent); break;
      case 'plan.step': this.book.stepProgress(p); break;
      case 'tool.call':
        if (SUBMITS.has(p.tool)) this.submits.set(p.tool_use_id || `${e.agent}|${p.tool}`, { tool: p.tool, args: p.args || {}, agent: e.agent });
        break;
      case 'tool.result':
        if (SUBMITS.has(p.tool)) {
          const key = p.tool_use_id || `${e.agent}|${p.tool}`, c = this.submits.get(key);
          this.submits.delete(key);
          const design = p.data?.device_design;
          if (c && p.status === 'ok' && (c.tool !== 'create_device_design' || design?.version === 'device_design_v1'))
            this.book.accepted(c.tool, c.tool === 'create_device_design' ? design : c.args, c.agent, e.id);
        }
        break;
      case 'user.message':
        if ((p.kind || 'message') === 'message' && !this.request) this.request = e;
        break;
      case 'agent.message':
        if ((e.agent || 'orchestrator') === 'orchestrator' && p.text) this.words = e;
        break;
      case 'notification':
        if (p.level === 'needs_human' && p.text) this.asks.push(e);
        break;
      default: break;
    }
    this.inv.add(e);
    this.log.add(e, run);
    this.version++;
    return run;
  }

  // ------------------------------------------------------------ derived
  get summary() {
    if (!this._sum || this._sum.v !== this.version) this._sum = { v: this.version, ...summarize(this.events) };
    return this._sum;
  }

  // A flake's role in the device, from the Picker's accepted selection ("top hBN")
  roleOf(id) {
    for (const selection of [...this.book.selections].reverse()) for (const [role, ids] of Object.entries(selection.selected_by_role || {})) {
      if ((ids || []).includes(id)) return role.replace(/_/g, ' ').replace(/\bhbn\b/i, 'hBN').replace(/\bgr\b/i, 'graphene');
    }
    return null;
  }
  scoreOf(id) { return [...this.book.selections].reverse().flatMap(s => s.assessments || []).find(a => a.flake_id === id)?.overall_score ?? null; }

  // what this run made or used, by sample id (sample updates after the request:
  // the ledger's seeding at startup is not something the run did)
  touched() {
    const out = { devices: new Set(), flakes: new Set(), chips: new Set() };
    let asked = false;
    for (const e of this.events) {
      if (e.kind === 'user.message' && ((e.payload || {}).kind || 'message') === 'message') asked = true;
      if (!asked || e.kind !== 'sample.updated') continue;
      const p = e.payload || {};
      if (p.kind === 'device') out.devices.add(p.id);
      else if (p.kind === 'flake') out.flakes.add(p.id);
      else if (p.kind === 'chip' && p.status && p.status !== 'bare' && p.status !== 'patterned') out.chips.add(p.id);
    }
    return out;
  }

  // What happened to one flake in this run, from the events: found,
  // photographed, looked at, chosen, picked up, stacked.
  flakeHistory(id) {
    const out = [], seen = { status: null, images: false, residue: false }, calls = new Map();
    const add = (e, text, cls) => out.push({ when: this.when(e), t: typeof e.wall === 'number' ? e.wall : e.ts || 0, text, cls });
    for (const e of this.events) {
      const p = e.payload || {};
      if (e.kind === 'sample.updated' && p.id === id) {
        const pr = p.props || {};
        if (seen.status == null) add(e, p.status === 'reactor_product' ? 'Accepted from a two-flake reaction'
          : p.status === 'prepared' ? 'Registered as a prepared reactor input'
          : `Found in the 10× scan of ${pr.chip_id || p.location}`, 'found');
        if (p.status !== seen.status && seen.status != null) {
          const t = { catalogued: 'Found again at 20× and 100×', lost: 'Not found again at 20×', vetoed: 'Excluded: a person said not to use it',
            reactor_consumed: `Consumed by ${pr.reactor_consumed_by || 'the accepted reaction'}`,
            on_stamp: `Picked up on ${p.location}`, stacked: `Stacked into ${pr.device || 'a device'} on ${p.location}`,
            candidate: seen.status === 'on_stamp' ? `Back on ${p.location}` : null }[p.status];
          if (t) add(e, t, p.status === 'lost' || p.status === 'vetoed' ? 'bad' : 'step');
        }
        const shot = Object.keys(pr.images && !Array.isArray(pr.images) ? pr.images : {});
        if (shot.length && !seen.images) { add(e, shot.includes('reactor') ? 'Saved reactor acceptance observation'
          : `Photographed at ${shot.map(k => k.replace('x', '×')).join(' and ')}`, 'found'); seen.images = true; }
        if (pr.residue && !seen.residue) { add(e, `Surface judged ${pr.residue.level} by the ${label(pr.residue.by || 'picker')}`, 'pick'); seen.residue = true; }
        seen.status = p.status;
      } else if (e.kind === 'tool.call' && (p.tool === 'view_flake_images' || p.tool === 'submit_flake_selection') && JSON.stringify(p.args || {}).includes(id)) {
        calls.set(p.tool_use_id || `${e.agent}|${p.tool}`, { e, p });
      } else if (e.kind === 'tool.result' && calls.has(p.tool_use_id || `${e.agent}|${p.tool}`)) {
        const key = p.tool_use_id || `${e.agent}|${p.tool}`, c = calls.get(key);
        calls.delete(key);
        if (p.status !== 'ok') continue;
        if (c.p.tool === 'view_flake_images') add(c.e, `${label(c.e.agent)} looked at its photos`, 'pick');
        else {
          const a = (c.p.args.assessments || []).find(x => x.flake_id === id);
          const role = Object.entries(c.p.args.selected_by_role || {}).find(([, ids]) => (ids || []).includes(id))?.[0];
          add(c.e, role ? `Chosen as ${role.replace(/_/g, ' ')}${a && a.overall_score != null ? ` (score ${Math.round(a.overall_score)})` : ''}${a && a.reason ? `: ${a.reason}` : ''}`
            : `Not chosen${a && a.reason ? `: ${a.reason}` : ''}`, 'pick');
        }
      } else if (e.kind === 'step.finished' && String(p.name || '').includes(id)) {
        const what = String(p.name).replace(id, shortId(id)).replace(/_/g, ' ');
        add(e, `${what}: ${p.status === 'done' ? 'done' : p.status}${p.corrections ? ` after ${p.corrections} correction${p.corrections > 1 ? 's' : ''}` : ''}`, p.status === 'done' ? 'step' : 'bad');
      } else if (e.kind === 'user.message' && p.kind === 'veto' && String(p.text || '').includes(id)) {
        add(e, 'You asked Qumus not to use it', 'bad');
      }
    }
    // a Picker's verdict lands when its call returns; show it at the call's time
    return out.sort((a, b) => a.t - b.t);
  }

  // What happened to any other sample (a chip, a stamp, a device) in this run
  sampleHistory(id) {
    const out = [];
    let last = null;
    const word = st => SAMPLE_WORD[st] || String(st || '').replace(/_/g, ' ');
    const where = loc => placeName(loc) || String(loc || '').replace(/_/g, ' ');
    for (const e of this.events) {
      const p = e.payload || {};
      if (e.kind !== 'sample.updated' || p.id !== id) continue;
      if (last && last.status === p.status && last.location === p.location) continue;
      const moved = last && last.location !== p.location, changed = last && last.status !== p.status;
      const text = !last ? `${cap(word(p.status)) || 'In the ledger'} · ${where(p.location)}`
        : moved ? `Moved to ${where(p.location)}${changed ? ` · ${word(p.status)}` : ''}`
          : cap(word(p.status));
      out.push({ when: this.when(e), text, cls: p.status === 'torn' ? 'bad' : !last ? 'found' : 'step' });
      last = { status: p.status, location: p.location };
    }
    return out;
  }

  // The last device command each instrument got (Lab · Status details)
  lastCommand(device) {
    for (let i = this.events.length - 1; i >= 0; i--) {
      const e = this.events[i], p = e.payload || {};
      if ((e.kind === 'device.command' || e.kind === 'safety.violation') && p.device === device) return e;
    }
    return null;
  }
}

// How a run ended, from its events alone: the History list's rules
// (ui/recording.py summarize_events), the same order and the same words.
const LAB_GAP_MAX_S = 3600;  // recording.py LAB_GAP_MAX_S

export function summarize(events) {
  let request = '', plan = [], stopped = false, error = false, cost = 0, asked = false, costBasis = null;
  const jobs = new Map(), devices = new Set(), flakes = new Set(), chips = new Set(), faults = [];
  let lab = 0, last = null;
  for (const e of events) {
    const k = e.kind, p = e.payload || {};
    if (typeof e.ts === 'number') {
      // the clock's forward steps; a jump between clocks (older run stores) is not lab work
      const d = last == null ? 0 : e.ts - last;
      if (d > 0 && d <= LAB_GAP_MAX_S) lab += d;
      last = e.ts;
    }
    if (k === 'user.message' && (p.kind || 'message') === 'message') { request = request || String(p.text || ''); asked = true; error = false; }
    else if (k === 'plan.updated' || k === 'plan.step') plan = p.plan || plan;
    else if (k === 'job.status' && e.run_id) jobs.set(e.run_id, p.status);
    else if (k === 'job.submitted' && e.run_id && !jobs.has(e.run_id)) jobs.set(e.run_id, 'queued');
    else if (k === 'system.stopped') stopped = true;
    else if (k === 'system.continued') stopped = false;
    else if (k === 'agent.error') error = true;
    else if (k === 'agent.turn_end') { cost += +(p.cost_usd || 0); error = !!p.is_error && p.reason !== 'interrupted'; costBasis ||= p.cost_basis; }
    else if (k === 'fault.injected') faults.push(String(p.fault || ''));
    else if (k === 'sample.updated' && asked) {
      if (p.kind === 'device') devices.add(p.id);
      else if (p.kind === 'flake') flakes.add(p.id);
      else if (p.kind === 'chip' && p.status && p.status !== 'bare' && p.status !== 'patterned') chips.add(p.id);
    }
  }
  const ends = [...jobs.values()];
  let outcome;
  if (ends.some(s => WAITS.has(s))) outcome = 'needs a person';
  else if (stopped) outcome = 'stopped';
  else if (error) outcome = 'error';
  else if (plan.length && plan.every(st => ['done', 'skipped'].includes(st.status || 'pending'))) outcome = 'done';
  else if (plan.some(st => st.owner === 'human' && ['running', 'in_progress'].includes(st.status))
    && plan.filter(st => st.owner !== 'human').every(st => ['done', 'skipped'].includes(st.status))
    && !ends.some(s => ['queued', 'running', 'paused'].includes(s))) outcome = 'awaiting review';
  else if (ends.includes('failed')) outcome = 'failed';
  else if (!plan.length && !jobs.size) outcome = request ? 'answered' : 'nothing asked';
  else outcome = 'unfinished';
  return { request, outcome, steps: plan.length, jobs: jobs.size, devices: [...devices].sort(), flakes: flakes.size, chips: chips.size,
    faults, lab_s: +lab.toFixed(1), cost_usd: +cost.toFixed(2), cost_basis: costBasis };
}

// an outcome as a tag: [text, class]
export const OUTCOME = {
  done: ['done', 'ok'], 'needs a person': ['needs a person', 'waiting_human'], 'awaiting review': ['awaiting review', 'waiting_human'], stopped: ['stopped', 'paused'], error: ['Agent error', 'failed'],
  failed: ['a job failed', 'failed'], answered: ['answered', 'done'], unfinished: ['unfinished', 'pending'], 'nothing asked': ['nothing asked', ''],
};
