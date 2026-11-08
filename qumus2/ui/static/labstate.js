// What the bench drawing shows, and how it gets from one picture to the next.
//
// `target` is the lab as the last applied frame says; `view` is what is drawn.
// A `lab.frame` (runtime/twin.py) changes the target; the page then eases the
// view to it over a short time that grows with the distance moved, so a fast
// burst of commands plays at a pace a person can follow.
//
// Recordings made before lab frames existed only have device commands; for
// them `legacyDelta` works out the same kind of frame from each command.

import { armBase, headPoint, exfoliatorOrigin } from './stations.js';

const NUM = { arms: ['x', 'y', 'z', 'rotation'], stages: ['x', 'y', 'z'], objects: ['x', 'y', 'rotation'] };

export const emptyLab = () => ({ arms: {}, stages: {}, manipulator: null, exfoliator: null, microscope: null, objects: {} });
const clone = v => (v == null ? v : JSON.parse(JSON.stringify(v)));

export function applyDelta(state, d) {
  for (const key of ['arms', 'stages', 'objects']) {
    if (!d[key]) continue;
    state[key] = state[key] || {};
    for (const [id, v] of Object.entries(d[key])) state[key][id] = { ...(state[key][id] || {}), ...v };
  }
  for (const id of d.gone || []) delete state.objects[id];
  for (const key of ['manipulator', 'exfoliator', 'microscope', 'reactor_microscope']) if (d[key]) state[key] = { ...(state[key] || {}), ...d[key] };
  return state;
}

// From GET /api/bench (or a recorded bench frame): the twin when there is one,
// else the older device list + world objects.
export function fromBench(b, layout) {
  if (b.twin) return clone(b.twin);
  const s = emptyLab();
  for (const d of b.devices || []) {
    if (d.kind === 'Arm' && d.pose) {
      const base = armBase(layout, d.id);
      s.arms[d.id] = { x: d.pose.x + base.x, y: d.pose.y + base.y, z: d.pose.z, rotation: d.pose.rotation || 0,
        vacuum: !!d.vacuum, homed: d.homed !== false };
    } else if (d.kind === 'Stage' && d.pose) s.stages[d.id] = { x: d.pose.x, y: d.pose.y, z: d.pose.z || 0 };
    else if (d.id === 'exfoliator' && d.positions) {
      const gx = d.positions.gantry_x || 0;
      s.exfoliator = { gantry_x: gx, gantry_z: d.positions.gantry_z || 0, roller_x: exfoliatorOrigin(layout).x + gx,
        applied: false, roll_left_mm: d.roll_left_mm };
    } else if (d.id === 'microscope') s.microscope = { objective: d.objective };
  }
  const m = b.stamp && b.stamp.manipulator;
  if (m) s.manipulator = { ...m, head: headPoint(layout, m), vacuum: !!b.stamp.held };
  for (const o of b.objects || []) s.objects[o.id] = { ...o };
  return s;
}

function lerp(a, b, t) { return a + (b - a) * t; }
const ease = t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

export class LabState {
  constructor(layout) {
    this.layout = layout;
    this.target = emptyLab();
    this.view = emptyLab();
    this.tween = null;
  }

  setAll(state) {
    this.target = clone(state);
    this.view = clone(state);
    this.tween = null;
  }

