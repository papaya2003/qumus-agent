import { pathToFileURL } from 'node:url';
// Offline event regressions; no browser, network, model calls or hardware.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { ToolAttempts } from '../qumus2/ui/static/toolattempts.js';
import { Session } from '../qumus2/ui/static/session.js';
import { Results } from '../qumus2/ui/static/results.js';
import { requestSeries, requestEmptyText } from '../qumus2/ui/static/requestchart.js';

import { LabState, fromBench } from '../qumus2/ui/static/labstate.js';
import { Replay } from '../qumus2/ui/static/replay.js';
// qumus-v2 recordings: here, or a qumus-v2 checkout's recordings/ (tests/conftest.py v2_recording)
const RECORDINGS = process.env.QUMUS_RECORDINGS_DIR ? pathToFileURL(process.env.QUMUS_RECORDINGS_DIR.replace(/\/?$/, '/')) : new URL('../recordings/', import.meta.url);

const recording = JSON.parse(readFileSync(new URL('openai-device-approval-20261025/recording.json', RECORDINGS)));
const collaboration = JSON.parse(readFileSync(new URL('openai-collaboration-20261025/recording.json', RECORDINGS)));
const legacyReactor = JSON.parse(readFileSync(new URL('archive/experimental-reactor-mechanics-20261026/recording.json', RECORDINGS)));
const spacedReactor = JSON.parse(readFileSync(new URL('archive/experimental-reactor-equal-spacing-20261026/recording.json', RECORDINGS)));
const clearReactor = JSON.parse(readFileSync(new URL('archive/experimental-reactor-clearance-20261026/recording.json', RECORDINGS)));
const pairReactor = JSON.parse(readFileSync(new URL('experimental-reactor-pair-normal-20261027-v3/recording.json', RECORDINGS)));

for (const [label, reactor] of [['original', legacyReactor], ['equal spacing', spacedReactor], ['assembly clearance', clearReactor], ['two-flake pair', pairReactor]]) {
test(`${label}: recorded insertion and retraction carry the chip through intermediate positions`, () => {
  const replay = new Replay(reactor, {});
  const lab = new LabState(reactor.layout);
  lab.setAll(fromBench(reactor.frames[0].bench, reactor.layout));
  let checked = 0;
  for (const e of reactor.events) {
    if (e.kind === 'device.command' && e.payload.device === 'reactor_insert') {
      const frame = replay.frameAfter(e);
      assert.ok(frame.seconds >= 4.9, 'insertion duration must survive timeline compression');
      const start = lab.view.stages.reactor_insert.y;
      const chipY = lab.view.objects.reactor_chip_01.y;
      const target = frame.event.payload.stages.reactor_insert.y;
      lab.push(frame.event.payload, frame.seconds * 1000);
      lab.step(lab.tween.t0 + lab.tween.ms / 2);
      assert.equal(lab.view.stages.reactor_insert.y, (start + target) / 2);
      assert.ok(Math.abs(lab.view.objects.reactor_chip_01.y - chipY - (target - start) / 2) < 1e-8);
      assert.ok(lab.view.stages.reactor_insert.y > 0 && lab.view.stages.reactor_insert.y < reactor.layout.reactor_module.geometry.process_y_mm);
      checked++;
    }
    if (e.kind === 'lab.frame') lab.applyNow(e.payload);
  }
  assert.equal(checked, 2);
  assert.equal(lab.view.stages.reactor_insert.y, 0);
  assert.equal(lab.view.stages.reactor_slot.y, 0);
  assert.ok(lab.view.reactor_microscope.focus_z > 0);
});
}
const event = (id, kind, payload, agent = 'hands', run_id = null) => ({ id, kind, payload, agent, run_id });

