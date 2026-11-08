// Experience (spec 10, self-evolution), shown in History · Experience: what
// the lab has learned. Each entry is a short text the Advisor can read (a
// claim, maybe a recipe) with where it came from and the real runs that voted
// for or against it. GET /api/experience (live) or the recording's copy.
//
//   left    by status (confirmed, on trial, retired), by topic, the evidence
//   centre  the cards; "No real experience yet" while no real run happened,
//           and the rule for confirming and retiring
//   right   one entry's provenance: its source, what it says, the votes
//
// Every field may be missing: the page then shows what it has, never an error.

import { h } from './ui.js';

const TOPIC_LABELS = {
  exfoliation: 'Exfoliation', search: 'Flake search', flake_choice: 'Flake choice',
  stacking: 'Stacking', equipment: 'Equipment', efficiency: 'Efficiency',
};
const SOURCE_LABELS = { review: 'Learned from real runs', human: 'Told by a person', prior: 'Prior knowledge (v1 or a paper)' };
const STATUS_LABELS = { confirmed: 'Confirmed', trial: 'On trial', retired: 'Retired' };
const STATUS_TAG = { confirmed: 'ok', trial: 'pending', retired: 'cancelled' };
const PARAM_LABELS = {
  target_temp_c: ['', ' °C'], dwell_s: ['dwell ', ' s'], num_massages: ['', ' massage'],
  peel_speed: ['peel speed ', ''], peel_angle_deg: ['angle ', '°'], pickup_temp_c: ['pick-up ', ' °C'],
  drop_temp_c: ['drop ', ' °C'], melt_temp_c: ['melt ', ' °C'], press_force: ['press ', ''],
};
const ORDER = ['confirmed', 'trial', 'retired'];

export function recipeText(recipe) {
  if (!recipe || typeof recipe !== 'object') return '';
  return Object.entries(recipe).map(([k, v]) => {
    const [pre, post] = PARAM_LABELS[k] || [`${k.replace(/_/g, ' ')} `, ''];
    const many = k === 'num_massages' && Number(v) !== 1 ? 's' : '';
    return `${pre}${v}${post}${many}`;
  }).join(' · ');
}

const plural = (n, word) => `${n} ${n === 1 ? word : /(s|x|ch|sh)$/.test(word) ? `${word}es` : `${word}s`}`;

export function ruleText(rule) {
  if (!rule) return '';
  const s = rule.confirm_min_support ?? 3, b = rule.confirm_min_batches ?? 2;
  const a = rule.confirm_max_against ?? 1, r = rule.retire_min_against ?? 2;
  return `New experience starts on trial. It is confirmed after ${plural(s, 'supporting real run')} from at least ${plural(b, 'batch')} ` +
    `(at most ${a} against), and retired once ${r} or more real runs contradict it and outnumber the support.`;
}

const titleOf = e => `${TOPIC_LABELS[e.topic] || e.topic || 'Experience'}${e.scope && e.scope.material ? ` · ${e.scope.material}` : ''}`;

export class ExperienceView {
  // hooks: changed(part); showRight(); showCentre() (a phone shows the cards)
  constructor(hooks) {
    this.hooks = hooks;
    this.data = undefined;   // undefined: not loaded; null: could not load
    this.f = { status: null, topic: null };
    this.sel = null;
  }

  entries() { return Array.isArray(this.data?.experiences) ? this.data.experiences.filter(Boolean) : []; }
  shown() { return this.entries().filter(e => (!this.f.status || e.status === this.f.status) && (!this.f.topic || e.topic === this.f.topic)); }
  set(patch) { Object.assign(this.f, patch); this.hooks.changed('all'); this.hooks.showCentre?.(); }
  select(id) { this.sel = id; this.hooks.changed('right'); this.hooks.showRight(); }

  renderIndex(box) {
    const all = this.entries(), d = this.data || {}, rows = [];
    const row = (t, n, on, fn) => h('button', { class: 'drow' + (on ? ' on' : ''), onclick: fn }, h('span', { class: 'dt', text: t }), h('span', { class: 'dn', text: String(n) }));
    rows.push(row('All', all.length, !this.f.status && !this.f.topic, () => this.set({ status: null, topic: null })));
    rows.push(h('div', { class: 'sec-t', text: 'Status' }));
    for (const st of ORDER) rows.push(row(STATUS_LABELS[st], all.filter(e => e.status === st).length, this.f.status === st, () => this.set({ status: this.f.status === st ? null : st })));
    const topics = [...new Set(all.map(e => e.topic).filter(Boolean))];
    if (topics.length) {
      rows.push(h('div', { class: 'sec-t', text: 'Topic' }));
      for (const t of topics) {
        const mats = [...new Set(all.filter(e => e.topic === t).map(e => e.scope?.material).filter(Boolean))];
        rows.push(row(TOPIC_LABELS[t] || t, `${all.filter(e => e.topic === t).length}${mats.length ? ` · ${mats.join(', ')}` : ''}`, this.f.topic === t, () => this.set({ topic: this.f.topic === t ? null : t })));
      }
    }
    rows.push(h('div', { class: 'sec-t', text: 'Evidence' }));
    rows.push(h('div', { class: 'drow static' }, h('span', { class: 'dt', text: 'Real lab runs' }), h('span', { class: 'dn', text: String(d.real_runs ?? 0) })));
    box.replaceChildren(...rows);
  }

