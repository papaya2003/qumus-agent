// Qumus v2 front end (spec 11.3). No build step: plain ES modules served by
// FastAPI. The page only reads the run store (events, bench, camera previews)
// and writes human messages, Stop, Continue and film reports; it never
// commands devices.
//
// The page is one scenario at a time (notes/v2-ui/structure-redesign, agreed
// with Jialei 2026-10-23): Progress, Results, Lab, History. The activity bar
// picks it; every scenario has the same three columns (the left says what to
// look at, the centre shows it, the right goes with it).
//
// "This run" is the current `serve` (the events after its session start), or
// the recording that plays. A recording plays in the page (?rec=<name> or
// ?run=<earlier run>), with "Back to live" while the live run goes on.
//
// One event path for everything: history and seeking apply events at once;
// live (and replayed) events go through a queue, so the conversation, plan,
// jobs and bench stay in step while the bench animates each `lab.frame`.
// Drawing happens in one requestAnimationFrame loop.

import { $, h, svg, api, post, label, flowLabel, linkIds, VERDICT, short, imgUrl, imageSource, isMicrograph, toast, fmtDur, AGENTS, fetchRecording } from './ui.js';
import { Shell, PHONE } from './shell.js';
import { Feed, offsetText } from './feed.js';
import { inspectionPreview } from './inspection.js';
import { TeamGraph } from './team.js';
import { StationBenchView } from './stations.js';
import { LabState, fromBench } from './labstate.js';
import { CameraCard } from './camera.js';
import { initPanes } from './panes.js';
import { renderRevision, planName } from './planbook.js';
import { Replay, fmtClock, eventTimes } from './replay.js';
import { renderCaption } from './actions.js';
import { ProgressTree } from './tree.js';
import { Panel } from './panel.js';
import { FlakeDetail, shortId, thickText } from './flakes.js';
import { Session } from './session.js';
import { Results } from './results.js';
import { LabView, devName } from './labview.js';
import { HistoryView } from './history.js';
import { ExperienceView } from './experience.js';
import { sampleDetail } from './details.js';
import { ModelSettings } from './modelsettings.js';

// a replay eases a bench frame in at most this long (at 1x); frames that
// come faster are coalesced, so the replay clock never waits for the bench
const REPLAY_FRAME_MS = 120;
const MICROSCOPE_FLOWS = new Set(['search_chip', 'stack_device']);
const LOOKERS = new Set(['overview_camera', 'microscope']);
// a part of an instrument lights up the instrument on the bench drawing
const LIT = { transfer_stage_vacuum: 'transfer_stage', transfer_arm_vacuum: 'transfer_arm', exfoliation_vacuum: 'exfoliator',
  exfoliation_heater: 'exfoliator', exfoliation_fan: 'exfoliator' };
// a scenario's data views redraw at most this often while events stream in
const SCENE_MS = 700;

// ------------------------------------------------------------------ state
const S = {
  lastId: 0, mode: 'scripted', busy: false, live: false, speed: 1, projectId: null, sessionStart: 0, serverLive: null,
  bench: null, layout: null, faults: {}, baseBigError: 0.05,
  tasks: {}, thinking: null, stopped: false,
  provider: 'openai', modelStatus: { state: 'unknown' }, modelError: null, linkOk: true, costUsd: null, liveNote: null,
  queue: [], playing: false, legacy: false,
  verification: null, attempts: [],
  laboratory: null,
  uiDirty: true, drawDirty: true, jobsDirty: true, safetyDirty: true,
  // lab actions (replay): step key "run|seq" -> { status, verdict, corrections, device };
  // the step each job is on; the job that acted last; a lab frame already shown
  steps: new Map(), runStep: {}, focus: null, preframe: null, actsDirty: true,
  // the title bar's sentence: when the current task started, when the run started,
  // and what a person has to do (a job waits, or an agent asked)
  sinceAt: null, runStart: null, waiting: null,
};
// the open scenario's data views need a redraw (renderScene)
const scene = { dirty: true, now: true, at: 0 };
// what the page plays: ?rec=<recording> or ?run=<earlier run on this computer>, &at=<event index>
const params = new URLSearchParams(location.search);
const PLAY = { rec: params.get('rec'), run: params.get('run'), at: params.get('at') };

let REPLAY = null;  // a Replay when the page plays a recording
let epoch = 0;      // bumps when a replay seek clears the page; stale playback stops
let bench = null, lab = null, camera = null;
let enlargedBench = null;
let labWaiters = [];

const feed = new Feed($('feed'), {
  openRun: id => openJob(id),
  ids: node => linkIds(node, openSample, id => run.inv.samples.has(id)),
  openPlan: rev => showProgressDetail({ t: 'plan', rev }),
  photo: src => openLightbox(src),
  inspection: e => inspectionPreview(REPLAY, e, openLightbox),
  faultText: p => faultText(p),
});
// this run: its plan book and jobs, its sample ledger, its problems
const run = new Session({ when: e => feed.when(e), faultText: p => faultText(p) });
const book = run.book;
const tree = new ProgressTree(book, { openRun: id => openJob(id), openTab: tab => openResultsPart(tab) });
$('plan-view').append(tree.root);
const panel = new Panel(run.log, { jump: key => { shell.open('progress'); tree.reveal(key); } });
const team = new TeamGraph($('team'));
const live = () => !REPLAY && S.live;
const showRight = () => shell.setPane('right');

const results = new Results(run, {
  live, run: id => (REPLAY ? Promise.resolve(REPLAY.run(id, S.lastId)) : api('/api/runs/' + id)),
  humanReview: async (id, report) => {
    if (!live()) throw new Error('Human review is available only in the live job record.');
    const result = await post(`/api/runs/${id}/human-review`, report);
    toast('Human review recorded; the job is parked for normal recovery.');
    return result;
  },
  photo: src => openLightbox(src), changed: () => sceneDirty(true), showRight, showCentre: () => shell.setPane('centre'),
  jump: key => { shell.open('progress'); tree.reveal(key); },
  costs: () => false,
  subtitle: () => (REPLAY ? 'of this recording' : 'of this run'),
});
const labv = new LabView({ s: () => run, bench: () => S.bench, view: () => (lab ? lab.view : {}), state: () => (lab ? lab.target : {}),
  live, changed: () => sceneDirty(true), showRight, showCentre: () => shell.setPane('centre') });
const hist = new HistoryView({
  ready: () => S.serverLive !== null,
  live: () => S.serverLive, current: () => live() ? run : null,
  playing: () => PLAY, play: (it, at) => playRun(it, at),
  photo: src => openLightbox(src), faultText: p => faultText(p),
  changed: part => part === 'right' ? renderRight(true) : sceneDirty(true), showRight,
  showCentre: () => shell.setPane('centre'),
});
const xp = new ExperienceView({ changed: () => sceneDirty(true), showRight, showCentre: () => shell.setPane('centre') });
let labTab = 'status', histTab = 'runs';
let progressDetail = null;  // Progress: details shown in place of the bench ({t: 'plan', rev} | {t: 'sample', id})

// the details context of a run (details.js): one FlakeDetail per run keeps its photo choice
const flakeDetails = new WeakMap();
function dctx(s) {
  if (!flakeDetails.has(s)) {
    flakeDetails.set(s, new FlakeDetail({
      inventory: s.inv, history: id => s.flakeHistory(id), roleOf: id => s.roleOf(id), live: () => s === run && live(),
      photo: src => openLightbox(src), redraw: () => renderRight(true),
      ask: id => { shell.open('progress', 'centre'); input.value = `About ${id}: `; autosize(); sendBtn.disabled = false; input.focus(); },
      veto: async id => {
        try { await post('/api/messages', { text: `Do not use flake ${id}.`, kind: 'veto' }); toast(`Told Qumus not to use ${id}`); } catch { toast('Could not send'); }
      },
    }));
  }
  const ctx = { s, flake: flakeDetails.get(s), live: () => s === run && live(), photo: src => openLightbox(src), changed: () => renderRight(true),
    open: id => { if (s === run) openSample(id); } };
  return ctx;
}

// a sample id clicked: its details next to what is on screen (Progress keeps
// the conversation and shows them in place of the bench; Lab shows them in
// Stock; Results and History in their details column)
function openSample(id) {
  const scene = shell.scene;
  if (scene === 'results') results.select({ t: 'sample', id });
  else if (scene === 'lab') { if (labTab !== 'stock' && !run.inv.samples.has(id)) return; labv.select({ t: 'sample', id }); }
  else showProgressDetail({ t: 'sample', id });
}
function showProgressDetail(item) {
  if (shell.scene !== 'progress') shell.open('progress');
  progressDetail = item;
  if (PHONE.matches) shell.setPane('right'); else shell.showLab(true);
  shell.render();
  renderRight(true);
}
function openJob(id) { shell.open('results', 'centre'); results.openDoc(`job:${id}`); }
function openResultsPart(tab) { shell.open('results'); results.openDoc(tab === 'device' ? 'plans' : tab === 'flakes' ? 'used' : 'layouts'); }

const shell = new Shell({ layout: scene => layout(scene), open: () => sceneDirty(true) });
initPanes();
const modelSettings = new ModelSettings($('model-settings'), $('billing-settings'), cfg => {
  S.model = cfg.model; S.models = [...new Set(Object.values(cfg.effective_models || {}).map(c => c.model))];
  S.modelError = null; renderMode(); renderModelStatus();
});
const nowCap = h('span', { class: 'bc-text', id: 'bc-text' });  // a replay's lab action, in the title bar

