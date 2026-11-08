"""Common geometry, evidence, operator inputs and execution guard regressions."""

import pytest
from test_reactor_workflow import CHIP, call

from qumus2.app import build_services
from qumus2.config import simulation_profile
from qumus2.records.geometry import current_pickup_evidence, pickup_coverage_errors


def rectangle(w, h):
    return ((-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2))


@pytest.fixture
def services():
    return build_services(testbench_config=simulation_profile(), use_openai=False)


def observed_pair(s):
    rid = s.store.create_run("search_chip", {}, project_id=s.project_id)
    s.store.set_run_status(rid, "succeeded")
    for fid, material, shape in (("top", "hBN", rectangle(.2, .1)), ("lower", "Gr", rectangle(.198, .098))):
        s.store.upsert_sample(fid, "flake", location="chip_001", status="catalogued", props={
            "material": material, "images": {"context": "optical-fixture.png"},
            "optical_geometry": {"outline_mm": shape, "units": "mm", "frame": "flake_center_chip_axes",
                "source": "simulated_optical_outline", "source_chip_id": "chip_001", "image": "optical-fixture.png", "job_id": rid},
            "pickup_assessment": {"flake_id": fid, "source_chip_id": "chip_001", "images": ["optical-fixture.png"], "job_id": rid, "clear": True}})
    return rid


def test_containment_requires_outlines_transforms_and_current_provenance(services):
    s = services
    observed_pair(s)
    assert not pickup_coverage_errors(s.store, ["top", "lower"])
    assert pickup_coverage_errors(s.store, ["top", "lower"], {"lower": {"rotation_deg": 2}})
    assert pickup_coverage_errors(s.store, ["top", "lower"], {"lower": {"xy_mm": [0, .002]}})
    # Area may be smaller but an elongated footprint still protrudes.
    sample = s.store.get_sample("lower")
    geometry = {**sample["props"]["optical_geometry"], "outline_mm": rectangle(.3, .01)}
    s.store.upsert_sample("lower", "flake", props={"optical_geometry": geometry})
    assert pickup_coverage_errors(s.store, ["top", "lower"])
    s.store.upsert_sample("lower", "flake", location="chip_002")
    assert current_pickup_evidence(s.store, s.store.get_sample("lower")) is None
    assert "provenance" in pickup_coverage_errors(s.store, ["top", "lower"])[0]


def test_scripted_ranking_prefers_clearance_and_rejects_stale_geometry(services):
    from qumus2.records.geometry import ranked_pickup_combinations

    s = services
    observed_pair(s)
    props = s.store.get_sample("lower")["props"]
    s.store.upsert_sample("roomier", "flake", location="chip_001", status="catalogued",
        props={**props, "pickup_assessment": {**props["pickup_assessment"], "flake_id": "roomier"},
               "optical_geometry": {**props["optical_geometry"], "outline_mm": rectangle(.1, .04)}})
    ranked = ranked_pickup_combinations(s.store, [["top"], ["lower", "roomier"]])
    assert ranked[0]["flake_ids"] == ["top", "roomier"]
    assert ranked[0]["nominal_clearance_mm"] > ranked[1]["nominal_clearance_mm"]
    s.store.upsert_sample("roomier", "flake", location="chip_002")
    assert [r["flake_ids"] for r in ranked_pickup_combinations(s.store, [["top"], ["lower", "roomier"]])] == [["top", "lower"]]


def test_coating_accepts_reported_thickness_without_extra_operator_fields(services):
    s = services
    ins = s.store.add_instruction(f"{CHIP} has 0.02 um Pd deposited on it.", project_id=s.project_id)
    args = {"chip_id": CHIP, "metal": "Pd", "thickness_nm": 20, "instruction_id": ins}
    result = call(s, "orchestrator", "record_metal_coating", args)
    assert result.status == "ok", result
    assert result.data["thickness_nm"] == 20 and "geometry" not in result.data
    assert result.data["preparation_reference_source"] == "instruction_receipt"
    assert call(s, "orchestrator", "record_metal_coating", {**args, "thickness_nm": 21}).status == "error"
    assert call(s, "orchestrator", "record_metal_coating", {**args, "thickness_nm": -1}).status == "denied"


def test_material_report_requires_human_evidence_and_preserves_history(services):
    from qumus2.records.materials import material_review

    s = services
    ins = s.store.add_instruction("carrier_gr_01 now carries hBN material tape.", project_id=s.project_id)
    args = {"carrier_id": "carrier_gr_01", "material": "hBN", "instruction_id": ins}
    result = call(s, "orchestrator", "record_material_source", args)
    assert result.status == "ok", result
    assert material_review(s.store.get_sample("carrier_gr_01"))["status"] == "confirmed"
    assert call(s, "orchestrator", "record_material_source", {**args, "material": "Gr"}).status == "error"
    assert call(s, "orchestrator", "record_material_source", args).data == result.data


def test_common_setup_and_visible_transfer_surface(services):
    s = services
    setup = call(s, "advisor", "get_lab_setup")
    assert {"reactor", "microscope", "exfoliator"} <= setup.data["instruments"].keys()
    reactor = call(s, "advisor", "get_reactor_setup")
    assert reactor.data == setup.data["instruments"]["reactor"]
    assert "chips" not in reactor.data and "workflow" not in reactor.data
    visible = {t.name for t in s.gateway.registry.for_agent("hands")}
    assert "stack_device" in visible and "prepare_material_metal" not in visible
    assert "get_reactor_setup" not in {t.name for t in s.gateway.registry.for_agent("advisor")}


def test_reexfoliation_submission_invalidates_old_search_evidence(services):
    observed_pair(services)
    assert current_pickup_evidence(services.store, services.store.get_sample("top"))
    services.store.create_run("exfoliate_chips", {"chip_ids": ["chip_001"], "material": "hBN"})
    assert current_pickup_evidence(services.store, services.store.get_sample("top")) is None
    assert pickup_coverage_errors(services.store, ["top", "lower"])


def test_reported_mica_replacement_refreshes_used_stamp_only_with_cited_act(services):
    s = services
    s.store.upsert_sample("stamp_01", "stamp", status="used")
    identity = s.store.add_instruction("stamp_01 has mica support.", project_id=s.project_id)
    args = {"stamp_id": "stamp_01", "film_type": "mica", "instruction_id": identity}
    assert call(s, "orchestrator", "report_new_film", args).status == "denied"
    report = s.store.add_instruction("I replaced the mica support on stamp_01 with new mica.", project_id=s.project_id)
    assert call(s, "orchestrator", "report_new_film", {**args, "instruction_id": report}).status == "ok"
    stamp = s.store.get_sample("stamp_01")
    assert stamp["status"] == "fresh" and stamp["props"]["support"]["film_type"] == "mica"
