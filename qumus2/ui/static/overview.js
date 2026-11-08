// A simple display of recorded state, not a replacement for the saved camera evidence.
import { svg } from './ui.js';

export class OverviewDiagram {
  constructor(layout) {
    this.layout = layout;
    this.root = svg('svg', { width: 1100, height: 650, viewBox: '0 0 1100 650', class: 'overview-diagram', role: 'img', 'aria-label': 'Simulated overview reconstructed from recorded state' });
    svg('rect', { width: 1100, height: 650, fill: '#f8fafc' }, this.root);
    this.text('Overview camera · recorded state diagram', 28, 34, 24).classList.add('diagram-heading');
    this.text('SIMULATED', 1065, 34, 22, 'end').classList.add('diagram-heading');
    svg('path', { d: 'M28 62H1072M28 584H1072', stroke: '#cad3df' }, this.root);
    const labels = { left_storage: 'Left storage', middle_storage: 'Middle storage', right_storage: 'Right storage', exfoliation_stage: 'Exfoliation', transfer_stage: 'Search / stacking', reactor_storage: 'Reactor storage', reactor: 'vdW reactor' };
    for (const [id, st] of Object.entries(layout.stations || {})) {
      if (st.placeholder) continue;
      const r = st.module || st.region, [x,y] = this.point(r.x[0], r.y[1]), [x1,y1] = this.point(r.x[1], r.y[0]);
      svg('rect', { x, y, width: x1-x, height: y1-y, rx: 8, fill: '#eef2f7', stroke: '#cad3df', 'stroke-width': 2 }, this.root);
      this.text(labels[id] || st.label || id, (x+x1)/2, y-12, 24, 'middle');
    }
    for (const slot of Object.values(layout.slots || {})) {
      if (layout.stations?.[slot.station]?.placeholder) continue;
      const [x,y] = this.point(slot.x, slot.y);
      svg('rect', { x: x-6, y: y-6, width: 12, height: 12, fill: 'none', stroke: '#cad3df' }, this.root);
    }
    this.tape = svg('path', { stroke: '#dfb960', 'stroke-width': 9, fill: 'none' }, this.root);
    this.items = svg('g', {}, this.root); this.markers = new Map();
    this.arms = svg('g', {}, this.root); this.armMarkers = new Map();
    for (const [i, [label, color]] of [['Chip', '#8974cc'], ['Material carrier', '#5592bf'], ['Stamp', '#4ba596']].entries()) {
      const x = 28 + i * 220;
      svg('rect', {x, y:602, width:18, height:18, fill:color}, this.root);
      this.text(label, x+28, 618, 21);
    }
    this.text('SIMULATED · saved state', 1065, 618, 21, 'end');
  }
  point(x,y) {
    const t = this.layout.table || { x: [0,1100], y: [0,650] };
    return [36+(x-t.x[0])/(t.x[1]-t.x[0])*1028, 568-(y-t.y[0])/(t.y[1]-t.y[0])*480];
  }
  text(value,x,y,size,anchor='start') {
    const t = svg('text', { x,y, fill:'#263348', 'font-size':size, 'font-family':'system-ui, sans-serif', 'text-anchor':anchor }, this.root);
    t.textContent = value; return t;
  }
  update(state) {
    for (const [id, node] of this.markers) if (!state.objects?.[id]) { node.remove(); this.markers.delete(id); }
    for (const [id, o] of Object.entries(state.objects || {})) {
      if (o.x == null || o.y == null) continue;
      let n = this.markers.get(id);
      if (!n) {
        n = svg('rect', { stroke:'#263348', fill:({chip:'#8974cc',carrier:'#5592bf',stamp:'#4ba596'})[o.kind] || '#66758a' }, this.items);
        svg('title', {}, n).textContent = id; this.markers.set(id,n);
      }
      const [x,y] = this.point(o.x,o.y), [w,h] = o.size_mm || [10,10];
      const scale = Math.max(0.82, 10 / Math.max(w, h));
      for (const [k,v] of Object.entries({x:-w*scale/2, y:-h*scale/2, width:w*scale, height:h*scale, transform:`translate(${x} ${y}) rotate(${-o.rotation || 0})`})) n.setAttribute(k,v);
    }
    for (const [id,a] of Object.entries(state.arms || {})) {
      let n = this.armMarkers.get(id);
      if (!n) {
        n = svg('g', { fill:'none', stroke:id === 'arm_left' ? '#be6b36' : '#347ca4', 'stroke-width':2 }, this.arms);
        svg('circle', {r:11}, n); svg('path', {d:'M-16 0H16M0-16V16'}, n);
        svg('title', {}, n).textContent = id; this.armMarkers.set(id,n);
      }
      const [x,y] = this.point(a.x,a.y); n.setAttribute('transform',`translate(${x} ${y})`);
    }
    const e = state.exfoliator, origin = this.layout.exfoliator?.origin;
    this.tape.setAttribute('d', e?.applied && origin ? `M${this.point(origin.x,origin.y).join(' ')}L${this.point(origin.x+(e.gantry_x || 0),origin.y).join(' ')}` : '');
  }
  image() { return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(this.root)); }
}