function layout(scene) {
  // nothing picked in Results: the right column shows the bench instead of an empty box
  if (scene === 'results') return { centre: 'v-results', right: results.sel ? 'detail' : 'bench' };
  if (scene === 'lab') return labTab === 'status' && labv.category?.t === 'overview'
    ? { centre: 'v-lab', right: labv.sel ? 'detail' : 'cams' } : { centre: 'v-stock', right: 'detail' };
  if (scene === 'history') return { centre: histTab === 'runs' ? 'v-history' : 'v-xp', right: 'detail' };
  return { centre: 'v-conv', right: scene === 'progress' && progressDetail ? 'detail' : 'bench' };
}

// ------------------------------------------------------------------ events
function handle(e, isLive) {
  if (e.id <= S.lastId) return;
  S.lastId = e.id;
  if (isLive) enqueue(e); else apply(e, false);
}

// the objective turned in now (a photo that comes now was taken with it)
const objectiveNow = () => (lab && lab.target.microscope ? lab.target.microscope.objective : null);

// Apply one event to every part of the page. Returns how long the bench needs
// to show it (ms, 0 = nothing moves).
function apply(e, isLive) {
  const p = e.payload || {};
  let ms = 0;
  const had = e.kind === 'sample.updated' ? JSON.stringify(run.inv.samples.get(p.id)?.props?.images || null) : null;
  const job = run.add(e);
  if (e.run_id) S.jobsDirty = true;
  if (REPLAY && e.run_id) trackStep(e, p);
  switch (e.kind) {
    case 'reactor.observation':
      camera.noteReactor(p);
      break;
    case 'tool.result':
      // Query tools may return an older photo or an inventory reference.
      // The current turret position is not evidence of its magnification.
      // Record lookups (get_* / list_*) return stored reference photos, e.g.
      // the target chip's synthetic reference at t=0: they must not fill the
      // microscope pane before anything was captured (seed 3 r13).
      if (/^(get_|list_)/.test(String(p.tool || ''))) break;
      for (const src of p.images || []) camera.noteImage(src);
      break;
    case 'step.verified': {
      if (!S.verification || S.verification.run_id !== e.run_id || S.verification.seq !== p.seq) S.attempts = [];
      S.attempts.push({ kind: 'verified', ...p });
      S.verification = { ...p, run_id: e.run_id };
      camera.verify(S.verification, S.attempts);
      for (const src of p.images || []) camera.noteImage(src, isMicrograph(src) ? objectiveNow() : null);
      const m = p.measurements || {};
      if (job && m.offset_mm != null) job.lastOffset = offsetText(m);
      // recordings without lab frames: the measured pose is the best guess of where the chip is
      const o = S.legacy && lab && lab.lastReleased;
      if (o && m.pose && m.offset_mm != null) {
        const d = { objects: { [o]: { x: m.pose.x, y: m.pose.y, rotation: m.pose.rotation, askew: p.verdict !== 'ok' } } };
        ms = push(d, isLive);
      }
      break;
    }
    case 'step.corrected':
      S.attempts.push({ kind: 'corrected', ...p });
      if (S.verification) camera.verify(S.verification, S.attempts);
      break;
    case 'agent.delegate': S.tasks = { ...S.tasks, [p.to]: p.task || '' }; S.thinking = p.to; S.sinceAt = Date.now(); break;
    case 'agent.done': {
      const t = { ...S.tasks }; delete t[e.agent]; S.tasks = t;
      S.thinking = 'orchestrator';
      break;
    }
    case 'agent.interrupted': S.tasks = {}; S.thinking = null; break;
    case 'agent.message': if (e.agent) S.thinking = e.agent; break;
    case 'user.message':
      if (p.kind === 'stop' || p.kind === 'continue') break;
      // a replay knows who works from its events, also while a seek applies them at once
      if (isLive || REPLAY) { S.busy = true; S.sinceAt = Date.now(); }
      if (S.runStart == null) S.runStart = e.wall ?? null;
      S.modelError = null;  // the next turn tries again
      S.waiting = null;    // a person is here and answered
      break;
    case 'system.stop_unconfirmed':
    case 'system.stopped': S.stopped = true; S.tasks = {}; S.thinking = null; if (isLive || REPLAY) S.busy = false; break;
    case 'system.continued': S.stopped = false; S.waiting = null; break;
    case 'job.status':
      if (p.status === 'waiting_human') S.waiting = { text: p.message || `${flowLabel(job?.flow)} waits for a person`,
        at: Date.now(), run: e.run_id, failureEventId: e.id };
      break;
    case 'job.human_reviewed':
      if (S.waiting?.run === e.run_id && S.waiting.failureEventId === p.failure_event_id) S.waiting = null;
      break;
    case 'notification':
      if (p.level === 'needs_human') S.waiting = { text: p.text || 'Qumus asks for a person', at: Date.now(), run: e.run_id,
        failureEventId: S.waiting?.run === e.run_id ? S.waiting.failureEventId : null };
      break;
    case 'agent.error': S.modelError = p; if (isLive || REPLAY) S.busy = false; break;
    case 'agent.turn_end':
      S.modelError = p.is_error ? { error: p.reason === 'budget_exhausted' ? 'budget exhausted' : (p.reason || 'the turn ended with an error') } : null;
      if (isLive || REPLAY) { S.busy = false; S.tasks = {}; S.thinking = null; }
      if (isLive && !REPLAY && p.cost_usd != null && S.costUsd != null) S.costUsd += +p.cost_usd;  // current until the next health poll
      break;
    case 'lab.frame': {
      S.safetyDirty = true;
      if (p.microscope_preview) camera?.notePreview(p.microscope_preview);
      if (S.preframe === e.id) { S.preframe = null; break; }  // already moving since its command
      const { device, command, microscope_preview, ...d } = p;
      ms = push(d, isLive);
      break;
    }
    case 'device.command':
      if (S.legacy && lab) {
        const d = lab.legacyDelta(p);
        if (d) { ms = push(d, isLive); S.safetyDirty = true; }
      } else if (isLive && REPLAY && REPLAY.axis === 'lab' && lab && !document.hidden) {
        // lab time: the move starts with its command and lasts until its frame, as on the real bench
        const f = REPLAY.frameAfter(e), span = f ? f.seconds * 1000 / Math.max(0.25, REPLAY.speed) : 0;
        if (span >= 40) {
          const { device, command, ...d } = f.event.payload;
          lab.push(d, Math.min(span, 6000));
          S.preframe = f.event.id;
          S.drawDirty = true;
        }
      }
      break;
    case 'sample.updated': {
      if (p.kind === 'stamp') S.drawDirty = true;
      // a search's revisit photographs each flake: the microscope frame shows the new photo
      const im = p.kind === 'flake' && p.props?.images;
      if (im && !Array.isArray(im) && (im['100x'] || im['20x']) && JSON.stringify(im) !== had) {
        camera.noteImage(im['100x'] || im['20x'], im['100x'] ? 100 : 20);
      }
      break;
    }
    default: break;
  }
  feed.add(e, job);
  tree.add(e);
  S.uiDirty = true;
  sceneDirty();
  return ms;
}

// What each lab action has done so far (the caption reads it).
function trackStep(e, p) {
  const r = e.run_id;
  if (e.kind === 'step.started') {
    const key = `${r}|${p.seq}`;
    S.steps.set(key, { status: 'in_progress', verdict: null, corrections: 0, device: null });
    S.runStep[r] = key;
  } else if (e.kind === 'device.command') {
    // the caption names the instrument that moves; a camera only when nothing else acts
    const st = S.steps.get(S.runStep[r]);
    if (st && (!st.device || !LOOKERS.has(p.device))) st.device = p.device;
  } else if (e.kind === 'step.verified' || e.kind === 'step.corrected' || e.kind === 'step.finished') {
    const st = S.steps.get(`${r}|${p.seq}`);
    if (!st) return;
    if (e.kind === 'step.verified') st.verdict = p.verdict;
    else if (e.kind === 'step.corrected') st.corrections += 1;
    else { st.status = p.status || 'done'; if (p.corrections != null) st.corrections = p.corrections; }
  } else { if (e.kind === 'job.status') S.actsDirty = true; return; }
  S.focus = r;
  S.actsDirty = true;
}

function push(d, isLive) {
  if (!lab) return 0;
  if (!isLive || document.hidden) { lab.applyNow(d); S.drawDirty = true; return 0; }
  const ms = REPLAY ? Math.min(lab.duration(d), REPLAY_FRAME_MS) / Math.max(0.25, REPLAY.speed) : lab.duration(d) / pace();
  lab.push(d, ms);
  S.drawDirty = true;
  return ms;
}

// Long backlogs (a search scans hundreds of fields) play faster, so the bench
// never falls minutes behind the conversation.
function pace() { return Math.max(0.25, S.speed) * (1 + Math.max(0, S.queue.length - 6) / 6); }

function enqueue(e) { S.queue.push(e); if (!S.playing) play(); }
const sleep = ms => new Promise(r => setTimeout(r, ms));
const labSettled = () => new Promise(r => labWaiters.push(r));

async function play() {
  S.playing = true;
  const ep = epoch;
  while (S.queue.length) {
    const e = S.queue.shift();
    const ms = apply(e, true);
    if (REPLAY) continue;  // the replay clock paces delivery; the bench coalesces what it cannot show in time
    if (ms > 0) await labSettled();
    else if (e.kind === 'step.verified' && !document.hidden) await sleep(300 / pace());
    if (ep !== epoch) return;
  }
  S.playing = false;
  if (!REPLAY) refreshBench(!S.busy);
}

