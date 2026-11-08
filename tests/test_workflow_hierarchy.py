"""Executable hierarchy and evidence contracts, without model or hardware calls."""
from __future__ import annotations

from copy import deepcopy

import pytest

from qumus2.flows.flow import get_flow
from qumus2.flows.library.material_metal import (
    MaterialMetalObserveParams,
    MaterialMetalPrepareParams,
)
from qumus2.flows.library.material_metal_device import reaction_workflow
from qumus2.flows.library.stacking import (
    PROCEDURE_ID,
    RELEASE_POLICY_ID,
    TRANSFER_PROFILE_ID,
    StackParams,
)
from qumus2.flows.step import Observation, Step, Verdict
from qumus2.flows.workflow import Assembly, Molecule, compact_manifest


class Checked(Step):
    def act(self, ctx, correction):
        pass

    def verify(self, ctx):
        return Observation(Verdict.OK)


def prepared_params():
    return MaterialMetalPrepareParams(project_id="project", target_chip_id="carrier", flake_ids=["hbn", "gr"],
        flake_chips={"hbn": "source_a", "gr": "source_b"}, materials={"hbn": "hBN", "gr": "Gr"},
        home_slots={"source_a": "a_slot", "source_b": "b_slot", "carrier": "shared"},
        stamp_id="stamp", stamp_slot="stamp_slot", target_xy_mm=(1, 2), reactor_home_slot="reactor_slot",
        procedure_contract=PROCEDURE_ID, transfer_profile=TRANSFER_PROFILE_ID, release_policy=RELEASE_POLICY_ID)


def test_composition_rejects_ambiguous_and_nonreviewed_checkpoints():
    with pytest.raises(ValueError, match="reviewed Steps"):
        Molecule("bad", "Do not evaluate model-authored source", ("move()",))
    with pytest.raises(ValueError, match="checked steps"):
        Molecule("empty", "No operation", ())
    step = Checked()
    with pytest.raises(ValueError, match="own Step"):
        Molecule("repeat", "No shared mutable correction state", (step, step))
    part = Molecule("first", "A checked operation", (step,))
    with pytest.raises(ValueError, match="unique"):
        Assembly("bad", "Duplicate child identity", (part, part))
    with pytest.raises(ValueError, match="shared"):
        Assembly("bad", "No cross-molecule mutable state", (part, Molecule("second", "Another operation", (step,))))


def test_preparation_reuses_transfer_molecules_and_checks_each_pickup():
    flow = get_flow("prepare_material_metal")
    p = prepared_params()
    workflow = flow.build_workflow(p)
    assert [part.name for part in workflow.molecules] == ["admit_and_handover", "v1_normal_sequence_v1_sim_v2_initialize_transfer",
        "pickup_layer_hbn", "pickup_layer_gr", "release_stack", "return_stamp", "return_prepared_carrier", "record_terminal"]
    for fid in p.flake_ids:
        part = next(part for part in workflow.molecules if part.name == f"pickup_layer_{fid}")
        # Device pickup shares the current V1 coverage/endpoint Step;
        # coverage does not by itself confirm a transfer.
        assert not any(step.name == f"assess_reactor_pickup_{fid}" for step in part.steps)
        assert any(type(step).__name__ == "DevicePickup" for step in part.steps)
    release = next(part for part in workflow.molecules if part.name == "release_stack")
    assert any(type(step).__name__ == "DeviceRelease" for step in release.steps)
    assert any(step.name == "verify_material_metal_preparation" for step in release.steps)
    assert [step.name for step in flow.build_steps(p)] == [step.name for step in workflow.build_steps()]
    # Plain dry transfer shares the middle operations, without claiming the
    # material-metal admission, inspection or release contract.
    stack = get_flow("stack_device").build_workflow(StackParams.model_validate(p.model_dump(), extra="ignore"))
    assert [part.name for part in stack.molecules] == [part.name for part in workflow.molecules[1:6]]


