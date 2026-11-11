// The lab column's two picture panes under the bench top view (Jialei
// 2026-11-11: "only three pictures"): a macro camera and a microscope. Each
// pane shows the source of the instrument that moves now ("哪个仪器在动就放哪个
// 相机/显微镜") and keeps the last one while nothing moves; its header names
// the source in a few words and has one Expand button.
//
//   Camera      overview_camera (the simulated lab has one macro camera; a
//               lab with a camera per module adds it to SLOTS.camera)
//   Microscope  microscope (transfer stage: chip search, stacking) or
//               reactor_microscope (while a reactor job runs)
//
// Live: the page asks GET /api/camera/{id}/latest for every source every
// 1.5 s (the testbench draws the current table and microscope scene).
// Recordings display immutable previews only when their lab.frame arrives;
// old recordings retain the newest captured microscope picture in the prefix.
// The overview is a simple diagram of saved lab state. Original overview
// photos stay in the conversation. The microscope's header names the
// objective of the photo it shows ("20× photo"), not the one turned in now.
// The mode (live or a recording) is said once, in the title bar.
//
// The last measured check (how far off a placement was) is in the title
// bar's sentence and the progress tree; verify() keeps it for a host element
// with id "verify" when a page has one.

import { $, h, api, fmt, imgUrl, imageSource, isMicrograph, VERDICT, stepName } from './ui.js';
import { OverviewDiagram } from './overview.js';

const CAMS = ['overview_camera', 'microscope', 'reactor_microscope'];
const SLOTS = [
  { id: 'camera', title: 'Camera', sources: ['overview_camera'] },
  { id: 'microscope', title: 'Microscope', sources: ['microscope', 'reactor_microscope'] },
];
const SOURCE = { overview_camera: 'Overview', microscope: 'Transfer stage', reactor_microscope: 'Reactor' };
const EXPAND_ICON = '<svg viewBox="0 0 24 24"><path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/></svg>';