// ------------------------------------------------------------------ drawing loop
function frame(now) {
  if (lab && bench) {
    const moving = lab.step(now);
    if (moving || S.drawDirty) {
      bench.render(lab.view, offline());
      if (enlargedBench && !$('lightbox').hidden) enlargedBench.render(lab.view, offline());
      if (REPLAY) camera?.diagram(S.layout, lab.view);
      if (shell.scene === 'lab' && labTab === 'status') labv.drawMap(offline());
      S.drawDirty = false;
    }
    if (!lab.tween && labWaiters.length) labWaiters.splice(0).forEach(r => r());
  }
  if (S.uiDirty) { S.uiDirty = false; renderUi(); }
  if (scene.dirty && (scene.now || now - scene.at >= SCENE_MS)) renderScene();
  requestAnimationFrame(frame);
}
// a hidden tab gets no animation frames: never leave the queue waiting on one
document.addEventListener('visibilitychange', () => {
  if (document.hidden && lab) { lab.settle(); labWaiters.splice(0).forEach(r => r()); }
});

function offline() {
  return new Set((S.bench?.devices || []).filter(d => d.offline).map(d => d.id));
}

// ------------------------------------------------------------------ the scenario's three columns
// Data changes redraw the open scenario at most every SCENE_MS; a click redraws at once.
function sceneDirty(now) { scene.dirty = true; if (now) scene.now = true; }