test('request chart uses saved token reports and never converts historical character measurements', () => {
  const events = [event(5, 'agent.request', { text_chars: 100, session_id: 'old' }, 'orchestrator'),
    event(6, 'agent.usage', { input_tokens: 999 }),
    event(8, 'agent.request', { text_chars: 25 }, 'eyes'),
    event(9, 'agent.request', { text_chars: null, measurement: 'unavailable' }),
    event(10, 'agent.request', { text_chars: 0, session_id: 'new' })];
  const series = requestSeries(events);
  assert.deepEqual(series.map(p => [p.number, p.input_tokens, p.output_tokens]), [[1, 999, null]]);
  assert.equal(series[0].historical, true);
  assert.deepEqual(requestSeries(events.slice(0, 2)).map(p => p.number), [1]);
  assert.equal(requestSeries(recording.events).length, recording.events.filter(e => e.kind === 'agent.usage').length);
  assert.deepEqual(requestSeries([event(1, 'agent.request', { text_chars: 100 })]), []);
});
test('request chart distinguishes playback position, live waiting, old data and scripted execution', () => {
  assert.match(requestEmptyText([], 'prefix'), /at this playback position/);
  assert.doesNotMatch(requestEmptyText([], 'prefix'), /Older recordings/);
  assert.match(requestEmptyText([], 'live'), /in this run yet/);
  assert.match(requestEmptyText([]), /saved in this recording/);
  const scripted = [event(1, 'notification', { text: 'agents: scripted mode' })];
  assert.match(requestEmptyText(scripted), /Scripted execution: no model requests/);
  assert.doesNotMatch(requestEmptyText([...scripted, event(2, 'agent.usage', {})]), /Scripted execution/);
});
const attempt = (log, id, args, status, agent = 'hands', data = {}) => {
  log.add(event(id, 'tool.call', {tool: 'operate', args, tool_use_id: `call-${id}`}, agent));
  return log.add(event(id + 1, 'tool.result', {tool: 'operate', status, data, tool_use_id: `call-${id}`}, agent));
};

test('the historical approval demo retains four real rejections, with later evidence for each recovery', () => {
  const s = new Session();
  for (const e of recording.events) s.add(e);
  assert.deepEqual(s.log.problems.map(p => p.id), [98, 2655, 5282, 7356]);
  assert.equal(s.log.problems.filter(p => !p.resolved).length, 0);
  assert.deepEqual([...s.log.tools.issues.values()].map(p => p.resolution.id), [102, 2676, 5499, 7359]);
  assert.equal(s.book.devicePlans.length, 1);
  assert.equal(s.book.designs.length, 1);
});

test('seeking before retry completion leaves the policy refusal unresolved', () => {
  const s = new Session();
  for (const e of recording.events.filter(e => e.id < 5499)) s.add(e);
  assert.deepEqual(s.log.problems.filter(p => !p.resolved).map(p => p.id), [5282]);
  assert.equal(s.log.tools.issues.get(5282).retryJob, 'run_8e2ca09c4480');
  s.reset();
  assert.equal(s.log.tools.issues.size, 0);
  assert.equal(s.book.designs.length, 0);
});

test('a successful call for another chip or agent does not clear a failure', () => {
  const t = new ToolAttempts();
  attempt(t, 1, {chip_id: 'one'}, 'error');
  attempt(t, 3, {chip_id: 'two'}, 'ok');
  attempt(t, 5, {chip_id: 'one'}, 'ok', 'eyes');
  assert.equal(t.issues.get(2).resolved, null);
  attempt(t, 7, {chip_id: 'one'}, 'ok');
  assert.equal(t.issues.get(2).resolution.id, 8);
});

test('a different plan step and a final done marker cannot resolve an earlier failure', () => {
  const t = new ToolAttempts();
  t.add(event(1, 'plan.updated', {plan: [{id: 's1', owner: 'hands', status: 'running'}]}));
  attempt(t, 2, {chip_id: 'one'}, 'error');
  t.add(event(4, 'plan.step', {plan: [{id: 's1', owner: 'hands', status: 'done'}, {id: 's2', owner: 'hands', status: 'running'}]}));
  attempt(t, 5, {chip_id: 'one'}, 'ok');
  t.add(event(7, 'agent.turn_end', {status: 'done'}));
  assert.equal(t.issues.get(3).resolved, null);
});