export class CameraCard {
  // openFlake(id): the flake library on that flake ("Looking at chip_001 · f014")
  constructor({ replay, lightbox, openFlake }) {
    this.replay = replay; this.lightbox = lightbox; this.openFlake = openFlake;
    this.cur = {};  // the picture each frame shows now
    this.lookId = null;
    this.latest = { overview_camera: null, microscope: null };  // newest recorded picture of each
    this.meta = {};
    this.active = null;
    this.frames = {};
    this.panes = {};
    this.sel = { camera: 'overview_camera', microscope: 'microscope' };
    for (const cam of CAMS) {
      const f = {
        box: h('div', { class: 'cam', 'data-cam': cam }),
        imgs: [h('img', { alt: '', class: 'on' }), h('img', { alt: '' })],
        // what the shown picture is (objective, preview or photo): the pane header shows it
        tag: h('span', { class: 'pane-note' }),
        cap: h('div', { class: 'cap' }),
        empty: h('div', { class: 'cam-empty' }),
      };
      f.box.replaceChildren(...f.imgs, f.empty, f.cap);
      if (cam === 'microscope') {
        // which flake the microscope looks at, when a photo or a job step says so
        f.look = h('button', { class: 'look', hidden: true, title: 'Open this flake\'s details',
          onclick: ev => { ev.stopPropagation(); if (this.lookId) this.openFlake?.(this.lookId); } });
        f.capText = h('span');
        f.lookLink = h('button', { class: 'look-link', hidden: true, text: 'Open flake ↗',
          onclick: ev => { ev.stopPropagation(); if (this.lookId) this.openFlake?.(this.lookId); } });
        f.cap.replaceChildren(f.capText, f.lookLink);
        f.box.appendChild(f.look);
        // a recorded stamp pass is one video clip (#205); click plays/pauses it
        // Two elements: the next clip loads and seeks behind the shown one and
        // only replaces it once its frame is decoded (no black between clips).
        const clip = () => { const v = h('video', { class: 'cam-video', hidden: true, muted: true, playsinline: true, preload: 'auto' }); v.muted = true; return v; };
        f.video = clip(); f.videoBack = clip();
        // a still of the shown clip's frame, kept under the clips (see showVideo)
        f.hold = h('canvas', { class: 'cam-hold', hidden: true });
        f.box.insertBefore(f.hold, f.empty);
        f.box.insertBefore(f.video, f.empty); f.box.insertBefore(f.videoBack, f.empty);
        // a preview raster with non-square pixels is drawn at its field's shape
        new ResizeObserver(() => f.imgs.forEach(i => this.fitImg(f, i))).observe(f.box);
      }
      f.box.onclick = () => {
        if (f.video && !f.video.hidden) { if (f.video.paused) f.video.play().catch(() => {}); else f.video.pause(); return; }
        if (f.box.closest('.expanded')) return; const src = f.diagram?.image() || f.imgs.find(i => i.classList.contains('on'))?.dataset.src; if (src) this.lightbox(src); };
      this.frames[cam] = f;
    }
    for (const slot of SLOTS) {
      const src = h('span', { class: 'pane-src' }), notes = h('span', { class: 'pane-notes' });
      const live = h('span', { class: 'pane-live', hidden: true, title: 'A job uses this instrument now' }, h('i'), 'moving');
      const expand = h('button', { class: 'pane-x', title: `Open a larger ${slot.title.toLowerCase()} view that keeps updating`,
        'aria-label': `Expand the ${slot.title.toLowerCase()}`, onclick: ev => { ev.stopPropagation(); this.expand(slot.id); } });
      expand.innerHTML = EXPAND_ICON;
      const body = h('div', { class: 'pane-b' }, ...slot.sources.map(c => this.frames[c].box));
      const box = h('section', { class: 'pane cam-pane', 'data-slot': slot.id },
        h('header', { class: 'pane-h' }, h('b', { text: slot.title }), src, notes, h('span', { class: 'grow' }), live, expand), body);
      for (const c of slot.sources) notes.append(this.frames[c].tag);
      this.panes[slot.id] = { slot, box, src, live, body };
    }
    $('cam').replaceChildren(...SLOTS.map(s => this.panes[s.id].box));
    for (const slot of SLOTS) this.select(slot.id, this.sel[slot.id]);
    const verify = $('verify');
    if (verify) verify.onclick = ev => { if (ev.target.closest('.vhist')) verify.classList.toggle('open'); };
    this.reset();
    if (!replay) setInterval(() => { if (!document.hidden) this.poll(); }, 1500);
  }

  // Show one source of a pane (the others stay mounted and keep their pictures).
  select(slotId, cam) {
    const pane = this.panes[slotId];
    if (!pane || !pane.slot.sources.includes(cam)) return;
    if (cam === 'reactor_microscope' && !this.reactor) cam = 'microscope';
    this.sel[slotId] = cam;
    for (const c of pane.slot.sources) {
      this.frames[c].box.hidden = c !== cam;
      this.frames[c].tag.hidden = c !== cam;
    }
    pane.src.textContent = SOURCE[cam] || cam;
    pane.box.dataset.source = cam;
  }

  // Move a whole pane into the lightbox, so the enlarged view is the same
  // live element (pictures, header, caption, flake link, source switching);
  // it goes back to its place in the column however the lightbox is closed.
  expand(slotId = 'microscope') {
    const pane = this.panes[slotId], box = $('lightbox');
    if (!pane || pane.box.classList.contains('expanded')) return;
    const slot = h('div', { hidden: true });
    pane.box.replaceWith(slot);
    const close = h('button', { class: 'pane-x pane-close', title: 'Close (Esc)', 'aria-label': 'Close the larger view', onclick: () => { box.hidden = true; } });
    close.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    pane.box.querySelector('.pane-h').append(close);
    const figure = h('figure', { class: 'expanded-cam' }, pane.box,
      h('figcaption', { text: (this.replay ? 'Recorded' : 'Live') + ' · keeps updating · Esc closes' }));
    figure.onclick = ev => ev.stopPropagation();
    pane.box.classList.add('expanded');
    box.replaceChildren(figure);
    box.hidden = false;
    const back = new MutationObserver(() => {
      if (!box.hidden && figure.contains(pane.box) && box.contains(figure)) return;
      back.disconnect();
      pane.box.classList.remove('expanded');
      close.remove();
      slot.replaceWith(pane.box);
      if (!box.contains(figure) || box.hidden) figure.remove();
    });
    back.observe(box, { attributes: true, attributeFilter: ['hidden'], childList: true });
  }