function renderScene() {
  scene.dirty = false; scene.now = false; scene.at = performance.now();
  const sc = shell.scene;
  if (sc === 'results') {
    $('results-sub').textContent = REPLAY ? 'of this recording' : 'of this run';
    results.renderIndex($('results-index'));
    results.renderCentre({ title: $('res-title'), sub: $('res-sub'), home: $('res-home'), body: $('res-body') });
  } else if (sc === 'lab') {
    document.querySelectorAll('#lab-tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === labTab));
    labv.tab = labTab;
    labv.renderIndex($('lab-index'));
    $('stock-title').textContent = labTab === 'status' ? 'Lab Status' : 'Stock';
    if (labTab === 'status' && labv.category?.t === 'overview') { $('labmap-sub').textContent = REPLAY ? 'top view · as recorded' : 'top view · now'; labv.renderMap($('labmap'), $('needs'), offline()); }
    else if (labTab === 'status') labv.renderStatus($('stock-body'), $('stock-sub'));
    else labv.renderStock($('stock-body'), $('stock-sub'));
  } else if (sc === 'history') {
    document.querySelectorAll('#hist-tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === histTab));
    if (histTab === 'runs') { hist.load(); hist.renderIndex($('hist-index')); hist.renderCentre($('hist-body')); }
    else { loadExperience(); xp.renderIndex($('hist-index')); xp.renderCentre($('xp-body'), !!REPLAY); }
  } else if (sc === 'settings') renderSettings();
  renderBadges();
  shell.render(true);  // the right column's kind may have changed (a detail picked, cleared)
  renderRight();
}
// the right column: the bench, the cameras or the details of what was clicked
function renderRight(force) {
  const sc = shell.scene, lay = layout(sc), box = $('r-detail');
  $('r-bench').hidden = lay.right === 'detail';
  box.hidden = lay.right !== 'detail';
  let d = null, title = 'Bench', sub = REPLAY ? 'as recorded' : 'live', back = null;
  if (lay.right === 'cams') { title = 'Cameras'; sub = REPLAY ? 'as recorded' : 'now'; }
  if (lay.right === 'detail') {
    // typing in the details (a name for the film report): data changes wait
    if (!force && box.contains(document.activeElement) && document.activeElement.matches('input, textarea')) return;
    if (sc === 'results') {
      d = results.detail(dctx(run)) || { title: 'Details', sub: '', node: h('div', { class: 'placeholder', text: 'Choose an entry in the middle column to see its contents here.' }) };
    } else if (sc === 'lab') {
      d = labv.detail(dctx(run)) || { title: 'Details', sub: '', node: h('div', { class: 'placeholder', text: 'Click a card in the middle column for its details.' }) };
      if (labv.category?.t === 'overview' && labTab === 'status') back = { text: '← Cameras', fn: () => { labv.sel = null; sceneDirty(true); } };
    } else if (sc === 'history') {
      d = histTab === 'runs' ? hist.detail(dctx(run)) : xp.detail();
      const current = histTab === 'runs' && hist.sel?.kind === 'current';
      d = d || { title: histTab === 'runs' ? 'Key moments' : 'Where it came from', sub: '', node: current
        ? h('div', { class: 'placeholder' }, 'The current run goes on in Progress; its plan, jobs and checks are there. ',
          h('button', { class: 'lnk-btn', text: 'Open Progress', onclick: () => shell.open('progress', 'centre') }))
        : h('div', { class: 'placeholder', text: histTab === 'runs' ? 'Pick a run on the left.' : 'Click an entry for where it came from and its votes.' }) };
    } else if (progressDetail) {
      d = progressItem(progressDetail);
      back = { text: '← Bench', fn: () => { progressDetail = null; shell.render(); renderRight(true); } };
    }
    if (d) { title = d.title; sub = d.sub || ''; back = d.back || back; }
    const keep = box.scrollTop, same = box.dataset.title === title;
    box.replaceChildren(d ? d.node : h('div'));
    box.dataset.title = title;
    box.scrollTop = same ? keep : 0;
  }
  $('r-title').textContent = title;
  $('r-sub').textContent = sub;
  $('r-back').hidden = !back;
  if (back) { $('r-back').textContent = back.text; $('r-back').onclick = back.fn; }
}
function progressItem(it) {
  if (it.t === 'sample') {
    const x = run.inv.samples.get(it.id);
    return x ? sampleDetail(x, dctx(run)) : { title: it.id, sub: '', node: h('div', { class: 'placeholder', text: 'Not in the sample ledger.' }) };
  }
  // a plan revision, with what changed against the one before
  const plans = book.plans, shown = plans.find(p => p.revision === it.rev) || plans[plans.length - 1];
  if (!shown) return { title: 'Plan', sub: '', node: h('div', { class: 'placeholder', text: 'No plan yet.' }) };
  return { title: planName(shown.revision), sub: shown.revision === plans[plans.length - 1].revision ? 'the latest plan' : 'an earlier plan',
    node: h('div', { class: 'detail-body' },
      plans.length > 1 ? h('div', { class: 'seg small' }, plans.map(p => h('button', { class: p.revision === shown.revision ? 'on' : '', text: planName(p.revision),
        onclick: () => { progressDetail = { t: 'plan', rev: p.revision }; renderRight(true); } }))) : null,
      ...renderRevision(book, shown)) };
}

// the activity bar's numbers: a job waits for a person; open problems; what the lab needs
function renderBadges() {
  shell.badge('progress', S.waiting ? '1' : '', 'wait');
  const open = run.log.problems.filter(p => !p.resolved).length;
  shell.badge('results', open ? String(open) : '', 'warn');
  const needs = S.bench ? labv.needs().length : 0;
  shell.badge('lab', needs ? String(needs) : '', 'warn');
  $('lab-n').hidden = !needs; $('lab-n').textContent = String(needs);
  shell.badge('more', open ? String(open) : '', 'warn');
}

document.querySelectorAll('#lab-tabs button').forEach(b => { b.onclick = () => { labTab = b.dataset.t; labv.sel = null; sceneDirty(true); }; });
document.querySelectorAll('#hist-tabs button').forEach(b => { b.onclick = () => { histTab = b.dataset.t; sceneDirty(true); }; });
$('res-home').onclick = () => results.openDoc('overview');
$('cams-link').onclick = () => { labv.sel = null; shell.setPane('right'); sceneDirty(true); };
// phone: the bench column of Progress (the top view, the cameras, the last check)
document.querySelectorAll('.bench-link').forEach(b => { b.onclick = () => { progressDetail = null; shell.setPane('right'); renderRight(true); }; });

let xpAt = 0;
async function loadExperience() {
  if (Date.now() - xpAt < 20000 && xp.data !== undefined) return;
  xpAt = Date.now();
  let data;
  try { data = REPLAY ? (REPLAY.rec.experience ?? null) : await api('/api/experience'); } catch { data = null; }
  xp.data = data;
  sceneDirty(true);
}

// ------------------------------------------------------------------ the rest of the page
let lastWorking = '';
function renderUi() {
  const busy = REPLAY ? S.busy && !REPLAY.ended : S.busy && !S.stopped;  // after Stop nobody thinks until Continue
  const working = Object.keys(S.tasks);
  const who = S.thinking && S.tasks[S.thinking] != null ? S.thinking : (working.length ? working[working.length - 1] : 'orchestrator');
  const key = `${busy}|${who}`;
  if (key !== lastWorking) { lastWorking = key; feed.setWorking(busy, who); }
  team.set({ busy, tasks: busy ? S.tasks : {}, thinking: busy ? who : null });
  if (S.jobsDirty) {
    S.jobsDirty = false;
    const runs = [...book.runs.values()];
    camera?.context(runs.some(r => r.status === 'running' && MICROSCOPE_FLOWS.has(r.flow)), runs.some(r => r.status === 'running'),
      runs.some(r => r.status === 'running' && r.flow.includes('reactor') && !r.flow.includes('unload')));
  }
  if (tree.dirty && shell.scene === 'progress') tree.render();
  panel.render();
  if (S.safetyDirty) { S.safetyDirty = false; renderSafety(); if (lab) camera?.caption(lab.target); if (shell.scene === 'lab') sceneDirty(); }
  if (REPLAY && S.actsDirty) { S.actsDirty = false; renderActions(); }
  camera?.look(lookingAt());
  renderPlanSub();
  renderControls();
  renderModelStatus();
  renderNow(busy, who);
  renderWaiting();
  renderRunTime();
  if (shell.scene === 'progress' && progressDetail) renderRight();
}

// the Progress heading: which plan, how many steps; a click shows the plan and its earlier revisions
function renderPlanSub() {
  const p = book.latest, el = $('plan-sub');
  const text = p ? `${planName(p.revision)} · ${p.plan.length} step${p.plan.length === 1 ? '' : 's'}${book.plans.length > 1 ? ' · earlier plans' : ''}` : 'No plan yet';
  if (el.textContent !== text) el.textContent = text;
  el.classList.toggle('lnk', !!p);
  el.title = p ? 'Show the plan (and what changed against the earlier ones)' : '';
}
$('plan-sub').onclick = () => { if (book.latest) showProgressDetail({ t: 'plan', rev: book.latest.revision }); };

// ------------------------------------------------------------------ title bar: one sentence
// Step k of n (the plan) · who does what · for how long · the last check.
let lastNow = '';
function renderNow(busy, who) {
  const latest = book.latest;
  const parts = [];
  if (latest && latest.plan.length) {
    const steps = latest.plan, i = steps.findIndex(st => st.status === 'running');
    const done = steps.filter(st => st.status === 'done').length;
    if (i >= 0) parts.push(['now-step', `Step ${i + 1} of ${steps.length}`, steps[i].title]);
    else if (done && done >= steps.length) parts.push(['now-step', `Done · ${steps.length} steps`]);
    else if (done) parts.push(['now-step', `Step ${done + 1} of ${steps.length}`]);
    else parts.push(['now-step', `${steps.length} steps planned`]);
  }
  let cls = '';
  if (S.waiting) {
    cls = 'wait';
    parts.push(['now-act', `⏸ Waiting for you · ${S.waiting.text}`, S.waiting.text]);
    parts.push(['now-t', fmtDur((Date.now() - S.waiting.at) / 1000)]);
  } else if (REPLAY) {
    parts.push(['cap']);
  } else if (busy) {
    const task = S.tasks[who];
    parts.push(['now-act', who === 'orchestrator' || !task ? `${label(who)} is thinking` : `${label(who)} · ${short(task, 90)}`, task || '']);
    if (S.sinceAt) parts.push(['now-t', fmtDur((Date.now() - S.sinceAt) / 1000)]);
  } else if (S.modelError) {
    cls = 'bad';
    parts.push(['now-act', 'The agent stopped with an error · send a message to try again']);
  } else if (S.stopped) {
    parts.push(['now-act', 'Stopped · press Continue to go on']);
  } else if (!latest) {
    parts.push(['now-idle', S.live ? 'Idle · tell Qumus what to do' : 'Connecting…']);
  } else {
    parts.push(['now-idle', 'Idle']);
  }
  const v = S.verification;
  if (v && v.measurements && v.measurements.offset_mm != null && !S.waiting) {
    const k = v.verdict === 'ok' ? '' : v.verdict === 'correctable' ? 'fix' : 'bad';
    parts.push(['now-check ' + k, `${k === 'bad' ? '✗' : k === 'fix' ? '↻' : '✓'} ${(+v.measurements.offset_mm).toFixed(2)} mm`,
      `Last check: ${v.name || ''} ${VERDICT[v.verdict] || v.verdict}`]);
  }
  const key = cls + '|' + parts.map(x => x.join('\u0001')).join('\u0002');
  if (key === lastNow) return;
  lastNow = key;
  const now = $('now');
  now.className = 'now ' + cls;
  const nodes = [];
  parts.forEach((x, i) => {
    if (i) nodes.push(h('span', { class: 'sep' }));
    nodes.push(x[0] === 'cap' ? nowCap : h('span', { class: x[0], text: x[1], title: x[2] || '' }));
  });
  now.replaceChildren(...nodes);
}

function renderWaiting() {
  const w = S.waiting;
  const el = $('sb-wait');
  const text = w ? `⏸ Waiting for you · ${fmtDur((Date.now() - w.at) / 1000)}` : '';
  if (el.textContent !== text) el.textContent = text;
  el.hidden = !w;
  const hint = w ? 'Qumus waits for you: do what it asks, then tell it here and it goes on'
    : 'Enter to send · Shift+Enter for a new line · Esc stops';
  if ($('hint').textContent !== hint) $('hint').textContent = hint;
}

function renderRunTime() {
  const el = $('run-time');
  if (REPLAY || S.runStart == null) { el.hidden = true; return; }
  const t = `run ${fmtDur(Date.now() / 1000 - S.runStart)}`;
  if (el.textContent !== t) el.textContent = t;
  el.title = 'Since your first message of this run';
  el.hidden = false;
}
setInterval(() => {
  if (S.busy) feed.tickWorking(S.sinceAt && !REPLAY ? (Date.now() - S.sinceAt) / 1000 : null);
  if (S.busy || S.waiting || S.runStart != null) S.uiDirty = true;
}, 1000);
// Full / Focus (messages only)
document.querySelectorAll('#feed-mode button').forEach(b => { b.onclick = () => feed.setFocus(b.dataset.m === 'focus'); });

// The flake under the microscope: the one whose photo the frame shows, else the
// one a running job's current step is named after (refind, stamp over, pick up).
const fileName = p => decodeURIComponent(String(p || '').split(/[/=]/).pop());
function lookingAt() {
  let id = null;
  const shown = camera?.cur?.microscope;
  if (shown) {
    const name = fileName(shown);
    for (const x of run.inv.samples.values()) {
      const im = x.kind === 'flake' && x.props.images;
      if (im && !Array.isArray(im) && Object.values(im).some(v => fileName(v) === name)) { id = x.id; break; }
    }
  }
  if (!id) {
    for (const r of book.runs.values()) {
      if (r.status !== 'running' || !MICROSCOPE_FLOWS.has(r.flow)) continue;
      const st = [...r.steps.values()].reverse().find(x => x.status === 'in_progress');
      const m = st && /chip_e?\d{2,3}_f\d{3}/.exec(st.name || '');
      if (m && run.inv.samples.has(m[0])) id = m[0];
    }
  }
  const x = id && run.inv.samples.get(id);
  if (!x) return null;
  const p = x.props;
  return { id, title: `Looking at ${p.chip_id || x.location} · ${shortId(id)}`,
    sub: [p.material, p.thickness_label ? thickText(p.material, p.thickness_label) : null, p.area_um2 != null ? `${Math.round(p.area_um2).toLocaleString()} µm²` : null].filter(Boolean).join(' · ') };
}

// ------------------------------------------------------------------ title bar: the mode, once
function modelName() {
  if (REPLAY?.meta.models?.length > 1) return 'Multiple models';
  if (!REPLAY && S.models?.length > 1) return 'Model team';
  const model = REPLAY ? REPLAY.meta.models?.[0] || REPLAY.meta.model : S.model;
  if (model?.includes('/')) return ({ openai: 'OpenAI', anthropic: 'Claude', deepseek: 'DeepSeek', kimi: 'Kimi', xai: 'Grok', atria: 'Atria' })[model.split('/')[0]] || model;
  return (REPLAY ? REPLAY.meta.provider : S.provider) === 'openai' ? 'OpenAI' : 'Claude';
}
function modeText() {
  if (REPLAY) {
    const m = REPLAY.meta, d = m.recorded_at ? new Date(m.recorded_at).toLocaleDateString([], { month: 'short', day: 'numeric' }) : '';
    if (PLAY.run) return ['REPLAY', `earlier run on this computer${d ? ` · ${d}` : ''}`, 'An earlier run of this computer plays back; nothing is sent anywhere'];
    return ['REPLAY', `${m.agents === 'sdk' ? `model-driven ${modelName()}` : 'scripted'} recording${d ? ` · ${d}` : ''}`,
      m.agents === 'sdk' ? `Model-driven recording: ${modelName()} drove the recorded laboratory; this page plays it back, nothing is sent anywhere`
        : 'Scripted agents on the testbench; this page plays it back, nothing is sent anywhere'];
  }
  const stationBadge = (S.laboratory?.stations || []).map(st => `${st.module} ${st.kind === 'real' ? 'REAL' : 'sim'}`).join(', ');
  return ['LIVE', `${S.laboratory?.mode === 'virtual' ? 'Virtual laboratory' : S.laboratory?.mode === 'mixed' ? 'Mixed laboratory' : 'Laboratory'}${stationBadge && S.laboratory?.mode !== 'virtual' ? ` (${stationBadge})` : ''} · ${S.workspace || 'Testbench'} · ${S.mode === 'sdk' ? S.models?.length > 1 ? `${S.models.length} team models` : S.model || modelName() : 'scripted'}`,
    S.mode === 'sdk' ? `${S.model || modelName()} drives the simulated lab; model calls use API credit`
      : 'Scripted agents: what the agents say and choose comes from a scenario file; tools, safety layer and testbench are real; nothing costs money'];
}
function renderMode() {
  const [k, t, title] = modeText();
  $('mode').className = 'mode ' + (REPLAY ? 'replay' : 'live');
  $('mode-k').textContent = k; $('mode-t').textContent = t; $('mode').title = title;
}

function renderSettings() {
  modelSettings.open(!!REPLAY, S.serverLive);
  renderLaboratory();
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  $('theme-name').textContent = (root.dataset.theme ? '' : 'system · ') + (dark ? 'dark' : 'light');
  const c = S.modelStatus || {}, [k, t] = modeText();
  const rows = [['Mode', `${k} · ${t}`], ['Server', location.host], ['Link', S.linkOk ? 'connected' : 'lost'],
    [modelName(), REPLAY ? 'not used (replay)' : S.mode !== 'sdk' ? 'not used (scripted)' : `${c.state || 'unknown'}${c.detail ? ` · ${c.detail}` : ''}`],
    ['Project', S.projectId || '–']];
  $('conn-info').replaceChildren(...rows.flatMap(([a, b]) => [h('dt', { text: a }), h('dd', { text: b })]));
}

function renderLaboratory() {
  const select = $('laboratory-mode'), status = $('laboratory-status'), setup = $('laboratory-setup');
  if (REPLAY) {
    select.replaceChildren(h('option', { value: 'recorded', text: 'Recorded laboratory origin' }));
    select.disabled = true;
    status.textContent = 'Playback: learning is disabled. A recording keeps its original laboratory provenance.';
    setup.textContent = 'Return to live to inspect the connected backend.';
    return;
  }
  const c = S.laboratory;
  if (!c) { select.disabled = true; status.textContent = 'Laboratory backend has not been verified; learning is disabled.'; return; }
  select.replaceChildren(...c.modes.map(m => h('option', { value: m.id,
    text: m.label + (m.available ? '' : ' · unavailable'), disabled: !m.available })));
  if (c.mode === 'unknown') select.prepend(h('option', { value: 'unknown', text: 'Unverified laboratory backend' }));
  select.value = c.mode;
  select.disabled = S.busy || c.mode === 'unknown';
  status.textContent = `${c.mode === 'virtual' ? 'Virtual laboratory' : 'Unverified laboratory'} · ${c.learning.detail}`;
  setup.textContent = `Real laboratory unavailable. ${c.real_setup_requirement}`;
}

$('laboratory-mode').onchange = async event => {
  try { S.laboratory = await post('/api/laboratory/mode', { mode: event.target.value }); }
  catch (err) { toast(err.message); }
  renderLaboratory();
};

function renderModelStatus() {
  let dot = 'idle', text, title;
  if (REPLAY) {
    dot = 'idle'; text = 'Playback is free · no model calls';
    title = 'A recording plays back. No model requests are sent.';
  } else if (S.mode !== 'sdk') {
    text = 'Scripted · no model calls';
    title = 'Scripted agents use no model credit. Start with --openai for OpenAI Agents SDK.';
  } else if (S.modelError) {
    dot = 'off'; text = `${modelName()} error`;
    title = `${S.modelError.error || S.modelError.type || 'error'}\nSend a message to try again.`;
  } else {
    const c = S.modelStatus || {}, when = c.checked_at ? new Date(c.checked_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null;
    const st = {
      ok: S.busy ? ['work', `${modelName()} working`] : ['live', `${modelName()} ready`],
      unreachable: ['off', `${modelName()} unreachable`], auth_failed: ['off', 'Key refused'], rate_limited: ['off', 'Gateway rate limited'], error: ['off', 'Gateway error'],
    }[c.state] || ['idle', `Checking ${modelName()}…`];
    [dot, text] = st;
    title = `${c.detail || 'not checked yet'}${when ? ` · checked ${when}` : ''}\nClick to check again (free: no prompt is sent).`;
  }
  const costEl = $('model-cost');
  costEl.textContent = ''; costEl.hidden = true;
  const chip = $('model-chip');
  $('model-dot').className = 'dot ' + dot;
  if ($('model-text').textContent !== text) $('model-text').textContent = text;
  chip.title = title;
  chip.classList.toggle('bad', dot === 'off');
}

function setConn(ok) {
  S.linkOk = ok;
  $('link-state').classList.toggle('down', !ok);
  $('conn-dot').className = 'dot ' + (ok ? 'live' : 'off');
  $('conn-text').textContent = ok ? 'Live' : 'Server link lost · reconnecting (if Qumus was stopped, start it again)';
  $('link-state').title = ok ? 'This page is connected to the Qumus server and updates live'
    : 'This page cannot reach the Qumus server; what it shows may be out of date';
}

function renderControls() {
  if (REPLAY) return;
  const runs = [...book.runs.values()];
  const paused = runs.some(r => r.status === 'paused'), running = runs.some(r => r.status === 'running' || r.status === 'queued');
  $('continue-btn').hidden = !(S.stopped || paused);
  $('stop-btn').classList.toggle('quiet', !S.busy && !running);
  syncComposer();
}
// One button in the corner like Claude's composer: Send while idle, Stop while
// Qumus works; both while it works and something is typed (a message during a
// run is read after the current tool call). Esc still stops at any time.
function syncComposer() {
  const working = !$('stop-btn').classList.contains('quiet');
  $('stop-btn').hidden = !working;
  $('send').hidden = working && !$('input').value.trim();
  // with both showing, Send is the main action and Stop steps back
  $('stop-btn').classList.toggle('secondary', working && !$('send').hidden);
}

$('stop-btn').onclick = async () => {
  const b = $('stop-btn'); b.classList.add('armed');
  try { await post('/api/stop'); toast('Stop: agents interrupted; every job pauses at its next safe point'); } catch { toast('Stop request failed'); }
  setTimeout(() => b.classList.remove('armed'), 1200);
};
$('continue-btn').onclick = async () => {
  try { await post('/api/continue'); toast('Continue: paused jobs go on from the right step'); } catch { toast('Continue request failed'); }
};
$('model-chip').onclick = async () => {
  if (REPLAY || S.mode !== 'sdk') return;
  $('model-text').textContent = 'Checking…';
  try { S.modelStatus = await post('/api/model/check'); } catch { /* keep the last state */ }
  S.uiDirty = true;
};

// Only what matters for safety: faults, heat, things held, unhomed arms, a
// lowered stamp, tape running out. Everything else is "normal".
let lastSafety = '';
function renderSafety() {
  const items = [], v = lab ? lab.target : {}, L = S.layout || {};
  const add = (level, text, title) => items.push(h('span', { class: 'sf ' + level, title: title || '' }, h('i'), text));
  for (const d of S.bench?.devices || []) {
    if (d.offline) add('danger', `${devName(d.id)} offline`, 'The device does not answer; commands to it fail.');
    else if (d.stuck_at != null) add('danger', `${devName(d.id)} stuck at ${d.stuck_at}`, 'The reading does not change.');
    else if (d.error) add('danger', `${devName(d.id)}: error`, d.error);
    if (!d.offline && d.temperature != null && (d.target != null || d.temperature > 35)) {
      add('warn', `${devName(d.id)} ${d.temperature} °C${d.target != null ? ` → ${d.target} °C` : ''}`, 'Heating (or still hot).');
    }
  }
  const held = id => Object.entries(v.objects || {}).find(([, o]) => o.held_by === id)?.[0];
  for (const [id, a] of Object.entries(v.arms || {})) {
    if (a.homed === false) add('warn', `${devName(id)} not homed`, 'The safety layer refuses moves until it homes.');
    if (a.vacuum) add('info', `${devName(id)} holding ${held(id) || 'vacuum on'}`, 'The nozzle vacuum is on.');
  }
  const m = v.manipulator, ta = L.transfer_arm || {};
  if (m && m.vacuum) add('info', `Stamp holder holding ${held('transfer_arm') || 'a stamp'}`);
  if (m && m.z != null && ta.approach_z != null && m.z < ta.approach_z) add('warn', 'Stamp lowered', `z ${(+m.z).toFixed(2)} mm, below the approach height ${ta.approach_z} mm.`);
  const ex = v.exfoliator, roll = (L.exfoliator || {}).roll_length_mm;
  if (ex && ex.applied) add('info', 'Tape on the exfoliation stage');
  if (ex && ex.roll_left_mm != null && roll && ex.roll_left_mm < 0.1 * roll) add('warn', `Tape low · ${Math.round(ex.roll_left_mm)} mm left`);
  if (!items.length) {
    const n = (S.bench?.devices || []).length;
    add('ok', 'All systems normal', `${n} devices online · nothing heating · nothing held · arms homed`);
  }
  // Only touch the DOM when something changed: a rebuilt chip replays its entry animation
  const key = items.map(n => n.className + '|' + n.textContent + '|' + n.title).join('\n');
  if (key === lastSafety) return;
  lastSafety = key;
  $('safety').replaceChildren(...items);
}

// ------------------------------------------------------------------ bench
async function refreshBench(reconcile) {
  let b;
  if (REPLAY) b = REPLAY.bench(S.lastId);
  else { try { b = await api('/api/bench'); } catch { return; } }
  S.bench = b; S.faults = b.faults || {};
  camera?.setReactor(!!b.layout?.reactor_module);
  if (!bench) {
    S.layout = b.layout;
    bench = new StationBenchView($('bench'), b.layout);
    bench.statusOf = id => run.inv.stampStatus(id);  // stamp status for the tooltip (ledger)
    bench.stampOf = id => run.inv.samples.get(id);
    // the top-view frame takes the table's own shape (plus its legend strip), so no blank bars
    $('bench').parentElement.style.aspectRatio = `${bench.w} / ${bench.h}`;
    $('labmap-wrap').style.aspectRatio = `${bench.w} / ${bench.h}`;
    lab = new LabState(b.layout);
    lab.setAll(fromBench(b, b.layout));
    S.baseBigError = (b.faults || {}).big_error_prob ?? 0.05;
    if (!REPLAY && b.sim) { S.speed = b.sim.speed || 1; markSpeed(); }
  } else if (reconcile && !S.playing && !lab.tween && (!REPLAY || S.legacy)) {
    lab.setAll(fromBench(b, S.layout));
  }
  if (!S.playing) S.busy = !!b.busy;  // while the page still plays a backlog, the events say who works
  S.drawDirty = S.uiDirty = S.safetyDirty = true;
  renderFaults();
}

// ------------------------------------------------------------------ testbench tools
// The fault menu comes from GET /api/faults (the testbench injector; a real lab
// has none and answers 404, so the menu stays hidden). Faults with a lasting
// state (probabilities, offline, stuck) toggle; the rest are one-shot.
const FAULT_UI = {
  next_placement: { title: 'Next placement askew', params: { dx: 2.0, dy: -1.0, drot: 6.0 } },
  crash: { title: 'Kill the job process', hint: 'Dies mid-job, restarts and resumes from the last checkpoint.' },
  pick_fail_next: { title: 'Next pick fails' },
  drop_in_transit: { title: 'Drop the next carried object' },
  pickup_fail_next: { title: 'Next flake pickup fails', hint: 'The stamp lifts off without the flake.' },
  release_fail_next: { title: 'Next stack release fails', hint: 'The stack stays on the stamp at drop-down.' },
  serial_timeout: { title: 'Serial timeout' },
  pick_fail_prob: { title: 'Picks fail often', params: { value: 0.4 } },
  big_error_prob: { title: 'Frequent big placement errors', params: { value: 0.5 } },
  device_offline: { title: 'Device offline' },
  sensor_stuck: { title: 'Heater sensor stuck' },
  vacuum_stuck: { title: 'Vacuum stuck' },
  stuck_at: { hidden: true },  // general form of the two above
};
const FAULT_ORDER = ['next_placement', 'crash', 'pick_fail_next', 'drop_in_transit', 'pickup_fail_next',
  'release_fail_next', 'serial_timeout', 'pick_fail_prob', 'big_error_prob', 'device_offline', 'sensor_stuck', 'vacuum_stuck'];
let faultMenu = [];

function devState(id) { return (S.bench?.devices || []).find(d => d.id === id); }
function faultParams(m) { return { ...(m.params || {}), ...((FAULT_UI[m.fault] || {}).params || {}) }; }
function faultActive(m) {
  const f = S.faults || {}, p = faultParams(m);
  switch (m.fault) {
    case 'next_placement': return !!(f.next_placement && Object.keys(f.next_placement).length);
    case 'pick_fail_prob': return f.pick_fail_prob > 0;
    case 'big_error_prob': return f.big_error_prob >= 0.5;
    case 'device_offline': return !!devState(p.device)?.offline;
    case 'sensor_stuck': case 'vacuum_stuck': case 'stuck_at': return devState(p.device)?.stuck_at != null;
    default: return (f.armed || {})[m.fault] > 0;  // one-shot faults show while armed
  }
}
function faultRequest(m) {
  const p = faultParams(m), on = faultActive(m);
  switch (m.fault) {
    case 'next_placement': return { fault: m.fault, params: on ? {} : p };
    case 'pick_fail_prob': return { fault: m.fault, params: { value: on ? 0 : p.value } };
    case 'big_error_prob': return { fault: m.fault, params: { value: on ? S.baseBigError : p.value } };
    case 'device_offline': return { fault: m.fault, params: { ...p, offline: !on } };
    case 'sensor_stuck': case 'vacuum_stuck': case 'stuck_at': return { fault: m.fault, params: { ...p, value: on ? null : p.value } };
    default: return { fault: m.fault, params: p };
  }
}
function faultText(p) {
  const q = p.params || {};
  switch (p.fault) {
    case 'next_placement': return Object.keys(q).length ? `next placement off by ${q.dx ?? 0}, ${q.dy ?? 0} mm and ${q.drot ?? 0}°` : 'askew placement cleared';
    case 'pick_fail_prob': return `pick failure chance ${Math.round((q.value || 0) * 100)}%`;
    case 'big_error_prob': return `big placement error chance ${Math.round((q.value || 0) * 100)}%`;
    case 'stuck_at': case 'sensor_stuck': case 'vacuum_stuck':
      return q.value == null ? `${devName(q.device)} unstuck` : `${devName(q.device)} stuck at ${q.value}`;
    case 'pick_fail_next': return `next ${q.count ?? 1} pick(s) fail`;
    case 'drop_in_transit': return 'next carried object drops';
    case 'serial_timeout': return `${devName(q.device)}: next ${q.count ?? 1} call(s) time out`;
    case 'crash': return `job process dies before device call ${q.after_calls ?? 1} from now`;
    case 'device_offline': return `${devName(q.device)} ${q.offline === false ? 'back online' : 'offline'}`;
    default: return String(p.fault || '').replace(/_/g, ' ');
  }
}
async function loadTools() {
  try { faultMenu = await api('/api/faults'); } catch { faultMenu = null; }
  if (!faultMenu) return;  // not a testbench: no tools
  faultMenu = faultMenu.filter(m => !(FAULT_UI[m.fault] || {}).hidden);
  const rank = f => { const i = FAULT_ORDER.indexOf(f); return i < 0 ? 99 : i; };
  faultMenu.sort((a, b) => rank(a.fault) - rank(b.fault));
  $('fault-list').replaceChildren(...faultMenu.map(m => {
    const p = faultParams(m), ui = FAULT_UI[m.fault] || {};
    return h('button', { class: 'fbtn', dataset: { fault: m.fault }, onclick: () => fault(m) },
      ui.title || m.fault.replace(/_/g, ' '), h('small', { text: (p.device ? `${devName(p.device)} · ` : '') + (ui.hint || m.description || '') }));
  }));
  $('speed-sec').hidden = S.mode !== 'scripted';  // model-backed agents set their own pace
  $('tools-btn').hidden = false; $('phone-tools').hidden = false; $('ptab-events').hidden = false;
  renderFaults();
}
function renderFaults() {
  if (!faultMenu) return;
  let any = false;
  for (const m of faultMenu) {
    const on = faultActive(m); any = any || on;
    const b = document.querySelector(`.fbtn[data-fault="${m.fault}"]`);
    if (b) b.classList.toggle('on', on);
  }
  $('fault-dot').hidden = !any;
}
async function fault(m) {
  const req = faultRequest(m);
  try { await post('/api/faults', req); toast('Injected: ' + faultText(req)); } catch { toast('Fault injection failed'); }
  refreshBench(false);
}
function markSpeed() { document.querySelectorAll('#speed-seg button').forEach(b => b.classList.toggle('on', +b.dataset.s === S.speed)); }
document.querySelectorAll('#speed-seg button').forEach(b => b.onclick = async () => {
  S.speed = +b.dataset.s; markSpeed();
  try { await post('/api/sim', { speed: S.speed }); } catch { /* playback speed still changes */ }
});

// ------------------------------------------------------------------ photos, keys, theme
function openLightbox(src) {
  enlargedBench = null;
  const box = $('lightbox');
  box.replaceChildren(h('figure', {}, h('img', { src: imgUrl(src), alt: imageSource(src) }), h('figcaption', { text: imageSource(src) })));
  box.hidden = false;
}
function expandBench() {
  if (!lab || !S.layout) return;
  const box = $('lightbox'), drawing = svg('svg', { id: 'bench-expanded', role: 'img', 'aria-label': 'Expanded animated bench' });
  const close = h('button', { class: 'lnk-btn', text: 'Close', onclick: () => { box.hidden = true; } });
  const controls = h('div', { class: 'expanded-controls' }, h('b', { text: 'Bench · top view' }));
  if (REPLAY) {
    controls.append(h('button', { class: 'lnk-btn', text: 'Play / pause', onclick: () => $('rp-play').click() }));
    controls.append(h('button', { class: 'lnk-btn', text: 'Next step', onclick: () => $('bc-next').click() }));
  }
  controls.append(close);
  const figure = h('figure', { class: 'expanded-bench' }, controls, drawing,
    h('figcaption', { text: REPLAY ? 'Recorded simulation · Space plays / pauses · Esc closes' : 'Live bench · world frame in mm · Esc closes' }));
  figure.onclick = e => e.stopPropagation();
  box.replaceChildren(figure);
  enlargedBench = new StationBenchView(drawing, S.layout);
  enlargedBench.statusOf = id => run.inv.stampStatus(id);
  enlargedBench.stampOf = id => run.inv.samples.get(id);
  enlargedBench.render(lab.view, offline());
  box.hidden = false;
}
document.querySelectorAll('.expand-bench').forEach(button => { button.onclick = expandBench; });
$('lightbox').onclick = () => { $('lightbox').hidden = true; };
// Escape closes what is open (the photo, then details next to the conversation);
// with nothing open it is Stop (the hint says so)
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!$('lightbox').hidden) { $('lightbox').hidden = true; return; }
  if (shell.scene === 'progress' && progressDetail) { progressDetail = null; shell.render(); renderRight(true); return; }
  if (e.target.closest && e.target.closest('input')) return;
  if (live() && (S.busy || !$('stop-btn').classList.contains('quiet'))) $('stop-btn').click();
});
// theme: auto -> light -> dark
$('theme-btn').onclick = () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = dark ? 'light' : 'dark';
  try { localStorage.setItem('qumus.theme', root.dataset.theme); } catch { /* private window */ }
  renderSettings();
};

