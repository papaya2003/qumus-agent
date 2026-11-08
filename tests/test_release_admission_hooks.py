"""Small original-admission/empty-stamp hooks; no World, jobs or rendering."""
from types import SimpleNamespace

import pytest

from qumus2.core.errors import PreconditionFailed
from qumus2.flows.aperture import CenterStampAperture
from qumus2.flows.library import stacking as m
from qumus2.records.device_design import build_design_pose_window_admission
from qumus2.runtime.store import RunStore
from qumus2.testbench.demo_scoring import _recorded_design_pose_window
from tests.test_device_design_pose_screen import bound_design, declared_spec
from tests.test_v1_transfer_sequence import bound_params


def declared_direct_contract():
    """Typed public record fixture only; no source pixels or job admission claimed."""
    from qumus2.core.polygons import rectangle
    from qumus2.records.geometry import BridgeRequirement, DirectContactContract, geometry_digest

    requirement = BridgeRequirement(contact_overlap_um=1., channel_width_um=2., placement_margin_um=.5,
                                    rationale="Declared unit contact geometry").model_dump()
    region = {"region_id":"pair", "units":"mm", "frame":"chip_center_axes", "anchor_mm":[0.,0.],
        "contact_pair":["left","right"], "polygons":[
            {"id":"left", "kind":"contact", "points_mm":rectangle(.01,.02,(-.015,0))},
            {"id":"right", "kind":"contact", "points_mm":rectangle(.01,.02,(.015,0))}]}
    target = {"target_chip_id":"target", "source":"simulated_design", "reference":"Declared unit target",
        "size_mm":[10.,10.], "region":region, "contact_gap_um":20., "contact_span_um":20., "contact_depth_um":10.}
    target["geometry_sha256"]=geometry_digest(target)
    intent = {"version":"direct-gr-contact-v1", "target_contact":target,"stamp_film":"PC",
        "direct_contact_material":"Gr","sole_transferred_layer":True,"receiving_layers_above_contacts":[],
        "target_declared_rotation_deg":0.,"bridge_requirement":requirement}
    intent["intent_sha256"]=geometry_digest(intent)
    outline=[list(p) for p in rectangle(.06,.008)]
    observation={"source_chip_id":"ca","outline_mm":outline,"source":"declared_unit_geometry"}
    return DirectContactContract(target_intent=intent,target_intent_sha256=intent["intent_sha256"],
        source_flake_id="a",source_outline_mm=outline,source_observation=observation,
        source_geometry_sha256=geometry_digest(observation),incoming_layers_top_first=[{"flake_id":"a","material":"Gr"}])


def record_fixture():
    design = bound_design(declared_spec())
    admission = build_design_pose_window_admission(design)
    params = m.StackParams(device_design=design, design_pose_window_admission=admission, flake_ids=list(design.pickup_order), materials={s.flake_id: s.material for s in design.sources}, target_chip_id=design.target_chip_id, stamp_id='stamp', stamp_slot='right_stamp_1', stamp_film='PC', release_policy=m.OBSERVED_RELEASE_POLICY_ID)
    store = RunStore(':memory:', bench='sim')
    raw = params.model_dump(mode='json')
    rid = store.create_run('stack_device', raw)
    m.StackDeviceFlow().reserve(params, store, rid)
    return (store, rid, raw, design)

def score(store, rid, raw):
    return _recorded_design_pose_window([e.model_dump() for e in store.events_after(0, limit=500, run_id=rid)], raw)

def test_fresh_sidecar_is_original_pre_motion_evidence_not_a_default():
    store, rid, raw, design = record_fixture()
    try:
        m._design_pose_window_binding(raw, design, store, store.get_run(rid))
        assert score(store, rid, raw)
        assert m.StackParams.model_fields['design_pose_window_admission'].default is None
    finally:
        store.close()