  setReactor(enabled) {
    this.reactor = !!enabled;
    if (!this.reactor && this.sel.microscope === 'reactor_microscope') this.select('microscope', 'microscope');
  }

  noteReactor(data) {
    this.meta.reactor_microscope = { reactor: data, objective: data.objective };
    for (const src of data.images || []) this.noteImage(src, data.objective);
    this.caption();
  }

  // a replay seek starts the page over
  reset() {
    this.latest = { overview_camera: null, microscope: null };
    this.meta = {};
    this.objOf = {};  // objective of each micrograph shown (a recording knows it from when it was taken)
    this.displayMeta = {};
    this.lastPreviewTs = this.lastAnalysisTs = this.lastShown5x = null;
    this.previewDense = false; this.darkSkips = 0;
    this.lab = null;
    for (const cam of CAMS) this.showEmpty(cam);
    this.caption();
    $('verify')?.classList.remove('open');
    $('verify')?.replaceChildren(h('div', { class: 'placeholder', text: 'No check with a measurement yet. After a placement, how far off it was shows here.' }));
  }

  // What the jobs are doing decides which source each pane shows and which
  // pane is marked "moving" (none while no job runs; the last source stays).
  context(wantMicroscope, anyRunning, wantReactor = false) {
    const cam = !anyRunning ? null : wantReactor && this.reactor ? 'reactor_microscope' : (wantMicroscope ? 'microscope' : 'overview_camera');
    if (cam === this.active) return;
    this.active = cam;
    if (cam === 'microscope' || cam === 'reactor_microscope') this.select('microscope', cam);
    this.panes.microscope.live.hidden = !(cam === 'microscope' || cam === 'reactor_microscope');
    // the macro camera sees the whole bench: something moves whenever a job runs
    this.panes.camera.live.hidden = !anyRunning;
    for (const s of SLOTS) this.panes[s.id].box.classList.toggle('active', !this.panes[s.id].live.hidden);
  }
  // objective: the objective the photo was taken with, when known (a flake's
  // 20x / 100x photo, or the one turned in when the photo came)
  noteImage(src, objective) {
    if (!src) return;
    const cam = /reactor_microscope_/.test(String(src)) ? 'reactor_microscope'
      : isMicrograph(src) || objective ? 'microscope' : 'overview_camera';
    this.latest[cam] = src;
    this.displayMeta[src] = { objective, purpose: 'capture' };
    if (objective != null && this.objOf[src] == null) this.objOf[src] = objective;
    if (this.replay) { this.show(src, cam); this.caption(); }
  }

