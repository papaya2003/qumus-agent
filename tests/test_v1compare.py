"""v1compare harness: v1 and v2 on the same testbench task, scored on ground truth."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from v1compare.trial import run_trial

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ["baseline", "askew_on_stage", "crash_holding_chip"])
def test_v2_flows_succeed_and_report_honestly(tmp_path, scenario):
    r = run_trial("v2", scenario, 1, tmp_path)
    assert "harness_error" not in r, r.get("harness_error")
    s = r["score"]
    assert s["truth_success"], s["problems"]
    assert not s["false_success"]
    assert s["recorded_real"] > 0 and s["ledger_ok"]
    assert s["extra_chips_used"] == 0
    assert r["interventions"] == 0
    if scenario != "baseline":
        assert r["injected"], "the scenario's fault was never injected"


def test_v2_offline_microscope_is_not_reported_as_success(tmp_path):
    r = run_trial("v2", "microscope_offline", 1, tmp_path)
    s = r["score"]
    assert not s["truth_success"] and not s["false_success"]


def test_v1_copy_matches_manifest():
    v1 = ROOT / "v1compare" / "v1"
    lines = [ln for ln in (v1 / "MANIFEST.sha256").read_text().splitlines()
             if ln and not ln.startswith("#")]
    import hashlib

    for ln in lines:
        digest, name = ln.split(maxsplit=1)
        assert hashlib.sha256((v1 / name).read_bytes()).hexdigest() == digest, name


def test_v1_exfoliates_then_reports_an_arm_timeout(tmp_path):
    # Frozen v1 imports LiteLLM through its package initializers, even in
    # flows mode. This case runs in the isolated v1 CI job; the modern SDK
    # environment cannot install LiteLLM's conflicting openai<3 dependency.
    for mod in ("smolagents", "litellm", "cv2", "sqlalchemy", "skimage", "watchdog"):
        pytest.importorskip(mod)
    out = tmp_path / "r.json"
    # one bench per process: v1 binds its databases at import. Relative paths,
    # as typed on the Mac: v1's bench chdirs into the workdir mid-run.
    # Seed 2: since the recipe model (PR #72) v1's fast peel (F500, 1 width
    # faster than the best) lifts the chip with the tape 1.5% of the time, and
    # seed 1 draws exactly that, a correct outcome but not what this test is about.
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    p = subprocess.run(
        [sys.executable, "-m", "v1compare.trial", "--system", "v1", "--scenario",
         "arm_timeout", "--seed", "2", "--workdir", "w", "--out", "r.json"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600)
    assert out.exists(), p.stderr[-3000:]
    r = json.loads(out.read_text())
    assert "harness_error" not in r, r.get("harness_error")
    s = r["score"]
    assert s["exfoliated_chips"] == 1  # v1's exfoliation ran on the testbench
    # v1's camera overlays land in the trial's workdir, where its agents look
    assert (tmp_path / "w" / "v1" / "images" / "yolo_captures" / "final_overlays"
            / "left_latest_final_result.jpg").is_file()
    assert r["injected"]
    assert not s["truth_success"] and not s["false_success"]
    assert not [i for i in r["incidents"] if "tape" in str(i)]  # v1 feeds tape in degrees


def test_v1_model_routes_use_bearer_auth(monkeypatch):
    from v1compare.agent import v1_model_env

    monkeypatch.setenv("QUMUS_ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("QUMUS_ANTHROPIC_BASE_URL", "https://gw.example/")
    monkeypatch.setenv("QUMUS_ANTHROPIC_MODEL_PREFIX", "anthropic/")
    for var in ("QUMUS_API_KEY", "QUMUS_API_BASE", "QUMUS_DEFAULT_MODEL_ID"):
        monkeypatch.delenv(var, raising=False)
    mid, headers = v1_model_env("claude-opus-5-5")
    assert mid == "openai/anthropic/claude-opus-5-5" and headers == {}
    assert os.environ["QUMUS_API_BASE"] == "https://gw.example/v1"
    mid, headers = v1_model_env("claude-opus-5-5", route="anthropic")
    assert mid == "anthropic/anthropic/claude-opus-5-5"
    assert headers == {"Authorization": "Bearer k"}
    assert os.environ["QUMUS_API_BASE"] == "https://gw.example"


def test_cost_meter_relaxes_forced_tool_choice_and_caps_spend(monkeypatch):
    litellm = pytest.importorskip("litellm")
    from v1compare.agent import BudgetExceeded, CostMeter

    seen = []

    class Usage:
        prompt_tokens, completion_tokens = 1_000_000, 0

    class Reply:
        usage = Usage()

    def fake(*args, **kw):
        seen.append((kw.get("tool_choice"), kw.get("extra_headers")))
        return Reply()

    monkeypatch.setattr(litellm, "completion", fake)
    monkeypatch.delenv("QUMUS_V1_FORCE_TOOL_CHOICE", raising=False)
    meter = CostMeter("claude-opus-5-5", 5.0, {"Authorization": "Bearer k"})
    meter.install()
    litellm.completion(model="m", messages=[], tool_choice="required")
    assert seen == [("auto", {"Authorization": "Bearer k"})] and meter.relaxed == 1
    assert meter.usd == pytest.approx(4.0)  # 1M input tokens at $4/Mtok
    litellm.completion(model="m", messages=[])
    with pytest.raises(BudgetExceeded):  # $8 > $5 cap: no third call
        litellm.completion(model="m", messages=[])
    assert len(seen) == 2


def test_success_verdict_reads_the_result_line():
    from v1compare.agent import reports_success

    assert reports_success("It worked; chip_002 did not need moving.\nRESULT: SUCCESS")
    assert not reports_success("Exfoliation done.\n**RESULT: FAILED**")
    assert not reports_success("The search failed.")  # no RESULT line: keyword guess


def test_system_prompt_parts_are_flattened_for_the_gateway():
    from v1compare.agent import flatten_system

    msgs = [{"role": "system", "content": [{"type": "text", "text": "a"},
                                           {"type": "text", "text": "b"}]},
            {"role": "user", "content": [{"type": "text", "text": "q"}]}]
    assert flatten_system(msgs) == [{"role": "system", "content": "a\nb"}, msgs[1]]


def test_v1_tool_calls_are_logged_with_depth(tmp_path, monkeypatch):
    smolagents = pytest.importorskip("smolagents")
    from v1compare.agent import log_tool_calls

    monkeypatch.setattr(smolagents.tools.Tool, "__call__", smolagents.tools.Tool.__call__)

    class Inner(smolagents.Tool):
        name, description, output_type = "inner", "i", "string"
        inputs = {"x": {"type": "string", "description": "x"}}

        def forward(self, x: str) -> str:
            return x * 2

    class Outer(smolagents.Tool):
        name, description, output_type = "outer", "o", "string"
        inputs = {"x": {"type": "string", "description": "x"}}

        def forward(self, x: str) -> str:
            return Inner()(x=x) + "!"

    log = tmp_path / "calls.jsonl"
    log_tool_calls(log)
    assert Outer()(x="a") == "aa!"
    recs = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert [(r["tool"], r["depth"], r["result"]) for r in recs] == [
        ("inner", 1, "aa"), ("outer", 0, "aa!")]


@pytest.mark.parametrize("fault,params,expected", [
    ("crash", {"after_calls": 1}, "SimulatedCrash"),
    ("serial_timeout", {"device": "arm_right", "count": 1}, "TransientError"),
])
def test_agent_mode_faults_fire_on_tool_worker_threads(fault, params, expected):
    # smolagents runs parallel tool calls on ThreadPoolExecutor threads, not
    # on the "job-..." thread the testbench counts device calls on
    import threading
    from types import SimpleNamespace

    from qumus2.testbench.faults import FaultInjector
    from v1compare.bench import wrap_fault_injector

    lab = SimpleNamespace(world=None, devices={})
    lab.faults = FaultInjector(lab)
    injected: list = []
    wrap_fault_injector(lab, [{"when": lambda w, d: True, "fault": fault, "params": params}],
                        injected, set(), whole_process=True)
    raised: list[str] = []

    def worker() -> None:
        for _ in range(3):
            try:
                lab.faults.before_call("arm_right")
            except BaseException as exc:  # noqa: BLE001 - SimulatedCrash is not an Exception
                raised.append(type(exc).__name__)

    th = threading.Thread(target=worker, name="ThreadPoolExecutor-0_0")
    th.start()
    th.join()
    assert injected and injected[0]["fault"] == fault
    assert expected in raised


def test_a_trial_never_overwrites_a_recording(tmp_path):
    from v1compare.agent import recording_dir

    assert recording_dir(tmp_path, "v2-baseline-seed1", "r1") == tmp_path / "v2-baseline-seed1"
    (tmp_path / "v2-baseline-seed1").mkdir()
    assert recording_dir(tmp_path, "v2-baseline-seed1", "r1") == tmp_path / "v2-baseline-seed1-r1"
    (tmp_path / "v2-baseline-seed1-r1").mkdir()
    assert recording_dir(tmp_path, "v2-baseline-seed1", "r1") == tmp_path / "v2-baseline-seed1-r1-2"