test('a schema refusal is corrected by the same agent calling the same tool for the same step', () => {
  const t = new ToolAttempts();
  t.add(event(1, 'plan.updated', {plan: [{id: 's6', owner: 'picker', status: 'running'}, {id: 's7', owner: 'picker', status: 'pending'}]}));
  const denied = (id, agent) => t.add(event(id, 'tool.denied', {tool: 'submit_flake_selection',
    args: {assessments: [{observed_risks: 'one sentence'}]}, reason: 'invalid arguments', rule: 'schema',
    tool_use_id: `call-${id}`}, agent));
  const accepted = (id, agent, status = 'ok') => {
    t.add(event(id, 'tool.call', {tool: 'submit_flake_selection', args: {assessments: [{observed_risks: ['one sentence']}]},
      tool_use_id: `call-${id}`}, agent));
    t.add(event(id + 1, 'tool.result', {tool: 'submit_flake_selection', status, tool_use_id: `call-${id}`}, agent));
  };
  denied(2, 'picker');
  denied(3, 'designer');
  accepted(4, 'designer');
  accepted(6, 'picker', 'error');
  assert.equal(t.issues.get(2).resolved, null);
  accepted(8, 'picker');
  assert.equal(t.issues.get(2).resolved, 'arguments corrected and accepted');
  assert.equal(t.issues.get(2).resolution.id, 9);
  // a refusal for another reason still needs the same target
  t.add(event(20, 'tool.denied', {tool: 'submit_flake_selection', args: {assessments: []}, reason: 'plan first',
    rule: 'plan_first', tool_use_id: 'call-20'}, 'picker'));
  accepted(21, 'picker');
  assert.equal(t.issues.get(20).resolved, null);
  // a later step cannot correct an earlier step's refusal
  denied(30, 'picker');
  t.add(event(31, 'plan.step', {plan: [{id: 's6', owner: 'picker', status: 'done'}, {id: 's7', owner: 'picker', status: 'running'}]}));
  accepted(32, 'picker');
  assert.equal(t.issues.get(30).resolved, null);
});

test('pending and failed retry jobs are not successful recovery', () => {
  const t = new ToolAttempts();
  attempt(t, 1, {chip_id: 'one'}, 'error');
  attempt(t, 3, {chip_id: 'one'}, 'pending', 'hands', {job_id: 'retry'});
  t.add(event(5, 'job.status', {status: 'succeeded'}, null, 'unrelated'));
  t.add(event(6, 'job.status', {status: 'failed'}, null, 'retry'));
  assert.equal(t.issues.get(2).resolved, null);
  attempt(t, 7, {chip_id: 'one'}, 'pending', 'hands', {job_id: 'next-retry'});
  t.add(event(9, 'job.status', {status: 'succeeded'}, null, 'next-retry'));
  assert.equal(t.issues.get(2).resolution.id, 9);
});

test('unknown tools compare nested arguments without dropping nested keys', () => {
  const t = new ToolAttempts();
  attempt(t, 1, {target: {x: 1, y: 2}}, 'error');
  attempt(t, 3, {target: {y: 3, x: 1}}, 'ok');
  assert.equal(t.issues.get(2).resolved, null);
  attempt(t, 5, {target: {y: 2, x: 1}}, 'ok');
  assert.equal(t.issues.get(2).resolution.id, 6);
});

test('multiple accepted layouts and plans remain independently addressable', () => {
  const s = new Session();
  for (const [i, tool] of ['submit_design', 'submit_design', 'submit_device_plan', 'submit_device_plan'].entries()) {
    s.add(event(i * 2, 'tool.call', {tool, args: {device_name: `Device ${i}`, shapes: [{layer: `layer-${i}`}]}}));
    s.add(event(i * 2 + 1, 'tool.result', {tool, status: 'ok'}));
  }
  assert.deepEqual(s.book.designs.map(x => x.recordId), ['1', '3']);
  assert.deepEqual(s.book.devicePlans.map(x => x.recordId), ['5', '7']);
  assert.equal(s.book.designs[0].shapes[0].layer, 'layer-0');
  assert.equal(s.book.design, s.book.designs[1]);
});

test('every index category opens the middle list and clears the previous detail', () => {
  const r = Object.create(Results.prototype);
  const calls = [];
  r.hooks = {changed: p => calls.push(p), showCentre: () => calls.push('middle'), showRight: () => calls.push('right')};
  for (const category of ['devices', 'plans', 'layouts', 'used', 'flakes', 'chips']) {
    r.sel = {t: 'layout', id: 'old'};
    r.openDoc(category);
    assert.equal(r.sel, null);
    assert.equal(r.doc, category);
  }
  assert.equal(calls.includes('right'), false);
  r.select({t: 'layout', id: '3'});
  assert.deepEqual(r.sel, {t: 'layout', id: '3'});
  assert.equal(calls.at(-1), 'right');
});

