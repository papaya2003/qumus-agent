// Hardware recovery requires a later accepted retry of the same task/target.
// Refused plan proposals can be superseded by an accepted plan; delegation
// prerequisite refusals can become historical once that role passes the gates.
// Neither case proves a physical job completed or erases the original refusal.
const TARGETS = ['chip_id', 'chip_ids', 'flake_id', 'flake_ids', 'target_chip_id', 'stamp_id', 'job_id', 'run_id', 'material', 'role', 'device_name'];
const canonical = x => Array.isArray(x) ? x.map(canonical) : x && typeof x === 'object'
  ? Object.fromEntries(Object.keys(x).sort().map(k => [k, canonical(x[k])])) : x;
const stable = x => JSON.stringify(canonical(x));
const taskStep = text => (text || '').match(/^\s*\[?([A-Za-z]*\d+[A-Za-z0-9_.-]*)\]?\s*[:\-\]]?\s/)?.[1];
const prerequisiteRules = new Set(['plan_first', 'eyes_first', 'advisor_first', 'sample_outcomes_required',
  'reactor_outcome_required', 'physical_operation_required', 'flow_location_contract', 'recipe_incomplete']);
const checkedPrerequisiteRules = new Set(['search_outcome_contract']);
const jobReads = new Set(['wait_job', 'get_job_status']);
const unknownJobResult = (call, p) => jobReads.has(call.tool) && call.args.job_id
  && (p.data?.error === 'unknown_job' && p.data.requested_job_id === call.args.job_id
    || (p.summary || '').startsWith(`no job '${call.args.job_id}':`));
const completionContract = step => stable(Object.fromEntries(['owner', 'operation', 'materials', 'operation_args',
  'sample_outcomes', 'reactor_outcome'].map(k => [k, step?.[k] ?? null])));
const completionRefusal = issue => issue.call.tool === 'save_project_status' && issue.call.args.status === 'done'
  && issue.event.agent === 'orchestrator' && issue.event.payload.status === 'error'
  && (/^Physical plan steps .* are still incomplete;|^Physical jobs remain unresolved in this project:/.test(issue.event.payload.summary || '')
    || issue.event.payload.data?.requirement_errors?.length > 0);

// A corrected inspection must retain every valid original region and replace
// the rejected name with regions the failure itself lists as supported. This
// does not infer aliases, relax hardware targets or match arbitrary errors.
function correctedInspection(failed, retry) {
  if (failed.call.tool !== 'inspect_bench') return false;
  const message = failed.event.payload.message || '';
  const match = message.match(/unknown region '([^']+)'; known: \[([^\]]*)\]/);
  if (!match) return false;
  const known = new Set([...match[2].matchAll(/'([^']+)'/g)].map(m => m[1]));
  const {regions: before, ...oldOptions} = failed.call.args;
  const {regions: after, ...newOptions} = retry.args;
  if (!Array.isArray(before) || !Array.isArray(after) || stable(oldOptions) !== stable(newOptions)) return false;
  const retained = before.filter(r => r !== match[1]);
  return before.includes(match[1]) && retained.length > 0 && retained.every(r => known.has(r) && after.includes(r))
    && after.every(r => known.has(r)) && after.some(r => !retained.includes(r));
}

