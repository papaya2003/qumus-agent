import { h, api, post, label, ROLES, AGENTS } from './ui.js';

// Keep the form mounted while the rest of Settings updates during playback.
export class ModelSettings {
  constructor(host, billingHost, changed) {
    this.host = host; this.billingHost = billingHost; this.changed = changed;
    this.loaded = false;
  }
  open(replay, serverLive) {
    if (this.loaded) {
      if (serverLive && !replay && Date.now() - this.billingAt > 10000) this.loadBilling();
      return;
    }
    // Boot can render a restored Settings tab before health resolves. Unknown
    // server mode must not latch the standalone-playback view as already loaded.
    if (serverLive == null) {
      this.host.textContent = 'Checking server connection…';
      this.billingHost.textContent = 'Waiting for server information…';
      return;
    }
    this.loaded = true;
    this.replay = replay;
    if (!serverLive) {
      this.host.textContent = 'Open the normal Qumus app to choose a model.';
      this.billingHost.textContent = 'Playback makes no model calls and costs nothing.';
      return;
    }
    this.host.textContent = 'Loading model choices…';
    this.loadModels(replay);
    this.loadBilling();
  }
  async loadModels(replay) {
    try {
      const cfg = await api('/api/models');
      if (!cfg.available) { this.host.textContent = cfg.detail; return; }
      const roles = AGENTS.filter(role => role !== 'orchestrator');
      const models = [...cfg.models];
      for (const id of [cfg.model, ...Object.values(cfg.agent_models || {}).map(c => c.model)]) {
        if (!models.some(m => m.id === id)) models.unshift({ id });
      }
      const catalog = id => models.find(m => m.id === id);
      // Registry inclusion does not grant access; only an account listing or the website does.
      const unusable = id => { const m = catalog(id); return m?.conversational === false || m?.account_listed === false
        || (m?.registered && !m?.website_listed && m?.account_listed !== true); };
      const modelSelect = (id, name, inherit = false) => {
        const select = h('select', { id, 'aria-label': name, disabled: replay });
        if (inherit) select.append(h('option', { value: '', text: 'Use default model' }));
        for (const m of models) select.append(h('option', { value: m.id,
          text: m.id + (m.account_listed === true ? ' · account listed' : m.registered && !m.website_listed ? ' · registry only' : '')
            + (m.conversational === false ? ` · ${(m.modalities || []).join(', ') || 'specialized'}` : m.vision === false ? ' · text only' : ''),
          disabled: inherit && m.conversational === false }));
        return select;
      };
      const select = modelSelect('model-choice', 'Model');
      const limit = h('input', { id: 'model-context-limit', type: 'number', readonly: true,
        placeholder: 'Unknown', 'aria-label': 'Context limit in tokens' });
      const limitNote = h('p', { class: 'muted small model-context-source' });
      const note = h('p', { class: 'model-warning small', role: 'status' });
      const status = h('p', { class: 'small model-status', role: 'status' });
      const summary = h('p', { class: 'muted small routing-summary' });
      const ratesBody = h('div', { class: 'model-rate-list' });
      const rates = h('details', { class: 'settings-details routing-rates' },
        h('summary', { text: 'Budget guard rates' }),
        h('p', { class: 'muted small', text: 'USD per million tokens. Each model has one pair of rates, shared by every agent using it. These conservative rates enforce the budget; billed-average estimates appear in Cost & billing.' }), ratesBody);
      const priceDraft = new Map(), rateInputs = new Map(), team = new Map();
      let saving = false;
      const apply = h('button', { id: 'model-apply', class: 'settings-save', text: 'Save settings', disabled: true });
      const discard = h('button', { class: 'row-btn', text: 'Discard changes', disabled: true });
      const reset = h('button', { class: 'row-btn', text: 'Use default for everyone', disabled: replay });
      const body = h('div', { class: 'routing-team', role: 'group', 'aria-label': 'Specialist models' });
      for (const role of roles) {
        const choice = modelSelect(`model-agent-${role}`, `${label(role)} model`, true);
        const capacity = h('span', { class: 'routing-capacity' });
        const badge = h('span', { class: 'routing-badge' });
        const detail = h('p', { class: 'muted small routing-effective' });
        const warning = h('p', { class: 'model-warning small' });
        const row = h('div', { class: 'routing-row', 'data-agent-model': role },
          h('div', { class: 'routing-role' }, h('span', { class: 'routing-avatar', 'data-a': role, text: label(role)[0] }),
            h('div', {}, h('strong', { text: label(role) }), h('p', { class: 'muted small', text: ROLES[role] }))),
          h('div', { class: 'routing-choice' }, choice, detail, warning),
          h('div', { class: 'routing-info' }, badge, capacity));
        body.append(row); team.set(role, { choice, capacity, badge, detail, warning });
      }
      const captureRates = () => {
        for (const [id, fields] of rateInputs) priceDraft.set(id, fields.map(f => f.value));
      };
      const ratesFor = id => {
        if (!priceDraft.has(id)) priceDraft.set(id, [catalog(id)?.input_price ?? catalog(id)?.administrator_quote?.input ?? '',
          catalog(id)?.output_price ?? catalog(id)?.administrator_quote?.output ?? '']);
        return priceDraft.get(id);
      };
      const draft = () => {
        captureRates();
        const config = id => ({ model: id, input_price: Number(ratesFor(id)[0]), output_price: Number(ratesFor(id)[1]) });
        return { ...config(select.value), agent_models: Object.fromEntries([...team].filter(([, row]) => row.choice.value)
          .map(([role, row]) => [role, config(row.choice.value)])) };
      };
      let savedDraft = '';
      const markDirty = () => {
        const changed = JSON.stringify(draft()) !== savedDraft;
        const unsupported = [select.value, ...[...team.values()].map(row => row.choice.value).filter(Boolean)].some(unusable);
        apply.disabled = replay || saving || !changed || unsupported;
        discard.disabled = replay || saving || !changed;
        status.textContent = changed ? 'Unsaved changes' : 'All changes saved';
      };
      const renderRates = () => {
        captureRates(); rateInputs.clear();
        const used = [...new Set([select.value, ...[...team.values()].map(row => row.choice.value).filter(Boolean)])];
        ratesBody.replaceChildren(...used.map((id, i) => {
          const values = ratesFor(id);
          const fields = ['Input', 'Output'].map((name, n) => {
            const input = h('input', { id: i === 0 ? `model-${name.toLowerCase()}-rate` : null,
              type: 'number', min: '0', step: 'any', value: values[n], disabled: replay,
              'aria-label': `${id} ${name.toLowerCase()} rate`, oninput: markDirty });
            return input;
          });
          rateInputs.set(id, fields);
          return h('div', { class: 'model-rate-row' }, h('strong', { text: id }),
            ...fields.map((input, n) => h('label', {}, n ? 'Output' : 'Input', input)));
        }));
        if (used.some(id => ratesFor(id).some(v => String(v).trim() === '' || !(Number(v) >= 0)))) rates.open = true;
      };
      const refresh = () => {
        const id = select.value, chosen = catalog(id), count = [...team.values()].filter(row => row.choice.value).length;
        limit.value = cfg.context_limits?.[id] ?? (id === cfg.model ? cfg.context_window_tokens : '') ?? '';
        const source = cfg.context_limit_sources?.[id];
        limitNote.textContent = !limit.value ? (cfg.context_limits?.[id] === null && cfg.context_default_limits?.[id] > 0
          ? 'Explicit deployment override: capacity unknown.' : chosen?.context_reason === 'administrator_zero'
          ? 'Administrator reported zero; usable capacity unknown.' : 'No exact administrator ID match; alias capacity is unverified.')
          : source === 'administrator_table'
          ? 'From the gateway administrator’s model catalog.' : 'From this deployment’s configuration.';
        note.textContent = chosen?.conversational === false ? 'This model requires a specialized API and cannot run agent conversations.'
          : chosen?.vision === false ? 'Text-only default: camera-dependent tasks may not work.' : '';
        if (chosen?.administrator_quote) {
          const quote = chosen.administrator_quote;
          note.textContent += ` Administrator quote: $${quote.input} input / $${quote.output} output per million tokens · ${chosen.quote_source_date}. Account applicability, cache prices and multipliers unverified; zero does not establish free usage.`;
        }
        if (chosen?.account_listed !== undefined) note.textContent += chosen.account_listed === true
          ? ' Account listed; successful calls and tool support are not verified by listing.'
          : ' Account access is not established. Registry inclusion does not grant access.';
        note.hidden = !note.textContent;
        for (const [role, row] of team) {
          const effective = row.choice.value || id, custom = !!row.choice.value;
          row.choice.options[0].textContent = 'Use default model';
          row.detail.textContent = custom ? 'Independent of the default' : effective;
          row.badge.textContent = custom ? 'Custom model' : 'Default';
          row.badge.classList.toggle('custom', custom);
          const value = cfg.context_limits?.[effective];
          row.capacity.textContent = value ? `${Number(value).toLocaleString('en-US')} tokens` : 'Context unknown';
          row.warning.textContent = catalog(effective)?.vision === false && ['eyes', 'hands', 'picker', 'reactor'].includes(role)
            ? 'Text only · this role may need camera images.' : '';
          row.warning.hidden = !row.warning.textContent;
        }
        summary.textContent = count ? `${count} specialist${count === 1 ? '' : 's'} use a custom model. The others follow the default.`
          : `All ${roles.length} specialists follow the default model.`;
        reset.disabled = replay || !count;
        renderRates(); markDirty();
      };
      const restore = () => {
        rateInputs.clear(); priceDraft.clear();
        priceDraft.set(cfg.model, [cfg.input_price ?? '', cfg.output_price ?? '']);
        for (const c of Object.values(cfg.agent_models || {})) priceDraft.set(c.model, [c.input_price, c.output_price]);
        select.value = cfg.model;
        for (const [role, row] of team) row.choice.value = cfg.agent_models?.[role]?.model || '';
        // Render the saved state before taking its comparison snapshot.
        refresh(); savedDraft = JSON.stringify(draft()); markDirty();
      };
      select.onchange = refresh;
      for (const row of team.values()) row.choice.onchange = refresh;
      reset.onclick = () => { for (const row of team.values()) row.choice.value = ''; refresh(); };
      discard.onclick = restore;
      apply.onclick = async () => {
        const choice = draft();
        const rateOk = v => String(v).trim() !== '' && Number.isFinite(Number(v)) && Number(v) >= 0;
        if ([choice, ...Object.values(choice.agent_models)].some(c => !ratesFor(c.model).every(rateOk))) {
          rates.open = true; status.textContent = 'Enter finite non-negative input and output budget rates for every selected model.';
          rates.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); return;
        }
        saving = true; markDirty(); status.textContent = 'Saving model assignments…';
        reset.disabled = discard.disabled = true;
        for (const fields of rateInputs.values()) for (const input of fields) input.disabled = true;
        select.disabled = true;
        for (const row of team.values()) row.choice.disabled = true;
        try {
          const saved = await post('/api/model', choice);
          Object.assign(cfg, saved); this.changed(saved); restore();
          status.textContent = 'Saved. These models will be used from the next plan.';
        } catch (err) { status.textContent = err.message || 'Could not save model assignments.'; }
        finally {
          saving = false;
          select.disabled = replay;
          for (const row of team.values()) row.choice.disabled = replay;
          for (const fields of rateInputs.values()) for (const input of fields) input.disabled = replay;
          reset.disabled = replay || ![...team.values()].some(row => row.choice.value);
          apply.disabled = replay || JSON.stringify(draft()) === savedDraft || [select.value, ...[...team.values()].map(row => row.choice.value).filter(Boolean)].some(unusable);
          discard.disabled = replay || JSON.stringify(draft()) === savedDraft;
        }
      };
      this.host.replaceChildren(
        h('div', { class: 'settings-section-heading' }, h('div', {}, h('h2', { text: 'Models & team' }),
          h('p', { class: 'muted', text: 'Choose a shared default. Give a specialist its own model only when you need to.' })),
          h('span', { class: 'tag', text: 'Experimental' })),
        h('div', { class: 'routing-default' },
          h('div', {}, h('label', {}, 'Default model', select),
            h('p', { class: 'muted small', text: 'Used by Qumus and every specialist set to Use default.' })),
          h('div', {}, h('label', {}, 'Context limit · tokens (input + output)', limit), limitNote)),
        h('p', { class: 'muted small model-catalog-count', text: `${cfg.models.length} catalog IDs · ${cfg.models.filter(m => m.registered).length} registered · ${cfg.models.some(m => m.account_listed != null) ? cfg.models.filter(m => m.account_listed).length : 'Unknown'} account listed` }), note,
        h('div', { class: 'routing-team-heading' }, h('h3', { text: 'Specialist models' }), reset), summary, body, rates,
        h('div', { class: 'routing-save-bar' }, h('div', {}, status,
          h('p', { class: 'muted small', text: 'Applies from the next plan with fresh conversation context. Finish any current work before saving.' })),
          h('div', { class: 'routing-save-actions' }, discard, apply)),
        ...(replay ? [h('p', { class: 'muted small', text: 'Return to live to change models. Recorded replies stay unchanged.' })] : []));
      restore();
    } catch (err) {
      this.host.replaceChildren(h('p', { text: err.message }), h('button', { class: 'row-btn', text: 'Retry model list', onclick: () => this.loadModels(replay) }));
    }
  }
  async loadBilling() {
    this.billingAt = Date.now();
    try {
      const b = await api('/api/billing'), snap = b.snapshot;
      const money = n => Number(n).toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 5 });
      const estimate = this.replay ? null : b.estimate;
      const rows = this.replay ? [h('p', { class: 'muted small', text: 'Playback makes no model calls and costs nothing.' })] : estimate ? [h('p', { class: 'billing-amount', text: estimate.usd == null ? 'Workspace estimate unavailable' : `$${Number(estimate.usd).toFixed(2)} estimated · this workspace` }),
        h('p', { class: 'muted small', text: estimate.requests ? `Based on ${estimate.requests} responses and the gateway’s average billed cost per model. Cache and output mix may vary.` : estimate.unpriced_requests || estimate.unpriced_turns ? 'Recorded model activity lacks matching gateway, token usage or calibration data.' : 'No model calls recorded in this workspace.' }),
        estimate.unpriced_requests ? h('p', { class: 'muted small', text: `${estimate.unpriced_requests} requests excluded: usage, gateway identity or calibrated price is unknown.` }) : null,
        estimate.unpriced_turns ? h('p', { class: 'muted small', text: `${estimate.unpriced_turns} historical turns lack per-request usage and are excluded.` }) : null,
      ]
        : [h('p', { class: 'muted small', text: 'No gateway-calibrated estimate is available for this workspace.' })];
      const requestEstimate = this.replay ? null : b.request_estimate;
      if (requestEstimate) rows.push(
        h('p', { class: 'small', text: requestEstimate.actual_usd != null
          ? `Request-matched billed cost: ${money(requestEstimate.actual_usd)}.`
          : requestEstimate.estimate_usd != null
          ? `Token-based estimate: ${money(requestEstimate.estimate_usd)} · ${requestEstimate.requests} requests. Actual bill unavailable.`
          : 'Token-based request estimate unavailable.' }),
        h('p', { class: 'muted small', text: `${requestEstimate.detail} ${requestEstimate.billed_requests} of ${requestEstimate.identified_requests} identified requests have matching bills${requestEstimate.billed_usd == null ? '' : ` totaling ${money(requestEstimate.billed_usd)}`}.` }),
        requestEstimate.unknown_bound_requests ? h('p', { class: 'muted small', text: `${requestEstimate.unknown_bound_requests} requests have no established cache-cost upper bound.` }) : null,
        requestEstimate.unpriced_requests || requestEstimate.unpriced_turns ? h('p', { class: 'muted small', text: `${requestEstimate.unpriced_requests} requests and ${requestEstimate.unpriced_turns} historical turns excluded from the token estimate.` }) : null);
      const priceSources = requestEstimate?.price_sources || [];
      if (priceSources.length) rows.push(h('p', { class: 'muted small', text: priceSources.slice(0, 8).map(p => `${p.model}: ${p.basis === 'request_billed_cost' ? 'request-matched bill' : p.basis === 'administrator_nominal_quote' ? 'administrator nominal quote' : p.basis === 'recorded_configured_token_rates' ? 'saved budget rates' : 'dated route rates'} · ${p.first_at}${p.last_at === p.first_at ? '' : ` to ${p.last_at}`} · ${p.requests} requests`).join('; ') + (priceSources.length > 8 ? `; ${priceSources.length - 8} additional price sources.` : '') }));
      const cache = requestEstimate?.cache_usage?.totals;
      const percent = n => n == null ? 'Unknown' : `${(n * 100).toFixed(1)}%`;
      if (cache?.attempts) rows.push(
        h('p', { class: 'muted small', text: `Input-token cache hit: ${percent(cache.cached_input_fraction)} · miss: ${percent(cache.cached_input_fraction == null ? null : 1 - cache.cached_input_fraction)}. These complementary shares use the same ${Number(cache.cache_eligible_input_tokens).toLocaleString('en-US')} input tokens with valid input/cache measurements in this workspace.` }),
        h('p', { class: 'muted small', text: `Request cache hit: ${cache.cache_hit_requests}/${cache.positive_input_requests} measured positive-input requests (${percent(cache.cache_hit_fraction)}). Coverage of known input tokens: ${percent(cache.cache_input_coverage)}. Requests with unknown cache counts: ${cache.unknown_cache_requests}; with unknown input counts: ${cache.unknown_input_requests}. Unknown attempts are excluded from these rates, not counted as misses.` }));
      const snapshot = snap ? h('details', { class: 'settings-details' },
        h('summary', { text: `Account bill snapshot · ${money(snap.total_usd)}` }),
        ...snap.by_model.map(m => h('p', { class: 'small', text: `${m.model}: ${money(m.usd)} · ${m.requests} requests${m.tokens ? ` · $${(m.usd * 1000000 / m.tokens).toFixed(2)} / million total tokens` : ''}` })),
        h('p', { class: 'muted small', text: `${snap.period} · ${snap.scope} · checked ${new Date(snap.verified_at).toLocaleString()}. This is an account snapshot, not this workspace’s bill.` })) : null;
      this.billingHost.replaceChildren(...[...rows, snapshot,
        b.url ? h('a', { href: b.url, target: '_blank', rel: 'noopener noreferrer', text: 'Open gateway bill ↗' }) : null].filter(Boolean));
    } catch { this.billingHost.textContent = 'Billing unavailable. Playback still makes no model calls.'; }
  }
}