@pytest.mark.parametrize("disposition", ["cold_storage", "observation_hold"])
def test_reaction_hierarchy_preserves_hot_checkpoints_and_final_disposition(disposition):
    p = MaterialMetalObserveParams(project_id="project", chip_id="carrier", home_slot="home",
        temperature_c=300, flow_sccm=50, duration_s=180, goal={"final_disposition": disposition})
    workflow = reaction_workflow({"reactor_module": {"geometry": {"process_y_mm": 160}}}, p)
    manifest = workflow.manifest()
    flat = workflow.build_steps()
    leaves = [step for part in manifest["children"] for step in part["checkpoints"]]
    assert [step["seq"] for step in leaves] == list(range(len(flat)))
    assert [step["name"] for step in leaves] == [step.name for step in flat]
    assert [step["name"] for step in leaves if not step["safe_to_resume"]] == ["set_reactor_conditions", "observe_material_metal_progress"]
    names = [part.name for part in workflow.molecules]
    assert ("store_carrier" in names) == (disposition == "cold_storage")
    assert names.index("shutdown_and_cool") < names.index("observe_after_cooling")
    assert flat[-1].cold_storage == (disposition == "cold_storage")
    if disposition == "cold_storage":
        store_part = next(part for part in workflow.molecules if part.name == "store_carrier")
        assert [step.name for step in store_part.steps] == ["position_reactor_assembly", "retract_reactor_carrier", "store_material_metal_carrier"]
    before = deepcopy(manifest)
    compact = compact_manifest(manifest)
    assert manifest == before
    assert all("checkpoints" not in part and "description" not in part for part in compact["children"])
    assert [part["start_seq"] for part in compact["children"]] == [part["start_seq"] for part in manifest["children"]]


def test_abstract_fixture_and_legacy_pair_do_not_inherit_modern_device_hierarchy():
    params = prepared_params().model_copy(update={"protocol_id": "explicit-abstract-fixture"})
    assert get_flow("prepare_material_metal").build_workflow(params) is None
    assert get_flow("prepare_reactor_pair").build_workflow(params) is None


@pytest.mark.parametrize("operation,raw", [
    ("load_reactor_chip", {"chip_id": "carrier", "home_slot": "home"}),
    ("unload_reactor_chip", {"chip_id": "carrier", "home_slot": "home"}),
    ("observe_reactor", {"chip_id": "carrier", "autofocus": False}),
    ("observe_reactor", {"chip_id": "carrier", "autofocus": True}),
    ("verify_reactor_state", {"chip_id": "carrier"}),
    ("assess_reactor_inputs", {"chip_id": "carrier", "flake_ids": ["hbn", "gr"],
        "home_slots": {"a": "a_slot", "b": "b_slot"}, "flake_chips": {"hbn": "a", "gr": "b"},
        "materials": {"hbn": "hBN", "gr": "Gr"}}),
])
def test_standalone_hierarchy_matches_the_actual_execution_interface(operation, raw):
    flow = get_flow(operation)
    params = flow.Params.model_validate(raw)
    declared = flow.build_workflow(params).build_steps()
    executed = flow.build_steps(params)
    assert [(type(s), s.name, s.safe_to_resume, vars(s)) for s in declared] == [
        (type(s), s.name, s.safe_to_resume, vars(s)) for s in executed]


def test_booking_failure_does_not_leave_a_job_or_partial_hierarchy(monkeypatch):
    from pydantic import BaseModel

    from qumus2.core.clock import SimClock
    from qumus2.devices.base import DeviceBundle
    from qumus2.flows.flow import FLOWS, Flow
    from qumus2.runtime.jobs import ThreadJobService
    from qumus2.runtime.store import RunStore
    from qumus2.safety.gate import SafetyGate
    from qumus2.safety.limits import Limits

    class Parameters(BaseModel):
        pass

    class FailedBooking(Flow):
        name = "hierarchy_booking_failure"
        Params = Parameters

        def build_workflow(self, params):
            return Molecule("check", "A checked operation", (Checked(),))

        def reserve(self, params, store, run_id):
            store.reserve("slot", "test_slot", run_id)
            raise ValueError("Simulated reservation failure")

    monkeypatch.setitem(FLOWS, FailedBooking.name, FailedBooking)
    store = RunStore(":memory:", clock=SimClock())
    try:
        # Submission provenance reads a real explicit bundle even though this
        # transaction-only fixture never executes a device operation.
        gate = SafetyGate(DeviceBundle({}), Limits.load(), store.clock)
        service = ThreadJobService(store, gate, None, workers=0)
        with pytest.raises(ValueError, match="reservation failure"):
            service.submit(FailedBooking.name, {})
        assert not store.list_runs()
        assert not store.reservations()
        assert not store.events_after()
    finally:
        store.close()
