import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { Replay, eventGap, eventTimes, eventTimeLabel } from '../qumus2/ui/static/replay.js';
import { moments } from '../qumus2/ui/static/history.js';

test('fast simulated instruments retain their lab time beside wall time', () => {
  const events = [
    { id: 1, ts: 100, wall: 1000, kind: 'job.submitted', payload: { flow: 'reactor_reaction' } },
    { id: 2, ts: 820, wall: 1000.2, kind: 'reactor.observation', run_id: 'rx', payload: { chip_id: 'c' } },
    { id: 3, ts: 821, wall: 1000.3, kind: 'sample.updated', payload: { id: 'p', kind: 'flake', status: 'reactor_product' } },
    { id: 4, ts: 841, wall: 1000.4, kind: 'reactor.process', payload: { chip_id: 'c', disposition: 'cold_storage' } },
  ];
  const times = eventTimes(events), replay = new Replay({ events }, {});
  assert.match(eventTimeLabel(times.get(2)), /^12:00 lab · 0:00 session$/);
  assert.deepEqual([...times.values()].map(t => t.lab), [...replay.real]);
  const nav = moments(events);
  assert.equal(nav.length, 4);
  assert.match(nav[2].text, /Single reactor product accepted: p/);
  assert.equal(nav[2].i, 2);
  assert.match(nav[3].text, /cold_storage/);
});

test('separate process epochs do not invent elapsed months', () => {
  const events = [{ id: 1, wall: 10, ts: 100 }, { id: 2, wall: 12, ts: 1793100000 },
    { id: 3, wall: 13, ts: 110 }];
  assert.equal(eventTimes(events).get(3).lab, 3);
});

test('historical reactor recording gains useful navigation without rewriting data', () => {
  const rec = JSON.parse(readFileSync(new URL('../recordings/experimental-reactor-pair-normal-20261027-v3/recording.json', import.meta.url)));
  const original = JSON.stringify(rec);
  const nav = moments(rec.events);
  assert.ok(nav.some(m => m.text.includes('Single reactor product accepted')));
  assert.ok(nav.some(m => m.text.includes('Two-flake reaction started')));
  assert.ok(nav.some(m => !m.when.startsWith('0:00 lab')));
  assert.equal(JSON.stringify(rec), original);
});

test('same finite timestamps are observed zero time and receive no fallback pacing', () => {
  const events = [
    {id: 1, ts: 100, kind: 'step.started', run_id: 'source', payload: {seq: 0, name: 'search'}},
    {id: 2, ts: 100, kind: 'tool.call', payload: {}},
    {id: 3, ts: 110, kind: 'lab.frame', payload: {}},
    {id: 4, ts: 110, kind: 'step.finished', run_id: 'source', payload: {seq: 0}},
  ];
  const replay = new Replay({events}, {}), times = eventTimes(events);
  assert.deepEqual(eventGap(events[0], events[1]), [0, 0, false]);
  assert.deepEqual([...times.values()].map(value => value.lab), [0, 0, 10, 10]);
  assert.ok([...times.values()].every(value => value.session === null));
  assert.equal(eventTimeLabel(times.get(4)), '0:10 lab · unknown session');
  assert.deepEqual([...replay.real], [0, 0, 10, 10]);
  assert.deepEqual([...replay.t], [0, 0, 5, 5]);
  assert.equal(replay.duration, 10);
  assert.equal(replay.labSeconds, 10);
  assert.equal(replay.realAt(2.5), 5, 'actual lab wait still interpolates');
  assert.equal(replay.realAt(replay.total + 100), 10, 'the clock cannot advance past the final observation');
  assert.equal(replay.indexAt(0), 1);
  assert.equal(replay.indexAt(2.5), 1);
});

test('unknown clocks preserve event pacing and unknown session labels without measured duration', () => {
  const events = Array.from({length: 5}, (_, index) => ({id: index + 1, kind: 'notification', payload: {}}));
  const replay = new Replay({events}, {});
  assert.deepEqual([...replay.t], [0, .25, .5, .75, 1]);
  assert.deepEqual([...replay.real], [0, 0, 0, 0, 0]);
  assert.equal(replay.duration, 0);
  assert.equal(replay.labSeconds, 0);
  assert.equal(replay.realAt(.625), 0);
  assert.ok([...eventTimes(events).values()].every(value => value.lab === 0 && value.session === null));
});

test('known wall timelines retain their existing combined-clock and zero-gap semantics', () => {
  const events = [
    {id: 1, ts: 100, wall: 1000}, {id: 2, ts: 100, wall: 1000},
    {id: 3, ts: 102, wall: 1004}, {id: 4, ts: 112, wall: 1004},
  ];
  const replay = new Replay({events}, {});
  assert.deepEqual([...replay.t], [0, 0, 4, 9]);
  assert.deepEqual([...replay.real], [0, 0, 4, 14]);
  assert.equal(replay.duration, 14);
  assert.equal(replay.labSeconds, 14);
  assert.equal(replay.realAt(2), 2);
  assert.equal(replay.realAt(6.5), 9);
  replay.axis = 'session'; replay.buildTimeline();
  assert.deepEqual([...replay.t], [0, 0, 4, 4]);
  assert.deepEqual([...replay.real], [0, 0, 4, 4]);
  assert.equal(replay.duration, 4);
});

test('partial clock coverage never uses pacing as a laboratory observation', () => {
  const events = [{id: 1, ts: 100}, {id: 2, ts: 100, wall: 1000},
    {id: 3, ts: 100, wall: 1003}, {id: 4, ts: 102}];
  const replay = new Replay({events}, {});
  assert.deepEqual([...replay.real], [0, 0, 3, 5]);
  assert.equal(replay.duration, 5);
  assert.deepEqual([...eventTimes(events).values()].map(value => value.session), [null, 0, 3, null]);
  assert.equal(replay.realAt(.125), .125);
});