test('the collaboration inspection stays failed until the corrected job succeeds', () => {
  const s = new Session();
  for (const e of collaboration.events.filter(e => e.id < 180)) s.add(e);
  assert.equal(s.log.problems.find(p => p.id === 147).resolved, undefined);
  for (const e of collaboration.events.filter(e => e.id >= 180)) s.add(e);
  assert.deepEqual(s.log.problems.map(p => p.id), [117, 147]);
  assert.equal(s.log.problems.filter(p => !p.resolved).length, 0);
  const failure = s.log.problems.find(p => p.id === 147);
  assert.deepEqual(failure.parts.map(p => p.id), [147, 180]);
  assert.equal(s.book.runs.get(failure.run).status, 'failed'); // Original evidence is not rewritten.
  assert.equal(s.summary.outcome, 'awaiting review');
});

const jobPlan = (s, id = 1, owner = 'eyes', revision = 1, step = 'check') => s.add(event(id, 'plan.updated', {
  project_id: 'project', revision, plan: [{id: step, owner, status: 'running'}],
}, 'orchestrator'));
const submit = (s, id, run, params, agent = 'eyes', flow = 'inspect_bench') =>
  s.add(event(id, 'job.submitted', {flow, params}, agent, run));
const status = (s, id, run, state, message = '') => s.add(event(id, 'job.status', {status: state, message}, null, run));
const inspectionFailure = () => {
  const s = new Session(); jobPlan(s);
  submit(s, 2, 'bad', {camera: 'overview', regions: ['storage', 'unrecognised label']});
  status(s, 3, 'bad', 'failed', `KeyError("unknown region 'unrecognised label'; known: ['storage', 'stage', 'microscope']")`);
  return s;
};

test('failed jobs recover only after a later matching job succeeds, never on submission or failure', () => {
  const s = new Session(); jobPlan(s, 1, 'hands');
  submit(s, 2, 'old', {chip_id: 'chip_A'}, 'hands', 'search_chip');
  submit(s, 3, 'already-running', {chip_id: 'chip_A'}, 'hands', 'search_chip');
  status(s, 4, 'old', 'failed', 'camera did not answer');
  status(s, 5, 'already-running', 'succeeded'); // Began before the failure: not its retry.
  submit(s, 6, 'other-target', {chip_id: 'chip_B'}, 'hands', 'search_chip');
  status(s, 7, 'other-target', 'succeeded');
  submit(s, 8, 'retry', {chip_id: 'chip_A'}, 'hands', 'search_chip');
  status(s, 9, 'retry', 'running');
  assert.equal(s.log.problems.filter(p => !p.resolved).length, 1);
  status(s, 10, 'retry', 'failed', 'still offline');
  assert.equal(s.log.problems.filter(p => !p.resolved).length, 2);
  submit(s, 11, 'working', {chip_id: 'chip_A'}, 'hands', 'search_chip');
  status(s, 12, 'working', 'succeeded');
  assert.equal(s.log.problems.filter(p => !p.resolved).length, 0);
  assert.equal(s.log.problems[0].parts.at(-1).run, 'working');
});

test('corrected region requests retain coverage, camera, plan and agent; arbitrary success cannot clear them', () => {
  const invalidRetries = [
    {params: {camera: 'overview', regions: ['microscope']}}, // Dropped an original valid target.
    {params: {camera: 'other-camera', regions: ['storage', 'stage']}},
    {params: {camera: 'overview', regions: ['storage']}}, // Removed, rather than corrected, the request.
    {params: {camera: 'overview', regions: ['storage', 'another invented name']}},
    {params: {camera: 'overview', regions: ['storage', 'stage']}, agent: 'hands'},
    {params: {camera: 'overview', regions: ['storage', 'stage']}, revision: 2},
    {params: {camera: 'overview', regions: ['storage', 'stage']}, step: 'another-task'},
  ];
  for (const candidate of invalidRetries) {
    const s = inspectionFailure();
    if (candidate.revision || candidate.step) jobPlan(s, 4, 'eyes', candidate.revision || 1, candidate.step || 'check');
    submit(s, 5, 'unrelated', candidate.params, candidate.agent || 'eyes');
    status(s, 6, 'unrelated', 'succeeded');
    s.add(event(7, 'agent.turn_end', {status: 'done'}));
    assert.equal(s.log.problems.filter(p => !p.resolved).length, 1, JSON.stringify(candidate));
  }
  const s = inspectionFailure();
  submit(s, 4, 'corrected', {camera: 'overview', regions: ['microscope', 'storage', 'stage']});
  status(s, 5, 'corrected', 'succeeded');
  assert.equal(s.log.problems[0].resolved, 'retry completed successfully');
});

