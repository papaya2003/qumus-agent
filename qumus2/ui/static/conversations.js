// Conversations: start a new one, or go back to an earlier one
// (qumus2/conversations.py; GET/POST /api/conversations).
//
// The button next to "Conversation" shows the conversation in use; it opens a
// dialog with every conversation and a form for a new one. Each conversation
// has its own chat, Task Plan and instructions; the lab, the samples, the
// experience and the budget are shared. The server refuses a switch while
// Qumus works or a job is under way (the dialog says why). After a switch the
// page reloads: it then shows the chosen conversation from this point on, and
// its earlier messages are in History.

import { $, h, api, post } from './ui.js';

const ago = (ts, now = Date.now() / 1000) => {
  if (!ts) return '';
  const s = Math.max(0, now - ts);
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
};

// One line of facts about a conversation row (GET /api/conversations).
export function conversationFacts(row, now) {
  const n = row.messages || 0;
  return [
    row.owner ? `by ${row.owner}` : null,
    row.last_active ? `active ${ago(row.last_active, now)}` : null,
    `${n} message${n === 1 ? '' : 's'}`,
    row.plan_steps ? `plan ${row.plan_done}/${row.plan_steps} done` : 'no plan yet',
    row.cost_usd > 0 ? `$${row.cost_usd.toFixed(2)}` : null,
  ].filter(Boolean).join(' · ');
}

export class Conversations {
  // project: the snapshot's project (the conversation in use)
  constructor(project, { toast }) {
    this.toast = toast;
    this.btn = $('conv-pick');
    this.dialog = $('conv-dialog');
    if (!this.btn || !this.dialog) return;
    this.setCurrent(project);
    this.btn.hidden = false;
    this.btn.addEventListener('click', () => this.open());
  }

  setCurrent(project) {
    const name = project?.name || 'Conversation';
    const owner = project?.state?.owner;
    this.btn.replaceChildren(h('span', { class: 'conv-name', text: name }), h('span', { class: 'conv-caret', text: '▾' }));
    this.btn.title = `Conversation in use: ${name}${owner ? ` (by ${owner})` : ''}. Click to start a new one or go back to an earlier one.`;
  }

  async open() {
    this.render(null, null);
    if (!this.dialog.open) this.dialog.showModal();
    try { this.render(await api('/api/conversations'), null); }
    catch (err) { this.render(null, `Could not load the conversations: ${err.message}`); }
  }

  async act(fn, busyText) {
    this.render(this.data, null, busyText);
    try {
      await fn();
      location.reload();  // the page now belongs to the chosen conversation
    } catch (err) {
      this.render(this.data, err.message);
    }
  }

  render(data, error, working) {
    this.data = data;
    const blocked = data?.blocked;
    const off = !!(blocked || working);
    const rows = (data?.conversations || []).map(r => h('div', { class: 'conv-row' + (r.active ? ' on' : '') },
      h('div', { class: 'conv-l' }, h('b', { text: r.name }), h('small', { text: conversationFacts(r) })),
      r.active ? h('span', { class: 'tag', text: 'in use' })
        : h('button', { class: 'row-btn conv-open', disabled: off || null, text: 'Continue',
          title: 'Go back to this conversation: its chat, plan and instructions',
          onclick: () => this.act(() => post(`/api/conversations/${encodeURIComponent(r.id)}/switch`), `Opening ${r.name}…`) })));
    const name = h('input', { id: 'conv-new-name', type: 'text', maxlength: '80', placeholder: 'e.g. hBN/graphene Hall bar, Nov 17', required: true });
    const owner = h('input', { id: 'conv-new-owner', type: 'text', maxlength: '80', placeholder: 'Who runs this experiment' });
    const form = h('form', { class: 'conv-new', onsubmit: ev => {
      ev.preventDefault();
      if (!name.value.trim()) { name.focus(); return; }
      this.act(() => post('/api/conversations', { name: name.value, owner: owner.value }), 'Starting the new conversation…');
    } },
    h('div', { class: 'sec-t', text: 'New conversation' }),
    h('label', { for: 'conv-new-name', text: 'Name' }), name,
    h('label', { for: 'conv-new-owner', text: 'Owner (optional)' }), owner,
    h('p', { class: 'muted small', text: 'Starts blank: Qumus does not see earlier messages and the plan is empty. The model choice is kept.' }),
    h('button', { class: 'row-btn conv-start', type: 'submit', disabled: off || null, text: 'Start new conversation' }));
    this.dialog.replaceChildren(...[  // replaceChildren would print a null as text
      h('div', { class: 'conv-head' }, h('h3', { text: 'Conversations' }), h('div', { class: 'grow' }),
        h('button', { class: 'icon-btn mini', type: 'button', title: 'Close', 'aria-label': 'Close', text: '✕', onclick: () => this.dialog.close() })),
      h('p', { class: 'muted small', text: 'Each conversation has its own chat, Task Plan and instructions. The lab, samples, experience and budget are shared.' }),
      blocked ? h('div', { class: 'banner conv-blocked', text: blocked }) : null,
      error ? h('div', { class: 'banner danger conv-error', text: error }) : null,
      working ? h('div', { class: 'muted small', text: working }) : null,
      h('div', { class: 'conv-list' }, data ? (rows.length ? rows : h('div', { class: 'placeholder', text: 'No conversation yet.' }))
        : error ? null : h('div', { class: 'placeholder', text: 'Loading…' })),
      form].filter(Boolean));
  }
}
