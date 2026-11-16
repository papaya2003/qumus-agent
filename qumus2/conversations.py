"""Conversations: several experiments, or several people, on one run store.

A conversation is a project in the run store (RunStore.create_project). One is
in use at a time (`Services.project_id`, remembered in meta `active_project`).

    belongs to one conversation   the chat (its sessions), the model's
                                  conversation (openai_session_id), the Task Plan
                                  and its history, the person's instructions,
                                  the model choice (openai_model_config)
    shared by all of them         the laboratory (chips, flakes, stamps, jobs,
                                  stations), the experience book, the budget
                                  ledger and the spend so far

A new conversation starts blank: the model sees no earlier message and the plan
is empty; it keeps the current conversation's settings (INHERITED_STATE). A
switch is allowed only while nothing is under way (switch_blocker): no turn,
no undelivered message, no job queued, running, paused or waiting for a person.
So a job started by one conversation is never carried on by another.

Every switch starts a new session (RunStore.begin_session, tagged with the
conversation): the page shows the conversation's chat from the switch on, and
History lists every earlier session under its conversation. Later
per-conversation settings (e.g. plan / execute / auto) go in the project state
like the model choice; add them to INHERITED_STATE if a new conversation should
start with the current one's value.

    GET  /api/conversations                       {active, conversations: [row], blocked}
    POST /api/conversations {name, owner}         new conversation, switched to; 409 if busy
    POST /api/conversations/{id}/switch           continue an earlier one; 409 if busy
"""

from __future__ import annotations

import asyncio
from typing import Any

#: project-state keys a new conversation copies from the one in use (settings, not history)
INHERITED_STATE = ("openai_model_config", "openai_context_limits")
#: job statuses that keep a conversation in use (the job must end first)
ACTIVE_JOB_STATUSES = ("queued", "running", "paused", "waiting_human", "parked")
NAME_MAX = 80


class ConversationBusy(RuntimeError):
    """A switch was asked while something is under way (the reason says what)."""


def switch_blocker(s: Any) -> str | None:
    """Why the conversation cannot change now, or None."""
    driver = s.driver
    if not getattr(driver, "idle", True) or s.runtime.busy:
        return "Qumus is still working. Wait until it has finished (or press Stop and wait until it has stopped)."
    if s.store.pending_instructions():
        return "A message is still waiting to be read. Wait a moment and try again."
    for run in s.store.list_runs():
        if run["status"] in ACTIVE_JOB_STATUSES:
            what = "waiting for a person" if run["status"] in ("waiting_human", "parked") else run["status"]
            return (f"Job {run['id']} ({run['flow']}) is {what}. A conversation can change only when no "
                    "job is queued, running, paused or waiting for a person.")
    return None


def _segments(store: Any) -> list[tuple[int, int | None, str | None]]:
    """Every session as (after_id, until_id or None for the open one, project id)."""
    starts = store.sessions()
    if not starts or starts[0] > 0:
        starts = [0, *starts]  # a store from before sessions were kept: one earlier session
    ends: list[int | None] = [*starts[1:], None]
    return [(a, b, store.session_project(a)) for a, b in zip(starts, ends, strict=True)]


def _costs(store: Any, segments: list[tuple[int, int | None, str | None]]) -> dict[str | None, float]:
    """Model spend per conversation, counted the way RunStore.spent_usd counts it."""
    import json

    rows = store._x("SELECT id, kind, payload_json FROM event "
                    "WHERE kind IN ('agent.turn_end', 'agent.usage') ORDER BY id").fetchall()
    out: dict[str | None, float] = {}
    for row in rows:
        p = json.loads(row["payload_json"] or "{}")
        if row["kind"] == "agent.turn_end" and p.get("cost_accounted_by_usage"):
            continue
        owner = next((pid for a, b, pid in reversed(segments) if row["id"] > a and (b is None or row["id"] <= b)),
                     None)
        out[owner] = out.get(owner, 0.0) + float(p.get("cost_usd") or 0)
    return out


