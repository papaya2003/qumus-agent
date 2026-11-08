// Lab actions: readable names for flow steps, what a step's check says, and
// the caption in the title bar that says which instrument does what right
// now (a replay). The caption reads the step state the page builds from the
// events it has shown (app.js `S.steps`), so it is right after a seek too.
// The rows themselves are the progress tree's third level (tree.js).

import { h, flowLabel } from './ui.js';

// "place_chip_002_on_transfer_stage" -> "Place chip_002 on transfer stage":
// ids (chip_002, chip_001_f002, stamp_01, chip_e01) keep their underscores.
export function actionText(name) {
  const ids = [];
  const s = String(name || '').replace(/(?<![a-z0-9])(?:chip|stamp|device)_[a-z]?\d+(?:_f\d+)?(?![a-z0-9])/g, m => { ids.push(m); return `\u0000${ids.length - 1}\u0000`; })
    .replace(/_/g, ' ').replace(/\u0000(\d+)\u0000/g, (_, i) => ids[+i]);
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// What a step's check says, for a person: [css class, short text, longer title].
export function checkState(st) {
  if (!st) return ['todo', '', 'Not started yet'];
  const fixes = st.corrections || 0;
  const fixed = fixes ? ` after ${fixes} correction${fixes > 1 ? 's' : ''}` : '';
  if (st.status === 'failed') return ['bad', 'failed', 'The step failed'];
  if (st.status === 'skipped') return ['skip', 'skipped', 'The step was skipped'];
  if (st.status === 'in_progress') {
    if (st.verdict === 'correctable') return ['fix', 'off target · correcting', 'The check found it off target; the step corrects it'];
    if (st.verdict === 'ok') return ['ok', 'check ✓', 'The check passed; finishing'];
    return ['going', 'running', 'The step runs; its check comes at the end'];
  }
  if (st.verdict === 'not_verifiable') return ['skip', 'no check', 'This step cannot be checked by a camera or sensor'];
  if (fixes) return ['fix', `✓${fixed}`, `Checked: in tolerance${fixed}`];
  if (st.verdict === 'ok') return ['ok', 'check ✓', 'Checked: in tolerance'];
  return ['ok', 'done', 'Done (no separate check)'];
}

// The one-line caption of the current lab action into `box`: { st, info,
// device } (info: a Replay step) or { idle: text }. Kept here so the line can
// move elsewhere on the page with no change to how it is built.
export function renderCaption(box, { st, info, device, idle }) {
  if (!info) { box.replaceChildren(h('span', { class: 'bc-idle', text: idle || '' })); return; }
  const [cls, text, title] = checkState(st);
  const [k, n] = info.pos;
  box.replaceChildren(
    ...(device ? [h('b', { class: 'bc-dev', text: device })] : []),
    h('span', { class: 'bc-act', text: actionText(info.name), title: info.name }),
    h('span', { class: `bc-check ${cls}`, text, title }),
    h('span', { class: 'bc-where', text: `${flowLabel(info.flow)} · step ${k}${n ? ` of ${n}` : ''}` }));
}

const ICON = { todo: '○', going: '▶', ok: '✓', fix: '↻', bad: '✗', skip: '–' };
