// Offline DOM checks of the real LabView/StationBenchView evidence consumer.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {LabView, recordedObservedScene} from '../qumus2/ui/static/labview.js';
import {StationBenchView, benchViewport, slotFrame} from '../qumus2/ui/static/stations.js';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.style = {}; this.listeners = {}; this.parentNode = null; this._text = '';
    this.classList = {
      contains: name => (this.attrs.class || '').split(/\s+/).includes(name),
      add: (...names) => this.setAttribute('class', [...new Set([...(this.attrs.class || '').split(/\s+/).filter(Boolean), ...names])].join(' ')),
      remove: (...names) => this.setAttribute('class', (this.attrs.class || '').split(/\s+/).filter(n => !names.includes(n)).join(' ')),
      toggle: (name, force) => {
        if (force ?? !this.classList.contains(name)) this.classList.add(name);
        else this.classList.remove(name);
      },
    };
  }
  set innerHTML(value) { throw new Error('Evidence must use textContent, never innerHTML'); }
  set className(value) { this.setAttribute('class', value); }
  get id() { return this.attrs.id; }
  get textContent() { return this._text+this.children.map(c => c instanceof Element ? c.textContent : c).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  append(...children) {
    for (const child of children) {
      if (child instanceof Element) { child.remove(); child.parentNode = this; }
      this.children.push(child);
    }
  }
  appendChild(child) { this.append(child); return child; }
  prepend(...children) {
    for (const child of children) if (child instanceof Element) { child.remove(); child.parentNode = this; }
    this.children.unshift(...children);
  }
  replaceChildren(...children) {
    for (const child of this.children) if (child instanceof Element) child.parentNode = null;
    this.children = []; this._text = ''; this.append(...children);
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(c => c !== this);
    this.parentNode = null;
  }
  matches(selector) {
    return selector.startsWith('.') ? this.classList.contains(selector.slice(1)) : this.tagName === selector;
  }
  closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector); }
  querySelector(selector) { return walk(this).find(n => n !== this && n.matches(selector)) || null; }
  querySelectorAll(selector) { return walk(this).filter(n => n !== this && n.matches(selector)); }
  click() { this.listeners.click?.({target: this}); }
}
function walk(root) { return [root, ...root.children.filter(c => c instanceof Element).flatMap(walk)]; }
function visibleText(root) {
  const children = root.tagName === 'details' && !root.open
    ? root.children.filter(c => c instanceof Element && c.tagName === 'summary') : root.children;
  return root._text+children.map(c => c instanceof Element ? visibleText(c) : c).join('');
}
globalThis.Node = Element;
globalThis.document = {createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag)};

const evidence = (changes = {}) => ({source: {kind: 'synthetic', reference: 'capture:9'}, frame: 'world',
  observed_at: 100, timebase: 'store_clock', freshness: {status: 'recent', age_s: 0, as_of: 100},
  calibration_id: null, calibration_version: null, uncertainty: null,
  assertion_ids: ['fact_cv'], event_ids: [9], ...changes});
const scene = () => ({schema_version: 1, through_event_id: 10,
  as_of: {value: 100, timebase: 'store_clock', basis: 'event_prefix'},
  source: 'stored_lab_evidence', uses_simulator_truth: false,
  objects: {'obs:fact_cv:0': {kind: 'chip', label: 'chip', x: 40, y: 50, z: null, rotation: null,
    identity_status: 'unidentified', subject: null, evidence: evidence(), attributes: {}}},
  subjects: [{subject: 'carrier_gr_01', pose_key: null, attributes: {material_identity: {
    value: 'Gr', status: 'recorded', evidence: evidence({source: {kind: 'operator_report', reference: 'chat:report',
      instruction_id: 'ins_1'}, frame: 'sample_identity', observed_at: null, timebase: 'unknown',
      freshness: {status: 'unknown', age_s: null, as_of: 100}})}}}],
  unresolved: [{subject: 'legacy_stage', property: 'pose', status: 'conflicted', reason: 'unknown_frame',
    value: [5, 9], evidence: evidence({source: {kind: 'historical_constant', reference: 'synthetic historical fixture'},
      frame: 'v1:arm_left', observed_at: null, timebase: 'unknown',
      freshness: {status: 'unknown', age_s: null, as_of: 100}})}],
  historical_frames: [], counts: {objects: 1, subjects: 1, unresolved: 1},
  omissions: {objects: 0, subjects: 0, unresolved: 0}, complete_reduction: true});

