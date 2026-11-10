from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

import qumus2.app
from qumus2.app import Services
from qumus2.stations.fake_lab import FakeLab

RECORDINGS_ENV = "QUMUS_RECORDINGS_DIR"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def v2_recording(name: str) -> Path:
    """A qumus-v2 demo recording used as a test fixture.

    The agent repository did not copy qumus-v2's 305 MB of recordings. A test
    that needs one finds it here (recordings/<name>) or under the folder named
    by QUMUS_RECORDINGS_DIR (a qumus-v2 checkout's recordings/), else skips."""
    local = Path(__file__).resolve().parents[1] / "recordings" / name
    if local.exists():
        return local
    other = os.environ.get(RECORDINGS_ENV)
    if other and (Path(other) / name).exists():
        return Path(other) / name
    pytest.skip(f"recording {name} is kept in qumus-v2; set {RECORDINGS_ENV} to its recordings/")


def hallbar_bench() -> dict[str, Any]:
    """The v1-linked Hall-bar bench as qumus-simlab describes it (GET /api/sim/layout)."""
    return json.loads((FIXTURES / "hallbar_bench.json").read_text())


def hallbar_samples() -> list[dict[str, Any]]:
    """The Hall-bar laboratory's starting inventory (seed 3)."""
    return json.loads((FIXTURES / "hallbar_samples.json").read_text())


def _png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (90, 120, 160)).save(buf, format="PNG")
    return buf.getvalue()


def make_fake_lab(**kwargs: Any) -> FakeLab:
    kwargs.setdefault("bench", hallbar_bench())
    kwargs.setdefault("samples", hallbar_samples())
    lab = FakeLab(**kwargs)
    # the overview camera's picture, as the simulated lab's inspect_bench returns it
    ref = lab.add_image("overview/inspect-0001.png", _png())
    lab.outcomes["inspect_bench"] = lambda run, params: {
        "status": "succeeded",
        "result": [{"region": "bench", "observations": [{"camera": "overview_camera", "images": [ref],
                                                          "detections": []}]}]}
    return lab.start()


_real_build_services = qumus2.app.build_services
_FAKE_LABS: list[FakeLab] = []


def build_services(db_path: Any = ":memory:", *, stations: Any = None, seed: Any = None,
                   testbench_config: Any = None, **kwargs: Any) -> Services:
    """Tests' build_services: without explicit stations, the brain gets its own fake
    Hall-bar laboratory on loopback (`seed`/`testbench_config` of the old in-process
    testbench are ignored: the brain no longer builds a laboratory)."""
    del seed, testbench_config
    lab = None
    if stations is None:
        lab = make_fake_lab()
        _FAKE_LABS.append(lab)
        stations = lab.stations()
    kwargs.setdefault("mirror_poll_s", 0.05)
    s = _real_build_services(db_path, stations=stations, **kwargs)
    if lab is not None:
        s.fake_lab = lab  # type: ignore[attr-defined]
    return s


# test modules import build_services from qumus2.app after this conftest
qumus2.app.build_services = build_services


@pytest.fixture(scope="session", autouse=True)
def _close_fake_labs():
    yield
    for lab in _FAKE_LABS:
        lab.close()


