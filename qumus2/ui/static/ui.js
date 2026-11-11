// Small shared helpers for the front end: DOM builder, names, formatting and
// a safe Markdown subset (the agents write Markdown; nothing is parsed as HTML).

export const $ = id => document.getElementById(id);

export function h(tag, props = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(n.dataset, v);
    else if (k === 'style' && typeof v === 'object') Object.assign(n.style, v);
    else n.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null && c !== false) n.append(c instanceof Node ? c : String(c));
  return n;
}

const SVGNS = 'http://www.w3.org/2000/svg';
export function svg(tag, attrs = {}, parent = null) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

export const api = (path, opts) => fetch(path, opts).then(async r => {
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : String(r.status));
  }
  return r.json();
});
export const costText = (amount, basis) => `$${(+amount || 0).toFixed(2)}${basis === 'configured_token_rates' ? ' est.' : ''}`;
export const modelLabel = m => m.models?.length > 1 ? 'Multiple models' : m.models?.[0] || m.model || (m.provider === 'openai' ? 'OpenAI' : 'Claude');
export const post = (path, body) => api(path, { method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify(body || {}) });

// The orchestrator is "Qumus" on the page; agent ids (C10) stay as they are.
export const AGENTS = ['orchestrator', 'advisor', 'eyes', 'hands', 'picker', 'designer', 'reactor'];
const LABELS = { orchestrator: 'Qumus', advisor: 'Advisor', eyes: 'Eyes', hands: 'Hands', picker: 'Picker',
  designer: 'Designer', reactor: 'Reactor', user: 'You', system: 'System' };
const MONO = { orchestrator: 'Q', advisor: 'A', eyes: 'E', hands: 'H', picker: 'P', designer: 'D', reactor: 'R', user: 'Y', system: '·' };
export const ROLES = { orchestrator: 'Plans and leads', advisor: 'Device Guidance', eyes: 'Looks and checks', hands: 'Runs the hardware',
  picker: 'Chooses flakes', designer: 'Draws the layout', reactor: 'Controls the virtual reactor' };
export const label = a => LABELS[a] || a || 'System';
export const avatar = (a, active) => h('span', { class: 'ag' + (active ? ' active' : ''), dataset: { a: a || 'system' },
  title: label(a), text: a === 'orchestrator' ? '' : MONO[a] || (a || '?')[0].toUpperCase() });

export const FLOW_LABELS = { place_chip_on_transfer_stage: 'Place chip on the stage', exfoliate_chips: 'Exfoliate onto chips',
  search_chip: 'Search a chip for flakes', revisit_chip: 'Revisit flakes on a chip', stack_device: 'Stack a device',
  inspect_bench: 'Inspect the bench',
  reactor_handover_in: 'Person puts a chip into the reactor tray', reactor_load: 'Load the chip into the crucible',
  reactor_heat: 'Run a furnace program', reactor_snapshot: 'Reactor microscope picture',
  reactor_record: 'Record the reactor microscope', reactor_unload: 'Unload the chip to the tray',
  reactor_handover_out: 'Person takes the chip from the reactor tray', reactor_stop: 'Stop the reactor' };
export const flowLabel = f => FLOW_LABELS[f] || (f || 'job').replace(/_/g, ' ');
// a job in words, with what it works on: "Search chip_001 for flakes"
export function jobTitle(flow, params) {
  const p = params || {};
  switch (flow) {
    case 'search_chip': return `Search ${p.chip_id || 'a chip'} for flakes`;
    case 'revisit_chip': return p.chip_id ? `Revisit flakes on ${p.chip_id}` : flowLabel(flow);
    case 'place_chip_on_transfer_stage': return p.chip_id ? `Place ${p.chip_id} on the stage` : flowLabel(flow);
    case 'exfoliate_chips': return `Exfoliate ${p.material || ''} onto ${(p.chip_ids || []).length ? p.chip_ids.join(', ') : 'chips'}`.replace('  ', ' ');
    case 'stack_device': return p.target_chip_id ? `Stack a device on ${p.target_chip_id}` : flowLabel(flow);
    default: return flowLabel(flow);
  }
}
export const RUN_STATUS = { queued: 'queued', running: 'running', paused: 'paused', waiting_human: 'needs a person', parked: 'parked',
  succeeded: 'succeeded', failed: 'failed', cancelled: 'cancelled' };
export const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'parked', 'waiting_human', 'paused']);
export const VERDICT = { ok: 'in tolerance', correctable: 'correctable', unclear: 'unclear', dangerous: 'dangerous' };