function fixture() {
  const layout = {table: {x: [0, 200], y: [0, 150]}, stations: {
    transfer_stage: {kind: 'microscope', x: 10, y: 10, region: {x: [0, 20], y: [0, 20]}},
  }, slots: {}, transfer_stage: {mechanics_version: 2, load: {x: 0, y: 0}, microscope: {x: 0, y: 30}},
  microscope: {center: {x: 10, y: 40}}, arm_right: {base: {x: 180, y: 30}},
  transfer_arm: {origin: {x: 30, y: 0}, load: {x: 40, y: 10}, over_stage: {x: 0, y: 40}},
  stamp_slide: {size: [40, 25], dome_inset_mm: 7}};
  const truth = {geometry: {mechanics_version: 2}, stages: {transfer_stage: {x: 0, y: 0, z: 0,
    fine_xy_mm: [.2, -.3], rotation: 12}}, arms: {arm_right: {x: 30, y: 60, z: 100, homed: true}},
    manipulator: {x: 40, y: 10, z: 6.5}, microscope: {objective: 20, focus_z: .7},
    objects: {hidden_truth_chip: {kind: 'chip', x: 150, y: 60, size_mm: [10, 10], material: 'secret'},
      hidden_truth_stamp: {kind: 'stamp', x: 170, y: 80, size_mm: [40, 25], status: 'used'}}};
  let ledgerReads = 0;
  const state = {live: false, bench: {layout, devices: [{id: 'arm_right'}], twin: {...truth, observed_scene: scene()}},
    session: {events: [{id: 10}], inv: {known: true, version: 1, samples: new Map(), of: () => [],
      isTarget: () => false, stampStatus: () => { ledgerReads++; return 'torn'; }}, book: {runs: new Map()}}, truth};
  const ctx = {bench: () => state.bench, s: () => state.session, live: () => state.live,
    view: () => state.truth, state: () => state.truth, changed() {}, showRight() {}};
  const section = new Element('section'); section.className = 'view';
  const expand = new Element('button'); expand.className = 'expand-bench';
  const body = new Element(); body.className = 'vbody'; section.append(expand, body);
  const wrap = new Element(), svg = new Element('svg'), legend = new Element(); legend.className = 'map-legend';
  svg.setAttribute('id', 'observed-test');
  const details = new Element(); wrap.append(svg); body.append(wrap, legend, details);
  const view = new LabView(ctx), render = () => view.renderMap(svg, details, new Set());
  render();
  return {state, ctx, view, svg, details, legend, expand, render, ledgerReads: () => ledgerReads};
}
function button(root, text) { return walk(root).find(n => n.tagName === 'button' && n.textContent === text); }

test('an animation tick before the first map mount cannot construct a view without a DOM target', () => {
  const f = fixture(), fresh = new LabView(f.ctx);
  assert.doesNotThrow(() => fresh.drawMap(new Set()));
  assert.equal(fresh.map, null);
});

test('observed points and human attributes update from saved scene, without hidden truth or guessed dimensions', () => {
  const f = fixture(), originalTruth = structuredClone(f.state.truth);
  assert.equal(f.view.mapMode, 'observed');
  assert.deepEqual([...f.view.map.objs.keys()], ['obs:fact_cv:0']);
  assert.equal(f.ledgerReads(), 0);
  assert.equal(f.view.map.gStage.getAttribute('display'), 'none');
  assert.equal(f.view.map.gArm.getAttribute('display'), 'none');
  assert.equal(f.view.map.gHolder.getAttribute('display'), 'none');
  assert.equal(f.view.ov.children.length, 0);
  assert.equal(f.legend.hidden, true);
  assert.equal(f.expand.disabled, true);
  assert.match(f.expand.title, /Observed evidence stays in this map/);
  const marker = f.view.map.objs.get('obs:fact_cv:0');
  assert.equal(marker.r.getAttribute('display'), 'none');
  assert.equal(marker.g.getAttribute('data-identity-status'), 'unidentified');
  assert.match(marker.tip.textContent, /dimensions unknown/);
  assert.match(f.details.textContent, /carrier_gr_01 · unlocated/);
  assert.match(f.details.textContent, /material_identity.*Gr/);
  assert.match(visibleText(f.details), /1 located · 1 named without a position · 1 unresolved/);
  assert.match(visibleText(f.details), /Source: Simulation · frame: world/);
  assert.doesNotMatch(visibleText(f.details), /fact_cv|ins_1|Assertions:|event prefix|store_clock|event_prefix|timebase:/);
  const disclosures = walk(f.details).filter(n => n.tagName === 'details');
  assert.equal(disclosures.length, 3);
  assert.ok(disclosures.every(n => !n.open && n.children[0].textContent === 'Evidence details'));
  assert.match(disclosures[0].textContent, /Assertions: fact_cv.*events: 9.*event prefix 10.*store_clock.*event_prefix/);
  disclosures[0].open = true;
  f.render();
  assert.equal(walk(f.details).find(n => n.tagName === 'details').open, true, 'an unchanged live refresh preserves the disclosure');
  const oldTransform = marker.g.getAttribute('transform');
  const current = f.state.bench.twin.observed_scene;
  current.objects['obs:fact_cv:0'].x = 80;
  current.objects['obs:fact_cv:0'].y = 25;
  current.subjects[0].attributes.material_identity.value = 'hBN';
  f.render();
  assert.notEqual(marker.g.getAttribute('transform'), oldTransform);
  assert.equal(marker.g.getAttribute('transform'), `translate(${f.view.map.sx(80).toFixed(2)} ${f.view.map.sy(25).toFixed(2)})`);
  assert.match(f.details.textContent, /material_identity.*hBN/);
  assert.deepEqual(f.state.truth, originalTruth);
  assert.equal(f.view.map.objs.has('carrier_gr_01'), false, 'a semantic report cannot invent a location');
});

