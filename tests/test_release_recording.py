"""A demonstration's title must be supported by actual testbench observations."""
from scripts.record_release import audit_story


def test_arming_faults_and_model_claims_do_not_prove_recovery():
    events = [{"id": n, "kind": "fault.injected", "payload": {"fault": fault}}
              for n, fault in enumerate(("serial_timeout", "pick_fail_next", "next_placement"), 1)]
    events.append({"id": 4, "kind": "agent.message", "payload": {
        "text": "Recovered from a serial timeout, pick came up empty and placement correction."}})
    runs = [{"flow": "place_chip_on_transfer_stage", "status": "succeeded", "params": {"chip_id": "chip_001"}}]
    samples = [{"id": "chip_001", "location": "transfer_stage"}]
    checks, _ = audit_story("recovery", events, runs, samples, [])
    assert checks["verified_transfer"]
    assert not checks["serial_timeout_observed"]
    assert not checks["empty_pickup_observed"]
    assert not checks["placement_correction_observed"]
    assert not checks["model_requests_measured"]


def test_combined_faults_are_observed_and_corrected_in_one_transfer(make_services):
    s = make_services()
    for name, params in [("serial_timeout", {"device": "arm_right", "count": 1}),
                         ("pick_fail_next", {"count": 1}),
                         ("next_placement", {"dx": 2.2, "dy": -1.2, "drot": 2.0})]:
        s.lab.faults.inject(name, params)
        s.store.emit("fault.injected", {"fault": name, "params": params})
    job = s.jobs.submit("place_chip_on_transfer_stage", {"chip_id": "chip_001"}, project_id=s.project_id)
    assert s.jobs.wait(job, 30).status == "succeeded"
    events = [e.model_dump() for e in s.store.events_after(0, limit=100000)]
    checks, evidence = audit_story("recovery", events, s.store.list_runs(), s.store.list_samples(), [])
    assert checks["serial_timeout_observed"] and checks["empty_pickup_observed"]
    assert checks["placement_correction_observed"] and checks["verified_transfer"]
    assert checks["chip_on_stage"] and checks["no_safety_violation"]
    # An offline flow test is not falsely promoted as live model evidence.
    assert not checks["model_requests_measured"] and not checks["model_turns_completed"]
    correction_ids = set(evidence["placement_correction_observed"])
    without_correction = [e for e in events if e["id"] not in correction_ids]
    incomplete, _ = audit_story("recovery", without_correction, s.store.list_runs(), s.store.list_samples(), [])
    assert not incomplete["placement_correction_observed"]


def test_combined_recording_requires_observed_faults_in_the_same_default_search(tmp_path):
    import pytest

    from qumus2.app import build_services
    from qumus2.config import simulation_profile
    from scripts.record_release import (
        audit_search_recovery,
        inject_transfer_faults,
        require_prepared_material,
    )

    s = build_services(tmp_path / 'combined.db', seed=7, testbench_config=simulation_profile(),
                       use_openai=False)
    with pytest.raises(RuntimeError, match='before sending the next scripted review'):
        require_prepared_material(s, 'chip_002', 'Gr')
    prepared = s.jobs.submit('exfoliate_chips', {'material': 'Gr', 'num_chips': 1,
        'chip_ids': ['chip_002'], 'target_temp_c': 22, 'dwell_s': 10,
        'num_massages': 1, 'peel_speed': 500}, project_id=s.project_id)
    assert s.jobs.wait(prepared, 30).status == 'succeeded'
    inject_transfer_faults(s)
    searched = s.jobs.submit('search_chip', {'chip_id': 'chip_002', 'material': 'Gr'},
                            project_id=s.project_id)
    assert s.jobs.wait(searched, 300).status == 'succeeded'
    events = [e.model_dump() for e in s.store.events_after(0, limit=100000)]
    checks, proof = audit_search_recovery(events, s.store.list_runs())
    assert all(checks.values()), (checks, proof)
    assert proof['recovered_search_job_ids'] == [searched]
    assert s.store.get_sample('chip_002')['status'] == 'searched'
    require_prepared_material(s, 'chip_002', 'Gr')
    with pytest.raises(RuntimeError, match='did not finish'):
        require_prepared_material(s, 'chip_003', 'hBN')
    s.store.set_plan(s.project_id, [{'id': 'unfinished', 'owner': 'hands', 'status': 'pending',
        'title': 'Retain the unresolved original storage condition', 'operation': 'search_chip',
        'sample_outcomes': [{'sample_id': 'chip_002', 'location': 'left_chip_2', 'status': 'searched'}]}],
        author='test fixture')
    with pytest.raises(RuntimeError, match='did not finish'):
        require_prepared_material(s, 'chip_002', 'Gr')
    assert s.store.get_sample('chip_001')['status'] == 'bare'
    assert s.store.spent_usd() == 0
    # Neither fault injection, narration nor failures on an unrelated job pass.
    false_events = [{**e, 'run_id': 'unrelated'} if e['kind'] == 'notification' else e
                    for e in events]
    rejected, _ = audit_search_recovery(false_events, s.store.list_runs())
    assert not rejected['three_faults_recovered_in_one_search']
