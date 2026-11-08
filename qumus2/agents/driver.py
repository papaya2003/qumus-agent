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

Once the runtime reports its run budget spent (`budget_exhausted`, OpenAIRuntime
with a --budget), messages are dropped (`user.dropped`) instead of sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from qumus2.agents.runtime import AgentRuntime, ReportedAgentError
from qumus2.core.scrub import scrub_text
from qumus2.interaction.inbox import Inbox, as_turn
from qumus2.runtime.jobs import JobService
from qumus2.runtime.store import RunStore

MESSAGE_KINDS = ("message", "plan_change", "veto")


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
        await self.runtime.interrupt()
        await asyncio.to_thread(self.jobs.stop_all)
        self._note_due = True
        self.store.mark_instruction(ins["id"], "handled", "interrupted + jobs paused")
        self.store.emit("system.stopped", {"instruction_id": ins["id"],
                                           "dropped": [i["id"] for i in dropped]})

    async def _continue(self, ins: dict[str, Any]) -> None:
        decisions = await asyncio.to_thread(self.jobs.resume_all)
        resumed, waiting = [], []
        for d in decisions:
            run = self.store.get_run(d["run_id"]) or {}
            item = {"run_id": d["run_id"], "flow": run.get("flow"), "reason": d["reason"]}
            if d["action"] == "resume":
                resumed.append({**item, "from_step": d["start_seq"] + 1,
                                "step": _step_name(run, d["start_seq"])})
            else:
                waiting.append({**item, "step": _step_name(run, d["start_seq"])})
        self._note_due = False
        self.store.mark_instruction(ins["id"], "handled", f"{len(resumed)} job(s) resumed")
        self.store.emit("system.continued", {"instruction_id": ins["id"], "resumed": resumed,
                                             "waiting_human": waiting})
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
        shown in the UI as `agent.error` instead of vanishing."""
        try:
            await fn(text)
        except Exception as e:
            recorded = self.store.events_after(e.event_id - 1, limit=1) if isinstance(e, ReportedAgentError) else []
            if not (recorded and recorded[0].id == e.event_id and recorded[0].kind == "agent.error"
                    and recorded[0].payload.get("error") == str(e)):
                self.store.emit("agent.error", {"error": scrub_text(str(e))[:2000],
                                                "type": type(e).__name__}, agent="orchestrator")
            raise

    async def run_forever(self) -> None:
        while True:
            await self.tick()
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


def _step_name(run: dict[str, Any], seq: int) -> str | None:
    from qumus2.flows.flow import get_flow

    try:
        flow = get_flow(run["flow"])
        steps = flow.build_steps(flow.Params.model_validate(run["params"]))
    except Exception:  # noqa: BLE001 - a name for a message only
        return None
    return steps[seq].name if 0 <= seq < len(steps) else None


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
