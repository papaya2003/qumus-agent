"""stack_device accepts the current design by canonical digest or design_id.

A live run passed the Designer's design_id where the canonical digest was
expected and was refused with a misleading "revision differs" message.
"""
import asyncio
from types import SimpleNamespace

import pytest

from qumus2.agents.tools import hardware
from qumus2.agents.tools.hardware import StackDeviceArgs

DESIGN = SimpleNamespace(design_id="design_" + "a" * 32, canonical_digest="b" * 64, revision=2,
                         pickup_order=("hbn_top", "gr"), target_chip_id="chip_e01",
                         model_dump=lambda mode="json": {"design_id": "design_" + "a" * 32})


@pytest.fixture
def stack(monkeypatch):
    import qumus2.records.device_design as dd
    import qumus2.records.reactor_requirements as rr

    submitted = []
    monkeypatch.setattr(rr, "established_requirements", lambda store, pid: None)
    monkeypatch.setattr(dd, "validate_design_current", lambda store, pid: DESIGN)
    monkeypatch.setattr(hardware, "submit", lambda ctx, kind, params: submitted.append((kind, params)) or "ok")
    store = SimpleNamespace(get_project=lambda pid: {"state": {"device_design": {"x": 1}}})
    ctx = SimpleNamespace(project_id="p1", store=store)

    def run(digest):
        args = StackDeviceArgs(flake_ids=["hbn_top", "gr"], target_chip_id="chip_e01", design_digest=digest)
        return asyncio.run(hardware.stack_device.__wrapped__(args, ctx)
                           if hasattr(hardware.stack_device, "__wrapped__") else hardware.stack_device(args, ctx))
    return run, submitted


@pytest.mark.parametrize("digest", [None, "b" * 64, "design_" + "a" * 32])
def test_current_design_reference_is_accepted(stack, digest):
    run, submitted = stack
    assert run(digest) == "ok"
    kind, params = submitted[-1]
    assert kind == "stack_device" and "design_digest" not in params and params["device_design"]


def test_stale_reference_names_the_current_design(stack):
    run, submitted = stack
    result = run("design_" + "c" * 32)
    assert result.status == "error" and not submitted
    assert "design_" + "a" * 32 in result.summary and "revision 2" in result.summary
    assert "Omit design_digest" in result.summary
