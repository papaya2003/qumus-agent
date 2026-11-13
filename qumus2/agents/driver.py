"""ConversationDriver: feeds UI instructions to the AgentRuntime.

Polls the instruction table. While the runtime is idle, each pending message
becomes the next user turn. While busy, messages wait for the PostToolUse path
(ToolGateway.post) to deliver them.

Stop and Continue (UI buttons):
  stop      messages not yet delivered are dropped (`user.dropped`), the runtime
            is interrupted and every job pauses at its next safe point
            (runtime/jobs.py); `system.stopped`.
  continue  every paused job resumes where restart recovery says it can
            (`system.continued` lists from which step, or that a person must
            look), then the runtime goes on (`AgentRuntime.continue_turn`) with
            a note saying so.
A message sent after Stop without Continue reaches the orchestrator with a note
that jobs are paused (they go on at Continue or when it retries them).

Model service outage (HTTP 429/5xx or no connection, after the SDK's own
per-request retries): the turn ends with `agent.error`; the driver then waits
(`agent.outage_wait`: the provider's retry-after, at least the next step of
OUTAGE_WAITS_S, whose last step repeats) and continues the same conversation
with a note saying so. By default it keeps trying until the service is back
(unattended runs last days); QUMUS_OUTAGE_MAX_HOURS sets a total limit. Waiting
costs nothing: refused requests are not billed. Stop cancels the wait; a spent
budget ends it (sends without any reply keep their reservation, so endless
timeouts run into the budget cap). Any other error still ends the turn.

Once the runtime reports its run budget spent (`budget_exhausted`, OpenAIRuntime
with a --budget), messages are dropped (`user.dropped`) instead of sent.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from qumus2.agents.runtime import AgentRuntime, ReportedAgentError
from qumus2.core.scrub import scrub_text
from qumus2.interaction.inbox import Inbox, as_turn
from qumus2.runtime.jobs import JobService
from qumus2.runtime.store import RunStore

log = logging.getLogger(__name__)

MESSAGE_KINDS = ("message", "plan_change", "veto")

#: waits (s) before each automatic continue after a model service outage; the
#: last one repeats until the service is back or QUMUS_OUTAGE_MAX_HOURS passes.
#: A turn that ran longer than OUTAGE_RESET_S before failing made progress, so
#: the waits start short again.
OUTAGE_WAITS_S = (15.0, 30.0, 60.0, 120.0, 240.0, 480.0, 600.0)
OUTAGE_MAX_WAIT_S = 900.0
OUTAGE_RESET_S = 300.0


def outage_max_s() -> float | None:
    """Total time to keep waiting for the model service; None = no limit."""
    raw = os.environ.get("QUMUS_OUTAGE_MAX_HOURS", "").strip()
    if not raw or float(raw) <= 0:
        return None
    return float(raw) * 3600.0


class ConversationDriver:
    def __init__(self, store: RunStore, runtime: AgentRuntime, jobs: JobService,
                 poll_s: float = 0.2) -> None:
        self.store, self.runtime, self.jobs = store, runtime, jobs
        self.inbox = Inbox(store)
        self.poll_s = poll_s
        self._task: asyncio.Task[None] | None = None
        self._turn: asyncio.Task[None] | None = None
        #: Stop pressed and nothing said since: the next message carries a note
        self._note_due = False
        #: the sleep before an automatic continue after a model outage (Stop cancels it)
        self._outage_sleep: asyncio.Task[None] | None = None
        #: waits used and the total limit (None = none), overridable in tests
        self.outage_waits_s: tuple[float, ...] = OUTAGE_WAITS_S
        self.outage_max_s: float | None = outage_max_s()

    async def tick(self) -> None:
        for s in self.inbox.drain(kinds=("stop",)):
            await self._stop(s)
        for c in self.inbox.drain(kinds=("continue",)):
            await self._continue(c)
        if self.runtime.busy or (self._turn and not self._turn.done()):
            return
        if getattr(self.runtime, "budget_exhausted", False):
            self._drop("the run budget is spent; not sent to the model")
            return
        items = self.inbox.drain()
        if items:
            text = as_turn(items)
            if self._note_due:
                text = self._stopped_note() + text
                self._note_due = False  # said once; the paused jobs stay paused
            self._turn = asyncio.create_task(self._run(self.runtime.send, text))

    def _drop(self, why: str) -> list[dict[str, Any]]:
        dropped = [i for i in self.store.pending_instructions() if i["kind"] in MESSAGE_KINDS]
        for i in dropped:
            self.store.mark_instruction(i["id"], "dropped", why)
            self.store.emit("user.dropped", {"instruction_id": i["id"], "text": i["text"],
                                             "reason": why})
        return dropped

    async def _stop(self, ins: dict[str, Any]) -> None:
        dropped = self._drop("Stop pressed before delivery")
        if self._outage_sleep is not None:
            self._outage_sleep.cancel()
        # The stations stop first (in a thread, at once): the physical stop must
        # not wait for the model's turn to unwind.
        stopping = asyncio.create_task(asyncio.to_thread(self.jobs.stop_all))
        await self.runtime.interrupt()
        try:
            await stopping
        except Exception as e:  # noqa: BLE001 - Stop is reported, never fatal to the driver
            log.exception("Stop was not confirmed by every station")
            self._note_due = True
            self.store.mark_instruction(ins["id"], "handled", f"interrupted; stop unconfirmed: {e}")
            self.store.emit("system.stop_unconfirmed", {"instruction_id": ins["id"], "error": str(e),
                                                        "dropped": [i["id"] for i in dropped]})
            return
        self._note_due = True
        self.store.mark_instruction(ins["id"], "handled", "interrupted + jobs paused")
        self.store.emit("system.stopped", {"instruction_id": ins["id"],
                                           "dropped": [i["id"] for i in dropped]})

    async def _continue(self, ins: dict[str, Any]) -> None:
        try:
            decisions = await asyncio.to_thread(self.jobs.resume_all)
        except Exception as e:  # noqa: BLE001 - reported to the page, the driver keeps running
            log.exception("Continue failed")
            self.store.mark_instruction(ins["id"], "handled", f"continue failed: {e}")
            self.store.emit("system.continue_failed", {"instruction_id": ins["id"], "error": str(e)})
            return
        resumed, waiting = [], []
        for d in decisions:
            run = self.store.get_run(d["run_id"]) or {}
            item = {"run_id": d["run_id"], "flow": run.get("flow"), "reason": d["reason"]}
            if d["action"] == "resume":
                resumed.append({**item, "from_step": d["start_seq"] + 1,
                                "step": _step_name(self.store, run, d["start_seq"])})
            else:
                waiting.append({**item, "step": _step_name(self.store, run, d["start_seq"])})
        self._note_due = False
        self.store.mark_instruction(ins["id"], "handled", f"{len(resumed)} job(s) resumed")
        self.store.emit("system.continued", {"instruction_id": ins["id"], "resumed": resumed,
                                             "waiting_human": waiting})
        running = self.runtime.busy or (self._turn is not None and not self._turn.done())
        if running and not getattr(self.runtime, "stopped", False):
            # Continue without a Stop (or pressed twice) while a turn runs: the
            # paused jobs resumed above; the running turn sees them. Never start
            # a second turn beside it.
            return
        self._turn = asyncio.create_task(
            self._run(self.runtime.continue_turn, _continue_text(resumed, waiting)))

    def _stopped_note(self) -> str:
        paused = self.store.list_runs("paused")
        jobs = "; ".join(f"{r['id']} ({r['flow']}, stopped at step "
                         f"{(r['meta'].get('paused') or {}).get('seq', 0) + 1})" for r in paused)
        return ("[System note: the user pressed Stop. "
                + (f"Paused jobs: {jobs}. They stay paused until the user presses Continue "
                   "or you call retry_job on them." if paused else "No hardware job is paused.")
                + "]\n")

    async def _run(self, fn: Callable[[str], Awaitable[None]], text: str) -> None:
        """Run one turn; an exception (gateway down, bad key, SDK crash) is
        shown in the UI as `agent.error` instead of vanishing. After a model
        service outage, wait and continue the same conversation (module doc)."""
        loop = asyncio.get_running_loop()
        failures, waited = 0, 0.0
        while True:
            started = loop.time()
            try:
                await fn(text)
                return
            except Exception as e:
                recorded = self.store.events_after(e.event_id - 1, limit=1) if isinstance(e, ReportedAgentError) else []
                if not (recorded and recorded[0].id == e.event_id and recorded[0].kind == "agent.error"
                        and recorded[0].payload.get("error") == str(e)):
                    self.store.emit("agent.error", {"error": scrub_text(str(e))[:2000],
                                                    "type": type(e).__name__}, agent="orchestrator")
                outage = getattr(e, "outage", None)
                if loop.time() - started > OUTAGE_RESET_S:
                    failures, waited = 0, 0.0
                step = self.outage_waits_s[min(failures, len(self.outage_waits_s) - 1)]
                hint = (outage or {}).get("retry_after_s") or 0.0
                wait = min(max(step, float(hint)), OUTAGE_MAX_WAIT_S)
                limit = self.outage_max_s
                if (not outage or getattr(self.runtime, "budget_exhausted", False)
                        or (limit is not None and waited + wait > limit)):
                    if outage:
                        self.store.emit("agent.outage_gave_up", {
                            "status": outage.get("status"), "attempts": failures, "waited_s": waited,
                            "text": "Model service still unavailable; automatic continue stopped. "
                                    "Send a message or press Continue to go on."}, agent="orchestrator")
                    raise
                failures += 1
                waited += wait
                self.store.emit("agent.outage_wait", {
                    "status": outage.get("status"), "retry_after_s": outage.get("retry_after_s"),
                    "wait_s": wait, "attempt": failures, "waited_s": waited, "max_s": limit,
                    "text": f"Model service unavailable (HTTP {outage.get('status') or 'no connection'}); "
                            f"continuing in {wait:.0f} s (attempt {failures}, "
                            f"{waited / 60:.0f} min waited so far)."},
                    agent="orchestrator")
            self._outage_sleep = asyncio.create_task(asyncio.sleep(wait))
            try:
                await self._outage_sleep
            except asyncio.CancelledError:
                if self._outage_sleep.cancelled() and not asyncio.current_task().cancelling():
                    self.store.emit("agent.outage_cancelled", {"reason": "Stop pressed"}, agent="orchestrator")
                    return
                raise
            finally:
                self._outage_sleep = None
            if getattr(self.runtime, "budget_exhausted", False):
                return
            fn = self.runtime.continue_turn
            text = (f"[System note: the model service was unavailable (HTTP "
                    f"{outage.get('status') or 'no connection'}), so your last turn ended early; "
                    f"waited {wait:.0f} s. Continue the task from where you stopped. Check the "
                    "current jobs and samples first; hardware jobs kept running meanwhile.]")

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end every later message
                log.exception("conversation driver tick failed")
            await asyncio.sleep(self.poll_s)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run_forever())

    async def wait_idle(self, timeout: float = 30.0) -> None:
        """Test helper: tick until no pending turn remains."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            await self.tick()
            if ((not self._turn or self._turn.done()) and not self.runtime.busy
                    and not self.store.pending_instructions()):
                if self._turn and self._turn.done() and self._turn.exception():
                    raise self._turn.exception()  # type: ignore[misc]
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("driver did not become idle")


def _step_name(store: Any, run: dict[str, Any], seq: int) -> str | None:
    """Name of a step from the mirrored lab step rows (the brain never builds flows)."""
    if not run:
        return None
    for step in store.get_steps(run["id"]):
        if step["seq"] == seq:
            return step["name"]
    return None


def _continue_text(resumed: list[dict[str, Any]], waiting: list[dict[str, Any]]) -> str:
    lines = ["[The user pressed Continue after Stop.]"]
    for r in resumed:
        lines.append(f"Job {r['run_id']} ({r['flow']}) continues from step {r['from_step']}"
                     f" ({r['step']}); {r['reason']}.")
    for w in waiting:
        lines.append(f"Job {w['run_id']} ({w['flow']}) waits for a person: {w['reason']}.")
    if not resumed and not waiting:
        lines.append("No hardware job was paused.")
    lines.append("Go on with the plan from where you stopped"
                 + ("; wait for the resumed jobs with wait_job before their next step."
                    if resumed else "."))
    return "\n".join(lines)
