// Frozen inspection previews use the same renderer as the Overview camera.
// Original pixels remain separately accessible; recorded evidence is never edited.
import {h} from './ui.js';
import {OverviewDiagram} from './overview.js';
import {fromBench, applyDelta} from './labstate.js';

export function inspectionPreview(replay, result, photo) {
  if (!replay || result.payload?.tool !== 'inspect_bench') return null;
  const images = result.payload.images || [];
  if (!images.length) return null;
  // Use the image-bearing observation event, not the time the UI opens the result.
  const observed = replay.events.find(e => e.id <= result.id && e.kind === 'step.verified' &&
    (e.payload?.images || []).some(src => images.includes(src))) || result;
  const initial = replay.frameAt(0);
  if (!initial || initial.id > observed.id || !initial.bench?.twin) return null;
  const state = fromBench(initial.bench, replay.rec.layout || {});
  for (const e of replay.events) {
    if (e.id > observed.id) break;
    if (e.id > initial.id && e.kind === 'lab.frame') applyDelta(state, e.payload || {});
  }
  const diagram = new OverviewDiagram(replay.rec.layout || {});
  diagram.update(state);
  const frozen = diagram.image();
  diagram.root.onclick = ev => { ev.stopPropagation(); photo(frozen); };
  return h('figure', {class: 'inspection-preview', dataset: {event: observed.id}}, diagram.root,
    h('figcaption', {text: `Simulated saved state · inspection event ${observed.id}. Same view as Overview at this time; original camera evidence below.`}),
    h('div', {class: 'inspection-originals'}, images.map((src, i) => h('button', {class: 'lnk-btn',
      text: `Original captured image ${i + 1} ↗`, onclick: ev => {ev.stopPropagation(); photo(src);}}))));
}
