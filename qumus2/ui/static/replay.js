// Replay of a recorded run (qumus2 replay; recording made by ui/recording.py).
// The page downloads the recording once and plays it itself: events go to
// the same handlers as live events, the bench comes from recorded frames.
// Pure state + timing; the page wires the controls (app.js).
//
// Time: the player's clock advances `speed` seconds per real second along one
// of two axes (docs/architecture.md, UI decisions 5 and 6):
//  - 'lab' (default): lab time. Each gap between two events lasts as long as
//    the slower of the two clocks says: the testbench's simulated clock (`ts`,
//    what the real instruments would take; it stands still while the model
//    thinks) or the wall clock (`wall`, the model's thinking; device commands take
//    ~0 wall time on the testbench). So instruments move at their real pace.
//  - 'session': the original run's wall clock (device bursts flash by).
// A gap longer than MAX_GAP_S plays for MAX_GAP_S. A pause (model thinking,
// nobody at the keyboard) is skipped, with a marker in the conversation and
// on the timeline; a long lab wait (heating, cooling, a slow lift) is
// fast-forwarded, and the bench caption says so. The timeline is linear in
// that compressed time; the clock shows the uncompressed time of the axis.
//
// Step mode: playback stops before every `step.started`, so each lab action
// can be watched and checked on its own (stepNext / stepPrev).

export const MAX_GAP_S = 5;
// Match recording.py: agent wall time and a separate lab's SimClock may use
// different epochs. Crossing between them is not months of laboratory work.
const LAB_GAP_MAX_S = 3600;
// Missing clock gaps may use this playback pace, never elapsed laboratory time.
const NO_TIMING_GAP_S = 0.25;
// the clock never jumps more than this per tick: a hidden tab holds the replay
const MAX_TICK_S = 0.25;
// how far past a device command its lab frame is looked for (events in between
// come from other threads: a tool call, another job)
const FRAME_LOOKAHEAD = 200;
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'parked', 'waiting_human', 'paused']);

// Use the same two clocks in the player, conversation and History. Lab time
// includes model thinking and simulated instrument time; session is wall time.
// Separate lab/agent epochs never become an invented months-long wait.
function observedClockGap(a, b) {
  const knownWall = Number.isFinite(a.wall) && Number.isFinite(b.wall);
  const finiteSim = Number.isFinite(a.ts) && Number.isFinite(b.ts);
  const rawSim = finiteSim ? b.ts - a.ts : 0;
  const knownSim = finiteSim && rawSim >= 0 && rawSim <= LAB_GAP_MAX_S;
  return {wall: knownWall ? Math.max(0, b.wall - a.wall) : 0,
    sim: knownSim ? rawSim : 0, known: knownWall || knownSim};
}

export function eventGap(a, b) {
  const {wall, sim} = observedClockGap(a, b);
  return [wall, Math.max(wall, sim), sim > wall];
}

export function eventTimes(events) {
  let lab = 0;
  const origin = events.find(e => Number.isFinite(e.wall))?.wall;
  return new Map(events.map((e, i) => {
    if (i) lab += eventGap(events[i - 1], e)[1];
    return [e.id, { lab, session: Number.isFinite(e.wall) && origin != null ? Math.max(0, e.wall - origin) : null }];
  }));
}

export function eventTimeLabel(time) {
  if (!time) return '';
  return `${fmtClock(time.lab)} lab · ${time.session == null ? 'unknown' : fmtClock(time.session)} session`;
}

export function fmtClock(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return (h ? `${h}:` : '') + `${mm}:${String(sec).padStart(2, '0')}`;
}

export class Replay {
  // hooks: deliver(e) live event; apply(e) history event (no animation);
  // skipped(seconds, e) a pause before e was cut short; reset() clear the page;
  // settle() redraw after a seek; progress(); ended()
  constructor(rec, hooks) {
    this.rec = rec;
    this.meta = rec.meta || {};
    this.hooks = hooks;
    this.events = [...(rec.events || [])].sort((a, b) => a.id - b.id);
    this.frames = [...(rec.frames || [])].sort((a, b) => a.id - b.id);
    this.runs = rec.runs || {};
    this.n = this.events.length;
    this.pos = 0;            // index of the next event to deliver
    this.now = 0;            // the clock, in compressed run seconds (see `t`)
    this.paused = true;
    this.speed = 4;
    this.axis = 'lab';
    this.stepMode = false;   // stop before every step
    this._once = false;      // stop before the next step, once
    this._resume = -1;       // the step start playback resumed from (not stopped at again)
    this._last = 0;          // performance.now() of the last tick
    this._lastSave = 0;
    this.key = `qumus.replay:${this.meta.recorded_at || ''}:${this.n}`;
    this.at = new Map(this.events.map((e, i) => [e.id, i]));
    this.steps = this.buildSteps();
    this.buildTimeline();
    this.chapters = this.buildChapters();
  }

