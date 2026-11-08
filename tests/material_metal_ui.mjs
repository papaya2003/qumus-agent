import assert from 'node:assert/strict';
import { test } from 'node:test';
import { materialMetalSummary } from '../qumus2/ui/static/materialmetal.js';
import { StationBenchView, stampStatusText } from '../qumus2/ui/static/stations.js';
import { Session } from '../qumus2/ui/static/session.js';

const carrier = {id: 'carrier', props: {
  material_metal_preparation: {scope: 'device_simulation', project_id: 'project', preparation_job_id: 'prepare',
    top_first_flake_ids: ['hbn', 'gr'], layer_order: ['hBN', 'Gr', 'Pd', 'silicon'], stamp_film: 'mica', release_images: ['release.png']},
  material_metal_observation: {job_id: 'react', qualitative_progress: .9, fully_visible: true, images: ['react.png']},
}};
const accepted = {kind: 'material_metal.simulation_accepted', payload: {
  project_id: 'project', chip_id: 'carrier', evidence_job_id: 'react', scope: 'device_simulation',
  simulation_accepted: true, experiment_accepted: false, goal: {preparation: {preparation_job_id: 'prepare'}},
}};

test('device simulation shows its actual ordered stack, observed image and explicit scope', () => {
  const report = materialMetalSummary(carrier, [accepted]);
  assert.equal(report.accepted, true);
  assert.equal(report.image, 'react.png');
  assert.deepEqual(report.layers, ['hBN', 'Gr', 'Pd', 'silicon']);
  assert.equal(report.stampFilm, 'mica');
  assert.equal(report.caption, 'Qualitative device simulation; real chemistry unverified');
});
test('foreign or earlier receipt cannot label a current observation accepted', () => {
  for (const patch of [{project_id: 'other'}, {chip_id: 'other'}, {evidence_job_id: 'older'},
    {experiment_accepted: true}, {scope: 'synthetic_workflow'}, {goal: {preparation: {preparation_job_id: 'old'}}}]) {
    assert.equal(materialMetalSummary(carrier, [{...accepted, payload: {...accepted.payload, ...patch}}]).accepted, false);
  }
  assert.equal(materialMetalSummary(carrier, []).accepted, false);
});
test('unmeasured views never display progress and abstract fixture is not a device report', () => {
  const copy = structuredClone(carrier);
  copy.props.material_metal_observation.fully_visible = false;
  assert.equal(materialMetalSummary(copy).progress, null);
  copy.props.material_metal_preparation.scope = 'synthetic_workflow';
  assert.equal(materialMetalSummary(copy), null);
});

test('used stamp tooltip follows measured support and updates without a lifecycle change', () => {
  const element = () => ({setAttribute() {}});
  const parent = {}, g = {...element(), parentNode: parent};
  const entry = {g, r: element(), dome: element(), flakes: element(), tip: {}, key: ''};
  const bench = {objs: new Map([['stamp_01', entry]]), gObjects: parent, k: 1,
    shown: size => size, sx: x => x, sy: y => y, statusOf: () => 'used'};
  const stamp = {kind: 'stamp', x: 0, y: 0, rotation: 0, film: true};
  StationBenchView.prototype.object.call(bench, 'stamp_01', stamp);
  assert.match(entry.tip.textContent, /used \(support retained\)/);
  StationBenchView.prototype.object.call(bench, 'stamp_01', {...stamp, film: false});
  assert.match(entry.tip.textContent, /used \(no film\)/);
  // Old replay frames lack film; their recorded ledger still has the evidence.
  bench.stampOf = () => ({props: {film: 'mica'}});
  StationBenchView.prototype.object.call(bench, 'stamp_01', {...stamp, film: undefined});
  assert.match(entry.tip.textContent, /used \(support retained\)/);
  bench.stampOf = () => ({props: {film: null}});
  StationBenchView.prototype.object.call(bench, 'stamp_01', {...stamp, film: true});
  assert.match(entry.tip.textContent, /used \(no film\)/);
  assert.equal(stampStatusText('used', undefined), 'used');
  assert.equal(stampStatusText('torn', false), 'film torn');
});

test('stamp history does not infer film transfer from used status', () => {
  const session = new Session();
  session.add({kind: 'sample.updated', payload: {id: 'stamp_01', kind: 'stamp', status: 'used',
    location: 'right_stamp_1', props: {film: 'mica', support: {film_type: 'mica'}}}});
  assert.doesNotMatch(session.sampleHistory('stamp_01')[0].text, /left on a device|no film/);
});