export const fmt = (x, d = 2) => (typeof x === 'number' ? x.toFixed(d) : x);
const rnd = (k, v) => (typeof v === 'number' && !Number.isInteger(v) ? +v.toFixed(3) : v);
export const short = (o, n = 160) => {
  const s = typeof o === 'string' ? o : JSON.stringify(o, rnd);
  return s && s.length > n ? s.slice(0, n) + '…' : s;
};
export const stepName = s => String(s || '').replace(/_/g, ' ');
export function fmtDur(s) {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min ${s % 60 ? `${s % 60} s` : ''}`.trim() : `${Math.floor(m / 60)} h ${m % 60} min`;
}

// Where a sample is, in words: storage slots read "middle board, slot 1" (the id
// stays in small print next to it where records are checked); instruments by
// their names. null: the location is a sample (a chip, a stamp), say its id.
const BOARDS = { left: 'left board', mid: 'middle board', right: 'right board' };
const PLACES = { transfer_stage: 'microscope stage', exfoliation_stage: 'exfoliation stage', stamp_handoff: 'stamp hand-off',
  transfer_arm: 'stamp holder', arm_left: 'Arm 1', arm_right: 'Arm 2', exfoliator: 'exfoliator' };
// tape left on the roll; a testbench set up with an endless roll (the v1/v2
// comparison trials) says so
export const tapeText = mm => (mm > 1e6 ? 'unlimited (testbench)' : `${Math.round(mm).toLocaleString()} mm`);

export function placeName(loc) {
  const m = /^(left|mid|right)_(chip|slide|stamp)_(\d+)$/.exec(String(loc || ''));
  if (m) return `${BOARDS[m[1]]}, ${m[2] === 'chip' ? '' : `${m[2]} `}slot ${m[3]}`;
  return PLACES[loc] || null;
}

// Sample ids in text become links (redesign item 7): a flake opens in the flake
// library, a chip or device on the Lab page. Only text nodes are touched (the
// agents' Markdown is never parsed as HTML); `known(id)` keeps ids the ledger
// does not have as plain text.
export const SAMPLE_ID = /\b(chip_e?\d{2,3}_f\d{3}|chip_e?\d{2,3}|device_\d{3}|stamp_\d{2})\b/g;
export function linkIds(root, open, known = () => true) {
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: n => (n.parentElement && n.parentElement.closest('a, button, .idlink') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT) });
  const hits = [];
  for (let n = walk.nextNode(); n; n = walk.nextNode()) if (n.nodeValue.search(SAMPLE_ID) >= 0) hits.push(n);
  for (const n of hits) {
    const parts = n.nodeValue.split(SAMPLE_ID);
    if (!parts.some((x, i) => i % 2 && known(x))) continue;
    n.replaceWith(...parts.map((x, i) => (i % 2 && known(x) ? h('span', { class: 'idlink', role: 'link', tabindex: '0', text: x, title: `Open ${x}`,
      onclick: ev => { ev.stopPropagation(); open(x); }, onkeydown: ev => { if (ev.key === 'Enter') { ev.stopPropagation(); open(x); } } }) : x)).filter(x => x !== ''));
  }
  return root;
}

export const imgUrl = p => (/^(\/api\/|data:image\/svg\+xml;)/.test(String(p)) ? p : '/api/image?path=' + encodeURIComponent(p));
// A recording keeps its photos as media/<file>; the replay server serves the
// one it was started with at /api/image. Any recording fetched by name (History
// plays and shows it in the page) gets its photo paths pointed at
// /api/recordings/<name>/media/<file> instead.
export async function fetchRecording(url, name) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} for ${url}`);
  let text = await r.text();
  if (name) text = text.split('"media/').join(`"/api/recordings/${encodeURIComponent(name)}/media/`);
  return JSON.parse(text);
}
// which camera took a testbench picture, from its file name (overview_camera_000012.png)
export function imageSource(p) {
  const m = /([a-z_]+?)_(\d+|live)\.\w+$/.exec(String(p || '').split(/[/=]/).pop() || '');
  const name = m ? m[1] : 'camera';
  return { camera_left: 'Left desktop camera', camera_right: 'Right desktop camera', microscope: 'Microscope',
    overview_camera: 'Overview camera', overview: 'Overview camera' }[name] || name.replace(/_/g, ' ');
}
export const isMicrograph = p => /microscope_\d+\.\w+$/.test(String(p || ''));

// ---------------------------------------------------------------- markdown
// The agents write Markdown (tables, lists, code, links); markdown.js builds
// DOM nodes and never parses their text as HTML.
export { markdown } from './markdown.js';

let toastT;
export function toast(t) {
  const n = $('toast');
  n.textContent = t; n.classList.add('show');
  clearTimeout(toastT); toastT = setTimeout(() => n.classList.remove('show'), 3200);
}