  renderCentre(body, replay) {
    if (this.data === undefined) { body.replaceChildren(h('div', { class: 'placeholder', text: 'Loading…' })); return; }
    if (!this.data) {
      body.replaceChildren(h('div', { class: 'placeholder', text: replay ? 'This recording was made before the experience book existed.' : 'Could not load the experience book.' }));
      return;
    }
    const d = this.data, out = [];
    out.push(h('div', { class: 'xp-banner', role: 'status', 'aria-label': 'Experience learning status' },
      h('b', { text: replay ? 'Playback · learning disabled. ' : d.learning?.enabled ? 'Physical-record review. ' : 'Learning disabled. ' }),
      replay ? 'A recording retains its original laboratory provenance.' :
        d.learning?.detail || 'Historical advice remains readable; new learning requires verified physical experiment records.',
      d.unverified_origin_runs > 0 ? h('p', { class: 'small', text: `${d.unverified_origin_runs} recording(s) have unknown or mixed origin and cannot teach experience.` }) : null));
    const real = d.bench === 'real';
    if (!(d.real_runs > 0)) {
      out.push(h('div', { class: 'xp-banner' }, h('b', { text: 'No real experience yet. ' }),
        'Experience comes only from experiments on the real lab bench, and none have run. The entries below are prior knowledge (v1, a paper) or what a person stated. ',
        real ? '' : 'Testbench runs are simulated: they never create, vote on, confirm or retire experience.',
        d.rule ? h('div', { class: 'xp-rule', text: ruleText(d.rule) }) : null));
    } else if (d.rule) out.push(h('p', { class: 'xp-sub', text: ruleText(d.rule) }));
    const base = Object.entries(d.baselines || {}).filter(([, b]) => b && b.recipe);
    if (base.length && !this.f.status && (!this.f.topic || this.f.topic === 'exfoliation')) {
      out.push(h('div', { class: 'o-h', text: 'Exfoliation recipe in use' }));
      out.push(h('dl', { class: 'kv xp-base' }, base.flatMap(([mat, b]) => [h('dt', { text: mat }),
        h('dd', {}, h('span', { class: 'xp-recipe', text: recipeText(b.recipe) }), ' ',
          h('span', { class: 'tag ' + (b.enforced ? 'ok' : 'pending'), text: b.enforced ? 'confirmed · required' : 'on trial · a suggestion' }))])));
    }
    const list = this.shown();
    for (const st of ORDER) {
      const group = list.filter(e => e.status === st);
      if (!group.length) continue;
      out.push(h('div', { class: 'o-h', text: `${STATUS_LABELS[st]} · ${group.length}` }));
      for (const e of group) out.push(this.card(e));
    }
    if (!list.length) out.push(h('div', { class: 'placeholder', text: this.entries().length ? 'Nothing matches.' : 'No experience recorded.' }));
    body.replaceChildren(...out);
  }

  card(e) {
    const sup = (e.support || []).length, ag = (e.against || []).length;
    return h('button', { class: 'card xp-card' + (this.sel === e.id ? ' on' : ''), onclick: () => this.select(e.id) },
      h('div', { class: 'xp-h' }, h('b', { text: titleOf(e) }), h('span', { class: 'tag ' + (STATUS_TAG[e.status] || ''), text: STATUS_LABELS[e.status] || e.status })),
      h('div', { class: 'xp-claim', text: e.claim || '' }),
      h('div', { class: 'muted small', text: [e.label || SOURCE_LABELS[e.source] || e.source, e.recipe ? recipeText(e.recipe) : null, `${sup} for · ${ag} against`].filter(Boolean).join(' · ') }));
  }

  detail() {
    const e = this.entries().find(x => x.id === this.sel);
    if (!e) return null;
    const d = e.detail || {}, sup = e.support || [], ag = e.against || [];
    const kv = [
      ['Status', h('span', { class: 'tag ' + (STATUS_TAG[e.status] || ''), text: STATUS_LABELS[e.status] || e.status })],
      ['Says', e.claim || ''],
      e.recipe ? ['Recipe', recipeText(e.recipe)] : null,
      ['Source', e.label || SOURCE_LABELS[e.source] || e.source || ''],
      d.reference ? ['Reference', d.reference] : null,
      d.user_words ? ['Their words', `“${d.user_words}”`] : null,
      d.observation ? ['Seen', d.observation] : null,
      d.cause_guess ? ['Possible reason', `${d.cause_guess} (a guess)`] : null,
      ['Real runs', String(e.verified_by_real_runs ?? 0)],
      ['Votes', `${sup.length} for · ${ag.length} against`],
      e.status === 'retired' && e.retired_reason ? ['Retired', e.retired_reason] : null,
    ].filter(Boolean);
    const votes = [...sup.map(r => ['for', r]), ...ag.map(r => ['against', r])];
    return {
      title: titleOf(e), sub: STATUS_LABELS[e.status] || '',
      node: h('div', { class: 'detail-body' },
        h('dl', { class: 'kv' }, kv.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', {}, v)])),
        votes.length ? h('div', { class: 'd-sec', text: 'Votes' }) : null,
        votes.length ? h('ul', { class: 'votes' }, votes.map(([v, r]) => h('li', { class: v }, h('b', { text: v }), ' ', h('span', { class: 'mono', text: `job ${r}` })))) : null,
        h('p', { class: 'muted small', text: 'After each real exfoliation the run votes for or against the recipe it used. Testbench runs never vote.' })),
    };
  }
}
