import test from 'node:test';
import assert from 'node:assert/strict';
import { compositionData } from '../qumus2/ui/static/contextcomposition.js';
import { requestSeries } from '../qumus2/ui/static/requestchart.js';

const ids = ['system_instructions','reference_skills','lab_snapshot','current_task','conversation_history',
  'tool_definitions','tool_calls','tool_results','media','protocol_unassigned'];
const sample = () => ({version:1, method:'utf8_bytes_div4_v1',status:'available',text_estimated_tokens:80,
  components:ids.map(category => ({category,status:['media','protocol_unassigned'].includes(category) ? 'unknown' : 'estimated',
    text_bytes:['media','protocol_unassigned'].includes(category) ? 0 : 40,item_count:1,
    estimated_tokens:['media','protocol_unassigned'].includes(category) ? null : 10,
    text_estimated_tokens:['media','protocol_unassigned'].includes(category) ? 0 : 10}))});

test('composition never fills service usage or borrows another model limit', () => {
  const make = (id, model, limit) => ({id,kind:'agent.request',agent:'eyes',payload:{
    request_id:String(id),call_id:String(id),session_id:'s',model,context_window_tokens:limit,
    token_measurement:'provider_usage_v1',input_composition:sample()}});
  const points = requestSeries([make(1,'a',10000),make(2,'b',null)]);
  assert.equal(points[0].input_tokens,null);
  assert.equal(points[0].remaining_tokens,null);
  assert.equal(points[1].context_window_tokens,null);
  assert.equal(points[1].input_composition.text_estimated_tokens,80);
  assert.equal(compositionData(points[1].input_composition).components.length,10);
});

test('old, unavailable and malformed category data is not reinterpreted', () => {
  for (const value of [undefined,null,{}, {version:2}, {...sample(),status:'unavailable'},
      {...sample(),components:[null]}, {...sample(),text_estimated_tokens:100},
      {...sample(),components:sample().components.map(p=>({...p,estimated_tokens:'10'}))}])
    assert.equal(compositionData(value),null);
  const duplicate=sample();duplicate.components[1]=duplicate.components[0];
  assert.equal(compositionData(duplicate),null);
});
