"""Which flake candidates get 20x/100x photos: slots per device-plan role (v1
kept graphene targets and graphite gates apart), and photographing stored
candidates later without a new scan."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from qumus2.flows.library.search import RevisitCandidates, RevisitRole, pick_revisits


def flakes(label: str, n: int, area: float = 100.0, start: int = 0) -> list[dict]:
    return [{"id": f"{label}_{start + i}", "props": {"labels": {"10x": label},
                                                     "area_um2": area - i}}
            for i in range(n)]


def labels(chosen: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in chosen:
        out[c["props"]["labels"]["10x"]] = out.get(c["props"]["labels"]["10x"], 0) + 1
    return out


def test_graphite_keeps_its_slots_when_monolayers_are_plenty():
    # before: one 15-slot queue, monolayer first -> no graphite was ever photographed
    cands = flakes("monolayer", 40, area=500) + flakes("manylayer", 8, area=80)
    got = labels(pick_revisits(cands, "Gr", None, 20))
    assert got == {"monolayer": 15, "manylayer": 5}


def test_every_hbn_bin_gets_slots():
    cands = (flakes("10-20", 30, area=900) + flakes("0-10", 2, area=60)
             + flakes("30-40", 3, area=70))
    got = labels(pick_revisits(cands, "hBN", None, 20))
    assert got["0-10"] == 2 and got["30-40"] == 3
    assert sum(got.values()) == 20  # free slots go to the bin that has more


def test_plan_roles_decide_even_outside_v1s_table():
    # trilayer graphene was not in v1's PREFERRED labels: never photographed before
    cands = flakes("monolayer", 30, area=900) + flakes("trilayer", 4, area=200)
    small = flakes("trilayer", 2, area=40, start=10)
    quota = [RevisitRole(role="channel", material="Gr", thickness_labels=["trilayer"],
                         min_area_um2=100, slots=3)]
    chosen = pick_revisits(cands + small, "Gr", quota, 20)
    ids = [c["id"] for c in chosen]
    assert ids[:3] == ["trilayer_0", "trilayer_1", "trilayer_2"]  # the role's slots first
    assert "trilayer_3" in ids  # free slots: other flakes the role would take
    assert not {"trilayer_10", "trilayer_11"} & set(ids)  # too small for the role
    assert len(chosen) == 20


def test_roles_of_other_materials_are_ignored():
    quota = [RevisitRole(role="top_hbn", material="hBN", thickness_labels=["10-20"],
                         slots=3)]
    got = labels(pick_revisits(flakes("monolayer", 5), "Gr", quota, 20))
    assert got == {"monolayer": 5}  # PREFERRED fills the free slots


def test_search_chip_fills_quotas_from_the_device_plan(services):
    from qumus2.agents.tooling import ToolContext
    from qumus2.agents.tools.hardware import revisit_quota

    s = services
    s.store.update_project_state(s.project_id, device_plan={"flake_roles": [
        {"role": "channel", "material": "Gr", "thickness_labels": ["1L"],
         "min_area_um2": 50, "count": 1},
        {"role": "top_hbn", "material": "hBN", "thickness_labels": ["10-20"],
         "min_area_um2": 100, "count": 2}]})
    ctx = ToolContext(store=s.store, jobs=s.jobs, agent="hands", project_id=s.project_id)
    q = revisit_quota(ctx)
    assert [(r["role"], r["thickness_labels"], r["slots"]) for r in q] == [
        ("channel", ["monolayer"], 3), ("top_hbn", ["10-20"], 6)]


def test_revisit_only_photographs_stored_candidates_without_a_new_scan(services):
    s, g = services, services.gateway
    st = s.jobs.wait(s.jobs.submit("exfoliate_chips", {"material": "Gr",
                                                       "chip_ids": ["chip_001"]}), 600)
    assert st.status == "succeeded"
    st = s.jobs.wait(s.jobs.submit("search_chip", {"chip_id": "chip_001",
                                                   "max_revisits": 2}), 600)
    assert st.status == "succeeded"

    def on_chip():
        return {f["id"]: f for f in s.store.list_samples(kind="flake", location="chip_001")}

    before, home = on_chip(), s.store.get_sample("chip_001")["location"]
    seen = {i for i, f in before.items() if f["status"] != "candidate"}
    waiting = [i for i, f in before.items() if f["status"] == "candidate"]
    assert len(seen) == 2 and len(waiting) >= 3

    r, _ = asyncio.run(g.call("hands", "search_chip", {
        "chip_id": "chip_001", "revisit_only": True, "max_revisits": 3}))
    assert r.status == "pending" and r.data["flow"] == "revisit_chip"
    st = s.jobs.wait(r.data["job_id"], 600)
    assert st.status == "succeeded"
    assert "scan_chip" not in [x["name"] for x in st.steps]

    after = on_chip()
    assert set(after) == set(before)  # no new candidates: nothing was rescanned
    newly = {i for i, f in after.items() if f["status"] != "candidate"} - seen
    assert len(newly) == 3 and newly <= set(waiting)
    assert all(after[i]["status"] == "catalogued" and after[i]["props"]["labels"].get("100x")
               for i in newly)
    assert s.store.get_sample("chip_001")["location"] == home  # stored back where it was


def test_revisit_only_needs_a_chip(services):
    r, _ = asyncio.run(services.gateway.call("hands", "search_chip", {"revisit_only": True}))
    assert r.status == "error" and "chip_id" in r.summary


def test_explicit_revisit_ids_override_automatic_quota_but_keep_order_and_limit(services):
    s = services
    for fid, label in (("few_a", "fewlayer"), ("tri", "trilayer"), ("few_b", "fewlayer")):
        s.store.upsert_sample(fid, "flake", location="chip_001", status="candidate",
                              props={"labels": {"10x": label}, "area_um2": 100, "images": {}})
    quota = [RevisitRole(role="channel", thickness_labels=["monolayer"], min_area_um2=1000, slots=2)]
    step = RevisitCandidates("chip_001", "Gr", 2, quota, only_new=True,
                             flake_ids=["tri", "few_b", "tri", "few_a"])
    assert [f["id"] for f in step._targets(SimpleNamespace(store=s.store))] == ["tri", "few_b"]
    s.store.upsert_sample("few_b", "flake", props={"images": {"20x": "already-recorded.png"}})
    # An explicit request can renew existing evidence; deduplication, requested
    # order and the quota still apply, regardless of a previous photograph.
    assert [f["id"] for f in step._targets(SimpleNamespace(store=s.store))] == ["tri", "few_b"]


def test_standalone_experiment_search_uses_all_thicknesses_at_tool_boundary(services):
    s, g = services, services.gateway
    s.store.update_project_state(s.project_id, task_kind="experiment", device_plan={"flake_roles": [
        {"role": "old_channel", "material": "Gr", "thickness_labels": ["monolayer"], "count": 1}]})
    # Capture only job submission; production tool builds the flow parameters.
    captured = {}
    g.jobs = SimpleNamespace(submit=lambda flow, params, **kwargs: captured.update(flow=flow, params=params) or "fixture")
    result = asyncio.run(g.call("hands", "search_chip", {"chip_id": "chip_001", "max_revisits": 5}))[0]
    assert result.status == "pending"
    roles = [RevisitRole.model_validate(r) for r in captured["params"]["revisit_quota"]]
    candidates = [f for label in ("monolayer", "bilayer", "trilayer", "fewlayer", "manylayer") for f in flakes(label, 1)]
    assert len(pick_revisits(candidates, "Gr", roles, 5)) == 5
    assert len(pick_revisits(candidates, "Gr", roles, 2)) == 2


def test_explicit_nonpreferred_revisit_gets_actual_paired_images(make_services, tmp_path):
    s = make_services(image_dir=tmp_path / "images")
    result = s.jobs.wait(s.jobs.submit("exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"]}), 600)
    assert result.status == "succeeded"
    result = s.jobs.wait(s.jobs.submit("search_chip", {"chip_id": "chip_001", "max_revisits": 0}), 600)
    assert result.status == "succeeded"
    candidates = s.store.list_samples(kind="flake", location="chip_001")
    requested = next(f for f in candidates if f["props"]["thickness_label"] in ("fewlayer", "trilayer"))
    original_ids = {f["id"] for f in candidates}
    assert requested["status"] == "candidate" and not requested["props"]["images"]
    response = asyncio.run(s.gateway.call("hands", "search_chip", {
        "chip_id": "chip_001", "revisit_only": True, "flake_ids": [requested["id"]], "max_revisits": 1}))[0]
    result = s.jobs.wait(response.data["job_id"], 600)
    assert result.status == "succeeded"
    assert "scan_chip" not in [step["name"] for step in result.steps]
    photographed = s.store.get_sample(requested["id"])
    assert photographed["status"] == "catalogued"
    assert all(Path(photographed["props"]["images"][key]).is_file() for key in ("20x", "100x"))
    assert {f["id"] for f in s.store.list_samples(kind="flake", location="chip_001")} == original_ids
    assert photographed["props"]["sim_id"] == requested["props"]["sim_id"]