  // These pictures describe the saved/current field, never a passed check.
  // Event-prefix callers invoke this only when the corresponding frame arrives.
  notePreview(preview) {
    if (!preview || typeof preview !== 'object') return;
    // Stacking (5X) has two frame streams: the twin's 4K motion previews and
    // the flow's 960x540 analysis captures (every ~2 s). Alternating them made
    // the contact front seem to flash and shrink (seed 3 r13). Show whichever
    // stream is dense right now: previews while they come faster than 1 s
    // (fast moves), otherwise the analysis captures (slow press, drop); a
    // sparse preview between analysis captures is skipped. All stay in the record.
    const ts = Number(preview.timestamp);
    if (Number(preview.objective) === 5 && Number.isFinite(ts)) {
      if (preview.purpose === 'motion_preview') {
        const prev = this.lastPreviewTs;
        this.lastPreviewTs = ts;
        this.previewDense = prev != null && ts >= prev && ts - prev <= 1;
        // A sparse preview while the pane is showing analysis captures is
        // skipped (the drop heating has one ~0.3 s before each capture).
        if (!this.previewDense && this.lastShown5x === 'analysis' && this.lastAnalysisTs != null
            && Math.abs(ts - this.lastAnalysisTs) <= 15) return;
        this.lastShown5x = 'preview';
      } else if (preview.purpose === 'analysis') {
        if (this.previewDense && this.lastPreviewTs != null && Math.abs(ts - this.lastPreviewTs) <= 3) return;
        this.lastAnalysisTs = ts;
        this.lastShown5x = 'analysis';
      }
    }
    this.meta.microscope = { ...preview };
    if (preview.video) {
      const key = `${preview.video}#t=${preview.video_time_s}`;
      this.displayMeta[key] = { ...preview, preview: true };
      this.showVideo(preview.video, Number(preview.video_time_s) || 0, key);
    } else if (preview.image) {
      this.displayMeta[preview.image] = { ...preview, preview: true };
      this.show(preview.image, 'microscope');
    } else {
      this.showEmpty('microscope');
      this.frames.microscope.empty.textContent = 'Microscope preview unavailable at this time.';
    }
    this.caption();
  }

  async poll() {
    for (const cam of CAMS) {
      if (cam === 'reactor_microscope' && !this.reactor) continue;
      try {
        const r = await api(`/api/camera/${cam}/latest`);
        this.meta[cam] = r.meta || {};
        if (cam === 'microscope' && (r.meta?.preview || r.meta?.purpose === 'motion_preview')) this.notePreview({ ...r.meta, image: r.image, timestamp: r.timestamp });
        else if (r.image) {
          this.displayMeta[r.image] = { ...r.meta };
          this.show(r.image, cam);
        }
        else if (!this.latest[cam]) this.showEmpty(cam);
      } catch { /* the server restarts or has no such camera: keep the last picture */ }
    }
    this.caption();
  }

  // One frame of a recorded stamp clip: seek the clip and hold it there.
  // The last frame's centre time can equal the clip's duration, where the
  // element shows nothing: clamp inside the stream. A new clip is loaded and
  // seeked on the hidden element; the shown picture stays until it is ready.
  showVideo(src, t, key) {
    const f = this.frames.microscope, url = imgUrl(src);
    f.request = null;
    const inside = v => (Number.isFinite(v.duration) && v.duration > 0 ? Math.max(0, Math.min(t, v.duration - 0.5 / 15)) : t);
    const front = f.video;
    if (!front.hidden && front.dataset.url === url) {
      f.videoReq = null;
      if (!front.paused) front.pause();
      const at = inside(front);
      if (Math.abs(front.currentTime - at) > 1e-3) front.currentTime = at;
      this.cur.microscope = key; this.caption();
      return;
    }
    const back = f.videoBack, req = `${url}#${t}`;
    f.videoReq = req;
    // Chromium can drop the shown clip's picture while the other element
    // starts decoding (seed 3 r13): hold a still of it underneath.
    if (!front.hidden && front.readyState >= 2 && front.videoWidth) {
      try {
        const w = Math.min(960, front.videoWidth), hh = Math.round(w * front.videoHeight / front.videoWidth);
        if (f.hold.width !== w || f.hold.height !== hh) { f.hold.width = w; f.hold.height = hh; }
        f.hold.getContext('2d').drawImage(front, 0, 0, w, hh);
        f.hold.hidden = false;
      } catch { /* keep whatever is held */ }
    }
    if (back.dataset.url !== url) { back.dataset.url = url; back.src = url; }
    if (!back.paused) back.pause();
    const reveal = () => {
      if (f.videoReq !== req) return;
      f.videoReq = null;
      back.hidden = false; front.hidden = true; if (!front.paused) front.pause();
      f.video = back; f.videoBack = front;
      f.box.classList.add('showing-video'); f.empty.hidden = true;
      this.cur.microscope = key; this.caption();
    };
    const seek = () => {
      if (f.videoReq !== req) return;
      const at = inside(back);
      if (Math.abs(back.currentTime - at) <= 1e-3 && back.readyState >= 2) { reveal(); return; }
      back.addEventListener('seeked', reveal, { once: true });
      back.currentTime = at;
    };
    if (back.readyState >= 1) seek(); else back.addEventListener('loadedmetadata', seek, { once: true });
    back.onerror = () => { if (f.videoReq !== req) return; f.empty.textContent = 'This recorded video could not be loaded.'; f.empty.hidden = false; };
  }