test('adjacent details disclose unresolved conflicts, stale/unknown age, calibration, uncertainty and omissions safely', () => {
  const f = fixture(), current = f.state.bench.twin.observed_scene;
  current.objects['obs:fact_cv:0'].label = '<img src=x onerror=alert(1)>';
  current.unresolved.push({subject: 'aged', property: 'pose', reason: 'stale_observation_time', status: 'recorded',
    evidence: evidence({freshness: {status: 'stale', age_s: 500, as_of: 600},
      uncertainty: {method: 'bounded', units: 'mm', bounds: {x: [-.1, .1]}},
      calibration_id: 'cal-1', calibration_version: 'v3'})});
  current.subjects[0].attributes.material_identity = {value: null, status: 'conflicted',
    reported_values: ['Gr', '<script>alert(1)</script>'], evidence: evidence({frame: 'sample_identity'})};
  current.omissions = {objects: 3, subjects: 2, unresolved: 1, historical_frames: 4};
  current.unresolved[0].conflicting_values = [{value: [5, 9], source: {kind: 'operator_report', reference: 'synthetic report'}, frame: 'fixture:A'},
    {value: [8, 3], source: {kind: 'historical_constant', reference: 'synthetic prior'}, frame: 'fixture:B'}];
  current.unresolved[0].evidence.omitted_assertions = 3;
  f.render();
  for (const text of ['conflicted', 'unknown_frame', 'Source:', 'historical_constant', 'v1:arm_left',
    'timebase: unknown', 'age: unknown', 'Calibration: unknown', 'uncertainty: unknown', 'Freshness: stale',
    '500.0 s', 'cal-1', 'bounded', 'Assertions: fact_cv', 'events: 9', '3 objects', '2 subjects',
    '4 original coordinate frames', 'Conflicting alternatives', 'fixture:A', 'fixture:B', '[8,3]',
    '3 additional assertions omitted from these details',
    '<script>alert(1)</script>']) assert.ok(f.details.textContent.includes(text), text);
  assert.ok(f.svg.textContent.includes('<img src=x onerror=alert(1)>'));
  assert.equal(walk(f.details).some(n => n.tagName === 'script' || n.tagName === 'img'), false);
});

test('earlier playback prefixes reject first-frame future scenes and absence never falls back to truth', () => {
  const f = fixture();
  f.state.session.events = [{id: 4}]; f.render();
  assert.equal(recordedObservedScene(f.ctx).scene, null);
  assert.equal(f.view.map.objs.size, 0);
  assert.match(f.details.textContent, /at this playback point.*0 observed objects/);
  f.state.session.events = []; f.render();
  assert.equal(f.view.map.objs.size, 0);
  f.state.session.events = [{id: 10}]; f.view.drawMap(new Set());
  assert.equal(f.view.map.objs.size, 1);
  delete f.state.bench.twin.observed_scene; f.view.drawMap(new Set());
  assert.equal(f.view.map.objs.size, 0);
  assert.match(f.details.textContent, /No observed scene recorded at this point.*0 observed objects/);
  assert.equal(f.state.truth.objects.hidden_truth_chip.x, 150);
});

