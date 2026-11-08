// Synthetic event fixtures for strictly evidenced recovery; no recording edits.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {ProblemLog} from '../qumus2/ui/static/panel.js';

const job = 'run_734f4678db1a', truncated = job.slice(0, -1);

test('first-request failure can recover before any plan exists using explicit project provenance', () => {
  const log = new ProblemLog();
  const add = (id, kind, payload) => log.add({id, kind, agent: 'orchestrator', payload});
  add(1, 'agent.error', {session_id: 's', project_id: 'p', error: 'first request failed'});
  add(2, 'user.message', {project_id: 'p', text: 'Try again'});
  add(3, 'agent.turn_end', {session_id: 's', project_id: 'other', reason: 'success', is_error: false});
  assert.equal(log.problems[0].resolved, undefined);
  add(4, 'agent.turn_end', {session_id: 's', project_id: 'p', reason: 'success', is_error: false});
  assert.match(log.problems[0].resolved, /same session/);
});

test('only an exact supervisor restart receipt resolves its historical warning', () => {
  const log = new ProblemLog();
  const add = (id, payload) => log.add({id, kind: 'notification', payload});
  add(1, {level: 'warn', reason: 'lab_restarting', restart_id: 'a', text: 'restarting'});
  add(2, {level: 'danger', reason: 'lab_restart_exhausted', text: 'human required'});
  add(3, {level: 'info', reason: 'lab_restarted', restart_id: 'other'});
  assert.equal(log.problems.some(p => p.resolved), false);
  add(4, {level: 'info', reason: 'lab_restarted', restart_id: 'a'});
  assert.match(log.problems[0].resolved, /startup recovery completed/);
  assert.equal(log.problems[1].resolved, undefined);
  assert.equal(log.problems[0].parts.length, 2);
});

test('model recovery needs same session, project and a later user request', () => {
  const log = new ProblemLog();
  const add = (id, kind, payload) => log.add({id, kind, agent: 'orchestrator', payload});
  add(1, 'plan.updated', {project_id: 'p', plan: []});
  add(2, 'agent.error', {session_id: 's', project_id: 'p', error: 'network failed'});
  add(3, 'agent.turn_end', {session_id: 's', reason: 'success', is_error: false});
  assert.equal(log.problems[0].resolved, undefined);
  add(4, 'user.message', {text: 'Continue'});
  add(5, 'agent.turn_end', {session_id: 'other', reason: 'success', is_error: false});
  assert.equal(log.problems[0].resolved, undefined);
  add(6, 'agent.turn_end', {session_id: 's', reason: 'success', is_error: false});
  assert.match(log.problems[0].resolved, /same session/);
  assert.equal(log.problems[0].parts[0].payload.error, 'network failed');
});
function fixture(options = {}) {
  const log = new ProblemLog(); let id = 0;
  const add = (kind, payload, agent = 'orchestrator', run_id = null) => {
    const e = {id: ++id, kind, payload, agent, run_id}; log.add(e); return e;
  };
  const step = {id: 's8', owner: 'hands', status: 'running', operation: 'search_chip', materials: ['hBN'],
    operation_args: {}, sample_outcomes: [{sample_id: 'chip_001', status: 'searched', in_storage: true}]};
  const plan = (project = 'p1', changed = {}) => add('plan.updated', {project_id: project, revision: id,
    plan: [{...step, ...changed}]});
  const link = (rid = job, project = 'p1', sid = 's8') => add('agent.job_linked',
    {project_id: project, step_id: sid, job_id: rid, flow: 'search_chip'}, 'hands', rid);
  const delegate = (sid = 's8', role = 'hands') => add('agent.delegate', {to: role, task: `${sid}: Read the original job`, tool_use_id: `d${id}`});
  const read = (rid, {status = 'ok', jobStatus = 'succeeded', role = 'hands', flow = 'search_chip', returned = rid} = {}) => {
    const key = `call${id}`;
    const call = add('tool.call', {tool: 'wait_job', args: {job_id: rid}, tool_use_id: key}, role);
    const event = add('tool.result', {tool: 'wait_job', status, tool_use_id: key,
      data: {run_id: returned, flow, status: jobStatus, done: jobStatus === 'succeeded'}}, role);
    return {call, event};
  };
  const checked = (ok, sid = 's8', role = 'hands', rid = job) => add('agent.delegation_checked',
    {to: role, step: sid, ok, status: ok ? 'done' : 'pending', facts: ok ? [`job ${rid} (search_chip) succeeded.`] : ['lookup failed']});
  plan();
  const original = options.noLink ? null : link();
  if (options.ambiguous) link('run_734f4678db1b');
  const failedStart = delegate();
  const call = add('tool.call', {tool: 'wait_job', args: {job_id: truncated}, tool_use_id: 'bad'}, 'hands');
  const error = add('tool.result', {tool: 'wait_job', tool_use_id: 'bad', status: 'error',
    summary: options.structured ? 'Unknown exact reference' : options.generic ? 'Network unavailable' : `no job '${truncated}': submit the job first`,
    data: options.structured ? {error: 'unknown_job', requested_job_id: truncated, known_jobs: [{job_id: job}]} : {job_id: truncated}}, 'hands');
  const failure = options.noFailure ? null : checked(false);
  return {log, add, plan, link, delegate, read, checked, original, failedStart, call, error, failure,
    issue: () => log.problems.find(p => p.id === error.id)};
}