  hideVideo(cam) {
    const f = this.frames[cam];
    if (!f.video) return;
    f.videoReq = null;
    if (f.hold) f.hold.hidden = true;
    if (f.video.hidden) return;
    f.video.pause(); f.video.hidden = true; f.box.classList.remove('showing-video');
  }

  // Preview rasters with non-square pixels (480x320 drawing a 16:9 field)
  // are stretched to the field's own shape instead of looking squeezed.
  fitImg(f, img) {
    const a = Number(img.dataset.aspect);
    if (!a) { img.style.removeProperty('width'); img.style.removeProperty('height'); img.style.removeProperty('left');
      img.style.removeProperty('top'); img.style.removeProperty('object-fit'); return; }
    // the picture's own area: the box minus the caption band under it
    const W = f.box.clientWidth, H = f.box.clientHeight - (f.cap?.offsetHeight || 0);
    if (!W || !H) return;
    const w = Math.min(W, H * a), hh = w / a;
    Object.assign(img.style, { width: `${w}px`, height: `${hh}px`, left: `${(W - w) / 2}px`, top: `${(H - hh) / 2}px`, objectFit: 'fill' });
  }

  // mean brightness of a loaded image (0-255), from a small copy
  brightness(img) {
    try {
      const c = this.probe || (this.probe = Object.assign(document.createElement('canvas'), { width: 16, height: 16 }));
      const g = c.getContext('2d', { willReadFrequently: true });
      g.drawImage(img, 0, 0, 16, 16);
      const d = g.getImageData(0, 0, 16, 16).data;
      let sum = 0; for (let i = 0; i < d.length; i += 4) sum += d[i] + d[i + 1] + d[i + 2];
      return sum / (d.length / 4 * 3);
    } catch { return null; }
  }

  show(src, cam) {
    if (this.replay && cam === 'overview_camera') return;
    this.hideVideo(cam);
    const f = this.frames[cam], url = imgUrl(src);
    const front = f.imgs.find(i => i.classList.contains('on')) || f.imgs[1], back = f.imgs.find(i => i !== front);
    f.request = url;
    if (front.dataset.url === url) { f.empty.hidden = true; return; }
    // Seeking can replace hundreds of images before any load finishes. Only
    // the latest requested frame may become visible; retain the last good one
    // while it loads, and never silently replace a failed image with a blank.
    const swap = () => {
      if (f.request !== url || back.dataset.url !== url) return;
      const meta = this.displayMeta[src] || {};
      // A motion preview taken mid objective switch / AF can be nearly black
      // (seed 3 r13): keep the previous picture for up to three of them.
      // nor replace the empty placeholder with an unfocused black one
      if (cam === 'microscope' && meta.purpose === 'motion_preview' && !front.dataset.url && meta.focus_plane == null) {
        const now = this.brightness(back);
        if (now != null && now < 40) return;
      }
      if (cam === 'microscope' && meta.purpose === 'motion_preview' && front.dataset.url && this.darkSkips < 3) {
        const now = this.brightness(back), before = front.dataset.light != null ? Number(front.dataset.light) : null;
        if (now != null && before != null && now < 40 && before > 60) { this.darkSkips += 1; return; }
      }
      this.darkSkips = 0;
      const m = meta.calibration?.mm_per_pixel;
      const a = Array.isArray(m) && m.length === 2 && back.naturalHeight
        ? (back.naturalWidth * Math.abs(m[0])) / (back.naturalHeight * Math.abs(m[1])) : null;
      if (a && Math.abs(a / (back.naturalWidth / back.naturalHeight) - 1) > 0.02) back.dataset.aspect = a; else delete back.dataset.aspect;
      this.fitImg(f, back);
      if (cam === 'microscope') { const b = this.brightness(back); if (b != null) back.dataset.light = b; else delete back.dataset.light; }
      // the old picture stays opaque underneath until the new one has faded in
      front.classList.remove('on'); front.classList.add('prev');
      back.classList.remove('prev'); back.classList.add('on');
      clearTimeout(f.prevTimer);
      f.prevTimer = setTimeout(() => { if (!front.classList.contains('on')) front.classList.remove('prev'); }, 400);
      back.alt = imageSource(src); f.empty.hidden = true;
      this.cur[cam] = src; this.caption();
    };
    back.dataset.url = url; back.dataset.src = src;
    back.onload = swap;
    back.onerror = () => {
      if (f.request !== url) return;
      f.empty.textContent = 'This recorded image could not be loaded.';
      f.empty.hidden = false;
    };
    back.src = url;
    if (back.complete && back.naturalWidth) swap();
  }