test('diagnostics toggle remains separate, restores current mechanics and cannot contaminate observed evidence', () => {
  const f = fixture(), saved = structuredClone(f.state.bench.twin.observed_scene);
  button(f.details, 'Simulation diagnostics').click();
  assert.equal(f.view.mapMode, 'diagnostics');
  assert.equal(f.legend.hidden, false);
  assert.equal(f.expand.disabled, false);
  assert.equal(f.view.map.objs.has('hidden_truth_chip'), true);
  assert.equal(f.view.map.objs.has('obs:fact_cv:0'), false);
  assert.notEqual(f.view.map.gStage.getAttribute('display'), 'none');
  assert.ok(f.view.ov.children.length > 0);
  assert.match(f.details.textContent, /same simulated state as Progress.*laboratory positions remain uncalibrated/);
  assert.match(f.view.map.plateTip.textContent, /fine XY.*rotation.*Z is mechanical height/);
  assert.match(f.view.map.objTip.textContent, /independent Z focus 0.7 mm/);
  assert.match(f.view.map.plate.getAttribute('transform'), /translate\(0.200 0.300\) rotate\(-12/);
  const reads = f.ledgerReads();
  button(f.details, 'Observed evidence').click();
  assert.deepEqual([...f.view.map.objs.keys()], ['obs:fact_cv:0']);
  assert.equal(f.view.ov.children.length, 0);
  assert.equal(f.ledgerReads(), reads);
  assert.deepEqual(f.state.bench.twin.observed_scene, saved);
});

test('virtual Lab map defaults to the exact Progress state while evidence stays explicitly selectable', () => {
  const f = fixture(), originalScene = structuredClone(f.state.bench.twin.observed_scene);
  f.state.bench.sim = {mode: 'scripted'};
  const virtual = new LabView(f.ctx);
  virtual.renderMap(f.svg, f.details, new Set());
  assert.equal(virtual.mapMode, 'diagnostics');
  assert.deepEqual([...virtual.map.objs.keys()], Object.keys(f.state.truth.objects));
  assert.equal(button(f.details, 'Simulation diagnostics').getAttribute('aria-pressed'), 'true');
  const progress = new StationBenchView(new Element('svg'), f.state.bench.layout);
  progress.render(f.state.truth);
  assert.equal(virtual.map.svg.getAttribute('viewBox'), progress.svg.getAttribute('viewBox'));
  for (const [id, object] of progress.objs) {
    assert.deepEqual(virtual.map.objs.get(id).r.attrs, object.r.attrs);
    assert.equal(virtual.map.objs.get(id).g.getAttribute('transform'), object.g.getAttribute('transform'));
  }
  f.state.truth.objects.hidden_truth_chip.x += 31;
  virtual.drawMap(new Set()); progress.render(f.state.truth);
  assert.equal(virtual.map.objs.get('hidden_truth_chip').g.getAttribute('transform'),
    progress.objs.get('hidden_truth_chip').g.getAttribute('transform'));
  button(f.details, 'Observed evidence').click();
  assert.deepEqual([...virtual.map.objs.keys()], ['obs:fact_cv:0']);
  assert.deepEqual(f.state.bench.twin.observed_scene, originalScene);
});

test('physical object dimensions and coordinate distances retain one mm scale on large tables', () => {
  const f = fixture(), layout = structuredClone(f.state.bench.layout);
  layout.table = {x: [0, 1440], y: [0, 630]};
  layout.slots = {small: {x: 400, y: 200, station: 'transfer_stage', holds: 'chip', size: [2.5, 10]}};
  const map = new StationBenchView(new Element('svg'), layout);
  map.render({objects: {
    chip: {kind: 'chip', x: 100, y: 200, size_mm: [10, 10]},
    device: {kind: 'device', x: 130, y: 240, size_mm: [2.5, 10]},
    slide: {kind: 'stamp', x: 200, y: 200, size_mm: [40, 25]},
  }});
  for (const [id, size] of [['chip', [10, 10]], ['device', [2.5, 10]], ['slide', [40, 25]]]) {
    assert.equal(+map.objs.get(id).r.getAttribute('width'), size[0]);
    assert.equal(+map.objs.get(id).r.getAttribute('height'), size[1]);
  }
  const slot = walk(map.svg).find(node => node.classList.contains('s-slot'));
  assert.ok(Math.abs(+slot.getAttribute('width') - (2.5 + 2.4)) < 1e-9, 'slot retains its declared 1.2 mm frame border');
  assert.ok(Math.abs(+slot.getAttribute('height') - (10 + 2.4)) < 1e-9);
  assert.equal(Math.hypot(map.sx(130)-map.sx(100), map.sy(240)-map.sy(200)), 50);
  layout.table.x[1] = 2880;
  const larger = new StationBenchView(new Element('svg'), layout);
  larger.render({objects: {chip: {kind: 'chip', x: 100, y: 200, size_mm: [10, 10]}}});
  assert.equal(+larger.objs.get('chip').r.getAttribute('width'), 10, 'changing table extent cannot grow a chip');
});

test('linked zero-position annotations fit both identical maps without moving geometry or rescaling mm', () => {
  const layout = structuredClone(fixture().state.bench.layout);
  layout.table = {x: [0, 1648], y: [0, 630]};
  layout.arm_left = {base: {x: 320, y: 0}};
  layout.arm_right = {base: {x: 840, y: 0}};
  layout.arm_reactor = {base: {x: 1288, y: 97.5}};
  const before = structuredClone(layout);
  const maps = [new StationBenchView(new Element('svg'), layout), new StationBenchView(new Element('svg'), layout)];
  assert.deepEqual(layout, before);
  assert.equal(maps[0].svg.getAttribute('viewBox'), maps[1].svg.getAttribute('viewBox'));
  // the search & stacking frame reaches 41 mm left of the table; base squares and captions do not change the scale
  assert.equal(maps[0].k, (1689 + 36) / 360, 'annotation scale is fixed before annotation padding');
  for (const map of maps) {
    const bases = walk(map.svg).filter(n => n.tagName === 'rect' && n.classList.contains('s-base'));
    assert.equal(bases.length, 3);
    const table = walk(map.svg).find(n => n.classList.contains('s-table'));
    const [tx, ty, tw, th] = ['x', 'y', 'width', 'height'].map(a => +table.getAttribute(a));
    for (const base of bases) {
      const [x, y, w, h] = ['x', 'y', 'width', 'height'].map(a => +base.getAttribute(a));
      const halfStroke = +base.getAttribute('style').match(/stroke-width:([\d.]+)/)[1] / 2;
      assert.equal(w, h, 'an arm base is a square');
      assert.ok(+base.getAttribute('rx') > 0, 'with rounded corners');
      assert.ok(x-halfStroke >= tx && x+w+halfStroke <= tx+tw, 'inside the drawn table');
      assert.ok(y-halfStroke >= ty && y+h+halfStroke <= ty+th, 'inside the drawn table');
      assert.match(base.textContent, /base\/zero position marker.*footprint uncalibrated/);
    }
    for (const part of walk(map.svg).filter(n => n.tagName === 'rect'
        && ['s-slot', 's-region', 's-qr'].some(c => n.classList.contains(c)))) {
      const [x, y, w, h] = ['x', 'y', 'width', 'height'].map(a => +part.getAttribute(a));
      assert.ok(x >= tx && x+w <= tx+tw && y >= ty && y+h <= ty+th, `${part.getAttribute('class')} inside the drawn table`);
    }
    assert.equal(Math.hypot(map.sx(130)-map.sx(100), map.sy(240)-map.sy(200)), 50);
  }
});

test('viewport fits bases beyond the table while planned markers do not enlarge historical views', () => {
  const cfg = {table: {x: [0, 1648], y: [0, 630]}, stations: {},
    arm_left: {base: {x: -10, y: 650}}, arm_right: {base: {x: 1660, y: -5}},
    arm_reactor: {planned: true, base: {x: -9999, y: -9999}}};
  const fitted = benchViewport(cfg);
  assert.equal(fitted.k, (1670+36)/360);
  for (const p of [cfg.arm_left.base, cfg.arm_right.base]) {
    assert.ok(p.x-fitted.b.x0 > 9*fitted.k && fitted.b.x1-p.x > 9*fitted.k);
    assert.ok(p.y-fitted.b.y0 > 9*fitted.k && fitted.b.y1-p.y > 9*fitted.k);
  }
  const historical = benchViewport({table: {x: [0, 200], y: [0, 150]}, stations: {},
    arm_right: {base: {x: 180, y: 30}}});
  assert.deepEqual(historical, {b: {x0: -16, x1: 216, y0: -16, y1: 166}, w: 232, h: 182, k: 1,
    table: {x: [-12, 212], y: [-12, 162]}});
  for (const p of [cfg.arm_left.base, cfg.arm_right.base]) {
    assert.ok(p.x-fitted.table.x[0] > 9*fitted.k && fitted.table.x[1]-p.x > 9*fitted.k, 'base square on the drawn table');
    assert.ok(p.y-fitted.table.y[0] > 9*fitted.k && fitted.table.y[1]-p.y > 9*fitted.k, 'base square on the drawn table');
  }
});

test('historical coordinates stay point-like in their original frame with no fabricated apparatus or transform', () => {
  const f = fixture(), prior = {id: 'v1:arm_left', label: 'v1:arm_left', bounds: {x: [-20, 20], y: [-10, 30]},
    count: 1, omitted_objects: 0, objects: {'prior:historical:0': {kind: 'prior', label: 'old waypoint',
      x: -3, y: 9, z: null, rotation: null, identity_status: 'historical', subject: 'waypoint',
      original_coordinates: [-3, 9, 4], linear_mm: 4,
      evidence: evidence({source: {kind: 'historical_constant', reference: 'synthetic original-frame fixture'},
        frame: 'v1:arm_left', observed_at: null, timebase: 'unknown',
        freshness: {status: 'unknown', age_s: null, as_of: 100}})}}};
  f.state.bench.twin.observed_scene.historical_frames = [prior];
  f.state.bench.twin.observed_scene.omissions.objects = 7;
  f.render();
  const select = walk(f.details).find(n => n.tagName === 'select');
  select.listeners.change({target: {value: prior.id}});
  assert.deepEqual(f.view.map.cfg.stations, {});
  assert.deepEqual(f.view.map.cfg.table, prior.bounds);
  assert.deepEqual([...f.view.map.objs.keys()], ['prior:historical:0']);
  assert.equal(f.view.map.plate, undefined);
  assert.equal(f.view.map.holder, undefined);
  assert.match(f.details.textContent, /original-frame prior geometry.*Original coordinates only/);
  assert.match(f.details.textContent, /Age and calibration unknown; no world transform or motion authority/);
  assert.match(f.details.textContent, /x -3, y 9, z unknown/);
  assert.match(f.details.textContent, /Original coordinates: \[-3,9,4\]/);
  assert.match(f.details.textContent, /Source linear axis: 4 mm; this is not sample Z/);
  assert.doesNotMatch(f.details.textContent, /Omitted from details: 7 objects/);
  assert.equal(f.view.map.objs.get('prior:historical:0').g.getAttribute('data-identity-status'), 'historical');
  walk(f.details).find(n => n.tagName === 'select').listeners.change({target: {value: 'world'}});
  assert.equal(f.view.map.cfg, f.state.bench.layout);
  assert.deepEqual([...f.view.map.objs.keys()], ['obs:fact_cv:0']);
});

test('a drawn board frame and comb notch come from the layout without changing the held object size', () => {
  const stamp = {x: 1051, y: 111, station: 'right_storage', holds: 'stamp', size: [40, 25], frame: [84, 28], frame_offset: [15, 0]};
  assert.deepEqual(slotFrame(stamp), {x: [1024, 1108], y: [97, 125]});
  assert.deepEqual(slotFrame({x: 3, y: 158, holds: 'chip', size: [10, 10], frame: [25, 35]}), {x: [-9.5, 15.5], y: [140.5, 175.5]});
  const layout = structuredClone(fixture().state.bench.layout);
  layout.stations.right_storage = {kind: 'storage', region: {x: [1024, 1184], y: [-4, 227]},
    notches: [{x: [1024, 1053], y: [101.75, 120.25]}]};
  layout.slots = {right_stamp_2: stamp};
  const map = new StationBenchView(new Element('svg'), layout);
  const notch = walk(map.svg).find(n => n.classList.contains('s-notch'));
  assert.ok(+notch.getAttribute('width') > 29, 'the gap covers the board edge line: open to the left');
  const edge = walk(map.svg).find(n => n.classList.contains('s-notch-edge'));
  assert.match(edge.getAttribute('d'), /^M[\d.-]+ [\d.-]+H[\d.-]+V[\d.-]+H[\d.-]+$/, 'outline on three sides only');
  const slot = walk(map.svg).find(n => n.classList.contains('s-slot'));
  assert.equal(+slot.getAttribute('width'), 84);
  assert.ok(map.table.x[0] <= 1024 && map.table.y[0] <= -4, 'the drawn table holds the whole board');
});
