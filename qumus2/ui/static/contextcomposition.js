// Detailed input attribution lives only in the request inspector.
import { h } from './ui.js';

const PARTS = [
  ['system_instructions', 'System and agent instructions', 'Role, rules and runtime instructions.'],
  ['reference_skills', 'Reference skills', 'Reference blocks inserted by the agent configuration.'],
  ['lab_snapshot', 'Current experiment snapshot', 'Current plan, requirements, project state and linked job evidence.'],
  ['current_task', 'Current user or delegated task', 'The current task actually included in this request.'],
  ['conversation_history', 'Conversation history', 'Earlier user messages and model answer text actually resent.'],
  ['tool_definitions', 'Tool definitions', 'Registered tool descriptions and argument schemas in this request.'],
  ['tool_calls', 'Tool call arguments', 'Retained tool names and call arguments.'],
  ['tool_results', 'Tool results', 'Returned records, job results and specialist reports; media is counted separately.'],
  ['media', 'Images and other media', 'Actual attachments after filtering. URLs, base64 and pixels are not treated as text tokens.'],
  ['protocol_unassigned', 'Protocol and unassigned input', 'Server framing and opaque input cannot be measured here. Unattributed visible text is identified separately.'],
];
const validCount = n => Number.isSafeInteger(n) && n >= 0;
const fmt = n => n.toLocaleString('en-US');

export function compositionData(raw) {
  if (raw?.version !== 1 || raw.method !== 'utf8_bytes_div4_v1' ||
      !['available', 'partial'].includes(raw.status) || !Array.isArray(raw.components) ||
      raw.components.length !== PARTS.length || !validCount(raw.text_estimated_tokens)) return null;
  if (raw.components.some(p => !p || typeof p !== 'object')) return null;
  const byId = new Map(raw.components.map(p => [p.category, p]));
  if (byId.size !== PARTS.length) return null;
  const parts = [];
  for (const [id, title, description] of PARTS) {
    const p = byId.get(id);
    if (!p || !['unknown', 'not_present', 'estimated', 'partial'].includes(p.status) ||
        !validCount(p.text_bytes) || !validCount(p.item_count) || !validCount(p.text_estimated_tokens) ||
        (p.status === 'unknown' ? p.estimated_tokens !== null : !validCount(p.estimated_tokens))) return null;
    parts.push({ ...p, title, description });
  }
  if (parts.reduce((n, p) => n + p.text_estimated_tokens, 0) !== raw.text_estimated_tokens) return null;
  return { ...raw, components: parts };
}

export function compositionInspector(raw, usage = {}) {
  const data = compositionData(raw);
  const root = h('details', { class: 'context-composition', open: true },
    h('summary', { text: 'Context composition' }));
  for (const [field, title] of [['input_tokens', 'Input'], ['output_tokens', 'Output']]) {
    const n = usage[field];
    root.append(h('p', { class: 'small', text: `${title}: ${validCount(n) ? fmt(n) : 'Unknown'} tokens · service report` }));
  }
  root.append(h('h4', { text: 'Input source estimates' }));
  if (!data) {
    root.append(h('p', { class: 'muted small', text: raw
      ? 'Composition is unavailable for this request. No categories were inferred.'
      : 'Composition was not recorded for this request. Historical totals cannot reveal its categories.' }));
    return root;
  }
  root.append(h('p', { class: 'muted small', text: '≈ Local rough estimates (tokens) · shares compare estimated text only. Images and protocol overhead remain unknown.' }),
    h('div', { class: 'composition-parts' }, ...data.components.map(p => {
      const share = data.text_estimated_tokens && p.text_estimated_tokens
        ? `${(p.text_estimated_tokens / data.text_estimated_tokens * 100).toFixed(1)}% of estimated text` : null;
      const value = p.status === 'unknown' ? 'Unknown' : p.status === 'not_present' ? '0 · not present'
        : `≈ ${fmt(p.estimated_tokens)}${p.status === 'partial' ? ' · partial' : ''}`;
      return h('details', { class: 'composition-part', 'data-category': p.category },
        h('summary', {}, h('span', {}, h('span', { text: p.title }), share ? h('small', { class: 'muted', text: share }) : null),
          h('b', { class: 'composition-value', text: value })),
        h('p', { class: 'muted small', text: p.description }),
        p.category === 'media' ? h('p', { class: 'small', text: `${fmt(p.item_count)} attachment${p.item_count === 1 ? '' : 's'} · separate media token usage is not supplied.` })
          : h('p', { class: 'small', text: `${fmt(p.text_bytes)} attributed UTF-8 text bytes · ${fmt(p.item_count)} text segments.` }),
        p.category === 'protocol_unassigned' && p.text_estimated_tokens
          ? h('p', { class: 'small', text: `≈ ${fmt(p.text_estimated_tokens)} unassigned text tokens; total protocol usage remains unknown.` }) : null,
        p.status === 'unknown' && !['media', 'protocol_unassigned'].includes(p.category)
          ? h('p', { class: 'muted small', text: 'Source attribution was unavailable; this is not a measured zero.' }) : null);
    })),
    h('details', { class: 'composition-method' }, h('summary', { text: 'How estimates work' }),
      h('p', { class: 'muted small', text: 'Text estimates use UTF-8 bytes ÷ 4, rounded up once per category. This is a portable proxy, not this model’s tokenizer; language and model differences can be substantial. Percentages refer to the local text estimate, not the service-reported input or model capacity.' }),
      h('p', { class: 'muted small', text: `Estimated text subtotal: ≈ ${fmt(data.text_estimated_tokens)} tokens. It excludes media and hidden framing. It is not scaled to match the provider total, used for billing, or substituted for missing usage.` })));
  return root;
}