  diagram(layout, state) {
    if (!this.replay) return;
    const f = this.frames.overview_camera;
    if (!f.diagram) {
      f.diagram = new OverviewDiagram(layout);
      f.box.append(f.diagram.root); f.box.classList.add('has-diagram');
      f.tag.textContent = 'simulated · saved state';
    }
    f.diagram.update(state); f.empty.hidden = true;
  }

  showEmpty(cam) {
    const f = this.frames[cam];
    this.hideVideo(cam);
    f.request = null;
    f.imgs.forEach(i => { i.onload = i.onerror = null; i.classList.remove('on', 'prev'); i.removeAttribute('src'); delete i.dataset.url; delete i.dataset.src; });
    this.cur[cam] = null;
    f.empty.hidden = false;
    f.empty.textContent = cam !== 'overview_camera' ? 'No micrograph yet. One appears when a job looks through the microscope.'
      : 'No overview picture yet.';
  }

  // lab: the bench view (objective, stage position) for the captions
  caption(lab) {
    if (lab) this.lab = lab;
    const L = this.lab || {};
    // the photo's own objective: live, the server says it with the photo
    const shown = this.cur.microscope;
    const m = shown ? this.displayMeta[shown] || {} : {};
    const obj = m.objective ?? (shown ? this.objOf[shown] : null);
    const mode = m.preview ? (this.replay ? 'saved preview' : 'live preview') : 'photo';
    this.frames.microscope.tag.textContent = [obj ? `${String(obj).replace(/x$/i, '')}×` : '', shown ? mode : ''].filter(Boolean).join(' ');
    const bits = [];
    const st = m.stage_kinematics?.sample_pose || (!this.replay ? (L.stages || {}).transfer_stage : null);
    if (st) bits.push(`stage ${fmt(st.x, 1)}, ${fmt(st.y, 1)} mm`);
    if ((m.focus_z ?? m.focus) != null) bits.push(`focus ${fmt(m.focus_z ?? m.focus, 3)} mm`);
    if (m.focus_plane) bits.push(`${m.focus_plane} plane`);
    if (m.preview) bits.push(m.purpose === 'analysis' ? 'analysis capture · not a verification'
      : `${String(m.source || '').startsWith('synthetic') ? 'simulated field' : 'field preview'} · not a verification`);
    this.frames.microscope.capText.textContent = bits.join(' · ');
    this.frames.microscope.capText.title = bits.join(' · ');  // the band is one line; the full text on hover
    const r = this.meta.reactor_microscope?.reactor || {};
    const pair = r.pair_observation;
    this.frames.reactor_microscope.tag.textContent = r.timestamp != null ? 'synthetic · saved observation' : 'synthetic image';
    this.frames.reactor_microscope.cap.textContent = [
      r.temperature_c != null ? `${fmt(r.temperature_c, 1)} °C` : null,
      r.flow_sccm != null ? `${fmt(r.flow_sccm, 1)} sccm` : null,
      r.coverage != null ? `${fmt(r.coverage * 100, 1)}% coverage` : null,
      pair ? `2 inputs · ${pair.product ? '1 verified product' : 'no verified product'}` : null,
      pair?.reaction_fraction != null ? `${fmt(pair.reaction_fraction * 100, 1)}% overlap reacted` : null,
      pair?.status,
      r.phase,
    ].filter(Boolean).join(' · ');
  }

