// Per-request provider usage. Never convert characters or cumulative turn totals.
import { h, svg, label, AGENTS } from './ui.js';
import { compositionInspector } from './contextcomposition.js';

const count = n => Number.isSafeInteger(n) && n >= 0 ? n : null;
const format = n => n === null ? 'Unknown' : n.toLocaleString('en-US');
const percent = n => n === null ? 'Unknown' : `${(n * 100).toFixed(1)}%`;
function safeGateway(value) {
  try {
    const u = new URL(value);
    return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password && !u.search && !u.hash ? value : null;
  } catch { return null; }
}

// Replay uses the supplied event prefix. Match the backend cache accounting
// identity rules; never infer cache counts from context size or prompt text.
function cacheAttempts(events) {
  const core = ['request_id', 'call_id', 'session_id', 'model'];
  const optional = ['provider', 'gateway', 'api', 'attempt'];
  const fields = ['input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_tokens'];
  const identity = e => JSON.stringify([...core.map(k => e.payload?.[k] ?? null), e.agent || null]);
  const validIdentity = e => [...core.map(k => e.payload?.[k]), e.agent].every(v => typeof v === 'string' && v.length);
  const deployment = e => JSON.stringify(optional.map(k => e.payload?.[k] === '' ? null : e.payload?.[k] ?? null));
  const signature = e => JSON.stringify(fields.map(k => [Object.hasOwn(e.payload, k), typeof e.payload[k], e.payload[k]]));
  const requests = new Map(), reports = new Map();
  for (const e of events) {
    if (e.kind === 'agent.request') {
      const id = typeof e.payload?.request_id === 'string' && e.payload.request_id ? e.payload.request_id : Symbol();
      if (!requests.has(id)) requests.set(id, []);
      requests.get(id).push(e);
    } else if (e.kind === 'agent.request_usage' && e.payload?.measurement === 'provider_usage_v1' && validIdentity(e)) {
      const id = identity(e);
      if (!reports.has(id)) reports.set(id, []);
      reports.get(id).push(e);
    }
  }
  const attempts = [];
  for (const group of requests.values()) {
    const e = group[0], id = identity(e), route = deployment(e);
    const conflict = group.some(q => identity(q) !== id || deployment(q) !== route);
    const matched = validIdentity(e) ? (reports.get(id) || []).filter(q => deployment(q) === route) : [];
    const inconsistent = matched.some(q => signature(q) !== signature(matched[0]));
    const usage = conflict || inconsistent ? {} : matched[0]?.payload || {};
    attempts.push({ request: e, agents: group.map(q => q.agent), usage, conflict, inconsistent });
  }
  return attempts;
}

export function requestCacheSummary(events, agent = null) {
  const result = { attempts: 0, input_tokens: 0, cache_eligible_input_tokens: 0, cached_input_tokens: 0,
    positive_input_requests: 0, cache_hit_requests: 0, unknown_input_requests: 0, unknown_cache_requests: 0,
    invalid_cache_requests: 0, conflicting_request_requests: 0, conflicting_usage_requests: 0 };
  for (const { usage, conflict, inconsistent } of cacheAttempts(events).filter(a => !agent || a.agents.includes(agent))) {
    const input = count(usage.input_tokens), cached = count(usage.cached_input_tokens);
    result.attempts++;
    result.conflicting_request_requests += Number(conflict);
    result.conflicting_usage_requests += Number(inconsistent);
    result.unknown_input_requests += Number(input === null);
    result.unknown_cache_requests += Number(cached === null);
    result.invalid_cache_requests += Number((usage.cached_input_tokens != null && cached === null) ||
      (input !== null && cached !== null && cached > input));
    result.input_tokens += input ?? 0;
    if (input !== null && cached !== null && cached <= input) {
      result.cache_eligible_input_tokens += input;
      result.cached_input_tokens += cached;
      result.positive_input_requests += Number(input > 0);
      result.cache_hit_requests += Number(cached > 0);
    }
  }
  result.cached_input_fraction = result.cache_eligible_input_tokens ? result.cached_input_tokens / result.cache_eligible_input_tokens : null;
  result.cache_hit_fraction = result.positive_input_requests ? result.cache_hit_requests / result.positive_input_requests : null;
  result.cache_input_coverage = result.input_tokens ? result.cache_eligible_input_tokens / result.input_tokens : null;
  return result;
}