test('seek and step replay use observed boundaries without adding same-time padding', () => {
  const events = [
    {id: 1, ts: 100, kind: 'step.started', run_id: 'r', payload: {seq: 0}},
    {id: 2, ts: 100, kind: 'step.finished', run_id: 'r', payload: {seq: 0}},
    {id: 3, ts: 110, kind: 'step.started', run_id: 'r', payload: {seq: 1}},
    {id: 4, ts: 110, kind: 'step.finished', run_id: 'r', payload: {seq: 1}},
  ];
  const applied = [], delivered = [];
  const replay = new Replay({events}, {reset: () => {applied.length = 0;},
    apply: event => applied.push(event.id), deliver: event => delivered.push(event.id),
    skipped() {}, settle() {}, progress() {}, ended() {}});
  replay.seekFraction(.5);
  assert.deepEqual(applied, [1, 2]);
  assert.equal(replay.realNow, 5);
  replay.seekIndex(0);
  replay.speed = 1;
  replay.stepNext();
  replay.tick(replay._last + 250);
  assert.deepEqual(delivered, [1, 2]);
  assert.equal(replay.realNow, .5);
  for (let index = 0; index < 24 && !replay.paused; index++) replay.tick(replay._last + 250);
  assert.equal(replay.pos, 2);
  assert.equal(replay.paused, true);
  assert.equal(replay.realNow, 10);
  replay.seekIndex(replay.n);
  assert.equal(replay.realNow, 10);
  assert.deepEqual(applied, [1, 2, 3, 4]);
});

test('fallback pacing is reserved for missing, nonfinite or epoch-invalid clock pairs', () => {
  const events = [{id: 1, ts: 100}, {id: 2, ts: 100}, {id: 3, ts: NaN},
    {id: 4, ts: 100}, {id: 5, ts: 3701}, {id: 6, ts: 3701},
    {id: 7, ts: 3701, wall: 1000}, {id: 8, ts: 3701, wall: 1000}];
  const replay = new Replay({events}, {});
  assert.deepEqual([...replay.t], [0, 0, .25, .5, .75, .75, .75, .75]);
  assert.ok([...replay.real].every(value => value === 0));
  assert.equal(replay.duration, 0);
  assert.equal(replay.realAt(.125), 0);
  assert.equal(replay.realAt(100), 0);
});

test('nonfinite wall-clock metadata stays unknown instead of poisoning either clock', () => {
  const events = [{id: 1, wall: NaN}, {id: 2, wall: Infinity}, {id: 3, wall: -Infinity}];
  const replay = new Replay({events}, {});
  assert.deepEqual([...replay.t], [0, .25, .5]);
  assert.deepEqual([...eventTimes(events).values()], [0, 1, 2].map(() => ({lab: 0, session: null})));
  assert.equal(replay.duration, 0);
  replay.axis = 'session'; replay.buildTimeline();
  assert.deepEqual([...replay.real], [0, 0, 0]);
  assert.equal(replay.duration, 0);
});

test('dense equal-time batches, explicit seeks and step boundaries deliver no duplicate or missing events', () => {
  const events = Array.from({length: 5005}, (_, index) => ({id: index + 1, ts: 100,
    kind: 'notification', payload: {}}));
  events.push({id: 5006, ts: 101, kind: 'notification', payload: {}});
  const applied = [], delivered = [];
  const replay = new Replay({events}, {reset: () => {applied.length = 0;},
    apply: event => applied.push(event.id), deliver: event => delivered.push(event.id),
    skipped() {}, settle() {}, progress() {}, ended() {}});
  replay.play();
  replay._last = 0;  // Exact test epoch; performance.now() fractions can round a 250 ms subtraction down.
  replay.tick(250);
  assert.equal(delivered.length, 3000, 'existing bounded scheduler yields between dense batches');
  replay.tick(500);
  assert.deepEqual(delivered, events.map(event => event.id));
  replay.seekIndex(3000); delivered.length = 0;
  assert.deepEqual(applied, events.slice(0, 3000).map(event => event.id));
  replay.play(); replay._last = 0; replay.tick(250);
  assert.equal(replay.now, 1);
  assert.deepEqual(delivered, events.slice(3000).map(event => event.id));

  // The original failure was correctly waiting just below t=1, not losing the event.
  replay.seekIndex(3000); delivered.length = 0;
  replay.play(); replay._last = 6.001; replay.tick(256.001);
  assert.equal(replay.now, .9999999999999999);
  assert.deepEqual(delivered, events.slice(3000, -1).map(event => event.id));
  replay.tick(replay._last + 16);
  assert.deepEqual(delivered, events.slice(3000).map(event => event.id));

  const steps = [0, 1].flatMap(seq => [
    {id: seq * 2 + 1, ts: 100, kind: 'step.started', run_id: 'r', payload: {seq}},
    {id: seq * 2 + 2, ts: 100, kind: 'step.finished', run_id: 'r', payload: {seq}},
  ]);
  delivered.length = 0;
  const stepped = new Replay({events: steps}, {deliver: event => delivered.push(event.id), progress() {}, ended() {}});
  stepped.stepNext(); stepped._last = 0; stepped.tick(250);
  assert.deepEqual(delivered, [1, 2]);
  assert.equal(stepped.paused, true);
  stepped.stepNext(); stepped._last = 0; stepped.tick(250);
  assert.deepEqual(delivered, [1, 2, 3, 4]);
  assert.equal(stepped.realNow, 0);
});