  // info: { id, title, sub } of the flake under the objective, or null
  look(info) {
    const f = this.frames.microscope, id = info ? info.id : null;
    const key = info ? `${info.id}|${info.title}|${info.sub}` : '';
    if (key === this.lookKey) return;
    this.lookKey = key; this.lookId = id;
    f.look.hidden = f.lookLink.hidden = !info;
    if (info) f.look.replaceChildren(h('b', { text: info.title }), info.sub ? h('span', { text: info.sub }) : null);
  }

  // ------------------------------------------------------------ last check
  verify(v, attempts) {
    if (!v || !$('verify')) return;
    const m = v.measurements || {}, e = v.expected || {};
    if (m.offset_mm == null) return;  // a check without a position measurement stays in the tree only
    const fixes = attempts.filter(a => a.kind === 'corrected').length;
    const state = v.verdict === 'ok' ? 'in tolerance' : (v.verdict === 'correctable' ? 'out of tolerance · correcting' : 'out of tolerance · cannot be corrected');
    const line = h('div', { class: 'vck' },
      h('span', { class: 'st ' + v.verdict, text: v.verdict === 'ok' ? '✓' : (v.verdict === 'correctable' ? '↻' : '✗') }),
      h('b', { text: stepName(v.name) }),
      h('span', { class: 'mono', text: `off ${fmt(m.offset_mm)} mm / ${fmt(m.drot_deg ?? 0, 1)}°` }),
      h('span', { class: 'vstate', text: state + (fixes ? ` · after ${fixes} fix${fixes === 1 ? '' : 'es'}` : '') }),
      h('button', { class: 'vhist', text: 'history', title: 'The measurements and every attempt of this check' }));
    const metric = (k, val, unit, tol) => {
      const bad = tol != null && Math.abs(val) > tol;
      const pct = tol ? Math.min(100, Math.abs(val) / (tol * 3) * 100) : 0;
      return h('div', { class: 'metric' }, h('div', { class: 'k', text: k + (tol != null ? ` · tolerance ${tol}${unit}` : '') }),
        h('div', { class: 'v' }, fmt(val, unit === '°' ? 1 : 2), h('small', { text: unit })),
        tol != null ? h('div', { class: 'bar' + (bad ? ' bad' : '') }, h('i', { style: `width:${pct}%` }), h('em', { style: 'left:33.3%' })) : h('div', { class: 'bar none' }));
    };
    const metrics = h('div', { class: 'metrics' }, metric('Position off by', m.offset_mm, ' mm', e.tolerance_mm), metric('Angle off by', m.drot_deg ?? 0, '°', e.tolerance_deg));
    const att = h('div', { class: 'attempts' }, attempts.map(a => a.kind === 'verified'
      ? h('div', { class: 'attempt' }, h('span', { class: 'n', text: a.attempt ?? '·' }),
        h('span', { text: a.measurements && a.measurements.offset_mm != null ? `off by ${fmt(a.measurements.offset_mm)} mm / ${fmt(a.measurements.drot_deg, 1)}°` : 'measured' }),
        h('span', { class: 'tag ' + a.verdict, text: VERDICT[a.verdict] || a.verdict }))
      : h('div', { class: 'attempt fix', text: `↳ ${a.action || 'correction'}` })));
    $('verify').replaceChildren(line, h('div', { class: 'vdetail' }, metrics, att));
  }
}