test('region correction does not reinterpret other errors or guess the target of an entirely invalid request', () => {
  for (const [regions, message] of [
    [['storage', 'stage'], 'camera offline'],
    [['unrecognised label'], `unknown region 'unrecognised label'; known: ['storage', 'stage']`],
  ]) {
    const s = new Session(); jobPlan(s);
    submit(s, 2, 'bad', {camera: 'overview', regions});
    status(s, 3, 'bad', 'failed', message);
    submit(s, 4, 'different', {camera: 'overview', regions: ['storage', 'microscope']});
    status(s, 5, 'different', 'succeeded');
    assert.equal(s.log.problems.filter(p => !p.resolved).length, 1);
  }
});

test('a later job cannot clear safety violations or a job still waiting for a person', () => {
  const s = new Session(); jobPlan(s);
  submit(s, 2, 'unsafe', {chip_id: 'chip_A'});
  s.add(event(3, 'safety.violation', {device: 'arm', command: 'move', reason: 'outside limits'}, null, 'unsafe'));
  status(s, 4, 'unsafe', 'failed', 'refused by safety');
  submit(s, 5, 'waiting', {chip_id: 'chip_A'});
  status(s, 6, 'waiting', 'waiting_human', 'check the chip');
  submit(s, 7, 'later', {chip_id: 'chip_A'});
  status(s, 8, 'later', 'succeeded');
  assert.equal(s.log.problems.filter(p => !p.resolved).length, 2);
});

test('a refused plan proposal remains recorded after an accepted replacement', () => {
  const t = new ToolAttempts();
  const plan = [{id:'s1', owner:'hands', title:'Place chip_001 on the transfer stage'}];
  t.add(event(1, 'tool.call', {tool:'update_plan', args:{plan, note:'placement', expected_version:1}, tool_use_id:'stale'}, 'orchestrator'));
  t.add(event(2, 'tool.result', {tool:'update_plan', status:'error', tool_use_id:'stale'}, 'orchestrator'));
  t.add(event(3, 'tool.call', {tool:'update_plan', args:{plan:[{...plan[0], title:'Process another chip'}], note:'placement', expected_version:3}, tool_use_id:'other'}, 'orchestrator'));
  t.add(event(4, 'tool.result', {tool:'update_plan', status:'ok', tool_use_id:'other'}, 'orchestrator'));
  assert.equal(t.issues.get(2).resolution.id, 4);
  assert.equal(t.issues.get(2).resolved, 'superseded by a later accepted Task Plan');
  assert.equal(t.issues.get(2).event.payload.status, 'error');
  t.add(event(5, 'tool.call', {tool:'update_plan', args:{plan, note:'placement', expected_version:4}, tool_use_id:'retry'}, 'orchestrator'));
  t.add(event(6, 'tool.result', {tool:'update_plan', status:'ok', tool_use_id:'retry'}, 'orchestrator'));
  assert.equal(t.issues.get(2).resolution.id, 4);
});


test('a later admitted delegation retires prerequisite refusals but not unrelated denials', () => {
  const t = new ToolAttempts();
  t.add(event(1, 'tool.denied', {tool:'delegate', args:{to:'hands', task:'s3: process'}, rule:'sample_outcomes_required'}, 'orchestrator'));
  t.add(event(2, 'tool.denied', {tool:'operate', args:{chip_id:'chip_001'}, rule:'safety'}, 'hands'));
  t.add(event(3, 'agent.delegate', {to:'eyes', task:'s1: inspect'}, 'orchestrator'));
  assert.equal(t.issues.get(1).resolved, null);
  t.add(event(4, 'agent.delegate', {to:'hands', task:'s3: process'}, 'orchestrator'));
  assert.equal(t.issues.get(1).resolution.id, 4);
  assert.match(t.issues.get(1).resolved, /prerequisite/);
  assert.equal(t.issues.get(2).resolved, null);
});

