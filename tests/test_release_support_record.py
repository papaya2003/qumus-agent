"""Record-only support integrity; RGB and physical acceptance remain separate."""

from copy import deepcopy

import pytest

from qumus2.flows.library import stacking as m
from tests.test_release_journal import score, support_record


def test_recorded_conditional_support_recomputes_original_opacity_geometry():
    assert score(support_record())


@pytest.mark.parametrize(
    "fault",
    [
        "mask",
        "profile",
        "load",
        "source_contract",
        "time",
        "command",
        "capture",
        "deleted",
        "earlier_positive",
        "capture_cap",
    ],
)
def test_missing_tampered_or_replaced_original_receipts_cannot_score(fault):
    values = support_record()
    events = values["events"]
    phase = values["phase"]
    params = values["params"]
    footprint = next(e for e in events if e["kind"] == "stack.release_footprint")
    current = next(
        e for e in events if e["id"] == values["current_receipts"]["capture"]["event_id"]
    )
    move = next(
        e
        for e in events
        if e["kind"] == "device.command" and e["payload"]["device"] == "transfer_arm"
    )
    if fault == "mask":
        events[-1]["payload"]["result"]["opaque_unknown_mask_sha256"] = "0" * 64
        phase["accepted_release_support"]["event_sha256"] = m._digest(events[-1]["payload"])
    elif fault == "profile":
        footprint["payload"]["profile"]["id"] = "other"
    elif fault == "load":
        params["flake_ids"] = ["wrong"]
    elif fault == "source_contract":
        params["direct_contact_contract"] = {"unit": "changed"}
    elif fault == "time":
        move["ts"] += 1.0
    elif fault == "command":
        move["payload"]["params"]["value"] -= 0.01
    elif fault == "capture":
        current["payload"]["microscope_preview"]["calibration"] = {}
    elif fault == "deleted":
        events.remove(move)
    elif fault == "earlier_positive":
        events.insert(-1, {**deepcopy(events[-1]), "id": events[-1]["id"] - 0.5})
    else:
        events.insert(
            events.index(footprint) + 1, {**deepcopy(footprint), "id": footprint["id"] + 0.5}
        )
    assert score(values) is False
