// Offline CameraCard behavior: no model calls, hardware, network or timers.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CameraCard } from '../qumus2/ui/static/camera.js';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.hidden = false;
    this.complete = false; this.naturalWidth = 0;
    this.classes = new Set();
    this.classList = {
      add: (...values) => values.forEach(v => this.classes.add(v)),
      remove: (...values) => values.forEach(v => this.classes.delete(v)),
      contains: value => this.classes.has(value),
      toggle: (value, force) => {
        if (force ?? !this.classes.has(value)) this.classes.add(value);
        else this.classes.delete(value);
      },
    };
  }
  set className(value) { this.classes = new Set(value.split(/\s+/)); }
  setAttribute(key, value) { this[key] = value; }
  removeAttribute(key) { delete this[key]; }
  addEventListener(key, fn) { this[`on${key}`] = fn; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  get lastChild() { return this.children.at(-1); }
}

const roots = new Map();
globalThis.Node = Element;
globalThis.document = {
  createElement: tag => new Element(tag),
  getElementById: id => {
    if (!roots.has(id)) roots.set(id, new Element());
    return roots.get(id);
  },
};

function card() { return new CameraCard({ replay: true }); }
function finishLoad(c) {
  const frame = c.frames.microscope;
  const img = frame.imgs.find(i => i.dataset.url === frame.request);
  img?.onload?.();
}
const field = (image, objective = 10, focus_z = .1) => ({ image, objective, focus_z,
  focus_plane: 'chip', purpose: 'preview', timestamp: 100, source: 'synthetic', verification: false });

test('blank and populated previews arrive only with the playback prefix, with their own objective/focus', () => {
  const c = card();
  assert.equal(c.cur.microscope, null);
  const events = [field('media/microscope_000001.png'), field('media/microscope_000002.png', 20, .5)];
  c.notePreview(events[0]); finishLoad(c);
  assert.equal(c.cur.microscope, events[0].image);
  assert.equal(c.latest.microscope, null, 'display preview must not become captured evidence');
  assert.equal(c.frames.microscope.tag.lastChild.textContent, '10× saved preview');
  assert.match(c.frames.microscope.capText.textContent, /focus 0.100 mm/);
  assert.match(c.frames.microscope.capText.textContent, /not a verification/);
  c.notePreview(events[1]);
  assert.match(c.frames.microscope.tag.lastChild.textContent, /^10×/, 'unloaded future pixels retain old capture labels');
  finishLoad(c);
  assert.equal(c.frames.microscope.tag.lastChild.textContent, '20× saved preview');
  assert.match(c.frames.microscope.capText.textContent, /focus 0.500 mm/);
  c.reset();
  assert.equal(c.cur.microscope, null);
  assert.equal(c.frames.microscope.tag.lastChild.textContent, '');
  c.notePreview(events[0]); finishLoad(c);
  assert.equal(c.cur.microscope, events[0].image);
  assert.doesNotMatch(c.frames.microscope.capText.textContent, /0.500/);
});

test('a seek discards late loads, unavailable fields clear stale pictures and historical captures retain photo labels', () => {
  const c = card();
  c.notePreview(field('media/microscope_000010.png', 100));
  const stale = c.frames.microscope.imgs.find(i => i.onload)?.onload;
  c.reset();
  c.notePreview(field('media/microscope_000001.png'));
  stale();
  assert.equal(c.cur.microscope, null);
  finishLoad(c);
  assert.equal(c.cur.microscope, 'media/microscope_000001.png');
  c.notePreview({ ...field(null), purpose: 'analysis' });
  assert.equal(c.cur.microscope, null);
  assert.match(c.frames.microscope.empty.textContent, /unavailable at this time/);
  c.noteImage('media/microscope_000003.png', 20); finishLoad(c);
  assert.equal(c.frames.microscope.tag.lastChild.textContent, '20× photo');
  assert.doesNotMatch(c.frames.microscope.capText.textContent, /preview|verification|focus/);
});