test('reactor owner review: both prerequisite refusals and the resumed budget blocker recover with links', () => {
  const data = JSON.parse(readFileSync(new URL('openai-integrated-reactor-20261027-v5/recording.json', RECORDINGS)));
  const s = new Session();
  for (const e of data.events) s.add(e);
  for (const id of [5075, 5083, 10985]) {
    const issue = s.log.problems.find(p => p.id === id);
    assert.ok(issue?.resolved, `issue ${id} should be recovered`);
    assert.equal(issue.parts[0].id, id, 'original evidence must remain');
    assert.ok(issue.parts.some(p => p.id === (id === 10985 ? 11498 : 5096)));
  }
  const prefix = new Session();
  for (const e of data.events.filter(e => e.id < 11498)) prefix.add(e);
  assert.ok(!prefix.log.problems.find(p => p.id === 10985).resolved, 'recovery must not leak into earlier playback');
});

test('another step, project or model session cannot clear the reactor blocker', () => {
  const s = new Session();
  s.add(event(1, 'plan.updated', {project_id: 'p1', revision: 1, plan: []}));
  s.add(event(2, 'tool.denied', {tool: 'delegate', args: {to: 'reactor', task: 's3: react'}, rule: 'reactor_outcome_required'}, 'orchestrator'));
  s.add(event(3, 'agent.delegate', {to: 'reactor', task: 's4: observe'}, 'orchestrator'));
  assert.ok(!s.log.problems[0].resolved);
  s.add(event(4, 'notification', {level: 'danger', text: 'estimated spend budget cannot cover another model request'}));
  s.add(event(5, 'agent.turn_end', {reason: 'budget_exhausted', is_error: true, session_id: 'old'}, 'orchestrator'));
  s.add(event(6, 'user.message', {text: 'Continue'}));
  s.add(event(7, 'agent.delegation_checked', {ok: true}));
  s.add(event(8, 'agent.turn_end', {reason: 'success', is_error: false, session_id: 'other'}, 'orchestrator'));
  assert.ok(!s.log.problems[1].resolved);
  s.add(event(9, 'plan.updated', {project_id: 'p2', revision: 1, plan: []}));
  s.add(event(10, 'agent.delegate', {to: 'reactor', task: 's3: react'}, 'orchestrator'));
  s.add(event(11, 'agent.turn_end', {reason: 'success', is_error: false, session_id: 'old'}, 'orchestrator'));
  assert.ok(s.log.problems.every(p => !p.resolved));
});

test('stable-ID refinement resolves a refused plan while a remaining completion blocker stays active', () => {
  const s = new Session();
  s.add(event(1, 'tool.result', {tool: 'update_plan', status: 'error', summary: 'scheme drift'}, 'orchestrator'));
  s.add(event(2, 'tool.result', {tool: 'save_project_status', status: 'error', summary: 'experiment incomplete'}, 'orchestrator'));
  s.add(event(3, 'tool.result', {tool: 'refine_plan', status: 'ok'}, 'orchestrator'));
  assert.ok(s.log.problems[0].resolved);
  assert.ok(!s.log.problems[1].resolved);
});

test('matching budget notices emitted before and after the failed turn both retain recovery links', () => {
  const s = new Session();
  s.add(event(1, 'plan.updated', {project_id: 'p', plan: []}));
  const notice = {level: 'danger', reason: 'budget_exhausted', text: 'Budget exhausted', project_id: 'p', session_id: 'same'};
  s.add(event(2, 'notification', notice));
  s.add(event(3, 'agent.turn_end', {reason: 'budget_exhausted', is_error: true, session_id: 'same'}, 'orchestrator'));
  s.add(event(4, 'notification', notice));
  s.add(event(5, 'user.message', {text: 'Continue the saved task'}));
  s.add(event(6, 'agent.delegation_checked', {ok: true}));
  s.add(event(7, 'agent.turn_end', {reason: 'success', is_error: false, session_id: 'same'}, 'orchestrator'));
  assert.equal(s.log.problems.length, 2);
  assert.ok(s.log.problems.every(p => p.resolved && p.parts.some(part => part.id === 7)));
});

