"""Matched model campaign adapter, readiness and independent evidence review.

CLI reporting is offline. The explicitly gated run_cell adapter calls the normal
record_three_demos engine and its TrialRegistry/SpendLedger. Missing evidence
stays unknown and blocks launch; no second story runner or state database exists.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qumus2.agents.openai_budget import nominal_rates, request_cost  # noqa: E402
from qumus2.agents.roster import ALL_AGENTS  # noqa: E402
from qumus2.agents.token_usage import cache_usage_summary  # noqa: E402
from qumus2.ui.recording import source_fingerprint  # noqa: E402
from scripts.record_three_demos import (  # noqa: E402
    STORIES,
    TrialRegistry,
    digest,
    http_accounting,
    http_evidence,
    recoverable_search_evidence,
)
from scripts.record_three_demos import (  # noqa: E402
    action_evidence as action_evidence,
)

MODELS = {
    "atria/atria-dawn": "Atria-Dawn-Preview",
    "olares-qumusphusis/qwen-3.8-27b": "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL",
    "qwen-token-plan/deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "qwen-token-plan/qwen3.8-max": "qwen3.8-max",
}
RESOURCES = ("estimated_usd", "http_attempts", "input_tokens", "output_tokens", "wall_seconds")
IDENTITIES = ("application", "config", "skills", "initial_state", "experience", "historical_evidence", "protocol", "harness")
CAPABILITIES = ("access_verified", "vision", "tools")

QUALIFICATION_PROMPT = (
    "Inspect the attached synthetic image. Call get_project_context exactly once. "
    "Then output only JSON with top_colors (the three circles left to right), "
    "lower_left_shape, and project_id copied from that tool result. "
    "Use lowercase English color and shape names. Do not make plans, delegate, "
    "change records or perform laboratory actions.")


def qualification_image():
    """Deterministic synthetic pixels; no camera, perception or simulation call."""
    from PIL import Image, ImageDraw

    picture = Image.new("RGB", (192, 128), "white")
    draw = ImageDraw.Draw(picture)
    for x, color in ((32, "orange"), (96, "blue"), (160, "green")):
        draw.ellipse((x - 18, 14, x + 18, 50), fill=color)
    draw.polygon([(16, 112), (48, 112), (32, 72)], fill="red")
    draw.rectangle((140, 78, 176, 112), fill="purple")
    stream = io.BytesIO()
    picture.save(stream, format="PNG")
    return stream.getvalue()


def observed_qualification(campaign, model):
    """Read scoped immutable result events in the existing registry; no new DB."""
    registry = Path(campaign["campaign_path"]) / "attempts.sqlite3"
    if not registry.is_file():
        return None
    source = digest({"identity": campaign["identity"], "campaign": campaign})
    with sqlite3.connect(registry.resolve().as_uri() + "?mode=ro", uri=True) as db:
        manifests = [json.loads(r[0]) for r in db.execute("SELECT manifest FROM attempt ORDER BY number DESC")]
    for manifest in manifests:
        if (manifest.get("model") != model or manifest.get("story") != "complete"
                or manifest.get("campaign_sha256") != digest(campaign)):
            continue
        path = Path(manifest["workspace"]) / "qumus.db"
        if not path.is_file():
            continue
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            events = [{"id": i, "ts": ts, "kind": k, "run_id": r, "agent": a, "payload": json.loads(p)}
                      for i, ts, k, r, a, p in db.execute("SELECT id,ts,kind,run_id,agent,payload_json FROM event ORDER BY id")]
        for event in reversed(events):
            result = event["payload"]
            if event["kind"] != "demo.qualification_result" or result.get("passed") is not True:
                continue
            prefix = [e for e in events if result.get("after_event_id", 0) < e["id"] <= result.get("through_event_id", 0)]
            if (result.get("source") == source and result.get("campaign_sha256") == digest(campaign)
                    and result.get("model") == model and result.get("gateway") == campaign["gateway"]
                    and result.get("api") == campaign["api"] and bool(result.get("checks"))
                    and all(v is True for v in result["checks"].values())
                    and digest(prefix) == result.get("event_prefix_sha256")):
                return result
    return None


def matched_stories():
    """Keep old Astra profiles immutable; reuse their exact faults and messages."""
    profiles = copy.deepcopy(STORIES)
    profiles["honest_stop"]["events"] = (copy.deepcopy(STORIES["complete"]["events"])
                                          + profiles["honest_stop"]["events"])
    return profiles


def _positive(value, *, integer=False, zero=False):
    return (type(value) in ((int,) if integer else (int, float))
            and math.isfinite(value) and (value >= 0 if zero else value > 0))


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _files_digest(paths):
    return digest({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(paths) if p.is_file()})


def source_identity(*, initial_state=None, experience=None, historical_evidence=None):
    """Read files only; never open a DB, build services or create a checkpoint."""
    if os.environ.get("QUMUS2_CONFIG_DIR") or os.environ.get("QUMUS2_MICROSCOPE_REPLAY_DIR"):
        raise ValueError("Campaign requires packaged configuration and simulated images without overrides")

    def snapshot(path):
        if path is None:
            return None
        p = Path(path)
        if p.suffix != ".json":
            raise ValueError("Supply an explicit immutable JSON snapshot, not a live database")
        json.loads(p.read_text())
        return hashlib.sha256(p.read_bytes()).hexdigest()

    return {
        "application": source_fingerprint(),
        "config": _files_digest((ROOT / "qumus2/config").rglob("*")),
        "skills": _files_digest((ROOT / "qumus2/agents/skills").rglob("*")),
        "initial_state": snapshot(initial_state),
        "experience": snapshot(experience),
        "historical_evidence": (snapshot(historical_evidence) if historical_evidence is not None
                                else digest({"historical_evidence": "explicitly_absent"})),
        "protocol": digest(matched_stories()),
        "harness": _files_digest([ROOT / "scripts/record_model_demos.py", ROOT / "scripts/record_three_demos.py",
                                   ROOT / "scripts/record_release.py", ROOT / "pyproject.toml"]),
    }


def campaign_template(identity):
    """Finite plan with pending coordinator acceptance/release controls."""
    # Sizing recommendations from the accepted reference's known tokens plus
    # retained unknown-send allowances, using the shared nominal pricing helper.
    # These are budget ceilings, not predicted bills or verified cache tariffs.
    recommended_usd = {
        "atria/atria-dawn": {"complete": 2, "collaboration": 2, "honest_stop": 2},
        "olares-qumusphusis/qwen-3.8-27b": {"complete": 1, "collaboration": 1, "honest_stop": 1},
        "qwen-token-plan/deepseek-v4.1-flash": {"complete": 3, "collaboration": 3, "honest_stop": 2},
        "qwen-token-plan/qwen3.8-max": {"complete": 9, "collaboration": 10, "honest_stop": 3},
    }
    cell_limits = {
        "complete": dict(http_attempts=180, input_tokens=4_500_000,
                         output_tokens=120_000, wall_seconds=7200),
        "collaboration": dict(http_attempts=120, input_tokens=3_000_000,
                              output_tokens=120_000, wall_seconds=7200),
        "honest_stop": dict(http_attempts=60, input_tokens=1_000_000,
                            output_tokens=60_000, wall_seconds=7200),
    }
    allocations = {
        model: {**{k: sum(v[k] for v in cell_limits.values()) for k in RESOURCES if k != "estimated_usd"},
                "estimated_usd": sum(recommended_usd[model].values())} for model in MODELS}
    return {
        "version": 2, "campaign_id": "matched-model-demos-20261029",
        "campaign_path": str(ROOT / "runs/checks/matched-model-demos-20261029"),
        "historical_evidence_path": None,
        "launch_authorized": False, "integrated_acceptance": False,
        "learning_enabled": False, "protocol_ready": False,
        "protocol_blockers": ["Independent final-world scoring contract needs integrated acceptance"],
        "profiles": matched_stories(), "seed": 8,
        "request_policy": {"http_max_retries": 2, "min_request_interval_s": 60.0, "max_output_tokens": 4096},
        "identity": identity, "gateway": "https://physics.bytefuture.ai/v1",
        "api": "responses", "stream": False, "web_search": False,
        "limits": {k: sum(v[k] for v in allocations.values()) for k in RESOURCES},
        "models": {m: {"deployment": deployment, "limits": allocations[m],
                        "capabilities": {}, "pricing": nominal_rates("https://physics.bytefuture.ai/v1", m),
                        "roles": dict.fromkeys(ALL_AGENTS, m)} for m, deployment in MODELS.items()},
        "cells": [{"id": f"m{mi + 1}-{story}", "model": model, "story": story,
                   "limits": {**cell_limits[story], "estimated_usd": recommended_usd[model][story]}}
                  for mi, model in enumerate(MODELS) for story in STORIES],
        "astra_reference": "Separate same-simulator baseline; not included or authorized",
        "pricing_contract": "Use shared billing helper evidence; no recorder tariff or cache discount",
        "budget_basis": "Coordinator-pending USD 39 total ceiling; accepted-reference workload projection, "
                        "not actual bills. Cache tariffs and target-model workload remain unknown.",
    }


def validate_campaign(campaign):
    """Reject malformed/expanded matrices and unbounded or duplicated allocations."""
    if campaign.get("version") not in (1, 2) or set(campaign.get("models", {})) != set(MODELS):
        raise ValueError("Exactly the four requested models are required")
    endpoint = urlsplit(campaign.get("gateway", ""))
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or not endpoint.path.rstrip("/").endswith("/v1")):
        raise ValueError("Sanitized HTTPS gateway API root required")
    if set(campaign.get("identity", {})) != set(IDENTITIES):
        raise ValueError("All source/configuration/skill/state identities required")
    if campaign.get("profiles") != matched_stories() or campaign.get("seed") != 8:
        raise ValueError("Frozen agreed stories and simulator seed required")
    if campaign.get("request_policy") != {
            "http_max_retries": 2, "min_request_interval_s": 60.0, "max_output_tokens": 4096}:
        raise ValueError("Frozen HTTP retry, pacing and output controls required")
    cells = campaign.get("cells", [])
    expected = {(m, s) for m in MODELS for s in STORIES}
    if (len(cells) != 12 or {(c.get("model"), c.get("story")) for c in cells} != expected
            or len({c.get("id") for c in cells}) != 12
            or any(not isinstance(c.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", c["id"]) for c in cells)):
        raise ValueError("Exactly twelve distinct requested model/story cells are required")
    if TrialRegistry.staged(campaign) and not any(c["id"] == TrialRegistry.FIRST_CELL and
            c["model"] == "atria/atria-dawn" and c["story"] == "complete" for c in cells):
        raise ValueError("Staged first cell must retain exact Atria robustness identity")
    for key, deployment in MODELS.items():
        entry = campaign["models"][key]
        if entry.get("deployment") != deployment:
            raise ValueError("Exact requested deployment identity changed")
        if entry.get("roles") != dict.fromkeys(ALL_AGENTS, key):
            raise ValueError("Every role must use the selected model")
    scopes = [campaign, *campaign["models"].values(), *cells]
    for scope in scopes:
        limits = scope.get("limits", {})
        if set(limits) != set(RESOURCES) or any(
                not _positive(limits[k], integer=k != "estimated_usd") for k in RESOURCES):
            raise ValueError("Finite positive dollar, request, token and time ceilings required")
    for model, entry in campaign["models"].items():
        for resource in RESOURCES:
            if sum(c["limits"][resource] for c in cells if c["model"] == model) > entry["limits"][resource]:
                raise ValueError("Cell allocations exceed the model ceiling")
    for resource in RESOURCES:
        if sum(m["limits"][resource] for m in campaign["models"].values()) > campaign["limits"][resource]:
            raise ValueError("Model allocations exceed the campaign ceiling")


def qualification_plan(campaign, model):
    """Read-only first-cell qualification contract, not a sender or new trial.

    The coordinator reviews/releases this stage after integrated acceptance.
    Execution uses the normal recorder, admission and ToolGateway.
    """
    validate_campaign(campaign)
    if model not in campaign["models"]:
        raise ValueError("Unknown requested route")
    entry = campaign["models"][model]
    caps = entry.get("capabilities", {})
    first = next(c for c in campaign["cells"] if c["model"] == model and c["story"] == "complete")
    credible = (_sha(caps.get("evidence_sha256")) and caps.get("gateway") == campaign["gateway"]
                and caps.get("model") == model and caps.get("deployment") == entry["deployment"]
                and caps.get("api") == campaign["api"] and bool(caps.get("source")))
    unsupported = credible and (caps.get("vision") is False or caps.get("tools") is False)
    known = all(caps.get(k) is True for k in CAPABILITIES) or bool(observed_qualification(campaign, model))
    limits = {"estimated_usd": min(1.0, first["limits"]["estimated_usd"]), "http_attempts": 2,
              "input_tokens": 131072, "output_tokens": 512, "wall_seconds": 300}
    return {
        "version": 1, "model": model, "deployment": entry["deployment"],
        "gateway": campaign["gateway"], "api": campaign["api"], "charged_cell_id": first["id"],
        "disposition": "unsupported" if unsupported else "not_required" if known else "qualification_pending_acceptance",
        "limits": limits, "fits_first_cell": all(limits[k] <= first["limits"][k] for k in RESOURCES),
        "same_registry_attempt_and_ledger": True, "separate_pilot_or_runner": False,
        "evidence_sha256": caps.get("evidence_sha256"), "access_or_capabilities_invented": False,
        "contract": [
            "After integrated acceptance/freeze, register the first cell normally and debit all qualification sends, time, tokens and unknown reservations against its existing caps.",
            "At most two actual HTTP sends including retries: one deterministic synthetic image and the existing read-only get_project_context tool, then its normal tool-result continuation; no hardware or raw truth.",
            "Require exact route/model/API, valid function IDs/arguments and a checkable visual relation. Preserve refusal, transport errors and unknown usage; no image stripping, fallback model or repeated speculative probes.",
            "On success retain evidence at the model's exact scope and begin the identical story at the normal fresh-conversation boundary, preserving shared budget and request history.",
            "Known credible vision:false/tools:false yields unsupported unstarted cells; absent listing alone yields unknown.",
        ],
        "execution_supported": True,
        "implementation_boundary": "Normal run_cell/record_demo first-attempt stage via --qualify; --qualification-only leaves the same attempt awaiting --resume. Global acceptance/release gates still apply.",
    }


def preflight(campaign, current_identity, *, cell_id=None):
    validate_campaign(campaign)
    blockers = []
    if campaign.get("launch_authorized") is not True:
        blockers.append("Coordinator acceptance/release control is not set after source freeze")
    if campaign.get("integrated_acceptance") is not True:
        blockers.append("Tasks 1-4 integrated acceptance/source freeze missing")
    if campaign.get("learning_enabled") is not False:
        blockers.append("Learning must be disabled with frozen experience and skills")
    if campaign.get("protocol_ready") is not True:
        blockers.extend(campaign.get("protocol_blockers") or ["Protocol acceptance missing"])
    if (campaign.get("web_search") is not False or campaign.get("stream") is not False
            or campaign.get("api") not in ("responses", "chat_completions")):
        blockers.append("Frozen supported transport and disabled hosted search required")
    for key in IDENTITIES:
        pinned = campaign.get("identity", {}).get(key)
        if not _sha(pinned) or pinned != current_identity.get(key):
            blockers.append(f"Missing or changed {key} identity")
    models = {}
    for model, entry in campaign["models"].items():
        missing = []
        caps = entry.get("capabilities", {})
        if (caps.get("gateway") != campaign.get("gateway") or caps.get("model") != model
                or caps.get("deployment") != entry["deployment"] or caps.get("api") != campaign.get("api")
                or not _sha(caps.get("evidence_sha256")) or not caps.get("source")):
            missing.append("Exact gateway/model/deployment/transport capability evidence missing")
        try:
            datetime.fromisoformat(caps.get("verified_at", "").replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            missing.append("Dated capability verification missing")
        for capability in CAPABILITIES:
            if caps.get(capability) is not True:
                state = "unsupported" if caps.get(capability) is False else "unknown"
                missing.append(f"{capability}: {state}")
        if not _positive(caps.get("context_window_tokens"), integer=True):
            missing.append("Request context capacity unknown")
        pricing = entry.get("pricing")
        # Delegate tariff validity, zero quotes and cache semantics to the shared helper.
        projection = request_cost({}, gateway=campaign["gateway"], model=model, rates=pricing)
        if not isinstance(pricing, dict) or projection.get("provenance") != pricing:
            missing.append("Scoped shared pricing evidence missing; zero nominal rates are allowed")
        observed = observed_qualification(campaign, model)
        if observed:
            missing = [m for m in missing if not any(m.startswith(k + ":") for k in CAPABILITIES)]
        models[model] = {"ready": not missing, "blockers": missing,
                         "account_listed": caps.get("account_listed"),
                         "listing_proves_callability": False, "observed_qualification": observed}
    ready = not blockers and all(m["ready"] for m in models.values())
    eligible = [m for m, status in models.items() if not blockers and status["ready"]]
    delivery = TrialRegistry.delivery_status(campaign)
    qualification = {m: qualification_plan(campaign, m) for m in models}
    for model, plan in qualification.items():
        other = [m for m in models[model]["blockers"] if not any(m.startswith(k + ":") for k in CAPABILITIES)]
        plan["execution_eligible"] = (not blockers and not other and plan["fits_first_cell"]
                                      and plan["disposition"] == "qualification_pending_acceptance"
                                      and TrialRegistry.staged(campaign)
                                      and plan["charged_cell_id"] in delivery["authorized_cells"])
    cells = [{"id": c["id"], "model": c["model"],
              "eligible": c["model"] in eligible,
              "status": "unsupported" if qualification[c["model"]]["disposition"] == "unsupported" else
                        "eligible" if c["model"] in eligible else
                        "global_acceptance_pending" if blockers else "qualification_required"}
             for c in campaign["cells"]]
    for cell in cells:
        cell["scientifically_eligible"] = cell["eligible"]
        cell["delivery_authorized"] = cell["id"] in delivery["authorized_cells"]
        cell["eligible"] = cell["eligible"] and cell["delivery_authorized"]
        if not cell["delivery_authorized"] and cell["status"] != "unsupported":
            cell["status"] = delivery["state"]
    eligible = [m for m in eligible if any(c["model"] == m and c["eligible"] for c in cells)]
    selected = None
    if cell_id is not None:
        selected = next((c for c in cells if c["id"] == cell_id), None)
        if selected is None:
            raise ValueError("Unknown requested cell")
    return {"ready": ready and all(c["delivery_authorized"] for c in cells), "blockers": blockers, "models": models,
            "scientific_matrix_ready": ready, "delivery": delivery,
            "whole_matrix_ready": ready and all(c["delivery_authorized"] for c in cells), "eligible_models": eligible, "cells": cells, "selected_cell": selected,
            "legacy_read_only": not TrialRegistry.staged(campaign),
            "qualification": qualification, "paid_execution_available": bool(eligible) and TrialRegistry.staged(campaign), "normal_recorder_adapter_available": True,
            "execution_source_sha256": digest({"identity": current_identity, "campaign": campaign}),
            "cell_count": 12}


def attempt_allowance(campaign, cell_id, attempts, next_usage, *, explicit_retry=False, continuing=False):
    """Pure cumulative check. Amounts come from shared ledger accounting, not tariffs.

    This check creates no attempt, reservation or laboratory state. Its inputs
    must be supplied by the existing recorder/ledger at eventual integration.
    """
    validate_campaign(campaign)
    cells = {c["id"]: c for c in campaign["cells"]}
    if cell_id not in cells:
        raise ValueError("Unknown requested cell")
    cell = cells[cell_id]
    totals = {scope: dict.fromkeys(RESOURCES, 0) for scope in ("campaign", "model", "cell")}
    seen, previous, histories = set(), [], {}
    for attempt in attempts:
        aid = (attempt.get("cell_id"), attempt.get("number"))
        if (aid in seen or aid[0] not in cells or not _positive(aid[1], integer=True)
                or attempt.get("identity") != campaign["identity"]
                or attempt.get("campaign_sha256") != digest(campaign)):
            raise ValueError("Duplicate, unknown or changed attempt identity")
        seen.add(aid)
        histories.setdefault(aid[0], []).append(attempt)
        entries, used = http_accounting(attempt.get("events", []))
        used["estimated_usd"] = attempt.get("ledger_reserved_and_settled_usd")
        used["wall_seconds"] = attempt.get("wall_seconds")
        if any(not _positive(used[k], integer=k not in ("estimated_usd", "wall_seconds"), zero=True) for k in RESOURCES):
            raise ValueError("Complete shared-ledger and elapsed-time accounting required")
        # Unknown provider usage counts its original allowance through http_accounting.
        for entry in entries:
            request = entry["request"]
            if (request is None or request.get("model") != cells[aid[0]]["model"]
                    or request.get("gateway") != campaign["gateway"] or request.get("api") != campaign["api"]):
                raise ValueError("Attempt request model/gateway/API or admission evidence missing")
            if not entry["usage_known"] and not entry["failure"]:
                raise ValueError("Unresolved send requires reconciliation before replacement")
        if entries and not request_identity_valid(attempt["events"], cells[aid[0]]["model"]):
            raise ValueError("Attempt request/usage/failure identity changed")
        unknown_reservations = [e["admission"].get("reservation_id") for e in entries if not e["usage_known"]]
        retained = attempt.get("retained_unknown_reservation_ids", [])
        if (any(not isinstance(r, str) or not r for r in unknown_reservations)
                or not isinstance(retained, list) or sorted(unknown_reservations) != sorted(retained)):
            raise ValueError("Shared ledger evidence must retain every unknown send reservation")
        for scope, applies in (("campaign", True),
                               ("model", cells[aid[0]]["model"] == cell["model"]),
                               ("cell", aid[0] == cell_id)):
            if applies:
                for resource in RESOURCES:
                    totals[scope][resource] += used[resource]
        if aid[0] == cell_id:
            previous.append(attempt)
    for history in histories.values():
        history.sort(key=lambda a: a["number"])
        if ([a["number"] for a in history] != list(range(1, len(history) + 1))
                or any(a.get("status") != "failed" for a in history[:-1])):
            raise ValueError("Every contributing cell must retain its complete attempt sequence")
    if previous:
        ended = previous[:-1] if continuing else previous
        if (any(a.get("status") != "failed" for a in ended)
                or continuing and previous[-1].get("status") not in ("started", "interrupted")
                or not continuing and not explicit_retry):
            raise ValueError("Only explicitly authorized failed replacements; no active/successful restart")
    if set(next_usage) != set(RESOURCES) or any(
            not _positive(next_usage[k], integer=k not in ("estimated_usd", "wall_seconds"), zero=True) for k in RESOURCES):
        raise ValueError("Bounded next reservation/request/token/time allowance required")
    scopes = {"campaign": campaign, "model": campaign["models"][cell["model"]], "cell": cell}
    for scope, spec in scopes.items():
        for resource in RESOURCES:
            if totals[scope][resource] + next_usage[resource] > spec["limits"][resource]:
                raise ValueError(f"Cumulative {scope} {resource} ceiling exceeded")
    return {"attempt_number": len(previous) if continuing else len(previous) + 1, "used": totals,
            "provider_unknowns_preserved": True, "creates_attempt": False}


def request_identity_valid(events, model):
    """Match recorded identities; a missing historical gateway is never filled."""
    requests = [e for e in events if e["kind"] == "agent.request"]
    usage = [e for e in events if e["kind"] == "agent.request_usage"]
    failures = [e for e in events if e["kind"] == "agent.request_failure"]
    by_request = {e["payload"].get("request_id"): e for e in requests}
    identity_ok = bool(requests) and len(by_request) == len(requests) and all(
        e["payload"].get("model") == model and e.get("agent") in ALL_AGENTS
        and all(isinstance(e["payload"].get(k), str) and e["payload"][k]
                for k in ("request_id", "session_id", "call_id", "gateway"))
        and e["payload"].get("api") in ("responses", "chat_completions")
        and _positive(e["payload"].get("attempt"), integer=True) for e in requests)
    for event in [*usage, *failures]:
        request = by_request.get(event["payload"].get("request_id"))
        identity_ok = identity_ok and bool(request) and request.get("agent") == event.get("agent") and all(
            event["payload"].get(k) == request["payload"].get(k)
            for k in ("model", "session_id", "call_id", "attempt"))
        if request and (event["kind"] == "agent.request_usage" or "gateway" in event["payload"]):
            identity_ok = identity_ok and event["payload"].get("gateway") == request["payload"].get("gateway")
        if request:
            identity_ok = identity_ok and all(event["payload"].get(k) == request["payload"].get(k)
                                             for k in ("api", "provider"))
    identity_ok = identity_ok and len({e["payload"].get("request_id") for e in usage}) == len(usage)
    return identity_ok


class CampaignAdapter:
    """Bind one requested cell to the normal engine and its existing registry.

    truth_reader defaults to the approved private evaluation_snapshot RPC.
    Injection supports offline tests; no call uses lab_state or advances physics.
    """

    def __init__(self, campaign, model, *, initial_state, experience, truth_reader=None, now=time.time, qualification=False):
        self.campaign = copy.deepcopy(campaign)
        validate_campaign(self.campaign)
        truth_reader = truth_reader or (lambda services: services.lab.world.evaluation_snapshot())
        if model not in MODELS or not callable(truth_reader):
            raise ValueError("Requested model and approved pure truth reader required")
        self.model, self.truth_reader, self.now = model, truth_reader, now
        self.qualifying = qualification
        self.qualification_phase = None
        self.initial_state, self.experience = Path(initial_state), Path(experience)
        self.historical_evidence = (Path(campaign["historical_evidence_path"])
                                    if campaign.get("historical_evidence_path") else None)
        self.expected_source = digest({"identity": self.campaign["identity"], "campaign": self.campaign})
        self.limits_by_story = {
            c["story"]: {**c["limits"],
                         "http_max_retries": self.campaign["request_policy"]["http_max_retries"],
                         "min_request_interval_s": self.campaign["request_policy"]["min_request_interval_s"]}
            for c in self.campaign["cells"] if c["model"] == model}

    def source(self):
        identity = source_identity(initial_state=self.initial_state, experience=self.experience,
                                   historical_evidence=self.historical_evidence)
        return digest({"identity": identity, "campaign": self.campaign})

    def check_ready(self):
        result = preflight(self.campaign, source_identity(initial_state=self.initial_state, experience=self.experience,
                                                         historical_evidence=self.historical_evidence))
        missing = result["models"][self.model]["blockers"]
        if self.qualifying:
            plan = qualification_plan(self.campaign, self.model)
            if plan["disposition"] == "unsupported":
                raise ValueError("Exact route has credible unsupported image/tool metadata")
            missing = [m for m in missing if not any(m.startswith(k + ":") for k in CAPABILITIES)]
        if result["blockers"] or missing:
            raise ValueError("Campaign is not released/ready: " + "; ".join(
                result["blockers"] + missing))

    def guard(self, registry, manifest, *, next_usage=None, request_payload=None, runtime=None):
        """Read the same registry/RunStore/ledger at start, resume and every send."""
        from qumus2.core.run_paths import original_run_path

        self.check_ready()
        TrialRegistry.require_delivery(self.campaign, manifest["cell_id"])
        if hasattr(self, "services"):
            from qumus2.records.experience import ExperienceBook

            if ExperienceBook(self.services.store).summary() != json.loads(self.experience.read_text()):
                raise ValueError("Active experience changed during the frozen comparison")
        if (manifest.get("campaign_sha256") != digest(self.campaign)
                or manifest.get("model") != self.model or manifest.get("source") != self.expected_source
                or self.source() != self.expected_source):
            raise ValueError("Frozen campaign/source/model identity changed")
        runtime_contract = manifest.get("runtime_contract") or {}
        pricing = self.campaign["models"][self.model]["pricing"]
        expected_runtime = {
            "base_url": self.campaign["gateway"], "api": self.campaign["api"], "model_id": self.model,
            "input_price": pricing["input_price"], "output_price": pricing["output_price"],
            "cached_input_price": pricing.get("cached_input_price"), "stream": self.campaign["stream"],
            "context_window_tokens": self.campaign["models"][self.model]["capabilities"]["context_window_tokens"],
        }
        if any(k not in runtime_contract or runtime_contract[k] != value for k, value in expected_runtime.items()):
            raise ValueError("Actual runtime routing, prices, buffering or context differs from the campaign")
        with registry.connect() as db:
            rows = db.execute("SELECT manifest,status,result FROM attempt ORDER BY story,number").fetchall()
        records = [(json.loads(raw), status, json.loads(result) if result else {})
                   for raw, status, result in rows]
        if not any(m["workspace"] == manifest["workspace"] for m, _, _ in records):
            records.append((manifest, "started", {}))
        attempts = []
        for saved, status, result in records:
            if saved.get("campaign_sha256") != digest(self.campaign) or saved.get("ledger") != manifest["ledger"]:
                raise ValueError("Registry contains another campaign or shared-ledger identity")
            current = saved["workspace"] == manifest["workspace"]
            path = Path(saved["workspace"]) / "qumus.db"
            events = []
            if path.exists():
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                    events = [{"id": i, "kind": k, "agent": a, "payload": json.loads(p)}
                              for i, k, a, p in db.execute("SELECT id,kind,agent,payload_json FROM event ORDER BY id")]
            elif not current and not (path.parent / "startup-failure.json").is_file():
                raise ValueError("Original attempt evidence missing")
            with sqlite3.connect(Path(saved["ledger"]).as_uri() + "?mode=ro", uri=True) as db:
                has_table = db.execute("SELECT 1 FROM sqlite_master WHERE name='spend'").fetchone()
                spend = db.execute("SELECT id,reserved,actual FROM spend WHERE scope=?",
                                   (str(original_run_path(path)),)).fetchall() if has_table else []
            entries, _ = http_accounting(events)
            admitted = {e["admission"].get("reservation_id") for e in entries}
            extra = [r for r in spend if r[0] not in admitted and r[2] != 0]
            if extra and not (current and next_usage is not None and len(extra) == 1 and extra[0][2] is None):
                raise ValueError("Ledger reservation lacks matching HTTP admission")
            unknown = {e["admission"].get("reservation_id") for e in entries if not e["usage_known"]}
            retained = [i for i, _, actual in spend if actual is None and i in unknown]
            elapsed = self.now() - saved["started_wall"] if status in ("started", "interrupted") else result.get("elapsed_wall_s")
            attempts.append({"cell_id": saved["cell_id"], "number": saved["attempt_number"], "status": status,
                             "identity": saved["campaign_identity"], "campaign_sha256": saved["campaign_sha256"],
                             "events": events, "ledger_reserved_and_settled_usd": sum(r if a is None else a for _, r, a in spend),
                             "retained_unknown_reservation_ids": retained, "wall_seconds": elapsed})
        admission = attempt_allowance(self.campaign, manifest["cell_id"], attempts,
                                       next_usage or dict.fromkeys(RESOURCES, 0), continuing=True)
        if self.qualifying and next_usage:
            if self.qualification_phase is None or runtime is None or request_payload is None:
                raise ValueError("Qualification must be initialized before transport admission")
            phase = self.qualification_phase
            if self.now() - phase["started_wall"] >= 300:
                raise ValueError("Qualification wall limit reached")
            current_events = [e.model_dump() for e in runtime.store.events_after(phase["after_event_id"], limit=1_000_000)]
            _, used = http_accounting(current_events)
            for key in ("http_attempts", "input_tokens", "output_tokens"):
                if used[key] + next_usage[key] > phase["limits"][key]:
                    raise ValueError("Qualification cumulative " + key + " limit reached")
            spent = runtime._ledger.total(runtime._budget_scope) - phase["ledger_before"]
            if spent + next_usage["estimated_usd"] > phase["limits"]["estimated_usd"]:
                raise ValueError("Qualification dollar limit reached")
            tools = request_payload.get("tools", [])
            if (runtime._request_context.get("agent") != "orchestrator" or next_usage["output_tokens"] > 256
                    or len(tools) != 1 or tools[0].get("name", tools[0].get("function", {}).get("name")) != "get_project_context"):
                raise ValueError("Qualification permits only the read-only tool and bounded orchestrator response")
            if used["http_attempts"] == 0 and phase["image_uri"] not in json.dumps(request_payload):
                raise ValueError("Qualification image was omitted or substituted")
        return admission

    def check_services(self, services, manifest, continuing):
        self.check_ready()
        from qumus2.records.experience import ExperienceBook

        actual_experience = ExperienceBook(services.store).summary()
        if actual_experience != json.loads(self.experience.read_text()):
            raise ValueError("Active experience differs from the frozen library")
        self.initialize_priors(services.store, continuing)
        if continuing:
            if services.store.get_meta("demo.initial_truth_sha256") != digest(json.loads(self.initial_state.read_text())):
                raise ValueError("Saved initial truth identity missing or changed")
        else:
            truth = self.truth_reader(services)
            if truth != json.loads(self.initial_state.read_text()):
                raise ValueError("Initial simulated truth differs from the frozen state/seed")
            services.store.set_meta("demo.initial_truth_sha256", digest(truth))
        services.store.set_meta("demo.experience_sha256", digest(actual_experience))
        self.services = services
        from qumus2.agents.policy import Decision

        gateway = services.gateway
        original = gateway.pre
        frozen_tools = {"teach_experience", "retire_experience", "propose_experience", "vote_experience", "review_runs"}

        def pre(agent, tool, args, tool_use_id=None):
            spec = gateway.registry.get(tool)
            if spec and spec.name in frozen_tools:
                services.store.emit("tool.denied", {"tool": tool, "rule": "demo_learning_frozen"}, agent=agent)
                return Decision(False, "Experience and skills are frozen during matched evaluation", "demo_learning_frozen")
            return original(agent, tool, args, tool_use_id)
        gateway.pre = pre

    def initialize_priors(self, store, continuing):
        """Use the one typed RunStore evidence API; never apply facts to devices."""
        from qumus2.records.lab_geometry import LabEvidence, record_evidence

        expected = self.campaign["identity"]["historical_evidence"]
        assertions = []
        if self.historical_evidence:
            raw = self.historical_evidence.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("Historical evidence packet changed")
            packet = json.loads(raw)
            if packet.get("destination_bench") != store.bench or not isinstance(packet.get("facts"), list):
                raise ValueError("Historical evidence must target this bench with typed facts")
            assertions = [LabEvidence.model_validate(fact) for fact in packet["facts"]]
        if continuing:
            if store.get_meta("demo.historical_evidence_sha256") != expected:
                raise ValueError("Saved historical-prior identity missing or changed")
            events = [e.payload for e in store.events_after(0, limit=1_000_000) if e.kind == "lab.evidence"]
            for assertion in assertions:
                matches = [e for e in events if e.get("assertion_id") == assertion.assertion_id]
                if len(matches) != 1 or matches[0] != assertion.model_dump(mode="json"):
                    raise ValueError("Saved historical assertion is missing or changed")
            return
        with store._transaction():
            for assertion in assertions:
                record_evidence(store, assertion)
            store.set_meta("demo.historical_evidence_sha256", expected)

    async def qualify(self, services, manifest):
        """Bounded SDK phase in this first-cell attempt, before any story work."""
        from dataclasses import replace

        from qumus2.agents.policy import Decision
        from qumus2.records.experience import ExperienceBook
        from scripts.record_three_demos import (
            TrialRegistry,
            configure_demo_pacing,
            install_retry_observer,
        )

        if (manifest["story"] != "complete" or manifest.get("attempt_number") != 1 or not self.qualifying
                or any(e.kind == "demo.qualification_started" for e in services.store.events_after(0, limit=1_000_000))):
            raise ValueError("Qualification belongs to the selected model's first complete cell")
        if qualification_plan(self.campaign, self.model)["disposition"] != "qualification_pending_acceptance":
            raise ValueError("Qualification is only for an unknown route; no repeated or unsupported probes")
        self.check_ready()
        rt = services.runtime
        rt._ensure()
        image = qualification_image()
        image_uri = "data:image/png;base64," + base64.b64encode(image).decode()
        prior = TrialRegistry._totals(manifest.get("prior_attempts", []))
        ledger_before = rt._ledger.total(rt._budget_scope)
        remaining = manifest["limits"]["estimated_usd"] - prior["estimated_usd"] - ledger_before
        if remaining <= 0:
            raise ValueError("First cell has no remaining qualification allowance")
        limits = {**qualification_plan(self.campaign, self.model)["limits"], "estimated_usd": min(1.0, remaining)}
        physical = self.truth_reader(services)
        experience = ExperienceBook(services.store).summary()
        samples, runs = services.store.list_samples(), services.store.list_runs()
        project = services.store.get_project(services.project_id) or {}
        controller = Path(manifest["workspace"]) / "qumus.testbench.pkl"
        controller_hash = hashlib.sha256(controller.read_bytes()).hexdigest() if controller.is_file() else None
        start = services.store.emit("demo.qualification_started", {
            "model": self.model, "source": manifest["source"], "limits": limits,
            "image_sha256": hashlib.sha256(image).hexdigest(), "cell_id": manifest["cell_id"]})
        self.qualification_phase = {"after_event_id": start, "started_wall": self.now(),
                                    "ledger_before": ledger_before, "limits": limits, "image_uri": image_uri}
        agent = rt._agents["orchestrator"]
        original_settings = agent.model_settings
        original_max = rt.cfg["openai"].get("max_output_tokens", 4096)
        original_pre = services.gateway.pre
        agent.instructions = "You are checking this exact route's image and read-only tool protocol. Follow the supplied bounded task."
        agent.tools = [t for t in agent.tools if t.name == "get_project_context"]
        agent.model_settings = replace(original_settings, max_tokens=256)
        rt.cfg["openai"]["max_output_tokens"] = 256

        def readonly_pre(role, tool, args, tool_use_id=None):
            spec = services.gateway.registry.get(tool)
            if role != "orchestrator" or not spec or spec.name != "get_project_context":
                services.store.emit("tool.denied", {"tool": tool, "rule": "demo_qualification_readonly"}, agent=role)
                return Decision(False, "Qualification permits only get_project_context", "demo_qualification_readonly")
            return original_pre(role, tool, args, tool_use_id)
        services.gateway.pre = readonly_pre
        exception = None
        try:
            await asyncio.wait_for(rt.send_input([{"role": "user", "content": [
                {"type": "input_text", "text": QUALIFICATION_PROMPT},
                {"type": "input_image", "image_url": image_uri}]}]), timeout=300)
        except Exception as exc:
            exception = type(exc).__name__
        finally:
            rt.cfg["openai"]["max_output_tokens"] = original_max
            services.gateway.pre = original_pre
        events = [e.model_dump() for e in services.store.events_after(start, limit=1_000_000)]
        http = http_evidence(events)
        entries, used = http_accounting(events)
        calls = [e for e in events if e["kind"] == "tool.call"]
        results = [e for e in events if e["kind"] == "tool.result"]
        try:
            answer = json.loads(str(rt.last_result.final_output))
        except (ValueError, AttributeError, TypeError):
            answer = {}
        now_project = services.store.get_project(services.project_id) or {}
        state_before = {k: v for k, v in project.get("state", {}).items() if k != "agents_last"}
        state_after = {k: v for k, v in now_project.get("state", {}).items() if k != "agents_last"}
        try:
            source_unchanged = self.source() == manifest["source"]
        except Exception as exc:
            source_unchanged = False
            exception = exception or type(exc).__name__
        checks = {
            "two_http_sends": len(entries) == 2,
            "provider_usage_complete": http["provider_usage_complete"],
            "request_identity": request_identity_valid(events, self.model),
            "no_http_failure": not any(e["kind"] == "agent.request_failure" for e in events),
            "readonly_tool_continuation": len(calls) == len(results) == 1 and
                calls[0]["payload"].get("tool") == results[0]["payload"].get("tool") == "get_project_context" and
                bool(calls[0]["payload"].get("tool_use_id")) and
                calls[0]["payload"].get("tool_use_id") == results[0]["payload"].get("tool_use_id") and
                results[0]["payload"].get("status") == "ok",
            "visual_relation": answer.get("top_colors") == ["orange", "blue", "green"] and answer.get("lower_left_shape") == "triangle",
            "project_from_tool": answer.get("project_id") == services.project_id,
            "physical_state_unchanged": self.truth_reader(services) == physical,
            "controller_checkpoint_unchanged": not controller_hash or hashlib.sha256(controller.read_bytes()).hexdigest() == controller_hash,
            "experience_unchanged": ExperienceBook(services.store).summary() == experience,
            "samples_and_jobs_unchanged": services.store.list_samples() == samples and services.store.list_runs() == runs,
            "project_unchanged": now_project.get("plan") == project.get("plan") and state_before == state_after,
            "bounded_usage": all(used[k] <= limits[k] for k in ("http_attempts", "input_tokens", "output_tokens")),
            "bounded_cost": rt._ledger.total(rt._budget_scope) - ledger_before <= limits["estimated_usd"],
            "bounded_time": self.now() - self.qualification_phase["started_wall"] <= 300,
            "source_unchanged": source_unchanged,
            "no_exception": exception is None,
        }
        result = {"version": 1, "passed": all(checks.values()), "checks": checks, "model": self.model,
                  "gateway": self.campaign["gateway"], "api": self.campaign["api"],
                  "deployment": self.campaign["models"][self.model]["deployment"],
                  "campaign_sha256": manifest["campaign_sha256"], "source": manifest["source"],
                  "after_event_id": start, "through_event_id": services.store.last_event_id(),
                  "event_prefix_sha256": digest(events), "image_sha256": hashlib.sha256(image).hexdigest(),
                  "usage": http, "charged_usage": used, "exception_type": exception,
                  "limits": limits, "ledger_charge_usd": rt._ledger.total(rt._budget_scope) - ledger_before,
                  "cost_basis": "same-ledger settled estimates and retained unknown reservations; not a provider invoice",
                  "elapsed_wall_s": self.now() - self.qualification_phase["started_wall"],
                  "observed_only": True, "backend_weights_attested": False}
        services.store.emit("demo.qualification_result", result)
        services.store.set_meta("demo.qualification", json.dumps(result))
        self.qualifying = False
        if not result["passed"]:
            raise ValueError("Qualification did not pass; failed/unknown evidence and reservations retained")
        await rt.select_model(self.model, rt.creds.input_price, rt.creds.output_price,
                              context_window_tokens=rt.creds.context_window_tokens, cached_input_price=rt.creds.cached_input_price)
        services.store.update_project_state(services.project_id, agents_last=project.get("state", {}).get("agents_last", {}))
        configure_demo_pacing(rt)
        install_retry_observer(rt)
        return result

    def collect_evidence(self, services, manifest):
        from qumus2.records.experience import ExperienceBook
        from qumus2.testbench.demo_scoring import score_terminal_truth
        from scripts.record_three_demos import recorded_v1_process_evidence

        before = services.store.last_event_id()
        runs = services.store.list_runs()
        if any(r["status"] in ("running", "queued") for r in runs):
            raise ValueError("Laboratory jobs must be quiescent before terminal truth capture")
        truth = self.truth_reader(services)
        samples = services.store.list_samples()
        events = [e.model_dump() for e in services.store.events_after(0, limit=1_000_000)]
        if services.store.last_event_id() != before:
            raise ValueError("Event prefix changed during terminal truth capture")
        score = score_terminal_truth(truth, samples, services.lab.bench, story=manifest["story"], events=events, runs=runs)
        process, process_binding = recorded_v1_process_evidence(services, events, manifest["story"], manifest["source"])
        experience = ExperienceBook(services.store).summary()
        packet = {"version": 1, "execution_source_sha256": manifest["source"],
                  "model": manifest["model"], "seed": manifest["seed"],
                  "campaign_sha256": manifest["campaign_sha256"], "cell_id": manifest["cell_id"],
                  "campaign_identity": manifest["campaign_identity"], "profile_sha256": manifest["profile_sha256"],
                  "event_count": len(events), "last_event_id": before, "original_event_prefix_sha256": digest(events),
                  "clock": {"store_clock_now": services.store.clock.now(),
                            "last_integrated_at": truth.get("transfer_dynamics", {}).get("updated_at")},
                  "truth": truth, "samples": samples, "bench": services.lab.bench, "runs": runs}
        path = Path(manifest["workspace"]) / f"terminal-truth-through-{before}.json"
        with path.open("x") as handle:
            json.dump(packet, handle, indent=2, allow_nan=False)
            handle.write("\n")
        checks = {"independent_truth_available": True,
                  "learning_library_unchanged": experience == json.loads(self.experience.read_text()),
                           "independent_terminal_truth_consistent": bool(score["checks"]) and all(score["checks"].values())}
        if manifest["story"] == "complete":
            checks["recorded_v1_process_consistent"] = process["consistent"] is True
        if manifest["story"] == "honest_stop":
            diagnostic = score_export({"events": events, "runs": {r["id"]: r for r in runs}},
                                      "honest_stop", model=self.model, profiles=self.campaign["profiles"])
            checks["three_recoverable_faults_observed_before_human_stop"] = diagnostic["recoverable_chain_observed"]
        return {"checks": checks,
                "evidence": {"terminal_truth_file": path.name,
                             "recorded_v1_process": process, "v1_process_binding": process_binding,
                             "terminal_truth_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "terminal_truth_event_count": len(events), "terminal_truth_last_event_id": before,
                             "independent_truth_score": score}}


async def run_cell(campaign, cell_id, *, initial_state, experience, truth_reader=None, resume=False, retry_failed=False,
                   qualification=False, qualification_only=False):
    """Single-cell adapter. No loops, alternate runner, role fallback or new ledger."""
    from dataclasses import replace

    from qumus2.agents.openai_credentials import load_openai_credentials
    from scripts.record_three_demos import CAMPAIGN, ROOT, record_demo

    validate_campaign(campaign)
    if not TrialRegistry.staged(campaign):
        raise ValueError("Legacy matched manifests are read-only; a staged version2 freeze is required")
    cell = next((c for c in campaign["cells"] if c["id"] == cell_id), None)
    if cell is None:
        raise ValueError("Unknown requested cell")
    adapter = CampaignAdapter(campaign, cell["model"], initial_state=initial_state,
                              experience=experience,
                              truth_reader=truth_reader or (lambda services: services.lab.world.evaluation_snapshot()),
                              qualification=qualification)
    if qualification and (cell["story"] != "complete" or resume or retry_failed):
        raise ValueError("Qualification starts in a fresh first complete-cell attempt; resume its saved fresh story normally")
    if qualification_only and not qualification:
        raise ValueError("Qualification-only requires explicit qualification")
    TrialRegistry.require_delivery(campaign, cell_id)  # Before credentials/workspace/transport.
    readiness = preflight(campaign, source_identity(initial_state=initial_state, experience=experience,
                                                   historical_evidence=adapter.historical_evidence))
    plan = readiness["qualification"][cell["model"]]
    if not readiness["blockers"] and plan["disposition"] == "unsupported" and cell_id == TrialRegistry.FIRST_CELL:
        delivery_registry(campaign).unsupported_first({"capabilities": campaign["models"][cell["model"]]["capabilities"],
                                                       "disposition": "unsupported_unstarted", "http_attempts": 0})
        raise ValueError("Unsupported first route remains unstarted; await_owner_review")
    adapter.check_ready()
    if qualification and qualification_plan(campaign, cell["model"])["disposition"] != "qualification_pending_acceptance":
        raise ValueError("Route is already qualified; start its normal story without another qualification")
    path = Path(campaign["campaign_path"]).resolve()
    if path == CAMPAIGN.resolve() or path.is_relative_to(CAMPAIGN.resolve()):
        raise ValueError("The original Astra campaign is immutable")
    creds = load_openai_credentials(ROOT / ".env")
    if creds is None or creds.base_url.rstrip("/") != campaign["gateway"] or creds.api != campaign["api"]:
        raise ValueError("Configured gateway/API credentials must match the frozen campaign")
    pricing, caps = campaign["models"][cell["model"]]["pricing"], campaign["models"][cell["model"]]["capabilities"]
    creds = replace(creds, model_id=cell["model"], input_price=pricing["input_price"],
                    output_price=pricing["output_price"], cached_input_price=pricing.get("cached_input_price"),
                    context_window_tokens=caps["context_window_tokens"], web_search=False,
                    stream=campaign["stream"],
                    test_budget_usd=cell["limits"]["estimated_usd"])
    return await record_demo(cell["story"], adapter.expected_source, resume=resume, retry_failed=retry_failed,
                             model=cell["model"], campaign_path=path, profiles=campaign["profiles"],
                             limits_by_story=adapter.limits_by_story, source_reader=adapter.source,
                             credentials=creds, campaign_contract=campaign, campaign_guard=adapter.guard,
                             services_check=adapter.check_services, evidence_reader=adapter.collect_evidence,
                             pre_story=adapter.qualify if qualification else None, qualification_only=qualification_only)


def delivery_registry(campaign):
    """Control metadata in the existing registry; never a runtime or spend store."""
    if not TrialRegistry.staged(campaign):
        raise ValueError("Staged version2 campaign required")
    return TrialRegistry(campaign["campaign_path"], model="atria/atria-dawn", profiles=campaign["profiles"],
                         campaign_contract=campaign)


def score_export(recording, story, *, model, expected_source=None, profiles=None,
                 truth_packet=None, truth_file_sha256=None, binding=None, recording_file_sha256=None):
    """Read-only source-scoped evidence checks; no model-success or truth proxy."""
    from qumus2.testbench.demo_scoring import audit_recorded_v1_process

    if story not in STORIES:
        raise ValueError("Unknown story")
    events = recording.get("events", [])
    requests = [e for e in events if e["kind"] == "agent.request"]
    failures = [e["id"] for e in events if e["kind"] == "agent.request_failure"]
    usage = [e for e in events if e["kind"] == "agent.request_usage"]
    identity_ok = request_identity_valid(events, model)
    profiles = STORIES if profiles is None else profiles
    recovery = recoverable_search_evidence(events, recording.get("runs", {}), story, profiles)
    stage_events = [e for e in events if e["kind"] == "experiment.stages_verified"]
    cold, cooling_scope = None, None
    if stage_events:
        payload = stage_events[-1]["payload"]
        runs = recording.get("runs", {})
        cooling_job = runs.get(payload.get("cooling_job_id"), {})
        projects = {r.get("project_id") for r in runs.values()}
        cooling_scope = (bool(cooling_job) and cooling_job.get("flow") == "cool_experiment_stages"
                         and cooling_job.get("status") == "succeeded" and len(projects) == 1
                         and payload.get("project_id") is not None
                         and cooling_job.get("project_id") == payload["project_id"])
        if cooling_scope:
            readings = payload.get("readings", {})
            cold = all((r := readings.get(device, {})) and
                       type(r.get("temperature_c")) in (int, float)
                       and math.isfinite(r["temperature_c"]) and r["temperature_c"] < 40
                       and r.get("target_c", "missing") is None and r.get("fan_commanded_on") is False
                       for device in ("exfoliation_heater", "stamp_heater"))
    source = recording.get("meta", {}).get("execution_source_sha256")
    process_binding = recording.get("meta", {}).get("acceptance_evidence", {}).get("v1_process_binding")
    process = audit_recorded_v1_process(events, recording.get("runs", {}), story=story,
                                      binding=process_binding, execution_source=source)
    http = http_evidence(events)
    if not identity_ok:
        http.update(provider_usage_complete=False, failed_http_recoveries_verified=False)
    truth_score, truth_binding, truth_passed = None, None, None
    if truth_packet is not None:
        from qumus2.testbench.demo_scoring import score_terminal_truth

        meta = recording.get("meta", {})
        evidence = meta.get("acceptance_evidence", {})
        binding = binding or {}
        truth_binding = {
            "file_sha256": _sha(truth_file_sha256) and truth_file_sha256 == evidence.get("terminal_truth_file_sha256"),
            "source": bool(expected_source) and source == expected_source == truth_packet.get("execution_source_sha256"),
            "model": truth_packet.get("model") == model,
            "campaign": bool(meta.get("campaign_sha256")) and meta["campaign_sha256"] == truth_packet.get("campaign_sha256"),
            "cell": bool(meta.get("cell_id")) and meta["cell_id"] == truth_packet.get("cell_id"),
            "profile": truth_packet.get("profile_sha256") == digest(profiles[story]),
            "prefix": bool(events) and truth_packet.get("event_count") == len(events)
                      and truth_packet.get("last_event_id") == max(e["id"] for e in events),
            "clock": _positive(truth_packet.get("clock", {}).get("store_clock_now"), zero=True),
            "original_prefix": (_sha(truth_packet.get("original_event_prefix_sha256")) and
                                truth_packet["original_event_prefix_sha256"] == binding.get("original_event_prefix_sha256")),
            "export_events": digest(events) == binding.get("export_events_sha256"),
            "export_file": _sha(recording_file_sha256) and recording_file_sha256 == binding.get("recording_file_sha256"),
            "binding_scope": (binding.get("execution_source_sha256") == source
                              and binding.get("campaign_sha256") == truth_packet.get("campaign_sha256")
                              and binding.get("cell_id") == truth_packet.get("cell_id")
                              and binding.get("truth_file_sha256") == truth_file_sha256),
        }
        truth_score = score_terminal_truth(truth_packet.get("truth"), truth_packet.get("samples", []),
                                          truth_packet.get("bench", {}), story=story, events=events,
                                          runs=truth_packet.get("runs", []))
        truth_passed = (all(truth_binding.values()) and bool(truth_score["checks"])
                        and all(truth_score["checks"].values()))
    return {
        "story": story, "model": model, "export_events_sha256": digest(events),
        "recorded_source": source,
        "source_matches": source == expected_source if expected_source else None,
        "request_role_model_identity_valid": identity_ok, "http": http,
        "cache_usage": cache_usage_summary(events),
        "request_pricing": [{"request_id": e["payload"].get("request_id"),
                             **request_cost(e["payload"], gateway=e["payload"].get("gateway", ""),
                                            model=e["payload"].get("model", ""))} for e in usage],
        "requests": len(requests), "usage_rows": len(usage), "failure_event_ids": failures,
        "unexercised_roles": sorted(set(ALL_AGENTS) - {e.get("agent") for e in requests}),
        "actions": recovery["actions"], "recoverable_chain_observed": recovery["verified"],
        "recoverable_search": recovery,
        "recorded_v1_process": process,
        "fault_consumption_independently_verified": None,
        "logged_safety_violations": [e["id"] for e in events if e["kind"] == "safety.violation"],
        "latest_recorded_stages_below_40": cold,
        "latest_cooling_job_scope_valid": cooling_scope,
        "recorded_acceptance": recording.get("meta", {}).get("acceptance_checks", {}),
        "independent_world_truth": truth_passed, "terminal_truth_binding": truth_binding,
        "independent_truth_score": truth_score, "narrative_review": "required",
        "qualified_for_new_campaign": False,
        "limitations": ["Saved acceptance flags are claims separate from recomputed evidence",
                        "Historical exports without a bound private snapshot lack per-flake world truth",
                        "Injection receipts alone do not prove fault consumption or physical boundary satisfaction",
                        "Latest cold readings do not prove no later heating or terminal physical safety"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    inspect = sub.add_parser("inspect", help="Print an unfunded template; no network or model calls")
    inspect.add_argument("--initial-state")
    inspect.add_argument("--experience")
    inspect.add_argument("--historical-evidence", help="Explicit private historical-prior packet; default is absent")
    check = sub.add_parser("preflight", help="Read an explicit campaign manifest and compare local hashes")
    check.add_argument("manifest")
    check.add_argument("--initial-state")
    check.add_argument("--experience")
    check.add_argument("--historical-evidence")
    check.add_argument("--cell", help="Report selected-cell eligibility separately from whole-matrix completeness")
    score = sub.add_parser("score", help="Review immutable export evidence without changing it")
    score.add_argument("recording")
    score.add_argument("--story", choices=STORIES, required=True)
    score.add_argument("--model", required=True)
    score.add_argument("--source")
    score.add_argument("--truth", help="Private terminal truth JSON saved by the normal recorder")
    score.add_argument("--binding", help="Private source/event/export binding receipt saved after export")
    score.add_argument("--matched", action="store_true", help="Use the agreed four-fault honest-stop protocol")
    run = sub.add_parser("run", help="Explicitly released single-cell execution through the normal recorder")
    run.add_argument("manifest")
    run.add_argument("--cell", required=True)
    run.add_argument("--initial-state", required=True)
    run.add_argument("--experience", required=True)
    run.add_argument("--freeze", required=True, help="Exact source hash from accepted preflight")
    run.add_argument("--live", action="store_true", required=True, help="Requires integrated acceptance and coordinator release")
    run.add_argument("--qualify", action="store_true", help="Bounded qualification in this complete-cell attempt before its story")
    run.add_argument("--qualification-only", action="store_true", help="Leave a successful qualification awaiting normal --resume")
    continuation = run.add_mutually_exclusive_group()
    continuation.add_argument("--resume", action="store_true")
    continuation.add_argument("--retry-failed", action="store_true")
    for mode in ("release-first", "approve-first"):
        control = sub.add_parser(mode, help="Persist delivery authorization only; no model calls")
        control.add_argument("manifest")
        control.add_argument("--initial-state", required=True)
        control.add_argument("--experience", required=True)
        control.add_argument("--freeze", required=True)
        if mode == "approve-first":
            control.add_argument("--attestation", required=True, help="Operator attests direct owner confirmation; application does not authenticate human authorship")
    args = parser.parse_args(argv)
    if args.mode in ("release-first", "approve-first"):
        campaign = json.loads(Path(args.manifest).read_text())
        identity = source_identity(initial_state=args.initial_state, experience=args.experience,
                                   historical_evidence=campaign.get("historical_evidence_path"))
        status = preflight(campaign, identity)
        if status["blockers"] or args.freeze != status["execution_source_sha256"]:
            raise ValueError("Accepted unchanged scientific freeze required for delivery control")
        registry = delivery_registry(campaign)
        result = (registry.release_first() if args.mode == "release-first" else
                  registry.approve_first(json.loads(Path(args.attestation).read_text())))
    elif args.mode == "run":
        campaign = json.loads(Path(args.manifest).read_text())
        if args.freeze != digest({"identity": campaign["identity"], "campaign": campaign}):
            raise ValueError("Explicit accepted execution-source hash required")
        result = asyncio.run(run_cell(campaign, args.cell, initial_state=args.initial_state,
                                      experience=args.experience, resume=args.resume, retry_failed=args.retry_failed,
                                      qualification=args.qualify, qualification_only=args.qualification_only))
    elif args.mode == "score":
        path = Path(args.recording)
        truth_path = Path(args.truth) if args.truth else None
        result = score_export(json.loads(path.read_text()), args.story, model=args.model, expected_source=args.source,
                              profiles=matched_stories() if args.matched else None,
                              truth_packet=json.loads(truth_path.read_text()) if truth_path else None,
                              truth_file_sha256=hashlib.sha256(truth_path.read_bytes()).hexdigest() if truth_path else None,
                              binding=json.loads(Path(args.binding).read_text()) if args.binding else None,
                              recording_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        result["recording_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        identity = source_identity(initial_state=args.initial_state, experience=args.experience,
                                   historical_evidence=args.historical_evidence)
        if args.mode == "inspect":
            result = campaign_template(identity)
            result["historical_evidence_path"] = str(Path(args.historical_evidence).resolve()) if args.historical_evidence else None
        else:
            result = preflight(json.loads(Path(args.manifest).read_text()), identity, cell_id=args.cell)
    print(json.dumps(result, indent=2, allow_nan=False))
    if args.mode == "preflight":
        return 0 if (result["selected_cell"]["eligible"] if result["selected_cell"] else result["whole_matrix_ready"]) else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