def list_conversations(s: Any) -> dict[str, Any]:
    """Every conversation, the one in use first, then the most recently used."""
    store = s.store
    segments = _segments(store)
    costs = _costs(store, segments)
    rows = []
    for proj in store.list_projects():
        mine = [(a, b) for a, b, pid in segments if pid == proj["id"]]
        last = None
        messages = 0
        for a, b in mine:
            r = store._x("SELECT MAX(ts) AS t, SUM(kind = 'user.message' AND "
                         "json_extract(payload_json, '$.kind') = 'message') AS n FROM event "
                         "WHERE id > ? AND (? IS NULL OR id <= ?)", (a, b, b)).fetchone()
            if r["t"] is not None:
                last = r["t"] if last is None else max(last, r["t"])
            messages += int(r["n"] or 0)
        plan = proj["plan"]
        rows.append({"id": proj["id"], "name": proj["name"], "owner": proj["state"].get("owner") or "",
                     "created_at": proj.get("created_at"), "last_active": last or proj["updated_at"],
                     "messages": messages, "cost_usd": round(costs.get(proj["id"], 0.0), 6),
                     "plan_steps": len(plan),
                     "plan_done": sum(1 for x in plan if x.get("status") in ("done", "skipped")),
                     "active": proj["id"] == s.project_id})
    rows.sort(key=lambda r: (not r["active"], -(r["last_active"] or 0)))
    return {"active": s.project_id, "conversations": rows, "blocked": switch_blocker(s)}


def _clean(text: str, what: str, *, required: bool) -> str:
    text = " ".join(str(text or "").split())
    if required and not text:
        raise ValueError(f"Give the conversation a {what}.")
    if len(text) > NAME_MAX:
        raise ValueError(f"The {what} is longer than {NAME_MAX} characters.")
    return text


async def create_conversation(s: Any, name: str, owner: str = "") -> dict[str, Any]:
    """A new, blank conversation, switched to at once. ValueError: bad name;
    ConversationBusy: something is under way (nothing is created then)."""
    name = _clean(name, "name", required=True)
    owner = _clean(owner, "owner", required=False)
    if why := switch_blocker(s):
        raise ConversationBusy(why)
    current = (s.store.get_project(s.project_id) or {}).get("state", {})
    project_id = s.store.create_project(name)
    s.store.update_project_state(project_id, owner=owner,
                                 **{k: current[k] for k in INHERITED_STATE if k in current})
    return await switch_conversation(s, project_id, created=True)


async def switch_conversation(s: Any, project_id: str, *, created: bool = False) -> dict[str, Any]:
    """Continue conversation `project_id` from where it was left. KeyError: no
    such conversation; ConversationBusy: something is under way."""
    project = s.store.get_project(project_id)
    if project is None:
        raise KeyError(project_id)
    if project_id == s.project_id:
        return project
    if why := switch_blocker(s):
        raise ConversationBusy(why)
    previous = s.project_id
    s.gateway.switch_project(project_id)
    try:
        switch_runtime = getattr(s.runtime, "switch_project", None)
        if switch_runtime is None:
            raise ConversationBusy("These agents cannot change conversation; restart Qumus instead.")
        await switch_runtime()
    except BaseException:
        s.gateway.switch_project(previous)
        raise
    s.driver.switch_project()
    after = s.store.last_event_id()
    s.store.begin_session(after, project_id)
    s.store.set_active_project(project_id)
    s.project_id = project_id
    s.session_start = after
    s.store.emit("conversation.switched", {
        "from": previous, "to": project_id, "name": project["name"],
        "owner": project["state"].get("owner") or "", "created": created})
    if getattr(s, "recorder", None) is not None:
        await asyncio.to_thread(s.recorder.mark_start)  # History plays this session on its own
    return s.store.get_project(project_id)
