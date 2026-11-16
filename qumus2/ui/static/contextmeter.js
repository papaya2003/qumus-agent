// The context meter beside the composer (context compaction, agents/compaction.py):
// "Context 82k / auto 500k" from the orchestrator's last measured request, and a
// Compact button. Its panel compacts now (optionally saying what the summary must
// keep) and sets the automatic point as a share of the model's context window.
// The server does the work: POST /api/compact goes through the instruction queue
// like Stop and Continue; GET /api/context is the reading.

import { $, h, api, post, toast } from './ui.js';

const KINDS = new Set(['agent.usage', 'agent.turn_end', 'context.compacted', 'context.session_changed',
  'context.settings_changed', 'model.changed', 'model.routing_changed']);

export const tokens = n => (n == null ? '—' : n >= 1e6 ? `${+(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`
  : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));

// what the meter says, from GET /api/context
export function meterText(c) {
  if (!c || !c.supported) return null;
  const now = c.input_tokens == null ? 'not measured yet' : `${c.estimated ? '≈' : ''}${tokens(c.input_tokens)}`;
  const at = c.auto ? `auto ${tokens(c.threshold_tokens)}` : 'auto off';
  return `Context ${now} / ${at}`;
}

export function meterTitle(c) {
  if (!c || !c.supported) return '';
  const window = c.window_tokens ? `${tokens(c.window_tokens)} token window of ${c.model}`
    : 'model window unknown, so a fixed point is used';
  const lines = [
    c.input_tokens == null ? 'The orchestrator has not sent a request in this conversation yet.'
      : `${c.estimated ? 'Estimated after the last compaction' : 'Measured by the model service at the last request'}: ${c.input_tokens.toLocaleString()} tokens.`,
    c.auto ? `Compacts automatically at ${c.window_percent}% (${c.threshold_tokens.toLocaleString()} tokens; ${window}).`
      : `Automatic compaction is off${c.auto_disabled_by_env ? ' (QUMUS_COMPACTION=off)' : ''}.`,
  ];
  if (c.compactions) lines.push(`Compacted ${c.compactions} time${c.compactions > 1 ? 's' : ''}; the full history stays in the run record.`);
  if (c.mode === 'scripted') lines.push('Scripted mode: Compact records a fixed free summary (no model).');
  return lines.join('\n');
}

export class ContextMeter {
  constructor(root) {
    this.root = root;
    this.state = null;
    this.timer = null;
    this.meter = h('button', { class: 'ctx-meter', id: 'ctx-meter', type: 'button', onclick: () => this.toggle() });
    this.bar = h('i', {});
    this.meter.append(h('span', { class: 'ctx-bar' }, this.bar), h('span', { class: 'ctx-text', text: 'Context' }));
    this.button = h('button', { class: 'ctx-compact', id: 'ctx-compact', type: 'button', text: 'Compact',
      title: 'Compact the conversation: Qumus writes a handoff summary of the older part and keeps the newest steps word for word',
      onclick: () => this.toggle() });
    this.focus = h('input', { type: 'text', maxlength: '1000', placeholder: 'Optional: what the summary must keep, e.g. every attempt on chip_012' });
    this.auto = h('input', { type: 'checkbox' });
    this.percent = h('input', { type: 'number', min: '10', max: '95', step: '5', class: 'ctx-pct' });
    this.pointNote = h('small', {});
    this.panel = h('div', { class: 'ctx-panel', hidden: true },
      h('b', { text: 'Compact the conversation' }),
      h('p', { text: 'The older part becomes a one-page handoff summary; the newest steps stay word for word. Nothing is deleted: History and replays keep everything. Experiments keep running.' }),
      h('div', { class: 'ctx-row' }, this.focus,
        h('button', { class: 'ctx-go', type: 'button', text: 'Compact now', onclick: () => this.compact() })),
      h('div', { class: 'ctx-row ctx-auto' },
        h('label', {}, this.auto, ' Compact automatically at '), this.percent, h('span', { text: '% of the model window' }),
        h('button', { class: 'ctx-save', type: 'button', text: 'Save', onclick: () => this.save() })),
      this.pointNote);
    this.focus.addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); this.compact(); } });
    root.replaceChildren(this.meter, this.button, this.panel);
    root.hidden = true;
    document.addEventListener('click', ev => {
      if (!this.panel.hidden && !root.contains(ev.target)) this.panel.hidden = true;
    });
  }

  // a live event: re-read the meter when it can have changed (at most once a second)
  event(e) {
    if (!KINDS.has(e.kind)) return;
    if (e.kind === 'agent.usage' && e.agent !== 'orchestrator') return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.refresh(), 800);
  }

  async refresh() {
    try { this.show(await api('/api/context')); } catch { /* an older server: keep it hidden */ }
  }

  show(c) {
    this.state = c;
    const text = meterText(c);
    this.root.hidden = !text;
    if (!text) return;
    this.meter.querySelector('.ctx-text').textContent = text;
    this.meter.title = meterTitle(c);
    const share = c.input_tokens && c.threshold_tokens ? Math.min(1, c.input_tokens / c.threshold_tokens) : 0;
    this.bar.style.width = `${Math.round(share * 100)}%`;
    this.root.classList.toggle('near', c.auto && share >= 0.8);
    this.button.disabled = !!(c.requested || c.compacting);
    this.button.textContent = c.compacting ? 'Compacting…' : c.requested ? 'Compact queued' : 'Compact';
    if (document.activeElement !== this.percent) this.percent.value = c.window_percent;
    if (document.activeElement !== this.auto) this.auto.checked = !!c.auto;
    this.auto.disabled = !!c.auto_disabled_by_env;
    this.pointNote.textContent = c.window_tokens
      ? `${c.model}: ${c.window_tokens.toLocaleString()} token window, so automatic compaction at ${c.threshold_tokens.toLocaleString()} tokens.`
      : `The model's window is unknown, so automatic compaction uses ${c.threshold_tokens.toLocaleString()} tokens.`;
  }

  toggle() {
    this.panel.hidden = !this.panel.hidden;
    if (!this.panel.hidden) { this.refresh(); this.focus.focus(); }
  }

  async compact() {
    try {
      await post('/api/compact', { focus: this.focus.value.trim() });
      toast(this.state && this.state.mode !== 'scripted' ? 'Compact: Qumus summarizes the older conversation (at once when idle, else before its next request)'
        : 'Compact: a fixed summary was recorded (scripted mode)');
      this.focus.value = '';
      this.panel.hidden = true;
      this.refresh();
    } catch (err) { toast(`Compact request failed: ${err.message}`); }
  }

  async save() {
    const pct = Number(this.percent.value);
    try {
      this.show(await post('/api/context/settings', { auto: this.auto.checked, window_percent: pct }));
      toast('Automatic compaction saved');
    } catch (err) { toast(err.message); }
  }
}

export function contextMeter() {
  const root = $('ctx');
  return root ? new ContextMeter(root) : null;
}
