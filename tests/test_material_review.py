"""Human inventory review is persistent, read-only to agents and never image-derived."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from qumus2.records.materials import material_review, review_material
from qumus2.ui.app import create_app


def review(s, **overrides):
    args = {"expected_material": "Gr", "by": "Test operator", "status": "confirmed"}
    return review_material(s.store, s.lab.bench, "carrier_gr_01", **(args | overrides))


def test_human_review_is_preserved_in_sample_events_and_read_by_agents(services):
    s = services
    assert material_review(s.store.get_sample("carrier_gr_01"))["status"] == "unreviewed"
    with TestClient(create_app(s, start_driver=False)) as client:
        r = client.post("/api/carriers/carrier_gr_01/material-review", json={
            "expected_material": "Gr", "by": "Test operator", "status": "confirmed",
            "note": "Checked preparation record A-12"})
        assert r.status_code == 200, r.text
        snapshot = client.get("/api/snapshot").json()
    sample = next(x for x in snapshot["samples"] if x["id"] == "carrier_gr_01")
    assert material_review(sample)["by"] == "Test operator"
    assert sample["props"]["material"] == "Gr"
    emitted = [e for e in s.store.events_after(0, limit=100_000)
               if e.kind == "sample.updated" and e.payload["id"] == "carrier_gr_01"]
    assert emitted[-1].payload["props"]["material_reviews"][-1]["note"] == "Checked preparation record A-12"
    result = asyncio.run(s.gateway.call("eyes", "list_samples", {"kind": "carrier"}))[0]
    carrier = next(x for x in result.data["samples"] if x["id"] == "carrier_gr_01")
    assert carrier["material_source"] == "inventory_label"
    assert carrier["human_review"]["status"] == "confirmed"


def test_blank_names_stale_labels_and_carriers_in_use_are_refused(services):
    s = services
    with pytest.raises(ValueError, match="name"):
        review(s, by=" ")
    with pytest.raises(ValueError, match="changed"):
        review(s, expected_material="hBN")
    s.store.upsert_sample("carrier_gr_01", "carrier", location="exfoliation_stage")
    with pytest.raises(ValueError, match="left storage"):
        review(s)
    s.store.upsert_sample("carrier_gr_01", "carrier", location="left_slide_1")
    job = s.store.create_run("exfoliate_chips", {})
    s.store.reserve("sample", "carrier_gr_01", job, "exfoliate_chips")
    with pytest.raises(ValueError, match="reserved"):
        review(s)
    assert material_review(s.store.get_sample("carrier_gr_01"))["status"] == "unreviewed"


def test_uncertain_identity_is_not_selected_for_exfoliation(services):
    s = services
    from qumus2.flows.library.exfoliation import ExfoliationFlow, ExfoliationParams

    flow = ExfoliationFlow()
    review(s, status="uncertain", note="The label cannot be read")
    with pytest.raises(ValueError, match="human material review"):
        flow.resolve_params(ExfoliationParams(material="Gr"), s.store)
    review(s)
    assert flow.resolve_params(ExfoliationParams(material="Gr"), s.store).carrier_id == "carrier_gr_01"
    assert len(s.store.get_sample("carrier_gr_01")["props"]["material_reviews"]) == 2


def test_a_changed_inventory_label_invalidates_previous_confirmation(services):
    s = services
    review(s)
    s.store.upsert_sample("carrier_gr_01", "carrier", props={"material": "hBN"})
    assert material_review(s.store.get_sample("carrier_gr_01"))["status"] == "unreviewed"