for (const structured of [false, true]) test(`exact read plus same-step completion recovers ${structured ? 'structured' : 'legacy'} truncation with all evidence`, () => {
  const s = fixture({structured});
  s.plan('p1', {note: 'A new note changes revision, not the completion contract'});
  const delegated = s.delegate();
  const read = s.read(job, {status: 'corrected'});
  assert.ok(!s.issue().resolved, 'successful reading alone is insufficient');
  const completed = s.checked(true);
  assert.match(s.issue().resolved, /exact original job/);
  assert.deepEqual(s.issue().parts.map(p => p.id), [s.error.id, s.original.id, s.failure.id,
    delegated.id, read.call.id, read.event.id, completed.id]);
  assert.equal(s.issue().parts[0].payload.summary, s.error.payload.summary);
});

for (const bad of ['no_link', 'ambiguous', 'late_link', 'generic_error', 'no_failed_delegation', 'other_project',
  'other_step', 'other_role', 'changed_contract', 'other_job', 'wrong_returned_id', 'read_failed', 'job_failed',
  'wrong_flow', 'check_failed', 'unrelated_check', 'read_before_delegation']) {
  test(`lookup remains active without complete matching evidence: ${bad}`, () => {
    const s = fixture({noLink: ['no_link', 'late_link'].includes(bad), ambiguous: bad === 'ambiguous',
      generic: bad === 'generic_error', noFailure: bad === 'no_failed_delegation'});
    if (bad === 'late_link') s.link();
    const sid = bad === 'other_step' ? 's10' : 's8', role = bad === 'other_role' ? 'reactor' : 'hands';
    const rid = bad === 'other_job' ? 'run_000000000001' : job;
    if (bad === 'other_job') s.link(rid);
    s.plan(bad === 'other_project' ? 'p2' : 'p1', {id: sid, owner: role,
      ...(bad === 'changed_contract' ? {sample_outcomes: [{sample_id: 'chip_002', status: 'searched'}]} : {})});
    if (bad === 'read_before_delegation') s.read(job);
    s.delegate(sid, role);
    if (bad !== 'read_before_delegation') s.read(rid, {role, status: bad === 'read_failed' ? 'error' : 'ok',
      jobStatus: bad === 'job_failed' ? 'failed' : 'succeeded', flow: bad === 'wrong_flow' ? 'exfoliate_chips' : 'search_chip',
      returned: bad === 'wrong_returned_id' ? 'run_000000000001' : rid});
    s.checked(bad !== 'check_failed', bad === 'unrelated_check' ? 's10' : sid, role, rid);
    s.add('tool.call', {tool: 'save_project_status', args: {status: 'done'}, tool_use_id: 'done'});
    s.add('tool.result', {tool: 'save_project_status', status: 'ok', tool_use_id: 'done'});
    assert.ok(!s.issue().resolved, 'project completion never clears an unrelated lookup');
  });
}