// ------------------------------------------------------------------ composer
const input = $('input'), sendBtn = $('send');
async function send(text) {
  text = (text ?? input.value).trim();
  if (!text) return;
  try { await post('/api/messages', { text }); } catch { toast('Could not send'); return; }
  input.value = ''; autosize(); sendBtn.disabled = true; syncComposer();
}
function autosize() { input.style.height = 'auto'; input.style.height = Math.min(140, input.scrollHeight) + 'px'; }
input.addEventListener('input', () => { autosize(); sendBtn.disabled = !input.value.trim(); syncComposer(); });
input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
sendBtn.onclick = () => send();

function emptyState() {
  const sugg = ['Place a chip on the transfer stage', 'Get me a graphene flake', 'Make a graphene device'];
  return h('div', { class: 'empty' },
    h('img', { class: 'empty-mark', src: '/static/logo.svg', alt: '', width: 64, height: 64 }),
    h('h3', { text: 'What should Qumus do?' }),
    h('div', { text: 'Qumus turns your request into a plan and hands the work to its team. You can write to it, Stop or Continue at any time.' }),
    h('div', { class: 'suggest' }, sugg.map(t => h('button', { text: t, onclick: () => send(t) }))),
    h('div', { class: 'muted small empty-more', text: 'Earlier runs are in History.' }));
}