@pytest.mark.parametrize('fault', ['missing', 'changed', 'wrong_source', 'late', 'original_submission'])
def test_sidecar_cannot_be_deleted_changed_or_acknowledged_after_motion(fault):
    store, rid, raw, design = record_fixture()
    try:
        event = next(e for e in store.events_after(0, limit=500, run_id=rid) if e.kind == 'lab.evidence')
        if fault == 'missing':
            store._x('DELETE FROM event WHERE id=?', (event.id,))
        elif fault == 'changed':
            raw['design_pose_window_admission']['report_sha256'] = '0' * 64
        elif fault == 'wrong_source':
            store._x("UPDATE event SET payload_json=json_set(payload_json,'$.source.sha256',?) WHERE id=?", ('0' * 64, event.id))
        elif fault == 'late':
            store._x('DELETE FROM event WHERE id=?',(event.id,))
            store.emit('step.started', {'name': 'motion'}, run_id=rid)
            store.emit('lab.evidence',event.payload,run_id=rid)
        else:
            raw['design_pose_window_admission'] = None
        with pytest.raises(ValueError):
            m._design_pose_window_binding(raw, design, store, store.get_run(rid))
        assert score(store, rid, raw) is False
    finally:
        store.close()

def test_old_absent_sidecar_stays_unscreened_but_v3_cannot_gain_one():
    design = bound_design(declared_spec())
    m._design_pose_window_binding({'release_policy': m.RELEASE_POLICY_ID}, design, None)
    with pytest.raises(ValueError, match='Unscreened'):
        m._design_pose_window_binding({'release_policy': m.OBSERVED_RELEASE_POLICY_ID}, design, None)

def test_scoped_workflow_centers_empty_stamp_before_first_pickup_only():
    contract=declared_direct_contract()
    params = bound_params(flake_ids=['a'], materials={'a': 'Gr'}, release_policy=m.OBSERVED_RELEASE_POLICY_ID,
        direct_contact_contract=contract,direct_contact_material='Gr',target_contact_region_id='pair',
        direct_contact_intent=contract.target_intent,bridge_requirement=contract.target_intent['bridge_requirement'])
    steps = m.StackDeviceFlow().build_steps(params)
    centers = [i for i, step in enumerate(steps) if isinstance(step, CenterStampAperture)]
    assert len(centers) == 1
    assert centers[0] < next((i for i, step in enumerate(steps) if isinstance(step, m.AdjustFirstContact)))
    assert centers[0] < next((i for i, step in enumerate(steps) if isinstance(step, m.PickUpFlake)))
    legacy = m.StackDeviceFlow().build_steps(bound_params())
    assert not any(isinstance(step, CenterStampAperture) for step in legacy)

def aperture_context(monkeypatch, *, started=False, wrong=False):
    phases = {'v1_pick_up_gr': {}} if started else {}
    return SimpleNamespace(run_id='r', store=SimpleNamespace(bench='sim', get_run=lambda rid: {'meta': {'normal_transfer': {'phases': phases}}}), params={'release_policy': m.OBSERVED_RELEASE_POLICY_ID, 'flake_ids': ['wrong' if wrong else 'a'], 'materials': {'a': 'Gr'}, 'stamp_film': 'PC', 'stamp_id': 'stamp', 'direct_contact_contract': declared_direct_contract().model_dump(mode='json')}, verification={'transfer_sequence': {'holder_xy_mapping': 'synthetic-parallel-world-axes-v1'}, 'manipulator': {'tolerance_mm': 0.005}}, bench={'transfer_arm': {'safe_z': 6.5}}, gate=SimpleNamespace(manipulator=lambda did: SimpleNamespace(get_positions=lambda: {'x': 40.0, 'y': 90.0, 'z': 6.5}), microscope=lambda did: SimpleNamespace(get_objective=lambda: 5)))

def test_typed_sole_gr_aperture_route_keeps_source_and_empty_stage_binding(monkeypatch):
    CenterStampAperture('stamp').preconditions(aperture_context(monkeypatch))
    with pytest.raises(PreconditionFailed):
        CenterStampAperture('stamp').preconditions(aperture_context(monkeypatch, wrong=True))
    with pytest.raises(PreconditionFailed, match='pickup attempt'):
        CenterStampAperture('stamp').preconditions(aperture_context(monkeypatch, started=True))