  // How long a delta takes to show (ms at 1x): longer moves take longer.
  duration(d) {
    let dist = 0, stage = 0;
    for (const [id, a] of Object.entries(d.arms || {})) {
      const f = this.target.arms[id];
      if (f && a.x != null) dist = Math.max(dist, Math.hypot(a.x - f.x, a.y - f.y, ((a.z ?? f.z) - f.z) * 0.6));
    }
    for (const [id, st] of Object.entries(d.stages || {})) {
      const f = this.target.stages[id];
      if (f && st.x != null) stage = Math.max(stage, Math.hypot(st.x - f.x, st.y - f.y));
    }
    if (d.manipulator && this.target.manipulator) {
      const a = d.manipulator.head, f = this.target.manipulator.head;
      if (a && f) dist = Math.max(dist, Math.hypot(a.x - f.x, a.y - f.y));
      if (d.manipulator.z != null && this.target.manipulator.z != null) stage = Math.max(stage, Math.abs(d.manipulator.z - this.target.manipulator.z) * 4);
    }
    if (d.exfoliator && this.target.exfoliator) {
      const a = d.exfoliator, f = this.target.exfoliator;
      dist = Math.max(dist, Math.hypot((a.roller_x ?? f.roller_x) - f.roller_x, ((a.gantry_z ?? f.gantry_z) - f.gantry_z)));
    }
    if (!dist && !stage) return 0;
    return Math.max(140, Math.min(800, 110 + dist * 3.2 + stage * 16));
  }

  // Apply a frame. Returns the ms the view needs to catch up (0 = instant).
  // A move still under way is never cut short (it used to snap to its end when
  // any other frame came in, e.g. while the model's tool call landed between a
  // command and its frame): a frame that moves nothing joins it as it goes; one
  // that moves something takes over from where the view is drawn now.
  push(d, ms) {
    const tw = this.tween, now = performance.now(), left = tw ? tw.t0 + tw.ms - now : 0;
    if (left > 0) {
      if (!this.duration(d)) {
        applyDelta(this.target, d);
        applyDelta(tw.to, clone(d));
        return left;
      }
      this.step(now);
      const from = clone(this.view);
      applyDelta(this.target, d);
      this.tween = { from, to: clone(this.target), t0: now, ms: Math.max(ms || 0, left) };
      return this.tween.ms;
    }
    this.settle();
    const from = clone(this.view);
    applyDelta(this.target, d);
    if (!ms) { this.view = clone(this.target); this.tween = null; return 0; }
    this.tween = { from, to: clone(this.target), t0: performance.now(), ms };
    return ms;
  }

  // Apply a frame at once (history, seeking): no copy of the whole lab per frame.
  applyNow(d) {
    this.settle();
    applyDelta(this.target, d);
    applyDelta(this.view, clone(d));
  }

  settle() { if (this.tween) { this.view = clone(this.tween.to); this.tween = null; } }

  // Advance the view; true while still moving.
  step(now) {
    const tw = this.tween;
    if (!tw) return false;
    const t = Math.min(1, (now - tw.t0) / tw.ms), q = ease(t);
    const v = clone(tw.to);
    for (const key of ['arms', 'stages', 'objects']) {
      for (const [id, b] of Object.entries(v[key] || {})) {
        const a = (tw.from[key] || {})[id];
        if (!a) continue;
        for (const f of NUM[key]) if (typeof a[f] === 'number' && typeof b[f] === 'number') b[f] = lerp(a[f], b[f], q);
      }
    }
    const m0 = tw.from.manipulator, m1 = v.manipulator;
    if (m0 && m1) {
      for (const f of ['l', 'x', 'y', 'z']) if (typeof m0[f] === 'number' && typeof m1[f] === 'number') m1[f] = lerp(m0[f], m1[f], q);
      if (m0.head && m1.head) m1.head = { x: lerp(m0.head.x, m1.head.x, q), y: lerp(m0.head.y, m1.head.y, q) };
    }
    const e0 = tw.from.exfoliator, e1 = v.exfoliator;
    if (e0 && e1) {
      for (const f of ['gantry_x', 'gantry_z', 'roller_x']) if (typeof e0[f] === 'number' && typeof e1[f] === 'number') e1[f] = lerp(e0[f], e1[f], q);
      // the drawing places the roll and the tape from these (twin `rolls`, `tape`), so they ease too
      const pairs = [[(e0.rolls || {}).movable, (e1.rolls || {}).movable, ['x', 'z']], [(e0.rolls || {}).fixed, (e1.rolls || {}).fixed, ['x', 'z']],
        [e0.tape, e1.tape, ['x0', 'z0', 'x1', 'z1']]];
      for (const [a, b, fs] of pairs) if (a && b) for (const f of fs) if (typeof a[f] === 'number' && typeof b[f] === 'number') b[f] = lerp(a[f], b[f], q);
    }
    this.view = v;
    if (t >= 1) { this.tween = null; this.view = clone(tw.to); }
    return t < 1;
  }

