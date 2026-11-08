import assert from 'node:assert/strict';
import {test} from 'node:test';
import {layoutPolygons} from '../qumus2/ui/static/planbook.js';
import {Session} from '../qumus2/ui/static/session.js';

const design = {version: 'device_design_v1', design_id: 'accepted', target: {contacts: [
  {role: 'source', points_mm: [[.4, .7], [.401, .7], [.401, .701]]}
]}, sources: [{material: 'Gr', outline_mm: [[.01, .02], [.03, .02], [.01, .04]],
  placement: {translation_mm: [.5, .6], rotation_deg: 90}}]};

test('layout displays saved rotated whole outlines and contacts in one coordinate frame', () => {
  const shapes = layoutPolygons(design);
  assert.deepEqual(shapes[0].points[0], [400, 700]);
  shapes[1].points.forEach((p, i) => {
    const expected = [[480, 610], [480, 630], [460, 610]][i];
    p.forEach((v, j) => assert.ok(Math.abs(v - expected[j]) < 1e-9));
  });
});

test('replay accepts the verified tool result, never caller intent or missing artifact data', () => {
  const session = new Session(); let id = 0;
  const call = key => session.add({id: ++id, kind: 'tool.call', agent: 'designer', payload: {
    tool: 'create_device_design', tool_use_id: key, args: {design_id: 'unverified'}}});
  const result = (key, status, data) => session.add({id: ++id, kind: 'tool.result', agent: 'designer',
    payload: {tool: 'create_device_design', tool_use_id: key, status, data}});
  call('failed'); result('failed', 'error', {device_design: design});
  call('missing'); result('missing', 'ok', {});
  assert.equal(session.book.designs.length, 0);
  call('verified'); result('verified', 'ok', {device_design: design});
  assert.equal(session.book.designs.length, 1);
  assert.equal(session.book.design.design_id, 'accepted');
  assert.deepEqual(layoutPolygons(session.book.design), layoutPolygons(design));
});