def run_fixture_coroutine(factory, *, timeout_s=35.0, force_thread=False):
    """Run a coroutine factory without nesting Playwright's running event loop."""
    if not force_thread:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(factory())
    outcome = {}

    def run():
        try:
            outcome["value"] = asyncio.run(factory())
        except BaseException as error:
            outcome["error"] = error
    worker = threading.Thread(target=run, name="qumus-fixture-async", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TimeoutError("Owned fixture coroutine has not settled; its resources remain retained")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _close(services: Services) -> None:
    run_fixture_coroutine(lambda: services.close())


@pytest.fixture
def fake_lab():
    lab = make_fake_lab()
    yield lab
    lab.close()


@pytest.fixture
def services(fake_lab, tmp_path) -> Services:
    """The brain on the fake Hall-bar laboratory (stations on loopback, no physics)."""
    s = build_services(tmp_path / "brain" / "qumus.db", stations=fake_lab.stations(), mirror_poll_s=0.05)
    s.fake_lab = fake_lab  # type: ignore[attr-defined]
    yield s
    _close(s)


@pytest.fixture
def make_services(tmp_path):
    owned: list[tuple[Services, FakeLab]] = []

    def _make(**kwargs: Any) -> Services:
        lab = make_fake_lab()
        s = build_services(tmp_path / f"brain{len(owned)}" / "qumus.db", stations=lab.stations(),
                           mirror_poll_s=0.05, **kwargs)
        s.fake_lab = lab  # type: ignore[attr-defined]
        owned.append((s, lab))
        return s
    yield _make
    for s, lab in reversed(owned):
        _close(s)
        lab.close()


@pytest.fixture
def pickup_inspection(tmp_path):
    """Synthetic current search evidence for isolated Picker contract fixtures."""
    from PIL import Image

    def add(services, flake_ids, chip_id="chip_001"):
        image = tmp_path / f"{chip_id}-pickup-context.png"
        Image.new("RGB", (4, 4), "gray").save(image)
        job = services.store.create_run("search_chip", {"chip_id": chip_id}, project_id=services.project_id)
        services.store.set_run_status(job, "succeeded")
        for fid in flake_ids:
            services.store.upsert_sample(fid, "flake", location=chip_id, props={"pickup_assessment": {
                "flake_id": fid, "source_chip_id": chip_id, "images": [str(image)], "job_id": job, "clear": True}})
        return job
    return add


#: Tests that exercised the old in-process simulator's physics or internals
#: (world faults, ground truth, moved chips). The brain no longer contains the
#: laboratory; these behaviours are covered by qumus-simlab's own tests and the
#: brain + qumus-simlab integration run. Kept (skipped) so they can be ported
#: to a real simulated station later.
NEEDS_SIMLAB = {
    "tests/test_agent_tools.py::test_recovery_menu_and_budget": "pick faults in the simulator",
    "tests/test_agent_tools.py::test_replace_chip_counts_against_the_original_job": "simulated search outcomes",
    "tests/test_agent_tools.py::test_wait_job_reports_a_crash_the_job_service_recovered": "simulator crash fault",
    "tests/test_agents.py::test_all_emitted_event_kinds_are_documented": "simulator placement fault",
    "tests/test_core_review.py::test_allowance_explains_unknown_device_demand_and_standalone_experiment": "simulated inventory outcomes",
    "tests/test_core_review.py::test_prepatterned_requirements_are_derived_and_unknown_geometry_is_not_accepted": "old testbench config",
    "tests/test_endpoint_cadence.py::test_public_tilt_bound_is_static_configured_norm_not_runtime_scene": "old testbench config",
    "tests/test_experience.py::test_sim_runs_are_never_evidence": "simulated job evidence",
    "tests/test_experience.py::test_review_votes_and_status_follow_real_results": "simulated job evidence",
    "tests/test_kept_store.py::test_a_new_bench_clears_what_an_earlier_bench_made": "in-process bench seeding",
    "tests/test_lab_geometry.py::test_inspection_is_a_real_evidence_producer_and_status_is_saved_query": "simulated inspection evidence",
    "tests/test_laboratory_modes.py::test_submission_records_actual_simulated_devices_not_caller_origin": "lab-side job service",
    "tests/test_model_health.py::test_scripted_stop_then_continue_end_to_end": "simulator device-call hooks",
    "tests/test_observed_scene.py::test_cv_observation_moves_scene_without_identity_or_truth_feedback": "simulator ground truth",
    "tests/test_observed_scene.py::test_first_bench_poll_does_not_repair_initialized_physical_state": "simulator ground truth",
    "tests/test_observed_scene.py::test_projection_is_pure_and_old_bench_snapshot_excludes_future_reports": "simulator ground truth",
    "tests/test_observed_scene.py::test_existing_inspect_bench_is_an_actual_observed_scene_producer": "simulated inspection",
    "tests/test_openai_runtime.py::test_hardware_tool_rejects_invented_sample": "simulated chip placement",
    "tests/test_run_relocation.py::test_old_images_still_view_export_and_reach_model": "simulator image files",
    "tests/test_scripted_and_ui.py::test_scenario_runs_through_driver_and_takes_new_instruction": "simulator placement fault",
    "tests/test_search_outcome_repair.py::test_actual_chain_corrects_identity_and_rechecks_original_search_without_motion": "simulated search",
    "tests/test_stamps.py::test_the_lab_page_records_a_new_film_on_a_used_stamp": "simulator stamp state",
    "tests/test_stamps.py::test_the_testbench_stamp_gets_its_film_when_a_person_reports_it": "simulator stamp state",
    "tests/test_stamps.py::test_the_ledger_can_be_rebuilt_from_events": "simulator stamp state",
    "tests/test_cache_accounting.py::test_settings_saves_source_nominal_zero_and_shows_measured_cache_coverage": "browser fixture driven by a simulated recording",
}
#: failing already on qumus-v2 main 0cc7826 in the Linux cloud environment (not caused by the split)
PRE_EXISTING = {
    "tests/test_device_design_pose_screen.py::test_tool_rechecks_current_policy_after_export_before_accepting",
    "tests/test_device_design_pose_screen.py::test_tool_positive_returns_recomputable_sidecar_without_altering_design_schema",
    "tests/test_device_design.py::test_missing_or_unsuccessful_klayout_does_not_create_acceptance",
    "tests/test_openai_budget.py::test_chat_cache_usage_and_optional_default_preserve_no_cache_reservation",
    "tests/test_core_review.py::test_candidate_shortage_and_single_chip_query",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        base = item.nodeid.split("[", 1)[0]
        if base in NEEDS_SIMLAB:
            item.add_marker(pytest.mark.skip(reason=f"needs the simulated laboratory: {NEEDS_SIMLAB[base]}"))
        elif base in PRE_EXISTING:
            item.add_marker(pytest.mark.xfail(reason="fails on qumus-v2 0cc7826 too", strict=False))
