// The team as a small organisation chart: Qumus (the orchestrator) on top, a
// stem down to a rail, one drop per subagent. No two lines cross and none
// runs through a circle or a name. Whoever works lights up; a link carries a
// moving dot while Qumus has handed that agent a task. Hover for the task.

import { svg, label, ROLES } from './ui.js';

const SUBS = ['advisor', 'eyes', 'hands', 'picker', 'designer', 'reactor'];
const W = 400, H = 100;
const TOP = { x: W / 2, y: 17, r: 13 };   // Qumus
const RAIL_Y = 47, SUB_Y = 71, SUB_R = 11, CORNER = 7;

// Stem → rail → drop, with rounded corners; a straight line under Qumus.
function linkPath(x) {
  const x0 = TOP.x, y0 = TOP.y + TOP.r + 1, y1 = SUB_Y - SUB_R - 5.5;
  if (Math.abs(x - x0) < 1) return `M${x0} ${y0}L${x} ${y1}`;
  const s = Math.sign(x - x0), c = CORNER;
  return `M${x0} ${y0}L${x0} ${RAIL_Y - c}Q${x0} ${RAIL_Y} ${x0 + s * c} ${RAIL_Y}` +
    `L${x - s * c} ${RAIL_Y}Q${x} ${RAIL_Y} ${x} ${RAIL_Y + c}L${x} ${y1}`;
}

export class TeamGraph {
  constructor(host) {
    this.host = host;
    this.root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'team', role: 'img', 'aria-label': 'Qumus and its team' });
    host.replaceChildren(this.root);
    this.nodes = {}; this.links = {};
    // Neutral paths must never paint over an active path's shared stem/rail.
    const gLinks = svg('g', { class: 't-base-links' }, this.root);
    const gActive = svg('g', { class: 't-active-links' }, this.root);
    const gNodes = svg('g', {}, this.root);
    SUBS.forEach((a, i) => {
      const x = 44 + i * ((W - 88) / (SUBS.length - 1));
      const d = linkPath(x);
      svg('path', { d, class: 't-path' }, gLinks);
      const g = svg('g', { class: 't-link', 'data-a': a }, gActive);
      svg('path', { d, class: 't-path t-highlight' }, g);
      const dot = svg('circle', { r: 2.6, class: 't-dot' }, g);
      const mo = svg('animateMotion', { dur: '1.4s', repeatCount: 'indefinite', path: d, begin: 'indefinite' }, dot);
      this.links[a] = { g, mo };
      this.nodes[a] = this.node(gNodes, a, x, SUB_Y, SUB_R);
    });
    this.nodes.orchestrator = this.node(gNodes, 'orchestrator', TOP.x, TOP.y, TOP.r);
    this.state = {};
    this.set({ busy: false, tasks: {} });
  }

  node(parent, a, x, y, r) {
    const g = svg('g', { class: 't-node', 'data-a': a, transform: `translate(${x} ${y})`, tabindex: 0 }, parent);
    const tip = svg('title', {}, g);
    svg('circle', { r: r + 4.5, class: 't-ring' }, g);
    svg('circle', { r, class: 't-disc' }, g);
    const t = svg('text', { class: 't-mono', 'text-anchor': 'middle', y: 3.6 }, g);
    t.textContent = a === 'orchestrator' ? 'Q' : label(a)[0];
    const name = svg('text', { class: 't-name' + (a === 'orchestrator' ? ' lead' : ''),
      'text-anchor': a === 'orchestrator' ? 'start' : 'middle', x: a === 'orchestrator' ? r + 8 : 0, y: a === 'orchestrator' ? 4 : r + 13.5 }, g);
    name.textContent = label(a);
    return { g, tip };
  }

  // state: {busy, tasks: {agent: task text}, thinking: agent working right now}
  set({ busy, tasks, thinking }) {
    const key = JSON.stringify([busy, tasks, thinking]);
    if (key === this._key) return;
    this._key = key;
    const working = new Set(Object.keys(tasks || {}));
    for (const a of SUBS) {
      const on = working.has(a), n = this.nodes[a], l = this.links[a];
      n.g.classList.toggle('active', on);
      n.g.classList.toggle('speaking', thinking === a);
      n.tip.textContent = `${label(a)} · ${ROLES[a]}` + (on ? `\nWorking on: ${tasks[a]}` : '\nIdle');
      if (on !== l.g.classList.contains('active')) {
        l.g.classList.toggle('active', on);
        if (on) l.mo.beginElement(); else l.mo.endElement();
      }
    }
    const q = this.nodes.orchestrator;
    q.g.classList.toggle('active', !!busy);
    q.g.classList.toggle('speaking', !!busy && (!thinking || thinking === 'orchestrator'));
    q.tip.textContent = `Qumus · ${ROLES.orchestrator}` + (busy ? (working.size ? `\nWaiting for ${[...working].map(label).join(', ')}` : '\nThinking') : '\nIdle');
  }
}
