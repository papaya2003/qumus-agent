"""Pure activation checks; run after applying activation.diff, without a World."""
from copy import deepcopy

import pytest

from qumus2.config import load_yaml
from qumus2.core.errors import PreconditionFailed
from qumus2.flows.library import stacking
from qumus2.records.discovery import discovery_policy
from tests.test_device_design_pose_screen import bound_design, declared_spec
from tests.test_v1_transfer_sequence import bound_params


def linked_flow():
    flow = stacking.StackDeviceFlow()
    flow._bench = {**load_yaml("bench.yaml"), "stamp_discovery": discovery_policy()}
    return flow


class NoLedger:
    def get_sample(self, sample):
        pytest.fail("Invalid activation reached the ledger")


def test_linked_activation_requires_typed_design_before_any_ledger_read():
    with pytest.raises(PreconditionFailed) as caught:
        linked_flow().resolve_params(bound_params(), NoLedger())
    assert caught.value.details["reason"] == "stamp_discovery_requires_design"


def test_changed_caller_policy_cannot_override_actual_bench():
    changed = discovery_policy()
    changed["clear_radius_mm"] = 2.
    with pytest.raises(ValueError, match="actual composed bench"):
        linked_flow().resolve_params(bound_params(stamp_discovery_policy=changed), NoLedger())


def test_legacy_bench_cannot_gain_r017_from_caller_policy():
    with pytest.raises(ValueError, match="actual composed bench"):
        stacking.StackDeviceFlow().resolve_params(bound_params(stamp_discovery_policy=discovery_policy()), NoLedger())


def test_fresh_bind_copies_actual_validated_policy_before_inventory_admission(monkeypatch):
    class AtInventory(Exception):
        pass
    class Inventory:
        def get_sample(self, sample):
            raise AtInventory
    flow = linked_flow()
    original_policy = deepcopy(flow._bench["stamp_discovery"])
    design = bound_design(declared_spec())
    params = bound_params(device_design=design, flake_ids=list(design.pickup_order),
        materials={"hBN": "hBN", "Gr": "Gr"}, flake_chips={"hBN": "ca", "Gr": "cb"})
    copies, copy = [], stacking.StackParams.model_copy
    def tracked(self, **kwargs):
        result = copy(self, **kwargs)
        copies.append(result)
        return result
    monkeypatch.setattr(stacking.StackParams, "model_copy", tracked)
    with pytest.raises(AtInventory):
        flow.resolve_params(params, Inventory())
    assert params.stamp_discovery_policy is None
    assert copies[0].stamp_discovery_policy == original_policy
    assert copies[0].stamp_discovery_policy is not flow._bench["stamp_discovery"]
    assert flow._bench["stamp_discovery"] == original_policy