  // ---------------------------------------------------------------- timeline
  // the gap before event i on each axis: [wall seconds, lab seconds]
  gaps(i) {
    return eventGap(this.events[i - 1], this.events[i]);
  }

  buildTimeline() {
    const ev = this.events, lab = this.axis === 'lab';
    const known = ev.map(e => e.wall).filter(w => Number.isFinite(w));
    this.wall0 = known.length ? known[0] : 0;
    this.t = new Float64Array(this.n);     // compressed seconds when event i is shown
    this.real = new Float64Array(this.n);  // seconds into the run on this axis
    this.skip = new Float64Array(this.n);  // seconds of the pause before event i that are skipped
    this.ff = new Float64Array(this.n);    // seconds of lab wait before event i that are fast-forwarded
    let t = 0, real = 0, lastWall = this.wall0, skipped = 0, labSeconds = 0;
    for (let i = 0; i < this.n; i++) {
      const w = Number.isFinite(ev[i].wall) ? ev[i].wall : lastWall;
      if (i) {
        const [wallGap, labGap, labWait] = this.gaps(i);
        let gap = lab ? labGap : wallGap;
        labSeconds += labGap;
        real += gap;
        // Equal finite timestamps are observed zero elapsed time. Only a
        // missing/invalid pair needs pacing; it never becomes a clock reading.
        if (!observedClockGap(ev[i - 1], ev[i]).known) gap = NO_TIMING_GAP_S;
        if (gap > MAX_GAP_S) {
          if (lab && labWait) this.ff[i] = gap - MAX_GAP_S;
          else { this.skip[i] = gap - MAX_GAP_S; skipped += this.skip[i]; }
          gap = MAX_GAP_S;
        }
        t += gap;
      }
      this.t[i] = t;
      this.real[i] = lab ? real : w - this.wall0;
      lastWall = w;
    }
    this.duration = lab ? real : (known.length ? known[known.length - 1] - this.wall0 : 0);
    this.labSeconds = labSeconds;  // the whole run in lab time (either axis)
    this.total = t;                // compressed length
    this.skipped = skipped;        // seconds of pauses cut out altogether
  }

  // Every lab action (a flow step): where it starts and ends in the event list.
  buildSteps() {
    const out = [], open = new Map(), pos = new Map();
    for (const [id, r] of Object.entries(this.runs)) {
      const seqs = (r.steps || []).map(st => st.seq).sort((a, b) => a - b);
      seqs.forEach((q, k) => pos.set(`${id}|${q}`, [k + 1, seqs.length]));
    }
    this.events.forEach((e, i) => {
      const p = e.payload || {};
      if (e.kind === 'step.started') {
        const key = `${e.run_id}|${p.seq}`;
        const st = { key, run: e.run_id, seq: p.seq, name: p.name || '', i, end: -1, ts0: e.ts, ts1: e.ts,
          flow: (this.runs[e.run_id] || {}).flow || '', pos: pos.get(key) || [p.seq + 1, 0] };
        open.set(key, st);
        out.push(st);
      } else if (e.kind === 'step.finished') {
        const st = open.get(`${e.run_id}|${p.seq}`);
        if (st) { st.end = i; st.ts1 = e.ts; open.delete(st.key); }
      }
    });
    this.stepAt = new Map(out.map(st => [st.key, st]));
    return out;
  }