test('v5 refused obligation edits recover only after the unchanged linked job completes', () => {
  const data = JSON.parse(readFileSync(new URL('openai-material-metal-default-20261028-v5/recording.json', RECORDINGS)));
  const replay = events => { const t = new ToolAttempts(); for (const e of events) t.add(e); return t; };
  const ids = [11678, 11683], checkId = 14483;
  const prefix = replay(data.events.filter(e => e.id < checkId));
  assert.ok(ids.every(id => !prefix.issues.get(id).resolved));
  const completed = replay(data.events);
  for (const id of ids) {
    const issue = completed.issues.get(id);
    assert.match(issue.resolved, /unchanged physical obligation/);
    assert.equal(issue.resolution.id, checkId);
    assert.equal(issue.event.payload.status, 'error');
    assert.ok(issue.resolutionEvidence.some(e => e.kind === 'agent.job_linked'));
    assert.ok(issue.resolutionEvidence.some(e => e.kind === 'job.status' && e.payload.status === 'succeeded'));
  }
  for (const variant of ['other-step', 'other-project', 'changed-contract', 'failed-check', 'missing-job-proof']) {
    const events = structuredClone(data.events);
    for (const e of events) {
      if (e.id === checkId && variant === 'other-step') e.payload.step = 's7';
      if (e.id === checkId && variant === 'failed-check') e.payload.ok = false;
      if (e.id === checkId && variant === 'missing-job-proof') e.payload.facts = [];
      if (e.id >= 11687 && e.kind === 'plan.step' && variant === 'other-project') e.payload.project_id = 'unrelated-project';
      if (e.id === 11687 && variant === 'changed-contract') e.payload.plan.find(s => s.id === 's6').operation_args.metal = 'other-metal';
    }
    const t = replay(events);
    assert.ok(ids.every(id => !t.issues.get(id).resolved), variant);
  }
});

test('a refused selection-blocked report recovers only when the same step completes with a selection for that role', () => {
  const plan = [{id: 's6', owner: 'picker', status: 'running', title: 'Select flakes'}];
  const run = variant => {
    const s = new Session();
    s.add(event(1, 'plan.updated', {project_id: 'p', revision: 4, plan}));
    s.add(event(2, 'agent.delegate', {to: 'picker', task: 's6: select flakes', tool_use_id: 'd1'}, 'orchestrator'));
    s.add(event(3, 'tool.call', {tool: 'report_selection_blocked', tool_use_id: 'b1',
      args: {role: 'top_hbn', action: 'relink_or_capture_optical_images', reason: 'images omitted'}}, 'picker'));
    s.add(event(4, 'tool.result', {tool: 'report_selection_blocked', tool_use_id: 'b1', status: 'error',
      summary: 'all candidates have images; this is not an image problem'}, 'picker'));
    s.add(event(5, 'agent.delegation_checked', {to: 'picker', step: 's6', ok: false, status: 'pending',
      facts: ['no submit_flake_selection was accepted.']}, 'orchestrator'));
    s.add(event(6, 'agent.delegate', {to: 'picker', task: 's6: select flakes again', tool_use_id: 'd2'}, 'orchestrator'));
    const role = variant === 'other-role' ? 'channel' : 'top_hbn';
    s.add(event(7, 'tool.call', {tool: 'submit_flake_selection', tool_use_id: 'x1',
      args: {selected_by_role: {[role]: ['chip_001_f002']}}}, 'picker'));
    s.add(event(8, 'tool.result', {tool: 'submit_flake_selection', tool_use_id: 'x1',
      status: variant === 'refused' ? 'error' : 'ok', summary: 'selection accepted'}, 'picker'));
    s.add(event(9, 'agent.delegation_checked', {to: 'picker', step: variant === 'other-step' ? 's7' : 's6',
      ok: true, status: 'done', facts: ['submit_flake_selection: selection accepted.']}, 'orchestrator'));
    return s.log.problems.find(p => p.id === 4);
  };
  const fixed = run('normal');
  assert.match(fixed.resolved, /accepted flake selection/);
  assert.equal(fixed.parts[0].id, 4, 'the refusal stays the first part');
  assert.ok(fixed.parts.some(part => part.id === 8) && fixed.parts.some(part => part.id === 9));
  for (const variant of ['other-role', 'other-step', 'refused']) assert.ok(!run(variant).resolved, variant);
});