// ------------------------------------------------------------------ live link
// Websocket, falling back to polling if the socket keeps failing. After a
// reconnect the page reloads if the server now has another run (a restarted
// serve, a new database or project): its event ids would not continue ours.
let wsDelay = 500, wsFails = 0, polling = false, dropped = false;
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/events?after=${S.lastId}`);
  let opened = false;
  ws.onopen = () => { opened = true; wsDelay = 500; wsFails = 0; setConn(true); if (dropped) { dropped = false; checkStore(); } };
  ws.onmessage = m => { try { handle(JSON.parse(m.data), true); } catch (err) { console.error(err); } };
  ws.onclose = () => {
    if (!opened && ++wsFails >= 3) { poll(); return; }
    if (opened) dropped = true;
    setConn(false); setTimeout(connect, wsDelay); wsDelay = Math.min(8000, wsDelay * 2);
  };
}
async function poll() {
  if (polling) return;
  polling = true;
  for (;;) {
    try {
      const page = await api(`/api/events?after=${S.lastId}&limit=500`);
      if (!S.linkOk) checkStore();
      setConn(true);
      page.forEach(e => handle(e, true));
      if (page.length === 500) continue;
    } catch { setConn(false); }
    await sleep(500);
  }
}
async function checkStore() {
  try {
    const snap = await api('/api/snapshot');
    const pid = snap.project && snap.project.id;
    if (snap.last_event_id < S.lastId || (S.projectId && pid && pid !== S.projectId) || (snap.session_start ?? 0) !== S.sessionStart) location.reload();
  } catch { /* try again on the next reconnect */ }
}
async function pollHealth() {
  try {
    const hl = await api('/api/health');
    if (REPLAY) {
      // a recording plays; the live run goes on: say on "Back to live" when it needs a person
      S.liveNote = hl.waiting ? ['wait', `${hl.waiting} needs a person`] : hl.busy ? ['busy', 'working'] : null;
      const n = $('back-note');
      n.hidden = !S.liveNote;
      if (S.liveNote) { n.textContent = S.liveNote[1]; n.className = S.liveNote[0]; }
      $('back-live').classList.toggle('wait', !!hl.waiting);
      return;
    }
    S.provider = hl.provider || 'openai';
    S.laboratory = hl.laboratory || null;
    renderLaboratory();
    const modelsChanged = JSON.stringify(hl.models || []) !== JSON.stringify(S.models || []);
    S.models = hl.models || [];
    if (hl.model && hl.model !== S.model) { S.model = hl.model; renderMode(); }
    else if (modelsChanged) renderMode();
    S.modelStatus = hl.model_status || S.modelStatus;
    if (hl.cost_usd != null) S.costUsd = hl.cost_usd;
    if (!S.playing) S.busy = !!hl.busy;
    S.uiDirty = true;
  } catch { /* the websocket shows the link state */ }
}

// ------------------------------------------------------------------ replay
// A recording plays in the page: events go through the same handlers as live
// ones; the bench, the jobs and the Results read the recording.
function playRun(it, at) {
  const q = new URLSearchParams();
  q.set(it.kind === 'rec' ? 'rec' : 'run', it.id);
  q.set('at', String(at ?? 0));
  // a new recording starts over: forget where the last visit of this one stopped
  location.href = `/?${q}`;
}
$('back-live').onclick = () => { location.href = '/'; };

function resetPage() {
  epoch++;
  labWaiters.splice(0).forEach(r => r());
  Object.assign(S, {
    lastId: 0, busy: false, tasks: {}, thinking: null, stopped: false, modelError: null,
    queue: [], playing: false, verification: null, attempts: [],
    steps: new Map(), runStep: {}, focus: null, preframe: null, actsDirty: true,
    sinceAt: null, runStart: null, waiting: null,
  });
  S.live = false;
  run.reset();
  feed.reset(recordingIntro());
  const fold = [tree.open, tree.closed];  // a seek keeps what the person unfolded or folded
  tree.reset(); [tree.open, tree.closed] = fold; tree.preload(REPLAY.events);
  panel.reset();
  camera.reset();
  results.jobNode = null;
  if (lab) { lab.setAll(fromBench(REPLAY.bench(0), S.layout)); lab.lastReleased = null; }
  S.uiDirty = S.drawDirty = S.jobsDirty = S.safetyDirty = true;
  sceneDirty(true);
}

function settlePage() {
  refreshBench(S.legacy);  // a replay's bench is at hand: this sets S.busy from the recorded frame at once
  // a seek to the end looks like playing to the end: nobody is still working
  if (REPLAY.ended) {
    S.busy = false; S.tasks = {}; S.thinking = null;
    feed.finishRecordedActivity(run, {modelDriven: REPLAY.meta.agents === 'sdk'});
  }
  S.live = true;
  S.uiDirty = S.drawDirty = S.jobsDirty = S.safetyDirty = true;
  sceneDirty(true);
  feed.toBottom(true);
}

function recordingIntro() {
  const m = REPLAY.meta;
  const when = m.recorded_at ? new Date(m.recorded_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : null;
  const facts = [m.agents === 'sdk' ? `Model-driven ${modelName()}${m.model && m.model !== 'Claude' ? ` (${m.model})` : ''}` : 'Scripted agents',
    PLAY.run ? 'this computer' : 'testbench', when, m.duration_s ? `${fmtClock(m.duration_s)} long` : null,
    REPLAY.labSeconds > (m.duration_s || 0) + 30 ? `${fmtClock(REPLAY.labSeconds)} of lab time` : null,
    'Playback is free'].filter(Boolean);
  return h('div', { class: 'rec-intro' },
    h('div', { class: 'rec-kicker', text: PLAY.run ? 'Earlier run' : 'Recorded run' }),
    h('h3', { text: m.title || 'Recorded run' }),
    m.description ? h('p', { text: m.description }) : null,
    h('div', { class: 'rec-facts' }, facts.map(f => h('span', { text: f }))));
}

let capPaused = null;  // the caption keeps the last action while paused
function replayProgress() {
  const R = REPLAY;
  if (!R) return;
  const pct = (R.fraction * 100).toFixed(2) + '%';
  $('rp-fill').style.width = pct;
  $('rp-knob').style.left = pct;
  $('rp-track').setAttribute('aria-valuenow', Math.round(R.fraction * 100));
  $('rp-time').textContent = `${fmtClock(R.realNow)} / ${fmtClock(R.duration)}`;
  $('rp-time').title = (R.axis === 'lab' ? 'Lab time: how long the real instruments would take, plus agent turns; long waits are fast-forwarded'
    : 'Time in the original session') + (R.skipped ? ` · ${fmtClock(R.skipped)} of pauses are skipped` : '');
  document.querySelectorAll('#rp-axis button').forEach(b => b.classList.toggle('on', b.dataset.a === R.axis));
  if (R.paused !== capPaused) { capPaused = R.paused; S.actsDirty = true; S.uiDirty = true; }
  $('bc-mode').classList.toggle('on', R.stepMode);
  $('bc-mode').setAttribute('aria-pressed', String(R.stepMode));
  $('bc-prev').disabled = R.pos === 0;
  $('bc-next').disabled = R.ended;
  const ff = !R.paused && R.ffAt(R.now), fft = ff ? `⏩ ${fmtDur(ff.seconds)} of lab time, fast-forwarded` : '';
  if ($('bc-ff').textContent !== fft) { $('bc-ff').textContent = fft; $('bc-ff').hidden = !fft; }
  $('rp-play').classList.toggle('playing', !R.paused);
  $('rp-play').title = R.paused ? (R.ended ? 'Play again (Space)' : 'Play (Space)') : 'Pause (Space)';
  $('rp-play').setAttribute('aria-label', R.paused ? 'Play' : 'Pause');
  document.querySelectorAll('#rp-speed button').forEach(b => b.classList.toggle('on', +b.dataset.s === R.speed));
  $('conn-dot').className = 'dot ' + (R.paused ? 'paused' : 'replay');
  // a stand-alone recording page has no Qumus behind it: say so where the link state is
  $('conn-text').textContent = (R.ended ? 'Replay finished' : R.paused ? 'Replay paused' : 'Replaying')
    + (S.serverLive ? '' : ' · recording only');
  if (!S.serverLive) $('link-state').title = 'This page only plays a recording and is not connected to a running Qumus: no model calls, no live run to go back to. Start Qumus with Start Qumus on the desktop to work live.';
}

let autoplay = null;  // the first visit starts playing after a moment, unless the user acts first
function userActed() { clearTimeout(autoplay); autoplay = null; }

function buildReplayBar() {
  const R = REPLAY, track = $('rp-track');
  renderTicks();
  const seekAt = x => { userActed(); const r = track.getBoundingClientRect(); R.seekFraction((x - r.left) / Math.max(1, r.width)); };
  let dragging = false;
  track.addEventListener('pointerdown', ev => { if (ev.target.closest('.rp-tick')) return; dragging = true; track.setPointerCapture(ev.pointerId); seekAt(ev.clientX); });
  track.addEventListener('pointermove', ev => {
    const r = track.getBoundingClientRect(), f = Math.max(0, Math.min(1, (ev.clientX - r.left) / Math.max(1, r.width)));
    $('rp-hover').textContent = fmtClock(R.realAt(f * R.total));
    $('rp-hover').style.left = (f * 100).toFixed(2) + '%';
    if (dragging) seekAt(ev.clientX);
  });
  const stop = () => { dragging = false; };
  track.addEventListener('pointerup', stop); track.addEventListener('pointercancel', stop);
  track.addEventListener('keydown', ev => {
    if (ev.key === 'Home' || ev.key === 'End') {
      ev.preventDefault(); userActed();
      R.seekIndex(ev.key === 'Home' ? 0 : R.n);
    }
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
      ev.preventDefault(); userActed();
      R.seekFraction(R.fraction + (ev.key === 'ArrowRight' ? 1 : -1) * 0.02);
    }
  });
  $('rp-play').onclick = () => { userActed(); R.toggle(); };
  $('rp-restart').onclick = () => { userActed(); R.seekIndex(0); R.play(); };
  document.querySelectorAll('#rp-speed button').forEach(b => b.onclick = () => { R.setSpeed(+b.dataset.s); S.speed = R.speed; replayProgress(); });
  document.querySelectorAll('#rp-axis button').forEach(b => b.onclick = () => { userActed(); R.setAxis(b.dataset.a); });
  $('bc-prev').onclick = () => { userActed(); R.stepPrev(); };
  $('bc-next').onclick = () => { userActed(); R.stepNext(); };
  $('bc-mode').onclick = () => { userActed(); R.setStepMode(!R.stepMode); };
  document.addEventListener('keydown', ev => {
    if (ev.target.closest('input, textarea, [role="slider"]') || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
      ev.preventDefault(); userActed();
      if (ev.key === 'ArrowRight') R.stepNext(); else R.stepPrev();
      return;
    }
    if (ev.code !== 'Space' || ev.target.closest('button')) return;
    ev.preventDefault(); userActed(); R.toggle();
  });
}

function renderTicks() {
  const R = REPLAY;
  $('rp-ticks').replaceChildren(...R.chapters.map(c => h('button', { class: `rp-tick k-${c.kind}`, style: `left:${(c.frac * 100).toFixed(2)}%`,
    title: c.text, 'aria-label': c.text,
    // a mark at the very end covers the track's right edge, which means "the end" (not that mark's start)
    onclick: ev => { ev.stopPropagation(); userActed(); R.seekIndex(c.frac >= 0.995 ? R.n : c.i); } })));
}

// The caption in the title bar (which instrument does what, the step's check).
function renderActions() {
  const R = REPLAY;
  const key = S.focus ? S.runStep[S.focus] : null, st = key ? S.steps.get(key) : null, info = key ? R.stepAt.get(key) : null;
  const job = S.focus ? book.runs.get(S.focus) : null;
  // while playing, only a job at work; paused (step by step), the action just watched stays
  const active = st && info && job && (job.status === 'running' || st.status === 'in_progress' || R.paused);
  renderCaption(nowCap, active ? { st, info, device: st.device && devName(st.device) }
    : { idle: R.ended ? 'End of the recording' : R.pos ? 'No instrument is moving' : 'Instrument actions show here as they happen' });
  tree.setCurrent(key);
  if (shell.scene === 'progress') tree.render();
  // the bench view lights up the active instrument
  bench?.highlight?.(active && st.device ? [LIT[st.device] || st.device] : []);
}

async function bootReplay() {
  let rec;
  if (PLAY.rec) rec = await fetchRecording(`/api/recording?name=${encodeURIComponent(PLAY.rec)}`, PLAY.rec);
  else if (PLAY.run) rec = await fetchRecording(`/api/history/${encodeURIComponent(PLAY.run)}/recording`);
  else {
    rec = await fetchRecording('/api/recording');
    // the replay server's own recording: History marks it as the one playing
    try { PLAY.rec = ((await api('/api/recordings')).find(r => r.current) || {}).name || null; } catch { /* the list is optional */ }
  }
  REPLAY = new Replay(rec, {
    deliver: e => handle(e, true),
    apply: e => handle(e, false),
    skipped: s => feed.skipped(s, S.busy),
    reset: resetPage,
    settle: settlePage,
    progress: replayProgress,
    rebuilt: renderTicks,
    ended: () => {
      S.busy = false; S.tasks = {}; S.thinking = null; S.uiDirty = true;
      feed.finishRecordedActivity(run, {modelDriven: REPLAY.meta.agents === 'sdk'});
      toast('End of the recording · Restart to watch again');
    },
  });
  S.legacy = !REPLAY.events.some(e => e.kind === 'lab.frame');
  camera = new CameraCard({ replay: true, lightbox: openLightbox, openFlake: id => openSample(id) });
  document.body.classList.add('replay');
  feed.clock = 'elapsed'; feed.stoppable = false;
  feed.recordedTimes = eventTimes(REPLAY.events);
  document.title = `${rec.meta?.title || 'Recorded run'} · Qumus v2 replay`;
  renderMode();
  $('back-live').hidden = !S.serverLive;
  $('bench-legend').textContent = 'recorded · mm';
  $('replay-bar').hidden = false;
  // a row of the tree seeks the replay: a lab action to its start, a device command to itself
  tree.hooks.seek = key => {
    userActed();
    const i = typeof key === 'number' ? REPLAY.at.get(key) : REPLAY.stepAt.get(key)?.i;
    if (i != null) REPLAY.seekIndex(i);
  };
  tree.preload(REPLAY.events);
  buildReplayBar();
  await refreshBench(true);
  const at = PLAY.at != null && PLAY.at !== '' ? Math.max(0, Math.min(REPLAY.n, +PLAY.at | 0)) : null;
  if (at != null) {
    // a key moment from History: play from there (a reload keeps the place it reached)
    shell.open('progress', 'centre');
    history.replaceState(null, '', location.pathname + location.search.replace(/[?&]at=\d+/, '').replace(/^&/, '?'));
    PLAY.at = null;
  }
  const saved = at == null ? REPLAY.restore() : null;
  if (saved) {
    REPLAY.speed = saved.speed || REPLAY.speed;
    REPLAY.stepMode = !!saved.stepMode;
    if (saved.axis && saved.axis !== REPLAY.axis) { REPLAY.axis = saved.axis; REPLAY.buildTimeline(); REPLAY.chapters = REPLAY.buildChapters(); renderTicks(); }
  }
  S.speed = REPLAY.speed;
  REPLAY.seekIndex(at != null ? at : saved ? saved.pos : 0, saved ? saved.now : undefined);
  REPLAY.start();
  setInterval(() => { if (!S.playing) refreshBench(S.legacy && !S.busy); }, 1500);
  if (S.serverLive) { pollHealth(); setInterval(pollHealth, 5000); }
  // a fresh visit starts playing; a reload keeps the state it had
  if (at != null) REPLAY.play();
  else if (!saved || !saved.paused) autoplay = setTimeout(() => { autoplay = null; REPLAY.play(); }, saved ? 0 : 900);
}

// ------------------------------------------------------------------ boot
// A replay that fails to start says so; it never falls back to the live page,
// which would wait for a server the replay does not have (review issue 2).
function replayFailed(err) {
  console.error(err);
  feed.reset(h('div', { class: 'empty' }, h('h3', { text: 'The replay could not start' }),
    h('div', { text: `${err && err.message ? err.message : err}` }),
    h('div', { class: 'muted', text: S.serverLive ? 'Go back to the live page and pick the recording again in History.' : 'Reload the page (Cmd+Shift+R). If it still fails, stop the replay with Ctrl+C, run it again and send the terminal output.' })));
  $('now').replaceChildren(h('span', { class: 'now-idle', text: 'Replay failed to start' }));
  $('conn-dot').className = 'dot off';
  $('conn-text').textContent = 'Replay error';
  $('back-live').hidden = !S.serverLive;
}

async function boot() {
  // Consume clicks made before Shell's module listeners existed, exactly once.
  const startupPage = document.documentElement.dataset.startupPage;
  delete document.documentElement.dataset.startupPage;
  document.dispatchEvent(new Event('qumus:navigation-ready'));
  if (startupPage) {
    if (PHONE.matches) shell.click(startupPage);
    else shell.open(startupPage);  // choosing a page during load must not fold it
  }
  requestAnimationFrame(frame);
  let health = null;
  try { health = await api('/api/health'); } catch { /* server not up yet; the websocket retries */ }
  S.serverLive = !health || health.mode !== 'replay';
  if (health) { S.mode = health.mode; S.provider = health.provider || 'openai'; S.model = health.model; S.models = health.models || []; S.workspace = health.workspace; S.modelStatus = health.model_status || S.modelStatus; S.laboratory = health.laboratory || null; }
  if (!S.serverLive || PLAY.rec || PLAY.run) {
    try { await bootReplay(); } catch (err) { replayFailed(err); }
    return;
  }
  if (health && health.cost_usd != null) S.costUsd = health.cost_usd;
  renderMode();
  camera = new CameraCard({ replay: false, lightbox: openLightbox, openFlake: id => openSample(id) });
  try {
    const snap = await api('/api/snapshot');
    S.projectId = snap.project?.id || null;
    S.sessionStart = snap.session_start ?? 0;
    S.lastId = S.sessionStart;  // this run: the events since this serve started
    run.inv.seed(snap.samples || []);  // the ledger now; the events keep it current
  } catch { /* optional */ }
  await refreshBench(true);
  await loadTools();
  // this run so far: applied at once, without animation
  for (;;) {
    let page;
    try { page = await api(`/api/events?after=${S.lastId}&limit=500`); } catch { break; }
    page.forEach(e => handle(e, false));
    if (page.length < 500) break;
  }
  await refreshBench(true);  // the twin now, after the history's frames
  if (!run.events.some(e => e.kind === 'user.message' || e.kind === 'agent.message')) feed.reset(emptyState());
  feed.toBottom(true);
  S.live = true;
  S.uiDirty = true;
  sceneDirty(true);
  connect();
  setInterval(() => refreshBench(!S.busy && !S.queue.length), 1500);
  setInterval(pollHealth, 5000);
}
boot();
