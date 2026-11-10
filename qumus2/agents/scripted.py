"""Scripted mock runtime: agents without an API key (spec 15.2).

Replays a scenario (agents/scenarios/*.yaml). Each user message runs the
next turn. Tool calls go through the real ToolGateway, so policy checks, job
service, StepRunner, SafetyGate and events are all real; only the model's words
and choices are scripted.

Scenario format:

    name: place_chip_demo
    turns:
      - actions:
          - say: {agent: orchestrator, text: "..."}
          - delegate: {to: hands, task: "..."}     # save_as: the scenario goes on
                                                    # after a refused delegation
          - tool: {agent: hands, name: place_chip_on_transfer_stage,
                   args: {chip_id: chip_001}, save_as: job}
          - tool: {agent: hands, name: wait_job, args: {job_id: "{{job.job_id}}"}}
          - done: {agent: hands, summary: "..."}

"{{name.key}}" is replaced by `data[key]` of the tool result saved as `name`
(dotted paths reach into it: "{{cands.candidates_by_role.channel.0.id}}");
`{{name._status}}` and `{{name._summary}}` are that result's status and summary;
`{{turn.message_id}}` is the id of the person's message that started the turn. A scripted turn can branch on it:

          - branch:
              result_of: result                # a save_as name ("on" is a YAML boolean)
              cases:
                ok: [ ...actions... ]
                corrected: [ ...actions... ]
              default: [ ...actions... ]

Choices that depend on what the testbench produced (which flakes exist) cannot
be written down in advance, so two actions stand in for the picker's and the
designer's judgement. Both call the same tools the model would, through the
gateway, so the code checks are real:

          - pick_flakes: {save_as: pick}   # compatible observed stack, or blocked
          - lay_out: {save_as: design}     # rectangles sized to the plan
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import yaml

from qumus2.agents.gateway import ToolGateway
from qumus2.agents.tooling import ToolResult
from qumus2.agents.tools.picking import has_images, require_images

SCENARIOS = Path(__file__).resolve().parent / "scenarios"
_TPL = re.compile(r"\{\{(\w+)((?:\.\w+)+)\}\}")
_MESSAGE_ID = re.compile(r"\[message id: ([\w-]+)\]")
#: tool results that leave a job for a person (a parked or failed job)
_STOPPED = ("needs_attention", "failed", "error")
#: re-place rounds for a parked off-target placement (retry_job's own budget
#: in config/agents.yaml recovery.per_job stops it earlier)
MAX_PLACEMENT_RETRIES = 3
# How long (wall seconds) the scripted mock keeps re-waiting on a job that is
# still queued/running. wait_job's timeout counts simulated seconds, which the
# accelerated clock spends in a few wall seconds, so a count of re-waits ran
# out during one full-resolution v1 search (about an hour of wall time).
MAX_PENDING_WAIT_S = 4 * 3600.0


def _job_of(r: ToolResult) -> str | None:
    d = r.data or {}
    return d.get("job_id") or d.get("run_id")


class _CannotContinue(Exception):
    """A template needs data an earlier step did not return (it failed or its
    job parked): the scripted turn stops the way an agent would."""

    def __init__(self, name: str, saved: dict[str, Any] | None) -> None:
        self.name, self.saved = name, saved or {}
        super().__init__(name)


def load_scenario(name_or_path: str) -> dict[str, Any]:
    p = Path(name_or_path)
    if not p.exists():
        p = SCENARIOS / f"{name_or_path}.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


class ScriptedRuntime:
    mode = "scripted"

    def __init__(self, gateway: ToolGateway, scenario: dict[str, Any],
                 step_delay_s: float = 0.0) -> None:
        self.gateway = gateway
        self.store = gateway.store
        self.turns: list[dict[str, Any]] = list(scenario.get("turns", []))
        self.delay = step_delay_s
        self._busy = False
        #: cleared by Stop: the turn waits before its next action until Continue
        self._go = asyncio.Event()
        self._go.set()
        self.saved: dict[str, dict[str, Any]] = {}
        self.results: list[Any] = []  # (agent, tool, ToolResult) for tests

    @property
    def busy(self) -> bool:
        return self._busy

    def _lookup(self, m: re.Match[str]) -> Any:
        name = m.group(1)
        try:
            v: Any = self.saved[name]
            for key in m.group(2)[1:].split("."):
                v = v[int(key)] if isinstance(v, list) else v[key]
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise _CannotContinue(name, self.saved.get(name)) from e
        return v

    def _subst(self, v: Any) -> Any:
        if isinstance(v, str):
            m = _TPL.fullmatch(v)
            if m:
                return self._lookup(m)
            return _TPL.sub(lambda m: str(self._lookup(m)), v)
        if isinstance(v, dict):
            return {k: self._subst(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self._subst(x) for x in v]
        return v

    @property
    def stopped(self) -> bool:
        """A turn is suspended by Stop and goes on at Continue."""
        return self._busy and not self._go.is_set()

    async def send(self, text: str) -> None:
        self._busy = True
        self._go.set()
        ok = False
        try:
            if not self.turns:
                self.store.emit("agent.message", {"text": f"(scripted mock) received: {text}. "
                                                  "No more scripted turns; set "
                                                  "QUMUS_OPENAI_* and serve --openai for model-backed agents."},
                                agent="orchestrator")
                return
            turn = self.turns.pop(0)
            # "{{turn.message_id}}": the person's message that started this turn
            # (tools that act on a person's word cite it, as the model would)
            ids = _MESSAGE_ID.findall(text)
            self.saved["turn"] = {"message_id": ids[-1] if ids else "", "message_ids": ids,
                                  "_status": "ok", "_summary": text}
            for action in turn.get("actions", []):
                await self._do(action)
                if self.delay:
                    await asyncio.sleep(self.delay)
            ok = True
        except _CannotContinue as e:
            await self._stop(e)
        finally:
            self.gateway.turn_finished(ok=ok)
            self._busy = False

    async def continue_turn(self, text: str) -> None:
        """Continue after Stop: the suspended turn goes on where it stopped (a
        wait on a paused job waits again); with none, `text` is the next turn."""
        if self.stopped:
            self._go.set()
            return
        await self.send(text)

    async def _do(self, action: dict[str, Any]) -> None:
        await self._go.wait()  # Stop holds the turn here until Continue
        (kind, body), = action.items()
        if kind == "repeat":
            # bounded loop: run `actions` up to `times`, stopping once
            # saved[until.result_of] has status until.status (or, with
            # until.status_not, once it has any other status)
            until = body.get("until") or {}

            def finished() -> bool:
                got = self.saved.get(until.get("result_of"), {}).get("_status")
                if "status_not" in until:
                    return got is not None and got != until["status_not"]
                return got == until.get("status")

            for _ in range(int(body["times"])):
                if finished():
                    break
                for sub in body.get("actions", []):
                    await self._do(sub)
            return
        if kind == "branch":
            status = self.saved[body["result_of"]]["_status"]
            for sub in body.get("cases", {}).get(status, body.get("default", [])):
                await self._do(sub)
            return
        body = self._subst(body)
        if kind == "say":
            self.store.emit("agent.message", {"text": body["text"]}, agent=body["agent"])
        elif kind == "delegate":
            # the same delegation rules as the SDK's Agent tool (plan first, eyes
            # first, advisor first); with save_as the scenario goes on after a
            # refusal, as an orchestrator would
            d = self.gateway.delegate(body["to"], task=body.get("task", ""))
            if not d.allow:
                saved = {"_status": "denied", "_summary": d.reason, "rule": d.rule}
                if body.get("save_as"):
                    self.saved[body["save_as"]] = saved
                    return
                raise _CannotContinue(f"delegation to {body['to']}", saved)
            if body.get("save_as"):
                self.saved[body["save_as"]] = {"_status": "ok",
                                               "_summary": f"delegated to {body['to']}"}
            self.store.emit("agent.delegate", {"to": body["to"], "task": body["task"]},
                            agent="orchestrator")
            # as the SDK's Agent tool: the step named first, else by owner
            self.gateway.delegation_started(f"scripted:{body['to']}", body["to"],
                                            f"{body['step']}: " if body.get("step") else "")
        elif kind == "done":
            self.store.emit("agent.done", {"summary": body["summary"]}, agent=body["agent"])
            # step_status: e.g. "running" when the agent hands the step back unfinished
            self.gateway.delegation_finished(f"scripted:{body['agent']}",
                                             body.get("step_status", "done"))
        elif kind == "tool":
            await self._tool(body["agent"], body["name"], body.get("args", {}),
                             body.get("save_as"))
        elif kind == "pick_flakes":
            await self._pick_flakes(body.get("agent", "picker"), body.get("save_as"),
                                    body.get("fixture_footprint_um"))
        elif kind == "lay_out":
            await self._lay_out(body.get("agent", "designer"), body.get("save_as"))
        else:
            raise ValueError(f"unknown scenario action {kind!r}")

    async def _stop(self, e: _CannotContinue) -> None:
        """Do what the agents would: ask for help on a job left parked, and say
        why the work stops."""
        bad = [(a, n, r) for a, n, r in reversed(self.results) if r.status in _STOPPED]
        # a parked job is the cause; later refusals (nothing to search) follow from it
        stopped = next((x for x in bad if _job_of(x[2])), bad[0] if bad else None)
        why = (f"{e.name} returned {e.saved.get('_status', 'nothing')}: "
               f"{e.saved.get('_summary', 'no result')}")
        if stopped:
            agent, name, r = stopped
            why = f"{name} returned {r.status}: {r.summary}"
            job = _job_of(r)
            helper = agent if agent in ("hands", "orchestrator") else "orchestrator"
            await self._tool(helper, "request_human_help",
                             {"reason": f"Scripted demo stopped: {why}",
                              **({"job_id": job} if job else {})})
        self.store.emit("agent.message", {"text": f"I have to stop here: {why}. A person "
                                          "needs to look before this can go on."},
                        agent="orchestrator")

    async def _call(self, agent: str, name: str, args: dict[str, Any]) -> tuple[ToolResult, Any]:
        loop = asyncio.get_running_loop()
        give_up = loop.time() + MAX_PENDING_WAIT_S
        while True:
            result, extra = await self.gateway.call(agent, name, args)
            job = result.data or {}
            # A wait that ran out while the job is still queued or running is not
            # an outcome: a real agent waits again (slow full-resolution captures
            # can make one search outlast a single wait).
            if (name == "wait_job" and result.status == "pending" and not job.get("done")
                    and job.get("status") in ("queued", "running")
                    and loop.time() < give_up):
                continue
            if name != "wait_job" or job.get("status") != "paused":
                return result, extra
            # the job stopped at Stop: wait for Continue (it resumes the job), then again
            await self._go.wait()
            await asyncio.sleep(0.2)

    def _missed_placement(self, job_id: str) -> dict[str, Any] | None:
        """The failed step of a parked job when it is an arm placement that
        landed outside tolerance (its L0 re-places ran out), else None."""
        run = self.store.get_run(job_id)
        if not run or run["status"] != "parked":
            return None
        failed = [s for s in self.store.get_steps(job_id) if s["status"] == "failed"]
        step = failed[-1] if failed else None
        m = (step or {}).get("measurements") or {}
        if step and step.get("safe_to_resume") and "arm_pose" in m and "offset_mm" in m:
            return step
        return None

    async def _tool(self, agent: str, name: str, args: dict[str, Any],
                    save_as: str | None = None) -> ToolResult:
        result, extra = await self._call(agent, name, args)
        job_id = args.get("job_id") if name == "wait_job" else None
        # Jialei 2026-11-03: a chip placed off target is placed again
        # automatically (recovery menu retry_job; its budget bounds the loop).
        for _ in range(MAX_PLACEMENT_RETRIES):
            if result.status != "needs_attention" or not job_id:
                break
            step = self._missed_placement(job_id)
            if step is None:
                break
            self.results.append((agent, name, result))
            m = step["measurements"]
            retry, _ = await self._call("hands", "retry_job", {"job_id": job_id, "reason": (
                f"{step['name']} landed {m['offset_mm']} mm / {m.get('drot_deg')} deg off "
                "target after its re-places; place it again")})
            self.results.append(("hands", "retry_job", retry))
            if retry.status != "pending":
                break
            result, extra = await self._call(agent, name, args)
        self.results.append((agent, name, result))
        if save_as:
            self.saved[save_as] = {**result.data, "_status": result.status,
                                   "_summary": result.summary}
        if extra:
            self.store.emit("agent.message", {"text": f"(scripted mock) noted: {extra}"},
                            agent="orchestrator")
        return result

    async def _revisit_pending(self, agent: str, got: ToolResult) -> bool:
        """As the real picker must: 10X-only stock is photographed before more exfoliation.

        Reports the image-evidence blocker for the first role with pending
        revisits, then the hands run the revisit requests the tool returned.
        Returns False when nothing is pending (a real shortage).
        """
        requests = got.data.get("revisit_requests_by_role") or {}
        role = next((r for r, reqs in requests.items() if reqs), None)
        if role is None:
            return False
        res = await self._tool(agent, "report_selection_blocked", {
            "role": role, "action": "relink_or_capture_optical_images",
            "reason": "Role-relevant candidates are still at 10X only; photograph them at "
                      "20X/100X before claiming a shortage"})
        if res.status != "ok":
            raise _CannotContinue("revisit report", {"_status": res.status, "_summary": res.summary})
        self.store.emit("agent.delegate", {"to": "hands", "task": "Photograph the pending "
                        "candidates at 20X/100X (search_chip revisit_only)."}, agent="orchestrator")
        seen: set[tuple[str, str]] = set()
        for reqs in requests.values():
            for req in reqs:
                args = req["explicit_target_args"]
                if (args["chip_id"], args["material"]) in seen:
                    continue
                seen.add((args["chip_id"], args["material"]))
                sub = await self._tool("hands", "search_chip", args)
                if sub.status != "pending":
                    raise _CannotContinue("revisit search", {"_status": sub.status,
                                                             "_summary": sub.summary})
                await self._tool("hands", "wait_job", {"job_id": sub.data["job_id"],
                                                       "timeout_s": 600})
        return True

    async def _pick_flakes(self, agent: str, save_as: str | None,
                           fixture_footprint_um: list[float] | None = None,
                           revisit_rounds: int = 2) -> None:
        """Stand-in picker: rank complete combinations by observed coverage."""
        from qumus2.records.geometry import ranked_pickup_combinations

        got = await self._tool(agent, "get_flake_candidates", {})
        if got.status != "ok":
            self._save(save_as, got)
            return

        async def revisit_then_retry() -> bool:
            if revisit_rounds > 0 and await self._revisit_pending(agent, got):
                await self._pick_flakes(agent, save_as, fixture_footprint_um, revisit_rounds - 1)
                return True
            return False

        roles, cands = got.data["roles"], got.data["candidates_by_role"]
        if "target_pairs" in got.data:
            await self._pick_target_pair(agent, save_as, got, revisit_then_retry)
            return
        slots, choices = [], []
        for r in roles:
            # Role constraints still apply before evaluating the whole stack.
            free = [f for f in sorted(cands[r["role"]], key=lambda f: -f.get("area_um2", 0))
                    if not require_images() or has_images({"props": f})]
            if len(free) < r["count"]:
                if await revisit_then_retry():
                    return
                # as the real picker must: unphotographed candidates are captured first
                unseen = require_images() and any(not has_images({"props": f})
                                                  for f in cands[r["role"]])
                res = await self._tool(agent, "report_selection_blocked", {
                    "role": r["role"], "action": "relink_or_capture_optical_images" if unseen
                    else "exfoliate_and_search_one_chip",
                    "reason": f"{len(free)} usable candidate(s) for {r['role']}, "
                              f"{r['count']} needed"})
                self._save(save_as, res.model_copy(update={"status": "blocked"}))
                return
            slots.extend([r["role"]] * r["count"])
            choices.extend([[f["id"] for f in free]] * r["count"])
        ranked = ranked_pickup_combinations(self.store, choices)
        if not ranked and await revisit_then_retry():
            return
        if not ranked:
            assessments = []
            for flake in cands[roles[0]["role"]]:
                await self._tool(agent, "view_flake_images", {"flake_id": flake["id"]})
                assessments.append({"flake_id": flake["id"], "feasible": False,
                    **{key: 50 for key in ("geometry_fitness", "visible_cleanliness", "continuity_uniformity",
                                          "evidence_confidence", "inventory_tiebreaker", "overall_score")},
                    "reason": "Current optical evidence does not establish a compatible combination with the other role candidates",
                    "observed_risks": ["Current calibrated outlines do not establish full pickup coverage at nominal alignment"]})
            res = await self._tool(agent, "report_selection_blocked", {
                "role": roles[0]["role"], "action": "exfoliate_and_search_one_chip",
                "assessments": assessments,
                "reason": "Current evidence does not establish complete top-hBN coverage; obtain another photographed source chip."})
            if res.status != "ok":
                raise _CannotContinue("coverage-shortage report", {"_status": res.status, "_summary": res.summary})
            self._save(save_as, res.model_copy(update={"status": "blocked"}))
            return
        chosen: dict[str, list[str]] = {r["role"]: [] for r in roles}
        for role, fid in zip(slots, ranked[0]["flake_ids"], strict=True):
            chosen[role].append(fid)
        for ids in chosen.values():
            for fid in ids:
                await self._tool(agent, "view_flake_images", {"flake_id": fid})
        picked = {f for ids in chosen.values() for f in ids}
        assessments = self._assessments(cands, picked, fixture_footprint_um)
        res = await self._tool(agent, "submit_flake_selection", {
            "selected_by_role": chosen, "assessments": assessments,
            "reasons": "Role-eligible observed combination ranked by minimum nominal edge clearance; actual pickup remains checked"})
        self._save(save_as, res)

    async def _pick_target_pair(self, agent: str, save_as: str | None, got: ToolResult,
                                revisit_then_retry: Any) -> None:
        """Eight-contact target: submit the solver's best pair, or name the shortage.

        The pair ranking (get_flake_candidates target_pairs) uses the same
        placement solver as the designer; submit_flake_selection still runs the
        full code check on it.
        """
        roles, cands = got.data["roles"], got.data["candidates_by_role"]
        pairs = got.data.get("target_pairs") or []
        if pairs:
            pair = pairs[0]
            for fid in pair["flake_ids"]:
                await self._tool(agent, "view_flake_images", {"flake_id": fid})
            res = await self._tool(agent, "submit_flake_selection", {
                "selected_by_role": pair["selected_by_role"],
                "design_placements": pair["design_placements"],
                "assessments": self._assessments(cands, set(pair["flake_ids"]), None),
                "reasons": f"Solver-ranked target pair with {pair['headroom_um']:.1f} um worst "
                           "pose-window headroom; actual pickup remains checked"})
            self._save(save_as, res)
            return
        if await revisit_then_retry():
            return
        shortage = got.data.get("target_pair_shortage") or {}
        material = shortage.get("material")
        role = next((r["role"] for r in roles if r["material"] == material), None)
        if role is None:
            raise _CannotContinue("target pair search", {
                "_status": "error", "_summary": shortage.get("reason", "no target pair and no shortage material")})
        assessments = []
        for flake in cands.get(role, []):
            await self._tool(agent, "view_flake_images", {"flake_id": flake["id"]})
            assessments.append({"flake_id": flake["id"], "feasible": False,
                **{key: 50 for key in ("geometry_fitness", "visible_cleanliness", "continuity_uniformity",
                                      "evidence_confidence", "inventory_tiebreaker", "overall_score")},
                "reason": f"Target pair search: {shortage.get('reason', 'no fitting pair')}",
                "observed_risks": [f"Placement solver limiting constraint: {shortage.get('limiting')}"]})
        res = await self._tool(agent, "report_selection_blocked", {
            "role": role, "action": "exfoliate_and_search_one_chip", "assessments": assessments,
            "reason": f"No observed {material} candidate completes a fitting pair for the eight-contact target."})
        if res.status != "ok":
            raise _CannotContinue("target shortage report", {"_status": res.status, "_summary": res.summary})
        self._save(save_as, res.model_copy(update={"status": "blocked"}))
        if save_as:
            self.saved[save_as]["material"] = material

    @staticmethod
    def _assessments(cands: dict[str, list[dict[str, Any]]], picked: set[str],
                     fixture_footprint_um: list[float] | None) -> list[dict[str, Any]]:
        assessments: list[dict[str, Any]] = []
        for fs in cands.values():
            for f in fs:
                if any(a["flake_id"] == f["id"] for a in assessments):
                    continue
                score = 80.0 if f["id"] in picked else 50.0
                a: dict[str, Any] = {
                    "flake_id": f["id"], "feasible": True, "geometry_fitness": score,
                    "visible_cleanliness": score, "continuity_uniformity": score,
                    "evidence_confidence": score, "inventory_tiebreaker": score,
                    "overall_score": score,
                    "surface_residue": "clean" if f["id"] in picked else "light",
                    "residue_evidence": "100X image: substrate around the flake is clear",
                    "reason": "compatible observed stack with greater nominal clearance" if f["id"] in picked else "not in the preferred compatible combination"}
                if fixture_footprint_um:
                    # Explicit scenario input, like the mock colour/residue judgement above.
                    # It exercises contracts only; never claim optical measurement.
                    a.update(usable_width_um=fixture_footprint_um[0],
                             usable_height_um=fixture_footprint_um[1],
                             geometry_evidence="Scripted synthetic fixture supplied by scenario; "
                             "not an image measurement or physical fit certification")
                if f.get("material") == "hBN":
                    a["hbn_color_judgement"] = {
                        "relative_body_color": "even blue-green", "background_comparison":
                        "clearly darker than the substrate", "thick_edge_visible": False,
                        "classification": "usable_color_family", "evidence": "20X image"}
                assessments.append(a)
        return assessments

    async def _lay_out(self, agent: str, save_as: str | None) -> None:
        """Stand-in designer: a channel at the plan's minimum size plus margins."""
        got = await self._tool(agent, "get_design_context", {})
        if got.status != "ok":
            self._save(save_as, got)
            return
        plan, by_role = got.data["device_plan"], got.data["selected_by_role"]
        ids = [f["id"] for fs in by_role.values() for f in fs]
        if plan.get("design_contract") == "device_design_v1":
            suggested = got.data.get("suggested_placement") or {}
            if suggested.get("status") == "fit":
                res = await self._tool(agent, "create_device_design", {
                    "placements": suggested["placements"], "contact_overlap_um": 1.0})
            else:
                res = await self._tool(agent, "report_design_infeasible", {
                    "flake_ids": ids, "failed_roles": sorted(by_role),
                    "reason": f"Placement solver: {suggested.get('summary', suggested.get('status'))}"})
                res = res.model_copy(update={"status": "blocked"}) if res.status == "ok" else res
            self._save(save_as, res)
            return
        chan = next((r["role"] for r in plan["flake_roles"] if r["material"] == "Gr"),
                    plan["flake_roles"][0]["role"])
        footprint = plan.get("required_footprint")
        width, height = ((footprint["width_um"], footprint["height_um"])
                         if footprint else (6, 12))
        shapes = [{"layer": chan, "x_um": 0, "y_um": 0, "w_um": width, "h_um": height}]
        for r in plan["flake_roles"]:
            if r["role"] != chan:
                shapes.append({"layer": r["role"], "x_um": -4, "y_um": -4,
                               "w_um": width + 8, "h_um": height + 8})
        if plan.get("contact_scheme"):
            if footprint:
                shapes += [{"layer": "contact_metal", "x_um": -6, "y_um": 0, "w_um": 8, "h_um": height},
                           {"layer": "contact_metal", "x_um": width - 2, "y_um": 0, "w_um": 8, "h_um": height}]
            else:
                shapes += [{"layer": "contact_metal", "x_um": 1, "y_um": -8, "w_um": 4, "h_um": 6},
                           {"layer": "contact_metal", "x_um": 1, "y_um": 14, "w_um": 4, "h_um": 6}]
        if "top" in plan.get("gate_scheme", "").lower():
            shapes.append({"layer": "gate_metal", "x_um": 0, "y_um": height + 6, "w_um": width, "h_um": 4}
                          if footprint else {"layer": "gate_metal", "x_um": 12, "y_um": 0, "w_um": 4, "h_um": 12})
        res = await self._tool(agent, "submit_design", {
            "flake_ids": ids, "shapes": shapes, "channel_role": chan,
            "notes": f"Scripted rectangle fixture: {width:g} x {height:g} um channel; not a measured or KLayout-certified layout."})
        self._save(save_as, res)

    def _save(self, save_as: str | None, result: ToolResult) -> None:
        if save_as:
            self.saved[save_as] = {**result.data, "_status": result.status,
                                   "_summary": result.summary}

    async def interrupt(self) -> None:
        if self._busy and self._go.is_set():
            self._go.clear()
            self.store.emit("agent.interrupted", {"reason": "user stop"}, agent="orchestrator")

    async def close(self) -> None:
        pass