function cachePanel(events, agent = null) {
  const c = requestCacheSummary(events, agent);
  return h('section', { class: 'request-cache-summary', 'aria-label': 'Cache usage' },
    h('h4', { text: 'Cache usage · service reports' }),
    h('p', { text: `Input-token cache hit: ${percent(c.cached_input_fraction)} · ${format(c.cached_input_tokens)} / ${format(c.cache_eligible_input_tokens)} measured input tokens` }),
    h('p', { text: `Requests with a cache hit: ${c.cache_hit_requests} / ${c.positive_input_requests} (${percent(c.cache_hit_fraction)})` }),
    h('p', { class: 'muted small', text: `Coverage of known input tokens: ${percent(c.cache_input_coverage)}. Unknown cache: ${c.unknown_cache_requests} requests; unknown input: ${c.unknown_input_requests}; invalid cache: ${c.invalid_cache_requests}; conflicting identities/reports: ${c.conflicting_request_requests + c.conflicting_usage_requests}.` }),
    h('p', { class: 'muted small', text: 'Only matched request usage counts. Missing or invalid measurements are excluded, not counted as misses. Retries are separate attempts; duplicate events count once. These rates do not identify whether a cache miss originated in the application or gateway.' }));
}

export function requestSeries(events) {
  const cacheUsage = new Map(cacheAttempts(events).map(a => [a.request.payload?.request_id, a.usage]));
  const usage = new Map(events.filter(e => e.kind === 'agent.request_usage' && e.payload?.request_id)
    .map(e => [e.payload.request_id, e]));
  const failures = new Map(events.filter(e => e.kind === 'agent.request_failure' && e.payload?.request_id)
    .map(e => [e.payload.request_id, e]));
  const completions = new Map(events.filter(e => e.kind === 'agent.usage' && e.payload?.request_id)
    .map(e => [e.payload.request_id, e]));
  const matches = (report, e) => report && report.agent === e.agent &&
    ['call_id', 'session_id', 'model'].every(k => report.payload[k] === e.payload[k]);
  const points = [];
  for (const e of events) {
    const p = e.payload || {};
    let measured, historical = false;
    if (e.kind === 'agent.request' && p.token_measurement === 'provider_usage_v1') {
      const report = usage.get(p.request_id);
      // A bad or unrelated record must not fill a request's missing measurement.
      measured = matches(report, e) ? report.payload : {};
    } else if (e.kind === 'agent.usage' && !p.token_measurement && !p.request_id
               && ('input_tokens' in p || 'output_tokens' in p)) {
      // Old recordings already saved response usage, but not request linkage/limits.
      // Preserve those reports as historical response points, not inferred retries.
      measured = p; historical = true;
      if (p.input_tokens === 0 && p.output_tokens === 0) measured = {}; // old SDK default ambiguity
    } else continue;
    const input = count(measured.input_tokens), output = count(measured.output_tokens);
    const total = input !== null && output !== null ? input + output : null;
    const limit = historical ? null : count(p.context_window_tokens) || null;
    points.push({ ...p, key: String(e.id ?? p.request_id ?? `response-${points.length + 1}`),
      number: points.length + 1, agent: e.agent || 'unknown', historical,
      failure: !historical && matches(failures.get(p.request_id), e) ? failures.get(p.request_id).payload : null,
      response_completed: !historical && !!matches(completions.get(p.request_id), e),
      input_tokens: input, output_tokens: output, total_tokens: total,
      cached_input_tokens: count(historical ? measured.cached_input_tokens : cacheUsage.get(p.request_id)?.cached_input_tokens),
      reasoning_output_tokens: count(measured.reasoning_output_tokens),
      context_window_tokens: limit, remaining_tokens: limit !== null && total !== null ? limit - total : null,
      usage_status: measured.usage_status || (input !== null && output !== null ? 'reported' : 'unavailable') });
  }
  // Link only recorded attempts within the same call and agent. Missing old
  // attempt IDs cannot establish a retry, and future replay events are absent.
  const groups = new Map();
  for (const p of points) {
    if (!p.request_id || !p.call_id || !p.session_id || !Number.isInteger(p.attempt)) continue;
    const key = JSON.stringify([p.call_id, p.session_id, p.model, p.agent]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  }
  for (const group of groups.values()) for (let i = 0; i < group.length; i++) {
    const p = group[i], later = group.slice(i + 1).filter(q => q.attempt > p.attempt);
    const previous = group.slice(0, i).findLast(q => q.attempt < p.attempt);
    if (previous) p.retry_of = { key: previous.key, number: previous.number };
    if (later.length) p.retried_by = { key: later[0].key, number: later[0].number };
    const recovered = later.find(q => q.response_completed && !q.failure);
    if (recovered) p.recovered_by = { key: recovered.key, number: recovered.number };
  }
  return points;
}

export function requestEmptyText(events, view = 'recording') {
  const mode = events.filter(e => e.kind === 'notification' && /^agents: (scripted|sdk) mode$/.test(e.payload?.text || '')).at(-1)?.payload.text;
  const modelEvidence = events.some(e => ['agent.usage', 'agent.request', 'agent.request_usage'].includes(e.kind) ||
    (e.kind === 'agent.turn_end' && ['openai', 'claude'].includes(e.payload?.provider)));
  if (mode === 'agents: scripted mode' && !modelEvidence) return 'Scripted execution: no model requests are made.';
  if (view === 'prefix') return 'No token usage is available at this playback position. Open History → Context for the complete recording.';
  if (view === 'live') return 'No token usage has been recorded in this run yet.';
  return 'No token usage was saved in this recording. Character counts cannot be converted into exact tokens.';
}

// Scale the measured stack, independently from capacity unless explicitly requested.
export function requestScale(points, mode = 'usage') {
  const peak = points.reduce((max, p) => Math.max(max,
    (p.input_tokens ?? 0) + (p.output_tokens ?? 0),
    mode === 'capacity' ? p.context_window_tokens ?? 0 : 0), 1);
  const step = 10 ** Math.floor(Math.log10(Math.max(peak / 4, 1)));
  const tick = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find(n => n * step >= peak / 4);
  return Math.max(4, tick * step * 4);
}

const agentColor = a => AGENTS.includes(a) ? `var(--a-${a}, var(--accent))` : 'var(--muted)';
const agentBadge = a => h('span', { class: 'request-agent-badge' },
  h('i', { style: { background: agentColor(a) } }), label(a));
const description = p => `Request ${p.number}: input ${format(p.input_tokens)}, output ${format(p.output_tokens)} tokens, ${label(p.agent)}, ${p.model || 'Unknown model'} · ${p.total_tokens !== null && p.context_window_tokens !== null ? `${(p.total_tokens / p.context_window_tokens * 100).toFixed(1)}% of context used` : 'Context ratio unknown'}`;

const FAILURE_LABELS = {
  invalid_request: 'Request rejected', authentication: 'Authentication rejected', access_denied: 'Access denied',
  timeout: 'Request timed out', conflict: 'Request conflict', request_too_large: 'Request too large',
  rate_limit: 'Rate limited', server_error: 'Service error', http_error: 'HTTP error',
  connection_error: 'Connection failed', cancelled: 'Request interrupted', invalid_response: 'Invalid model response',
  response_error: 'Response processing failed', response_failed: 'Model reported a failure',
  response_incomplete: 'Model response incomplete',
};
export const requestStatus = p => p.recovered_by ? `Recovered by #${p.recovered_by.number}`
  : p.retried_by ? `Retried as #${p.retried_by.number}` : p.failure ? FAILURE_LABELS[p.failure.failure_kind] || 'Request failed'
  : p.response_completed ? 'Response received' : 'Outcome not recorded';

export function requestInspector(p, compact = false, onRequest = null) {
  const remaining = p.remaining_tokens, limit = p.context_window_tokens;
  const capacity = limit === null
    ? (p.historical ? 'Context limit: Unknown · Remaining: Unknown (not recorded).'
      : 'Context limit: Unknown · Remaining: Unknown. No usable model deployment limit was recorded.')
    : `${p.context_limit_source === 'administrator_table' ? 'Administrator table context limit' : 'Configured context limit'}: ${format(limit)} tokens · ` + (remaining === null
      ? 'Remaining: Unknown (usage unavailable).'
      : remaining < 0 ? `Over the configured limit by ${format(-remaining)} tokens.`
      : `Remaining after input + output: ${format(remaining)} tokens.`);
  const ratio = limit !== null && p.total_tokens !== null ? p.total_tokens / limit : null;
  const root = h('div', { class: compact ? 'request-detail' : 'request-inspector' },
    h('div', { class: 'request-selected-title' }, h('b', { text: `Request ${format(p.number)}` }), agentBadge(p.agent),
      p.attempt > 1 ? h('span', { class: 'tag', text: `Retry ${p.attempt - 1}` }) : null),
    h('div', { class: 'muted small', text: `${p.model || 'Unknown model'}${p.historical ? ' · historical response usage' : ''}` }),
    h('div', { class: 'request-attempt-status' },
      h('b', { text: requestStatus(p) }),
      p.retried_by ? h('span', { class: 'muted small', text: 'A retry is another attempt within the same agent call.' }) : null,
      p.failure ? h('span', { class: 'muted small', text: `${FAILURE_LABELS[p.failure.failure_kind] || 'Request failed'}${p.failure.http_status ? ` · HTTP ${p.failure.http_status}` : ''}${p.failure.exception_type ? ` · ${p.failure.exception_type}` : ''}` })
        : p.retried_by ? h('span', { class: 'muted small', text: 'The reason for this earlier attempt was not recorded.' }) : null,
      ...[['retry_of', 'Previous attempt'], ['recovered_by', 'Recovered by'], ...(!p.recovered_by || p.retried_by?.key !== p.recovered_by.key ? [['retried_by', 'Next attempt']] : [])]
        .filter(([key]) => p[key]).map(([key, title]) => onRequest
          ? h('button', { class: 'lnk-btn', text: `${title}: request #${p[key].number}`, onclick: () => onRequest(p[key].key) })
          : h('span', { class: 'small', text: `${title}: request #${p[key].number}` }))),
    compact ? h('div', { class: 'request-metrics' },
      h('span', {}, 'Input: ', h('b', { text: format(p.input_tokens) })),
      h('span', {}, 'Output: ', h('b', { text: format(p.output_tokens) }))) : null,
    h('b', { class: 'small', text: `Used (input + output): ${format(p.total_tokens)} tokens` }),
    h('p', { class: 'request-cache-detail small', text: `Cached input: ${format(p.cached_input_tokens)} tokens · input-token cache hit: ${percent(p.input_tokens > 0 && p.cached_input_tokens !== null && p.cached_input_tokens <= p.input_tokens ? p.cached_input_tokens / p.input_tokens : null)}` }),
    ratio === null ? null : h('div', { class: 'request-capacity' },
      h('div', { class: 'request-capacity-label' }, h('b', { text: `${(ratio * 100).toFixed(1)}% used` }),
        h('span', { text: remaining < 0 ? 'Over capacity' : `${((1 - ratio) * 100).toFixed(1)}% remaining` })),
      h('div', { class: 'request-capacity-track', role: 'meter', 'aria-label': 'Context capacity used',
        'aria-valuemin': 0, 'aria-valuemax': limit, 'aria-valuenow': Math.min(limit, p.total_tokens),
        'aria-valuetext': `${format(p.total_tokens)} of ${format(limit)} tokens${remaining < 0 ? ', over capacity' : ''}` },
        h('i', { class: ratio >= .9 ? 'near-limit' : '', style: { width: `${Math.min(100, ratio * 100)}%` } }))),
    h('p', { class: 'muted small', text: capacity }),
    ratio !== null && ratio >= .9 ? h('strong', { class: 'token-warning small', text: remaining < 0
      ? 'Reported usage exceeds the configured limit; check the deployment setting.' : 'Near the configured context limit.' }) : null);
  if (!compact) {
    root.append(h('p', { class: 'muted small', text: 'Input/output totals come from service usage reports. Local category estimates below do not replace them.' }),
      compositionInspector(p.input_composition, p));
    if (p.reasoning_output_tokens !== null) root.append(h('p', { class: 'muted small', text:
      `Reasoning output: ${format(p.reasoning_output_tokens)} tokens (service report; already included in Output).` }));
    const facts = [['Calling agent', label(p.agent)], ['Agent ID', p.agent], ['Target model', p.model], ['API', p.api], ['Usage report', p.usage_status],
      ['Gateway', safeGateway(p.gateway)], ['Request ID', p.request_id], ['Logical call', p.call_id], ['Session', p.session_id],
      ['Attempt', p.attempt], ['HTTP status', p.failure?.http_status],
      ['Provider request ID', p.failure?.provider_request_id], ['Provider error code', p.failure?.provider_error_code], ['Output budget', p.max_output_tokens == null ? null : `${format(p.max_output_tokens)} tokens`]];
    root.append(h('dl', { class: 'request-facts' }, facts.filter(([, v]) => v != null).flatMap(([k, v]) =>
      [h('dt', { text: k }), h('dd', { text: String(v) })])),
    h('p', { class: 'muted small', text: 'Service-reported tokens include reported image input. Cached input remains in the context; reasoning is already included in output. Missing usage stays unknown. Capacity uses this request’s recorded limit and does not predict the next request.' }));
  }
  return root;
}

export function requestChart(events, state = {}, { view = 'recording', onSelect } = {}) {
  const all = requestSeries(events), agents = [...new Set(all.map(p => p.agent))];
  const root = h('section', { class: 'request-chart', 'aria-label': 'Request tokens' });
  if (!all.length) {
    state.current = null;
    root.append(h('h3', { text: 'Request tokens' }), h('p', { class: 'placeholder', text: requestEmptyText(events, view) }));
    return root;
  }
  if (!agents.includes(state.agent)) state.agent = '';
  const render = (animate = false) => {
    const oldScroll = root.querySelector('.request-chart-scroll')?.scrollLeft ?? state.scrollLeft ?? 0;
    const previousMax = state.visibleMax ?? state.maxY;
    const points = state.agent ? all.filter(p => p.agent === state.agent) : all;
    const lanes = agents.filter(a => !state.agent || a === state.agent);
    const reported = points.filter(p => p.total_tokens !== null);
    const peak = reported.reduce((max, p) => Math.max(max, p.total_tokens), 0);
    let selected = points.find(p => p.key === state.selectedKey) || points.at(-1);
    state.current = selected.key;
    const filter = h('select', { 'aria-label': 'Filter requests by agent', onchange: e => {
      state.agent = e.target.value; render();
      onSelect?.(all.find(p => p.key === state.current), false);
    } }, h('option', { value: '', text: `All agents (${all.length})` }), ...agents.map(a =>
      h('option', { value: a, text: `${label(a)} (${all.filter(p => p.agent === a).length})` })));
    filter.value = state.agent;
    const controls = h('div', { class: 'request-controls' }, h('label', {}, 'Agent ', filter),
      h('div', { class: 'seg small', role: 'group', 'aria-label': 'Token chart scale' },
        ...[['usage', 'Actual usage'], ['capacity', 'Full capacity']].map(([value, text]) =>
          h('button', { text, class: (state.scale || 'usage') === value ? 'on' : '',
            'aria-pressed': String((state.scale || 'usage') === value), onclick: () => { if (state.scale === value) return; state.scale = value; render(true); } }))));
    const width = Math.max(760, points.length * (matchMedia('(pointer: coarse)').matches ? 44 : 32) + 120), left = 96, right = 24, top = 30, plotH = 180;
    const plotW = width - left - right, laneTop = top + plotH + 58, height = laneTop + lanes.length * 24 + 10;
    const maxY = requestScale(points, state.scale);
    state.maxY = maxY;
    const zoomFrom = animate && previousMax && !matchMedia('(prefers-reduced-motion: reduce)').matches ? previousMax : maxY;
    const x = i => left + (i + .5) * plotW / points.length;
    let visibleMax = zoomFrom;
    const y = n => top + plotH * (1 - n / visibleMax);
    const scaled = [];
    const axisLabels = [];
    const axis = svg('svg', { class: 'request-fixed-axis', viewBox: `0 0 ${left} ${height}`,
      'aria-hidden': 'true', style: `width:${left}px;height:${height}px` });
    const chart = svg('svg', { class: 'request-series', viewBox: `${left} 0 ${width - left} ${height}`, role: 'group',
      'aria-label': 'Request number versus total context tokens', 'data-max-tokens': maxY,
      style: `width:${width - left}px;min-width:${width - left}px;height:${height}px` });
    const text = (value, attrs, host = chart) => { const node = svg('text', attrs, host); node.textContent = value; };
    text('Tokens', { x: left - 10, y: 16, 'text-anchor': 'end', class: 'request-axis-title' }, axis);
    for (let i = 0; i <= 4; i++) {
      svg('line', { x1: left, y1: top + plotH * (1 - i / 4), x2: width - right, y2: top + plotH * (1 - i / 4), class: 'request-grid' }, chart);
      text(format(zoomFrom * i / 4), { x: left - 10, y: top + plotH * (1 - i / 4) + 4, 'text-anchor': 'end' }, axis);
      axisLabels.push(axis.lastChild);
    }
    const ticks = [...new Set(Array.from({ length: Math.min(6, points.length) }, (_, i) =>
      Math.round(i * (points.length - 1) / Math.max(1, Math.min(6, points.length) - 1))))];
    for (const i of ticks) text(format(points[i].number), { x: x(i), y: top + plotH + 23, 'text-anchor': 'middle' });
    text('Request number', { x: left + plotW / 2, y: top + plotH + 44, 'text-anchor': 'middle', class: 'request-axis-title' });
    lanes.forEach((a, i) => {
      const cy = laneTop + i * 24;
      svg('line', { x1: left, x2: width - right, y1: cy, y2: cy, class: 'request-grid' }, chart);
      text(label(a), { x: left - 10, y: cy + 4, 'text-anchor': 'end', class: 'request-agent-label' }, axis);
    });
    const revealPoint = p => {
      const scroller = root.querySelector('.request-chart-scroll');
      if (!scroller) return;
      const position = x(points.indexOf(p)) - left;
      if (position < scroller.scrollLeft + 20) scroller.scrollLeft = position - 20;
      else if (position > scroller.scrollLeft + scroller.clientWidth - 20) scroller.scrollLeft = position - scroller.clientWidth + 20;
      state.scrollLeft = scroller.scrollLeft;
    };
    const select = (p, { remember = true, reveal = false, notify = false } = {}) => {
      selected = p; state.current = p.key;
      if (remember) state.selectedKey = p.key;
      for (const el of root.querySelectorAll('[data-request-number]')) {
        const active = +el.dataset.requestNumber === p.number;
        el.classList.toggle('selected', active);
        el.setAttribute('aria-pressed', String(active));
      }
      if (notify) { revealPoint(p); onSelect?.(p, reveal); }
    };
    state.selectKey = key => {
      const target = points.find(p => p.key === key);
      if (target) { select(target); revealPoint(target); }
    };
    const activate = p => select(p, { reveal: true, notify: true });
    const bind = (el, p) => {
      el.addEventListener('focus', () => select(p, { notify: true }));
      el.addEventListener('click', () => activate(p));
      el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(p); } });
    };
    for (let i = 0; i < points.length; i++) {
      const p = points[i], px = x(i), barW = Math.min(14, plotW / points.length * .65);
      if (i && (p.model !== points[i - 1].model || p.session_id !== points[i - 1].session_id)) {
        const boundary = svg('line', { x1: (x(i - 1) + px) / 2, x2: (x(i - 1) + px) / 2,
          y1: top, y2: top + plotH, class: 'request-model-boundary' }, chart);
        svg('title', {}, boundary).textContent = `New model or session: ${p.model || 'Unknown'}`;
      }
      const group = svg('g', { class: 'request-point', 'data-number': p.number, 'data-request-number': p.number,
        tabindex: '0', role: 'button', 'aria-label': `${description(p)} · ${requestStatus(p)}` }, chart);
      svg('title', {}, group).textContent = `${description(p)} · ${requestStatus(p)}`;
      svg('rect', { x: px - plotW / points.length / 2, y: top - 4, width: plotW / points.length,
        height: plotH + 8, class: 'request-hit' }, group);
      let stack = 0;
      for (const [field, cls] of [['total_tokens', 'total']]) {
        if (p[field] === null) continue;
        const bar = svg('rect', { x: px - barW / 2, width: barW, class: `token-${cls} request-bar` }, group);
        scaled.push({ node: bar, start: stack, value: p[field], kind: 'bar' });
        if (p[field] === 0) scaled.push({ node: svg('circle', { cx: px, r: 2, class: `token-${cls}` }, group), value: stack, kind: 'circle' });
        stack += p[field];
      }
      if (p.total_tokens === null) {
        const node = svg('text', { x: px, 'text-anchor': 'middle', class: 'token-unknown' }, group);
        node.textContent = p.retried_by ? '↻' : p.failure ? '!' : '?';
        scaled.push({ node, value: stack, kind: 'unknown' });
      }
      bind(group, p);
      const lane = svg('g', { class: 'request-agent-point', 'data-request-number': p.number,
        'data-agent': p.agent, tabindex: '0', role: 'button', 'aria-label': `Request ${p.number} by ${label(p.agent)}` }, chart);
      const ly = laneTop + lanes.indexOf(p.agent) * 24;
      svg('title', {}, lane).textContent = description(p);
      svg('rect', { x: px - plotW / points.length / 2, y: ly - 11, width: plotW / points.length, height: 22, fill: 'transparent' }, lane);
      svg('rect', { x: px - barW / 2, y: ly - 4, width: barW, height: 8, rx: 2,
        fill: agentColor(p.agent), class: 'request-agent-mark' }, lane);
      bind(lane, p);
    }
    if (state.scale === 'capacity') {
      for (let start = 0; start < points.length;) {
        const p = points[start]; let end = start;
        const same = q => q.model === p.model && q.gateway === p.gateway && q.session_id === p.session_id
          && q.context_window_tokens === p.context_window_tokens && q.context_limit_source === p.context_limit_source;
        while (end + 1 < points.length && points[end + 1].number === points[end].number + 1 && same(points[end + 1])) end++;
        if (p.context_window_tokens !== null) {
          const node = svg('line', { x1: x(start) - 7, x2: x(end) + 7,
            class: `request-line token-limit${start === end ? ' singleton' : ''}` }, chart);
          svg('title', {}, node).textContent = `${p.model || 'Unknown model'}: ${format(p.context_window_tokens)} tokens · requests ${p.number}–${points[end].number}`;
          scaled.push({ node, value: p.context_window_tokens, kind: 'limit' });
          if (x(end) - x(start) > 100) {
            const caption = svg('text', { x: x(start) - 7, class: 'request-limit-label' }, chart);
            caption.textContent = format(p.context_window_tokens);
            scaled.push({ node: caption, value: p.context_window_tokens, kind: 'unknown' });
          }
        }
        start = end + 1;
      }
    }
    const drawScale = scale => {
      visibleMax = scale; state.visibleMax = scale;
      axisLabels.forEach((node, i) => { node.textContent = format(Math.round(scale * i / 4)); });
      for (const { node, kind, value, start } of scaled) {
        if (kind === 'bar') {
          node.setAttribute('y', y(start + value)); node.setAttribute('height', value / scale * plotH);
        } else if (kind === 'limit') {
          node.setAttribute('y1', y(value)); node.setAttribute('y2', y(value));
        } else node.setAttribute(kind === 'circle' ? 'cy' : 'y', y(value) - (kind === 'unknown' ? 5 : 0));
      }
    };
    drawScale(zoomFrom);
    root.replaceChildren(...[h('h3', { text: 'Request tokens' }),
      h('p', { class: 'muted small', text: 'Select a bar or agent mark to inspect the request on the right.' }),
      h('div', { class: 'tags' }, h('span', { class: 'tag', text: `${points.length} request / response points` }),
        reported.length ? h('span', { class: 'tag', text: `Peak total context: ${format(peak)} tokens` }) : null,
        reported.length !== points.length ? h('span', { class: 'tag warn', text: `${points.length - reported.length} usage reports incomplete or unavailable` }) : null,
        points.some(p => p.retried_by) ? h('span', { class: 'tag', text: `${points.filter(p => p.retried_by).length} retried attempts · ${points.filter(p => p.recovered_by).length} recovered` }) : null),
      controls, cachePanel(events, state.agent),
      h('div', { class: 'request-legend' }, ...[['total', 'Total context'],
        ...(state.scale === 'capacity' ? [['limit', 'Recorded capacity per request']] : [])].map(([cls, title]) => h('span', {}, h('i', { class: `token-${cls}` }), title)),
        h('span', { class: 'muted', text: '↻ Retried · ! Failed/interrupted · ? Unknown' }),
        h('span', { class: 'muted', text: state.scale === 'capacity' ? 'Scale includes recorded limits' : 'Scale fits actual usage · capacity shown separately' })),
      h('div', { class: 'request-plot' }, axis,
        h('div', { class: 'request-chart-scroll', tabindex: '0', role: 'region', 'aria-label': 'Scrollable request chart' }, chart)),
    ].filter(Boolean));
    const scroller = root.querySelector('.request-chart-scroll');
    const navigator = h('input', { type: 'range', min: 0, max: 0, step: 1, value: 0,
      'aria-label': 'Scroll requests', class: 'request-scroll-control' });
    root.append(h('div', { class: 'request-scroll-track' }, navigator));
    const syncScroll = () => {
      const maximum = Math.max(0, scroller.scrollWidth - scroller.clientWidth);
      navigator.max = maximum; navigator.value = scroller.scrollLeft; navigator.disabled = !maximum;
      navigator.style.setProperty('--thumb-width', `${Math.max(36, scroller.clientWidth ** 2 / Math.max(1, scroller.scrollWidth))}px`);
      navigator.setAttribute('aria-valuetext', maximum ? `${Math.round(scroller.scrollLeft / maximum * 100)}% through requests` : 'All requests visible');
    };
    navigator.oninput = () => { scroller.scrollLeft = Number(navigator.value); };
    scroller.addEventListener('scroll', syncScroll, { passive: true });
    state.scrollObserver?.disconnect();
    state.scrollObserver = new ResizeObserver(syncScroll); state.scrollObserver.observe(scroller);
    requestAnimationFrame(syncScroll);
    scroller.addEventListener('scroll', () => { if (root.isConnected && scroller.clientWidth) state.scrollLeft = scroller.scrollLeft; }, { passive: true });
    state.restoreViewport = () => requestAnimationFrame(() => { if (root.isConnected && scroller.clientWidth) scroller.scrollLeft = state.scrollLeft ?? oldScroll; });
    requestAnimationFrame(() => { if (root.contains(chart)) { scroller.scrollLeft = state.scrollLeft ?? oldScroll; if (state.scrollLeft == null) revealPoint(selected); } });
    select(selected, { remember: false });
    if (zoomFrom !== maxY) {
      chart.dataset.zooming = 'true';
      let started;
      const zoom = now => {
        if (!root.contains(chart)) return;
        started ??= now;
        const t = Math.min(1, (now - started) / 420), eased = t * t * (3 - 2 * t);
        drawScale(Math.exp(Math.log(zoomFrom) * (1 - eased) + Math.log(maxY) * eased));
        if (t < 1) requestAnimationFrame(zoom);
        else { drawScale(maxY); delete chart.dataset.zooming; }
      };
      requestAnimationFrame(zoom);
    }
  };
  render();
  return root;
}
