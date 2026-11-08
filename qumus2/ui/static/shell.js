// The workbench shell: the activity bar picks the scenario (Progress, Results,
// Lab, History; Testbench and Settings are tool pages next to the
// conversation). A scenario sets all three columns: its page on the left, its
// view in the centre, and what the right column shows (the bench, the cameras
// or the details of what was clicked). Clicking the scenario that is open
// folds the left column away; the bench column can be hidden and shown again.
//
// On a phone the activity bar becomes four tabs at the bottom (Chat, Progress,
// Lab, More) and one column shows at a time ("pane"); a ‹ in a column's
// header goes back one column. The choices are remembered.

import { $ } from './ui.js';

const SCENES = ['progress', 'results', 'lab', 'history', 'tools', 'settings'];
const MORE = new Set(['results', 'history', 'tools', 'settings']);
export const PHONE = matchMedia('(max-width: 860px)');

export class Shell {
  // hooks: layout(scene) -> { centre: view id, right: 'bench' | 'cams' | 'detail' };
  // open(scene) every time a scenario is shown (pages load lazily)
  constructor(hooks) {
    this.hooks = hooks || {};
    this.app = $('app');
    this.scene = 'progress';
    this.pane = 'centre';       // phone: the column on screen
    this.more = 'results';      // phone: the scenario More opens
    try {
      const saved = JSON.parse(localStorage.getItem('qumus.shell') || '{}');
      if (SCENES.includes(saved.scene)) this.scene = saved.scene;
      if (saved.side === false) this.app.classList.add('no-side');
      if (saved.lab === false) this.app.classList.add('no-lab');
    } catch { /* private window */ }
    document.querySelectorAll('.ab, .phone-pages button').forEach(b => {
      b.addEventListener('click', () => this.click(b.dataset.page));
    });
    document.querySelectorAll('.pback').forEach(b => { b.onclick = () => this.setPane(b.dataset.back); });
    $('lab-close').onclick = () => this.showLab(false);
    $('lab-show').onclick = () => this.showLab(true);
    PHONE.addEventListener('change', () => this.render());
    this.render();
  }

  click(page) {
    if (PHONE.matches) {
      if (page === 'chat') { this.scene = 'progress'; this.pane = 'centre'; }
      else if (page === 'progress') { this.scene = 'progress'; this.pane = 'side'; }
      else if (page === 'lab') { this.scene = 'lab'; this.pane = 'side'; }
      else if (page === 'more') { this.scene = this.more; this.pane = this.firstPane(this.more); }
      else if (MORE.has(page)) { this.scene = this.more = page; this.pane = this.firstPane(page); }
      this.render();
      return;
    }
    if (page === this.scene && this.sideOpen && page !== 'settings') this.app.classList.add('no-side');
    else { this.scene = page; this.app.classList.remove('no-side'); }
    this.render();
  }
  // a phone opens a scenario on its overview where it has one
  firstPane(scene) { return scene === 'results' ? 'centre' : 'side'; }

  // show a scenario (a link, a click in a list); `pane`: the phone column to show
  open(scene, pane) {
    if (PHONE.matches) {
      if (MORE.has(scene)) this.more = scene;
      this.scene = scene;
      this.pane = pane || (scene === 'progress' ? 'centre' : this.firstPane(scene));
    } else {
      this.scene = scene;
      this.app.classList.remove('no-side');
    }
    this.render();
  }
  // phone: show another column of the open scenario (no effect on a wide screen)
  setPane(pane) { if (this.pane !== pane) { this.pane = pane; this.render(); } }
  showLab(on) { this.app.classList.toggle('no-lab', !on); this.render(); }
  get sideOpen() { return this.scene === 'settings' || !this.app.classList.contains('no-side'); }
  get phone() { return PHONE.matches; }

  // the tab a phone highlights
  tab() {
    if (this.scene === 'progress') return this.pane === 'side' ? 'progress' : 'chat';
    return this.scene === 'lab' ? 'lab' : 'more';
  }

  // quiet: only lay the columns out again (the scenario's own redraw calls it)
  render(quiet) {
    const phone = PHONE.matches, scene = this.scene;
    const lay = this.hooks.layout ? this.hooks.layout(scene) : { centre: 'v-conv', right: 'bench' };
    this.app.dataset.scene = scene;
    this.app.dataset.r = lay.right;
    this.app.dataset.pane = phone ? this.pane : '';
    this.app.dataset.view = phone ? this.tab() : '';
    for (const p of SCENES) $('page-' + p).hidden = p !== scene;
    document.querySelectorAll('#ed > .view').forEach(v => { v.hidden = v.id !== lay.centre; });
    $('lab').dataset.r = lay.right;
    document.querySelectorAll('.ab').forEach(b => {
      const on = phone ? b.dataset.page === this.tab() : b.dataset.page === scene && this.sideOpen;
      b.classList.toggle('on', on);
      b.classList.toggle('here', !phone && b.dataset.page === scene);
    });
    document.querySelectorAll('.phone-pages button').forEach(b => b.classList.toggle('on', b.dataset.page === scene));
    const bench = lay.right === 'bench';
    $('lab-close').hidden = !bench || phone;
    $('lab-show').hidden = phone || !bench || !this.app.classList.contains('no-lab');
    if (!quiet && this.hooks.open) this.hooks.open(scene);
    try {
      localStorage.setItem('qumus.shell', JSON.stringify({ scene, side: this.sideOpen, lab: !this.app.classList.contains('no-lab') }));
    } catch { /* private window */ }
  }

  // a number on an activity item ("waiting for a person", things that need a person)
  badge(page, text, cls) {
    const el = $('badge-' + page);
    if (!el) return;
    el.hidden = !text;
    if (text) { el.textContent = text; el.className = 'badge ' + (cls || ''); }
  }
}