  // ---------------------------------------------------------- old recordings
  // A device command of a recording without lab frames -> the frame it implies.
  legacyDelta(p) {
    const L = this.layout, s = this.target, d = {};
    const arm = s.arms[p.device];
    if (arm) {
      if (p.command === 'move_to' && p.params?.pose) {
        const b = armBase(L, p.device), q = p.params.pose;
        const to = { x: q.x + b.x, y: q.y + b.y, z: q.z, rotation: q.rotation || 0 };
        d.arms = { [p.device]: to };
        d.objects = {};
        for (const [id, o] of Object.entries(s.objects)) if (o.held_by === p.device) d.objects[id] = { x: to.x, y: to.y };
      } else if (p.command === 'home') {
        const hm = (L[p.device] || {}).home, b = armBase(L, p.device);
        if (hm) d.arms = { [p.device]: { x: hm.x + b.x, y: hm.y + b.y, z: hm.z, rotation: hm.rotation || 0, homed: true } };
      } else if (p.command === 'set_vacuum') {
        d.arms = { [p.device]: { vacuum: !!p.params?.on } };
        d.objects = {};
        if (p.params?.on) {
          let best = null, bd = Infinity;
          for (const [id, o] of Object.entries(s.objects)) {
            const dd = Math.hypot(o.x - arm.x, o.y - arm.y), reach = Math.max(4, ((o.size_mm || [10])[0]) / 2);
            if (!o.held_by && dd < reach && dd < bd) { best = id; bd = dd; }
          }
          if (best) d.objects[best] = { held_by: p.device, on: null };
        } else {
          for (const [id, o] of Object.entries(s.objects)) if (o.held_by === p.device) d.objects[id] = { held_by: null };
          this.lastReleased = Object.keys(d.objects)[0] || null;
        }
      }
      return d;
    }
    if (p.device === 'transfer_stage' && p.command === 'move_to' && p.params?.pose) {
      const f = s.stages.transfer_stage || { x: 0, y: 0 }, q = p.params.pose;
      d.stages = { transfer_stage: { x: q.x ?? 0, y: q.y ?? 0, z: q.z ?? 0 } };
      d.objects = {};
      for (const [id, o] of Object.entries(s.objects)) {
        if (o.held_by || o.on !== 'transfer_stage') continue;
        d.objects[id] = { x: o.x + (q.x ?? 0) - f.x, y: o.y + (q.y ?? 0) - f.y };
      }
      return d;
    }
    if (p.device === 'transfer_arm' && p.command === 'move_axis_to' && s.manipulator) {
      const m = { ...s.manipulator, [p.params.axis]: p.params.value };
      m.head = headPoint(L, m);
      d.manipulator = m;
      d.objects = {};
      for (const [id, o] of Object.entries(s.objects)) if (o.held_by === 'transfer_arm') d.objects[id] = { x: m.head.x, y: m.head.y };
      return d;
    }
    if (p.device === 'exfoliator' && s.exfoliator && (p.command === 'apply' || p.command === 'peel')) {
      const o = exfoliatorOrigin(L), path = (L.exfoliator || {}).apply_path || [[-60, -75]];
      const [gx, gz] = p.command === 'apply' ? path[path.length - 1] : [0, 0];
      d.exfoliator = { gantry_x: gx, gantry_z: gz, roller_x: o.x + gx, applied: p.command === 'apply' };
      return d;
    }
    return null;
  }
}
