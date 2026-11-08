import test from 'node:test';
import assert from 'node:assert/strict';
import { requestSeries, requestEmptyText, requestScale } from '../qumus2/ui/static/requestchart.js';
const event = (id, kind, payload, agent = 'eyes') => ({ id, kind, payload, agent });
const request = (id, model = 'a', limit = 1000) => event(id, 'agent.request', {
  request_id: `r${id}`, call_id: 'c1', session_id: model, model, context_window_tokens: limit,
  token_measurement: 'provider_usage_v1', text_chars: 999999,
});
const report = (r, input, output) => event(r.id + 1, 'agent.request_usage', {
  ...r.payload, input_tokens: input, output_tokens: output,
});

test('retries, missing responses, real zeros and partial usage remain distinct', () => {
  const r1 = request(1), r2 = request(2), r3 = request(4), r4 = request(6);
  const events = [r1, r2, report(r2, 600, 100), r3, report(r3, 0, 0), r4, report(r4, 50, null)];
  const p = requestSeries(events);
  assert.deepEqual(p.map(p => [p.input_tokens, p.output_tokens, p.remaining_tokens]),
    [[null, null, null], [600, 100, 300], [0, 0, 1000], [50, null, null]]);
  assert.equal(requestSeries(events.slice(0, 2))[1].input_tokens, null);
  const invalid = report(r1, 500, 10); invalid.payload.model = 'different';
  assert.equal(requestSeries([r1, invalid])[0].input_tokens, null);
});

test('each request retains its model, limit and signed remaining capacity', () => {
  const a = request(1, 'a', 1000), b = request(3, 'b', 2000), c = request(5, 'c', null);
  const p = requestSeries([a, report(a, 900, 200), b, report(b, 1000, 100), c, report(c, 50, 20)]);
  assert.deepEqual(p.map(p => [p.model, p.remaining_tokens]), [['a', -100], ['b', 900], ['c', null]]);
});

test('small usage stays legible under a million-token capacity and retains agent identity', () => {
  const a = request(1, 'large', 1050000), b = request(2, 'small', 131072);
  b.agent = 'hands';
  const bu = report(b, 32000, 616); bu.agent = 'hands';
  const points = requestSeries([a, report(a, 9356, 189), b, bu]);
  assert.equal(requestScale(points), 40000);
  assert.equal(requestScale(points, 'capacity'), 1200000);
  assert.deepEqual(points.map(p => [p.number, p.key, p.agent]), [[1, '1', 'eyes'], [2, '2', 'hands']]);
  assert.equal(points[0].remaining_tokens, 1040455);
  const partial = requestSeries([a, report(a, 50, null)]);
  assert.equal(requestScale(partial), 50);
  assert.equal(partial[0].total_tokens, null);
  assert.equal(requestScale(requestSeries([a])), 4);
  assert.equal(requestScale(requestSeries([a, report(a, 0, 0)])), 4);
});

test('legacy reports are preserved without guessing request links, zeros or limits', () => {
  const events = [event(1, 'agent.request', { text_chars: 100 }),
    event(2, 'agent.usage', { input_tokens: 90, output_tokens: 10, model: 'old' }),
    event(3, 'agent.usage', { input_tokens: 0, output_tokens: 0 }),
    event(4, 'agent.usage', { input_tokens: 0, output_tokens: 0, token_measurement: 'provider_usage_v1' })];
  const p = requestSeries(events);
  assert.equal(p.length, 2); assert.equal(p[0].historical, true);
  assert.equal(p[0].total_tokens, 100); assert.equal(p[0].context_window_tokens, null);
  assert.equal(p[1].input_tokens, null);
  assert.deepEqual(requestSeries(events.slice(0, 1)), []);
  assert.match(requestEmptyText(events.slice(0, 1)), /Character counts cannot/);
});

test('retry recovery uses exact call identity and completed responses without filling failed tokens', () => {
  const a = request(100), b = request(101), c = request(102);
  a.payload.attempt = 1; b.payload.attempt = 2; c.payload.attempt = 3;
  const fail = event(103, 'agent.request_failure', {...a.payload, failure_kind:'server_error', http_status:503});
  const fail2 = event(104, 'agent.request_failure', {...b.payload, failure_kind:'timeout', exception_type:'ReadTimeout'});
  const complete = event(105, 'agent.usage', {...c.payload, input_tokens:90, output_tokens:10});
  const events = [a, fail, b, fail2, c, report(c, 90, 10), complete];
  const p = requestSeries(events);
  assert.equal(p[0].failure.http_status, 503);
  assert.equal(p[0].retried_by.number, 2);
  assert.equal(p[0].recovered_by.number, 3);
  assert.equal(p[1].recovered_by.number, 3);
  assert.equal(p[2].retry_of.number, 2);
  assert.equal(p[0].input_tokens, null);
  assert.equal(p[0].remaining_tokens, null);
  const prefix = requestSeries(events.slice(0,4));
  assert.equal(prefix[0].retried_by.number,2);
  assert.equal(prefix[0].recovered_by,undefined);
  const wrong = structuredClone(fail); wrong.agent='hands';
  assert.equal(requestSeries([a,wrong])[0].failure,null);
  const other = structuredClone(c); other.agent='hands';
  assert.equal(requestSeries([a,other,complete])[0].retried_by,undefined);
  const failedResponse = event(106, 'agent.request_failure', {...c.payload, failure_kind:'response_incomplete'});
  assert.equal(requestSeries([...events,failedResponse])[0].recovered_by,undefined);
});

test('old attempts link through call/agent/session/model and do not invent a failure cause', () => {
  const a = request(110), b = request(111);
  a.payload.attempt=1; b.payload.attempt=2;
  const done=event(112,'agent.usage',{...b.payload,input_tokens:100,output_tokens:10});
  const p=requestSeries([a,b,report(b,100,10),done]);
  assert.equal(p[0].recovered_by.number,2);
  assert.equal(p[0].failure,null);
  assert.equal(p[0].total_tokens,null);
  const crossSession=structuredClone(b);crossSession.payload.session_id='another';
  assert.equal(requestSeries([a,crossSession])[0].retried_by,undefined);
});