export class ToolAttempts {
  constructor() {
    this.plan = []; this.revision = null; this.project = null;
    this.calls = new Map(); this.issues = new Map(); this.jobs = new Map();
    this.jobCalls = new Map(); this.jobIssues = new Map(); this.unsafeJobs = new Set();
    this.delegations = new Map(); this.jobLinks = new Map();
  }
  callKey(e) { const p = e.payload || {}; return p.tool_use_id || `${e.agent}|${p.tool}`; }
  context(e) {
    const p = e.payload || {}, args = p.args || {};
    const step = this.plan.find(s => s.owner === e.agent && ['running', 'in_progress'].includes(s.status));
    const target = Object.fromEntries(TARGETS.filter(k => args[k] != null).map(k => [k, args[k]]));
    // An optimistic-lock retry may refresh its version while submitting the
    // identical plan. Different plans/notes remain different attempts.
    const scopeArgs = p.tool === 'update_plan' ? Object.fromEntries(Object.entries(args).filter(([k]) => k !== 'expected_version')) : args;
    const active = [...this.delegations.values()].filter(d => d.project === this.project && d.role === e.agent);
    return { step: step?.id || null,
      scope: `${e.agent}|${step?.id || 'unplanned'}|${p.tool}|${stable(Object.keys(target).length ? target : scopeArgs)}`,
      // the same agent calling the same tool for the same plan step, whatever the arguments
      stepTool: `${this.project}|${e.agent}|${step?.id || 'unplanned'}|${p.tool}`,
      task: e.agent && step ? stable([this.project, this.revision, e.agent, step.id, p.tool]) : null,
      tool: p.tool, args, event: e, project: this.project, delegation: active.length === 1 ? active[0] : null,
      planContracts: ['update_plan', 'refine_plan'].includes(p.tool)
        ? this.plan.map(s => ({id: s.id, owner: s.owner, operation: s.operation, contract: completionContract(s)})) : [] };
  }
  unknownReference(call, e) {
    const id = call.args.job_id, p = e.payload, d = call.delegation;
    if (!d || !unknownJobResult(call, p) || !/^run_[0-9a-f]{8,}$/i.test(id || '')) return null;
    // Display-only association for a truncated reference: the exact original
    // job must already be linked to this step. Never an execution alias.
    const links = [...this.jobLinks.values()].filter(link => link.payload.project_id === d.project
      && link.payload.step_id === d.step && link.payload.flow === d.operation
      && link.run_id?.startsWith(id) && link.run_id.length > id.length && link.id < e.id);
    return links.length === 1 ? {requested: id, original: links[0]} : null;
  }
  add(e) {
    const p = e.payload || {}, changed = { opened: [], resolved: [], recoveredJobs: [] };
    const resolve = (issue, evidence, text, proofs = [evidence]) => {
      issue.resolved = text; issue.resolution = evidence; issue.resolutionEvidence = proofs; changed.resolved.push(issue);
    };
    if (e.kind === 'plan.updated' || e.kind === 'plan.step') {
      this.plan = p.plan || this.plan; this.revision = p.revision ?? this.revision; this.project = p.project_id ?? this.project;
    }
    if (e.kind === 'agent.job_linked' && p.project_id && p.step_id && e.run_id === p.job_id) {
      const key = stable([p.project_id, p.step_id, e.run_id]);
      if (!this.jobLinks.has(key)) this.jobLinks.set(key, e);
    }
    if (e.kind === 'agent.delegate') {
      const step = this.plan.find(s => s.id === taskStep(p.task) && s.owner === p.to);
      // A new delegation of the same step to the same role replaces one that
      // never got its check (e.g. the brain stopped and resumed from a save
      // point): only the newest can still be checked or carry tool calls.
      if (this.project && step) for (const [key, d] of [...this.delegations.entries()]) {
        if (d.project === this.project && d.role === p.to && d.step === step.id) this.delegations.delete(key);
      }
      if (this.project && step) this.delegations.set(p.tool_use_id || e.id,
        {event: e, project: this.project, step: step.id, role: p.to, operation: step.operation,
          contract: completionContract(step), reads: [], selections: []});
      for (const issue of this.issues.values()) {
        const prior = issue.event.payload;
        if (!issue.resolved && prior.tool === 'delegate' && prior.args?.to === p.to
          && prerequisiteRules.has(prior.rule) && issue.project === this.project
          && (taskStep(prior.args?.task) ? taskStep(prior.args.task) === taskStep(p.task) : prior.rule === 'plan_first')) {
          resolve(issue, e, 'a later delegation passed the prerequisite checks; original refusal retained');
        }
      }
    }
    if (e.kind === 'agent.delegation_checked') {
      const active = [...this.delegations.entries()].filter(([, d]) => d.project === this.project
        && d.role === p.to && d.step === p.step);
      if (active.length === 1) {
        const [key, d] = active[0];
        for (const issue of this.issues.values()) {
          if (issue.resolved || !this.project || issue.project !== this.project) continue;
          if (p.ok === false && issue.lookup && issue.call.delegation === d) issue.failedDelegation = e;
          if (p.ok !== true || p.status !== 'done') continue;
          const prior = issue.event.payload;
          // A rejected attempt to remove/change an unfinished obligation is
          // recovered when that unchanged obligation actually completes.
          // Merely declaring the project done or completing another job is not proof.
          const obligation = issue.physicalObligation;
          if (obligation && obligation.id === d.step && obligation.owner === d.role
            && obligation.operation === d.operation && obligation.contract === d.contract) {
            const links = [...this.jobLinks.values()].filter(link => link.payload.project_id === d.project
              && link.payload.step_id === d.step && link.payload.flow === d.operation
              && link.id > issue.event.id && this.jobs.get(link.run_id)?.payload.status === 'succeeded'
              && (p.facts || []).includes(`job ${link.run_id} (${d.operation}) succeeded.`));
            if (links.length === 1) resolve(issue, e,
              'the unchanged physical obligation completed with its linked successful job; rejected edit retained',
              [d.event, links[0], this.jobs.get(links[0].run_id), e]);
          }
          // A refused selection-blocked report (e.g. "all candidates have
          // images") is recovered when the same picker step later completes
          // with an accepted selection that fills that role: the claimed
          // blocker did not hold. The refusal itself stays on record.
          if (prior.tool === 'report_selection_blocked' && issue.event.agent === d.role
            && (issue.call.delegation?.step || issue.call.step) === d.step
            && (p.facts || []).some(f => f.startsWith('submit_flake_selection:'))) {
            const selection = d.selections.find(sel => sel.event.id > issue.event.id
              && (sel.call.args.selected_by_role?.[issue.call.args?.role] || []).length > 0);
            if (selection) resolve(issue, e,
              'the same step later completed with an accepted flake selection for this role; refused report retained',
              [d.event, selection.call.event, selection.event, e]);
          }
          if (prior.tool === 'delegate' && checkedPrerequisiteRules.has(prior.rule)
            && prior.args?.to === p.to && taskStep(prior.args.task) === p.step) {
            resolve(issue, e, 'the same step passed its prerequisites and completed its delegation', [d.event, e]);
          }
          if (!issue.lookup || !issue.failedDelegation || d.event.id <= issue.failedDelegation.id
            || issue.call.delegation.step !== d.step || issue.call.delegation.role !== d.role
            || issue.call.delegation.contract !== d.contract) continue;
          const rid = issue.lookup.original.run_id;
          const read = d.reads.find(r => r.event.payload.data.run_id === rid || r.event.payload.data.job_id === rid);
          if (!read || !(p.facts || []).some(f => f.startsWith(`job ${rid} (`) && f.includes(') succeeded'))) continue;
          resolve(issue, e, 'the exact original job was read and the same step passed its completion check; failed lookup retained',
            [issue.lookup.original, issue.failedDelegation, d.event, read.call.event, read.event, e]);
        }
        this.delegations.delete(key);
      }
    }
    if (e.kind === 'job.submitted') this.jobCalls.set(e.run_id,
      this.context({...e, payload: {tool: p.flow, args: p.params || {}}}));
    if (e.kind === 'safety.violation') this.unsafeJobs.add(e.run_id);
    if (e.kind === 'tool.call') this.calls.set(this.callKey(e), this.context(e));
    if (e.kind === 'tool.denied' || e.kind === 'tool.result') {
      const call = this.calls.get(this.callKey(e)) || this.context(e);
      this.calls.delete(this.callKey(e));
      if (e.kind === 'tool.denied' || p.status === 'error' || p.status === 'denied') {
        const issue = { id: e.id, scope: call.scope, project: call.project, call, event: e, resolved: null, retryJob: null };
        if (p.status === 'error') {
          issue.unknownJob = !!unknownJobResult(call, p);
          issue.lookup = this.unknownReference(call, e);
          const refusedStep = (p.summary || '').match(/^Unfinished physical step (\S+) must remain in the Task Plan with its owner and completion conditions\./)?.[1];
          if (e.agent === 'orchestrator' && call.project && refusedStep) {
            const matches = call.planContracts.filter(s => s.id === refusedStep && s.operation);
            if (matches.length === 1) issue.physicalObligation = matches[0];
          }
        }
        this.issues.set(e.id, issue); changed.opened.push(issue);
      } else if (['ok', 'corrected', 'pending'].includes(p.status)) {
        const observed = p.data?.run_id || p.data?.job_id;
        if (['ok', 'corrected'].includes(p.status) && call.delegation && jobReads.has(call.tool)
          && observed === call.args.job_id && p.data?.status === 'succeeded' && p.data?.done === true
          && p.data?.flow === call.delegation.operation) call.delegation.reads.push({call, event: e});
        if (p.status === 'ok' && call.tool === 'submit_flake_selection' && call.delegation
          && e.agent === call.delegation.role) call.delegation.selections.push({call, event: e});
        for (const issue of this.issues.values()) {
          if (issue.resolved || issue.project !== call.project) continue;
          if (p.status === 'ok' && call.project && call.tool === 'save_project_status' && call.args.status === 'done'
            && e.agent === 'orchestrator' && completionRefusal(issue)) {
            resolve(issue, e, 'the same project later passed its completion checks', [call.event, e]);
            continue;
          }
          // An unknown-reference failure needs the later exact read AND the
          // code-written completion check, not an arbitrary successful call.
          if (issue.unknownJob) continue;
          if (p.status === 'ok' && ['update_plan', 'refine_plan'].includes(call.tool) && ['update_plan', 'refine_plan'].includes(issue.event.payload.tool)
            && issue.event.agent === e.agent) {
            resolve(issue, e, 'superseded by a later accepted Task Plan');
            continue;
          }
          // A call refused by the argument schema never ran. The same agent's
          // accepted call of that tool for the same step is its correction,
          // even though the corrected arguments necessarily differ.
          if (p.status === 'ok' && issue.event.kind === 'tool.denied' && issue.event.payload.rule === 'schema'
            && issue.call.stepTool === call.stepTool) {
            resolve(issue, e, 'arguments corrected and accepted');
            continue;
          }
          if (issue.scope !== call.scope) continue;
          const job = p.data?.job_id;
          if (job) {
            issue.retryJob = job;
            if (this.jobs.get(job)?.payload.status === 'succeeded') resolve(issue, this.jobs.get(job), 'retry completed successfully');
          } else if (p.status === 'ok') resolve(issue, e, 'corrected and accepted');
        }
      }
    }
    if (e.kind === 'job.status') {
      this.jobs.set(e.run_id, e);
      const call = this.jobCalls.get(e.run_id);
      if (p.status === 'failed' && call) this.jobIssues.set(e.run_id,
        {run: e.run_id, call, event: e, resolved: null});
      if (p.status === 'succeeded' && call?.task) for (const issue of this.jobIssues.values()) {
        if (issue.resolved || this.unsafeJobs.has(issue.run) || issue.call.task !== call.task
          || call.event.id <= issue.event.id) continue;
        if (stable(issue.call.args) !== stable(call.args) && !correctedInspection(issue, call)) continue;
        issue.resolved = 'retry completed successfully'; issue.retryJob = e.run_id; issue.resolution = e;
        changed.recoveredJobs.push(issue);
      }
      if (p.status === 'succeeded') for (const issue of this.issues.values()) {
        if (!issue.resolved && issue.retryJob === e.run_id) resolve(issue, e, 'retry completed successfully');
      }
    }
    return changed;
  }
}