  // index of the last event shown at compressed time c (-1 before the first)
  indexAt(c) {
    let lo = 0, hi = this.n - 1, best = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (this.t[mid] <= c) { best = mid; lo = mid + 1; } else hi = mid - 1; }
    return best;
  }

  // original run seconds at compressed time c (the clock ticks between events)
  realAt(c) {
    const i = this.indexAt(c);
    if (i < 0) return 0;
    if (i + 1 >= this.n || this.real[i + 1] === this.real[i]) return Math.min(this.real[i], this.duration);
    let r = this.real[i] + (c - this.t[i]);
    if (i + 1 < this.n && this.ff[i + 1] > 0) {  // a fast-forwarded wait: the clock runs fast through it
      r = this.real[i] + (c - this.t[i]) * (this.real[i + 1] - this.real[i]) / Math.max(1e-9, this.t[i + 1] - this.t[i]);
    }
    if (i + 1 < this.n) r = Math.min(r, this.real[i + 1]);
    return Math.min(r, this.duration);
  }

  // The lab wait being fast-forwarded at compressed time c: { seconds } or null.
  ffAt(c) {
    const i = this.indexAt(c);
    if (i < 0 || i + 1 >= this.n || !(this.ff[i + 1] > 0)) return null;
    return { seconds: this.real[i + 1] - this.real[i] };
  }

  // The lab frame that event e (a device command) leads to, and how many
  // compressed seconds after e it is shown: the move's own duration. Other
  // threads write into the run store while a device moves (the model's tool calls,
  // another job's submit), so the frame need not be the very next event: it is
  // the first frame of the same device and command before that device's next
  // command (none = the command changed nothing).
  frameAfter(e) {
    const i = this.at.get(e.id), p = e.payload || {};
    if (i == null) return null;
    for (let j = i + 1; j < this.n && j <= i + FRAME_LOOKAHEAD; j++) {
      const f = this.events[j], q = f.payload || {};
      if (q.device !== p.device) continue;
      if (f.kind === 'lab.frame' && q.command === p.command) return { event: f, seconds: this.t[j] - this.t[i] };
      if (f.kind === 'device.command') return null;
    }
    return null;
  }

  setAxis(axis) {
    if (axis === this.axis || (axis !== 'lab' && axis !== 'session')) return;
    this.axis = axis;
    this.buildTimeline();
    this.chapters = this.buildChapters();
    // stay between the last shown event and the next one: nothing is delivered twice
    this.now = this.pos >= this.n ? this.total : this.pos > 0 ? this.t[this.pos - 1] : 0;
    this._last = performance.now();
    this.save();
    this.hooks.rebuilt?.();
    this.hooks.progress();
  }

  buildChapters() {
    const out = [];
    this.events.forEach((e, i) => {
      const p = e.payload || {};
      let kind = null, text = null;
      if (this.skip[i] >= 5) out.push({ i, kind: 'skip', text: `${Math.round(this.skip[i])} s skipped (nothing happened on the bench)`, frac: this.total ? this.t[i] / this.total : 0 });
      if (e.kind === 'user.message') { kind = 'user'; text = `You: ${p.text || ''}`; }
      else if (e.kind === 'agent.delegate') { kind = p.to || 'agent'; text = `${cap(e.agent || 'orchestrator')} → ${cap(p.to)}: ${p.task || ''}`; }
      else if (e.kind === 'fault.injected') { kind = 'fault'; text = `Fault injected: ${(p.fault || '').replace(/_/g, ' ')}`; }
      else if (e.kind === 'step.corrected') { kind = 'fix'; text = `Auto-correct: ${(p.name || '').replace(/_/g, ' ')} · ${p.action || ''}`; }
      else if (e.kind === 'tool.denied') { kind = 'fault'; text = `Blocked: ${p.tool} · ${p.reason || ''}`; }
      else if (e.kind === 'system.stopped') { kind = 'fault'; text = 'Stop pressed'; }
      else if (e.kind === 'system.continued') { kind = 'fix'; text = 'Continue pressed'; }
      else if (e.kind === 'agent.error') { kind = 'fault'; text = `Model error: ${p.error || p.type || ''}`; }
      else if (e.kind === 'job.status' && (p.status === 'parked' || p.status === 'failed' || p.status === 'waiting_human')) {
        kind = 'fault'; text = `Job ${p.status.replace('_', ' ')}: ${p.message || ''}`;
      }
      if (kind) out.push({ i, kind, text: text.length > 140 ? text.slice(0, 140) + '…' : text, frac: this.total ? this.t[i] / this.total : 0 });
    });
    return out;
  }

  // ------------------------------------------------------------- data views
  frameAt(lastId) {
    const f = this.frames;
    let lo = 0, hi = f.length - 1, best = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (f[mid].id <= lastId) { best = mid; lo = mid + 1; } else hi = mid - 1; }
    return best >= 0 ? f[best] : (f[0] || null);
  }

  // what GET /api/bench would have answered after event `lastId`
  bench(lastId) {
    const fr = this.frameAt(lastId);
    const b = fr ? fr.bench : {};
    return {
      layout: this.rec.layout || {},
      devices: clone(b.devices || []), objects: clone(b.objects || []), stamp: clone(b.stamp || null),
      twin: clone(b.twin || null),  // recordings made since lab frames exist
      faults: clone(b.faults || { armed: {} }), busy: !!b.busy && fr.id <= lastId,  // nobody works before the first event
      sim: { speed: this.speed, mode: 'replay' },
    };
  }

  // what GET /api/runs/{id} would have answered after event `lastId`
  run(id, lastId) {
    const r = this.runs[id];
    if (!r) return null;
    const started = new Set(), finished = new Set();
    let status = 'queued';
    for (const e of this.events) {
      if (e.id > lastId) break;
      if (e.run_id !== id) continue;
      if (e.kind === 'step.started') started.add(e.payload.seq);
      else if (e.kind === 'step.finished') finished.add(e.payload.seq);
      else if (e.kind === 'job.status') status = e.payload.status;
    }
    const steps = (r.steps || []).filter(st => started.has(st.seq)).map(st => finished.has(st.seq) ? st
      : { ...st, status: 'in_progress', measurements: null, finished_at: null });
    const done = TERMINAL.has(status);
    return { ...r, status, steps, result: done ? r.result : null, error: done ? r.error : null };
  }

  // ---------------------------------------------------------------- playback
  get lastShown() { return this.pos > 0 ? this.events[this.pos - 1] : null; }
  get realNow() { return this.ended ? this.duration : this.realAt(this.now); }
  get fraction() { return this.total ? Math.min(1, this.now / this.total) : (this.ended && this.n ? 1 : 0); }
  get ended() { return this.pos >= this.n; }

  play() {
    if (this.ended) this.seekIndex(0);
    this.paused = false;
    this._resume = this.pos;
    this._last = performance.now();
    this.hooks.progress();
  }
  pause() { this.paused = true; this.hooks.progress(); this.save(); }
  toggle() { if (this.paused) this.play(); else this.pause(); }
  setSpeed(s) { this.speed = s; this.save(); this.hooks.progress(); }
  setStepMode(on) { this.stepMode = !!on; this.save(); this.hooks.progress(); }

  // Play to the start of the next lab action and stop there.
  stepNext() {
    if (this.ended) return;
    this._once = true;
    if (this.paused) this.play();
  }
  // Go back to just before the last lab action that started, paused.
  stepPrev() {
    let k = -1;
    for (const st of this.steps) { if (st.i < this.pos) k = st.i; else break; }
    this.paused = true;
    this._once = false;
    this.seekIndex(Math.max(0, k));
  }

  seekFraction(f) {
    if (f >= 0.995) { this.seekIndex(this.n); return; }  // the right edge means the end, not just before it
    const c = Math.max(0, Math.min(1, f)) * this.total;
    this.seekIndex(this.indexAt(c) + 1, c);
  }

  // Show everything before event `i` at once, then continue from there
  // (at compressed time `c`, by default the moment event i is due).
  seekIndex(i, c) {
    i = Math.max(0, Math.min(this.n, i | 0));
    this.hooks.reset();
    for (let k = 0; k < i; k++) {
      if (this.skip[k] > 0) this.hooks.skipped(this.skip[k], this.events[k]);
      this.hooks.apply(this.events[k]);
    }
    this.pos = i;
    this.now = c != null ? c : (i < this.n ? this.t[i] : this.total);
    if (this.ended) this.paused = true;  // seeking to the end while playing: show Play, not Pause
    this._last = performance.now();
    this.hooks.settle();
    this.hooks.progress();
    this.save();
  }

  // One tick of the clock: deliver every event that is due by now.
  tick(nowMs) {
    if (this.paused || this.ended) return;
    const dt = Math.min(MAX_TICK_S, Math.max(0, (nowMs - this._last) / 1000));
    this._last = nowMs;
    this.now = Math.min(this.total, this.now + dt * this.speed);
    let n = 0;
    while (this.pos < this.n && this.t[this.pos] <= this.now) {
      if ((this.stepMode || this._once) && this.pos !== this._resume && this.events[this.pos].kind === 'step.started') {
        this.now = this.t[this.pos];  // stop just before the next lab action
        this.paused = true;
        this._once = false;
        this.save();
        break;
      }
      const i = this.pos++;
      if (this.skip[i] > 0) this.hooks.skipped(this.skip[i], this.events[i]);
      this.hooks.deliver(this.events[i]);
      if (++n >= 3000) break;  // an enormous burst: let the page paint, go on next tick
    }
    this.hooks.progress();
    if (nowMs - this._lastSave > 700) { this.save(); this._lastSave = nowMs; }
    if (this.ended) { this.paused = true; this._once = false; this.save(); this.hooks.ended(); this.hooks.progress(); }
  }

  start() {
    const loop = now => { this.tick(now); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }

  // ------------------------------------------------- survive a page reload
  save() {
    try {
      sessionStorage.setItem(this.key, JSON.stringify({ pos: this.pos, now: this.now, paused: this.paused, speed: this.speed,
        axis: this.axis, stepMode: this.stepMode }));
    } catch { /* private window */ }
  }
  restore() {
    try { const v = JSON.parse(sessionStorage.getItem(this.key) || 'null'); return v && typeof v.pos === 'number' ? v : null; } catch { return null; }
  }
}

function cap(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }
function clone(v) { return v == null ? v : JSON.parse(JSON.stringify(v)); }
