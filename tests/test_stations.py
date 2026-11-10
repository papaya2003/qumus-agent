"""The brain reaches the laboratory only through contract v1 stations (qumus2/stations)."""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path

import pytest
from conftest import _close, _png, make_fake_lab
from fastapi.testclient import TestClient

from qumus2.app import build_services
from qumus2.stations import config
from qumus2.stations.catalog import FlowCatalog
from qumus2.stations.launcher import _is_local, ensure_simlab
from qumus2.stations.mirror import IMAGE_PREFIX
from qumus2.ui.app import create_app

ROOT = Path(__file__).resolve().parents[1]
LAB_PACKAGES = ("qumus2.flows", "qumus2.safety", "qumus2.devices", "qumus2.testbench", "qumus2.simlab",
                "qumus_simlab")


def test_the_brain_imports_no_laboratory_code():
    for path in (ROOT / "qumus2").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else (
                [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            for name in names:
                assert not name.startswith(LAB_PACKAGES), f"{path.relative_to(ROOT)} imports {name}"
    for gone in ("flows", "safety", "devices", "testbench", "simlab"):
        assert not (ROOT / "qumus2" / gone).exists()


def test_stations_yaml_default_and_errors(tmp_path):
    st = config.load(None) if not os.environ.get("QUMUS_STATIONS") else None
    if st is not None and st.path is None:
        assert st.urls == {"exfoliation": "http://127.0.0.1:8781/", "stacking": "http://127.0.0.1:8782/",
                           "reactor": "http://127.0.0.1:8783/"}
        assert st.all_sim and st.records_from == "stacking" and st.bench == "sim"
        assert st.simlab_dir == (ROOT.parent / "qumus-simlab").resolve()
    f = tmp_path / "stations.yaml"
    f.write_text("simlab_dir: lab\nrecords_from: stacking\nmodules:\n"
                 "  stacking: {url: 'http://192.168.1.20:8782', kind: real}\n"
                 "  reactor: {url: 'http://127.0.0.1:18772/lab/', kind: real, adapter: console, session: qumus}\n")
    st = config.load(f)
    assert st.modules["stacking"].url == "http://192.168.1.20:8782/"  # another computer: only the URL differs
    assert st.modules["reactor"].extra == {"adapter": "console", "session": "qumus"}
    assert st.bench == "real" and not st.any_sim and st.simlab_dir == (tmp_path / "lab").resolve()
    for bad in ("modules: {}", "modules: {cooking: {url: 'http://x/'}}",
                "modules: {stacking: {url: 'ftp://x/'}}", "modules: {stacking: {url: 'http://x/', kind: maybe}}",
                "records_from: reactor\nmodules: {stacking: {url: 'http://x/'}}"):
        f.write_text(bad)
        with pytest.raises(ValueError):
            config.load(f)
    with pytest.raises(FileNotFoundError):
        config.load(tmp_path / "missing.yaml")


def test_catalog_learns_jobs_from_describe():
    from qumus2.contract.models import CommandSpec

    cat = FlowCatalog(known={"search_chip": "stacking"})
    assert cat.module_of("search_chip") == "stacking" and cat.module_of("new_job") is None
    cat.learn("reactor", {"new_job": CommandSpec("new_job", {}, raw={"job": True}),
                          "read_all": CommandSpec("read_all", {}), "job_retry": CommandSpec("job_retry", {})})
    assert cat.module_of("new_job") == "reactor" and "read_all" not in cat.flow_names()
    assert cat.flow_names("reactor") == ["new_job"]


def test_a_job_goes_to_its_station_and_comes_back_through_the_record_feed(services):
    s, lab = services, services.fake_lab
    ref = lab.add_image("microscope/20x/flake_9.png", _png())
    lab.outcomes["search_chip"] = lambda run, p: {
        "status": "succeeded", "result": {"flakes": ["flake_9"]},
        "steps": [{"name": "chip_to_microscope"}, {"name": "scan", "safe_to_resume": False}],
        "samples": [{"id": "flake_9", "kind": "flake", "location": p["chip_id"], "status": "catalogued",
                     "props": {"material": "hBN", "images": {"20x": ref}}}]}
    s.store.upsert_sample("chip_002", "chip", props={"brain_note": "picked by the advisor"})
    rid = s.jobs.submit("search_chip", {"chip_id": "chip_002", "material": "hBN"},
                        project_id=s.project_id, requested_by="hands")
    assert s.store.get_run(rid)["flow"] == "search_chip"  # the run row is already mirrored
    st = s.jobs.wait(rid, 5)
    assert st.done and st.status == "succeeded" and [x["name"] for x in st.steps] == ["chip_to_microscope", "scan"]
    assert st.steps[1]["safe_to_resume"] is False
    # the module that owns the flow got it, with the job envelope stripped into the run
    assert lab.commands[-1][:2] == ("stacking", "search_chip")
    run = s.store.get_run(rid)
    assert run["project_id"] == s.project_id and run["requested_by"] == "hands"
    # the brain's own sample write reached the lab before the job (put_records)
    assert lab.store.get_sample("chip_002")["props"]["brain_note"] == "picked by the advisor"
    assert lab.put_records[-1]["project"][0]["id"] == s.project_id
    # pictures: downloaded into the brain's image folder, path rewritten
    local = Path(s.store.get_sample("flake_9")["props"]["images"]["20x"])
    assert local.is_file() and local.read_bytes() == lab.images["microscope/20x/flake_9.png"]
    assert local.is_relative_to(Path(s.store.path).parent / "images")
    # lab events are re-emitted with the lab's id
    mirrored = [e for e in s.store.events_after(0, limit=10000) if e.run_id == rid and e.kind == "job.status"]
    assert mirrored and all("lab_event_id" in e.payload for e in mirrored)
    # brain-side meta survives a later lab update of the run
    s.store.update_run_meta(rid, purpose="find hBN")
    lab.set_run(rid, meta={"workflow": {"x": 1}})
    s.jobs.sync()
    assert s.store.get_run(rid)["meta"] == {"purpose": "find hBN", "workflow": {"x": 1}}
    # pushing the flake back maps the local picture to its image id again
    s.store.upsert_sample("flake_9", "flake", props={"selected_by": "picker"})
    s.jobs.push()
    pushed = next(r for r in lab.put_records[-1]["samples"] if r["id"] == "flake_9")
    assert pushed["props"]["images"]["20x"] == IMAGE_PREFIX + "microscope/20x/flake_9.png"


def test_job_controls_unknown_flows_and_stop(services):
    s, lab = services, services.fake_lab
    with pytest.raises(KeyError):
        s.jobs.submit("bake_a_cake", {})
    rid = s.jobs.submit("exfoliate_chips", {"material": "Gr", "num_chips": 1}, project_id=s.project_id)
    assert lab.commands[-1][:2] == ("exfoliation", "exfoliate_chips")
    s.jobs.cancel(rid)
    assert ("control", "job_cancel", {"run_id": rid}) in lab.commands
    s.jobs.retry(rid)
    assert s.jobs.wait(rid, 5).status == "succeeded"
    assert s.jobs.complete_experiment(s.project_id, "done")["completed"] is True
    s.jobs.stop_all()
    assert "stack_device" in s.jobs.flow_names() and "put_records" not in s.jobs.flow_names()
    # the sim-era reactor job flows are retired: the reactor is a Lab Console station now
    assert "reactor_growth" not in s.jobs.flow_names()


def test_a_fresh_laboratory_restarts_the_mirror(tmp_path):
    lab1 = make_fake_lab()
    db = tmp_path / "qumus.db"
    s = build_services(db, stations=lab1.stations())
    lab1.put_sample({"id": "chip_x", "kind": "chip", "location": "left_chip_1", "props": {}})
    s.jobs.sync()
    assert s.store.get_sample("chip_x")
    _close(s)
    lab1.close()
    lab2 = make_fake_lab()
    s2 = build_services(db, stations=lab2.stations())
    try:
        assert s2.store.get_sample("chip_x") is None  # the old lab's ledger is gone
        assert len(s2.store.list_samples()) == len(lab2.store.list_samples())
    finally:
        _close(s2)
        lab2.close()


def test_the_page_reads_the_laboratory_from_the_stations(services, tmp_path):
    lab = services.fake_lab
    lab.bench_state = {"devices": [{"id": "arm_right", "pose": {"z": 5}, "offline": False}],
                       "objects": [{"id": "stamp_01", "kind": "stamp"}], "twin": {"arms": {}},
                       "stamp": {"held": None}, "faults": {"armed": {}}}
    lab.previews["overview_camera"] = {"image_id": "overview/live.png", "timestamp": 12.5, "meta": {"k": 1}}
    lab.add_image("overview/live.png", _png())
    with TestClient(create_app(services, start_driver=False)) as c:
        health = c.get("/api/health").json()
        assert health["mode"] == "scripted" and health["mirror"]["online"] is True
        assert health["stations"]["stacking"]["kind"] == "sim"
        laboratory = health["laboratory"]
        assert laboratory == c.get("/api/laboratory").json()
        assert laboratory["mode"] == "virtual" and laboratory["backend"] == "stations"
        assert {m["id"]: m["available"] for m in laboratory["modes"]} == {"virtual": True, "real": False}
        assert laboratory["learning"]["enabled"] is False
        windows = c.get("/api/windows").json()["windows"]
        assert {w["module"] for w in windows} == {"exfoliation", "stacking", "reactor"}
        assert all(w["reachable"] and w["kind"] == "sim" for w in windows)
        assert c.get("/api/stations").json()["records_from"] == "stacking"
        b = c.get("/api/bench").json()
        assert "transfer_stage" in b["layout"]["regions"]
        assert b["devices"][0]["id"] == "arm_right" and b["objects"][0]["status"] == "fresh"
        assert "observed_scene" in b["twin"] and b["busy"] is False
        cam = c.get("/api/camera/overview_camera/latest").json()
        assert cam["timestamp"] == 12.5 and cam["image"].startswith("/api/image?path=images/overview/live.png")
        assert c.get(cam["image"]).status_code == 200
        assert c.get("/api/faults").json()[0]["fault"] == "pick_fail_next"
        assert c.post("/api/faults", json={"fault": "pick_fail_next", "params": {}}).json() == {"ok": True}
        assert lab.faults[-1]["fault"] == "pick_fail_next"
        assert c.post("/api/faults", json={"fault": "meteor", "params": {}}).status_code == 400
        snap = c.get("/api/snapshot").json()
        assert snap["stations"]["modules"]["reactor"]["kind"] == "sim" and len(snap["samples"]) >= 20
        iid = c.post("/api/messages", json={"text": "hi"}).json()["instruction_id"]
        assert iid.startswith("ins_")
        assert c.get("/api/runs/nope").status_code == 404
        assert c.post("/api/stop").status_code == 200
        assert "Qumus" in c.get("/").text
    assert not services.store.list_runs()  # driver off: page reads never dispatch a job


def test_tools_get_the_bench_from_the_station(services):
    assert services.lab.bench["slots"] and services.gateway.bench is services.lab.bench
    assert services.store.get_meta("station_bench")  # kept for offline History and tools


def test_scripted_turn_runs_against_the_stations(services):
    s = services

    async def go():
        s.store.add_instruction("Look at the bench", project_id=s.project_id)
        await s.driver.wait_idle(timeout=30)

    asyncio.run(go())
    flows = [r["flow"] for r in s.store.list_runs()]
    assert flows and all(m in ("exfoliation", "stacking", "reactor") for m, *_ in s.fake_lab.commands
                         if m != "control")


def test_the_launcher_only_starts_a_sim_lab_on_this_computer(tmp_path):
    assert _is_local("http://127.0.0.1:8782/") and _is_local("http://localhost:1/")
    assert not _is_local("http://192.168.1.20:8782/")
    remote = config.from_urls({"stacking": "http://192.0.2.1:9/"})
    with pytest.raises(RuntimeError, match="another computer"):
        ensure_simlab(remote, data_dir=tmp_path, wait_s=1)
    local = config.parse({"simlab_dir": str(tmp_path / "nowhere"),
                          "modules": {"stacking": {"url": "http://127.0.0.1:9/"}}})
    with pytest.raises(RuntimeError, match="no qumus-simlab checkout"):
        ensure_simlab(local, data_dir=tmp_path, wait_s=1)
    lab = make_fake_lab()
    try:  # already running: nothing is started
        assert ensure_simlab(lab.stations(), data_dir=tmp_path).process is None
    finally:
        lab.close()


def test_one_laboratory_behind_several_windows_is_mirrored_once(tmp_path):
    lab = make_fake_lab()
    s = build_services(tmp_path / "qumus.db", stations=lab.stations(records_from=["stacking", "exfoliation"]))
    try:
        rid = s.jobs.submit("exfoliate_chips", {}, project_id=s.project_id)
        assert s.jobs.wait(rid, 5).status == "succeeded"
        s.jobs.sync()
        mirrored = [e for e in s.store.events_after(0, limit=10000)
                    if e.kind == "job.status" and e.run_id == rid and "lab_event_id" in e.payload]
        assert len(mirrored) == 1  # same lab_id: the exfoliation window's feed is skipped
        assert s.jobs.mirror.mirrors[1].duplicate and s.jobs.mirror.online
    finally:
        _close(s)
        lab.close()


def test_a_separate_station_has_its_own_feed(tmp_path):
    from qumus2.stations.fake_lab import FakeLab

    lab = make_fake_lab()
    lab.services.pop("exfoliation").close()  # the Hall-bar lab without its exfoliation window
    other = FakeLab(modules=("exfoliation",)).start()  # a separate computer with its own store
    urls = {**lab.urls, "exfoliation": other.urls["exfoliation"]}
    st = config.from_urls(urls, records_from=["stacking", "exfoliation"])
    s = build_services(tmp_path / "qumus.db", stations=st)
    try:
        assert len(s.store.list_samples()) == 20  # the Hall-bar ledger from the primary feed
        rid = s.jobs.submit("exfoliate_chips", {}, project_id=s.project_id)
        assert other.commands[-1][:2] == ("exfoliation", "exfoliate_chips")
        assert s.jobs.wait(rid, 5).status == "succeeded"  # the run row came from the other feed
        assert not s.jobs.mirror.mirrors[1].duplicate
        assert s.store.get_meta("mirror_records_lab_id:exfoliation") == other.lab_id
        assert len(s.store.list_samples()) == 20  # the other fresh lab_id did not clear the ledger
        assert not other.put_records  # put_records goes to the primary feed only
    finally:
        _close(s)
        other.close()
        lab.close()