test('one exact read recovers both corresponding failed delegations, retaining both original errors', () => {
  const s = fixture();
  s.delegate();
  s.add('tool.call', {tool: 'wait_job', args: {job_id: truncated}, tool_use_id: 'bad2'}, 'hands');
  const second = s.add('tool.result', {tool: 'wait_job', status: 'error', summary: `no job '${truncated}': unknown`,
    data: {job_id: truncated}, tool_use_id: 'bad2'}, 'hands');
  const secondFailure = s.checked(false);
  s.delegate(); s.read(job); const done = s.checked(true);
  for (const [eid, failure] of [[s.error.id, s.failure.id], [second.id, secondFailure.id]]) {
    const pr = s.log.problems.find(p => p.id === eid);
    assert.ok(pr.resolved && pr.parts.some(p => p.id === failure) && pr.parts.some(p => p.id === done.id));
    assert.equal(pr.parts[0].id, eid);
  }
});

test('a later job created with the missing ID cannot erase a previously unlinked lookup failure', () => {
  const s = fixture({noLink: true});
  s.link(truncated);
  s.delegate(); s.read(truncated); s.checked(true, 's8', 'hands', truncated);
  assert.ok(!s.issue().resolved);
});

for (const bad of [null, 'other_step', 'other_project', 'other_role', 'failed', 'different_rule']) {
  test(`search prerequisite recovery requires the same completed delegation: ${bad || 'matched'}`, () => {
    const s = fixture();
    const blocked = s.add('tool.denied', {tool: 'delegate', rule: bad === 'different_rule' ? 'safety' : 'search_outcome_contract',
      args: {to: 'hands', task: 's8: Search'}});
    const sid = bad === 'other_step' ? 's10' : 's8', role = bad === 'other_role' ? 'reactor' : 'hands';
    s.plan(bad === 'other_project' ? 'p2' : 'p1', {id: sid, owner: role});
    const delegated = s.delegate(sid, role);
    const issue = () => s.log.problems.find(p => p.id === blocked.id);
    assert.ok(!issue().resolved, 'admission is not the requested completed-delegation evidence');
    const checked = s.checked(bad !== 'failed', sid, role);
    assert.equal(!!issue().resolved, bad === null);
    if (!bad) assert.deepEqual(issue().parts.map(p => p.id), [blocked.id, delegated.id, checked.id]);
  });
}

for (const bad of [null, 'other_project', 'blocked_status', 'generic_error', 'no_call', 'other_agent']) {
  test(`project completion only resolves a matching completion refusal: ${bad || 'matched'}`, () => {
    const s = fixture();
    s.add('tool.call', {tool: 'save_project_status', args: {status: 'done', summary: 'Old report'}, tool_use_id: 'old'});
    const refused = s.add('tool.result', {tool: 'save_project_status', status: 'error', tool_use_id: 'old',
      summary: bad === 'generic_error' ? 'Unexpected server error' : "Physical plan steps ['s8'] are still incomplete; verify them."});
    if (bad === 'other_project') s.plan('p2');
    const role = bad === 'other_agent' ? 'hands' : 'orchestrator';
    const call = bad === 'no_call' ? null : s.add('tool.call', {tool: 'save_project_status', args: {
      status: bad === 'blocked_status' ? 'blocked' : 'done', summary: 'Updated report'}, tool_use_id: 'new'}, role);
    const result = s.add('tool.result', {tool: 'save_project_status', status: 'ok', tool_use_id: 'new'}, role);
    const issue = s.log.problems.find(p => p.id === refused.id);
    assert.equal(!!issue.resolved, bad === null);
    if (!bad) assert.deepEqual(issue.parts.map(p => p.id), [refused.id, call.id, result.id]);
    assert.ok(!s.issue().resolved, 'the distinct unknown job failure stays active');
  });
}
