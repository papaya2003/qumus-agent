"""Agent-mode trial: the whole system, with real model replies, on the testbench.

    python -m v1compare.agent --system v1 --scenario baseline --seed 1 \\
        --workdir runs/checks/v1compare/agent/v1_baseline_s1 --budget-usd 4

Both systems get the same task text and the same testbench (same seed, same
fault trigger). v1 runs its own orchestrator (smolagents ToolCallingAgent with
its five managed agents, create_qumus_agent) through LiteLLM; v2 runs its SDK
team (build_services(use_openai=True)) as the UI does. Scoring is the same as
in flows mode (v1compare/scoring.py), plus cost.

Real model calls cost money: run only on the Mac, after a cost estimate and
the owner's OK. The spend cap is enforced in code: v1 stops before the next
model call once its metered cost passes --budget-usd (metered from token
counts at list prices, an upper estimate); v2 runs under a run budget of
--budget-usd plus the OpenAI runtime's persistent project ledger. Either
can overshoot by the one call in flight.

Nobody answers questions during a trial. If a system stops to ask, the harness
replies once with NO_OPERATOR and counts one human intervention; a crash that
needs the task to be given again counts one too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from qumus2.config import load_yaml
from qumus2.testbench.faults import SimulatedCrash
from v1compare.scoring import score
from v1compare.trial import SCENARIOS, arm_scenario

TASK = ("Get me graphene flakes: exfoliate graphene (material code Gr) onto one bare chip, "
        "then search that chip under the microscope, record the flakes you find, and put "
        "the chip back in storage. Nobody is watching this run, so do not wait for "
        "confirmation. Finish with a short report that says plainly whether it worked, "
        "and make its last line exactly RESULT: SUCCESS or RESULT: FAILED.")
NO_OPERATOR = ("No operator is available. Decide yourself, finish the task, and report "
               "plainly whether it worked.")
RESTARTED = ("The system crashed during the previous attempt and has just been restarted. "
             "Check the state of the lab before acting. The task again: ")
MAX_OPERATOR_REPLIES = 2

#: USD per million input / output tokens (list prices; cached input counted at
#: the full rate, so the meter overestimates). Override with
#: QUMUS_V1_PRICE_PER_MTOK="in,out".
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0),
          "gpt-4o": (2.5, 10.0)}

ASKING = ("?", "please confirm", "would you like", "shall i", "let me know", "do you want",
          "should i", "awaiting", "waiting for your")
FAILING = ("fail", "could not", "couldn't", "unable", "❌", "error", "not complete",
           "incomplete", "did not", "didn't", "aborted", "stopped")


def asks_operator(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(k in t[-400:] for k in ASKING)


def reports_success(text: str) -> bool:
    """The system's own verdict: its RESULT line, else a keyword guess (any
    failure word counts as not claiming success)."""
    t = (text or "").lower()
    verdicts = re.findall(r"result:\s*\**\s*(success|failed)", t)
    if verdicts:
        return verdicts[-1] == "success"
    return bool(t) and not any(k in t for k in FAILING)


def flatten_system(messages: list[Any]) -> list[Any]:
    """smolagents sends every message's content as a list of text parts. Turn
    a system message made only of text parts into one string, the form every
    gateway route passes on (see gateway_probe: a route may drop the parts)."""
    out = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else None
        role = getattr(role, "value", role)
        c = m.get("content") if isinstance(m, dict) else None
        if role == "system" and isinstance(c, list) and all(
                isinstance(p, dict) and p.get("type") == "text" for p in c):
            m = {**m, "role": "system", "content": "\n".join(p.get("text", "") for p in c)}
        out.append(m)
    return out


class BudgetExceeded(BaseException):  # noqa: N818 - must get through `except Exception`
    pass


class CostMeter:
    """Counts LiteLLM usage for every model call v1 makes and stops the run
    before the next call once the cap is passed."""

    def __init__(self, model: str, cap_usd: float,
                 extra_headers: dict[str, str] | None = None) -> None:
        self.extra_headers = extra_headers or {}
        # The gateway refuses a forced tool choice for Claude Opus 5.5
        # ("tool_choice: type tool and any are not supported for this model"),
        # and smolagents' ToolCallingAgent forces one on every call. Relax it to
        # "auto"; a reply without a tool call then goes through smolagents' own
        # parse-and-retry path. QUMUS_V1_FORCE_TOOL_CHOICE=1 keeps it forced.
        self.relax_tool_choice = os.environ.get("QUMUS_V1_FORCE_TOOL_CHOICE", "0") != "1"
        self.relaxed = 0
        env = os.environ.get("QUMUS_V1_PRICE_PER_MTOK")
        if env:
            self.price = tuple(float(x) for x in env.split(","))
        else:
            self.price = next((p for k, p in PRICES.items() if k in model), PRICES["claude-opus-5-5"])
        self.cap, self.usd, self.calls = cap_usd, 0.0, 0
        self.tokens_in = self.tokens_out = 0
        self._lock = threading.Lock()

    def install(self) -> None:
        import litellm

        real = litellm.completion

        def completion(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                if self.usd >= self.cap:
                    raise BudgetExceeded(f"v1 spend cap ${self.cap:.2f} reached "
                                         f"(metered ${self.usd:.2f})")
            msgs = kwargs.get("messages")
            if msgs:
                kwargs["messages"] = flatten_system(msgs)
            tc = kwargs.get("tool_choice")
            if self.relax_tool_choice and tc not in (None, "auto", "none"):
                kwargs["tool_choice"] = "auto"
                self.relaxed += 1
            if self.extra_headers:
                kwargs["extra_headers"] = {**(kwargs.get("extra_headers") or {}),
                                           **self.extra_headers}
            r = real(*args, **kwargs)
            u = getattr(r, "usage", None)
            if u is not None:
                with self._lock:
                    self.calls += 1
                    self.tokens_in += int(getattr(u, "prompt_tokens", 0) or 0)
                    self.tokens_out += int(getattr(u, "completion_tokens", 0) or 0)
                    self.usd = (self.tokens_in * self.price[0]
                                + self.tokens_out * self.price[1]) / 1e6
            return r

        litellm.completion = completion


# ----- v1 -------------------------------------------------------------------------

def v1_model_env(model: str, route: str = "openai") -> tuple[str, dict[str, str]]:
    """Point v1's own LLM settings (common/llm_config.py) at the lab gateway.
    Returns the LiteLLM model id and extra headers for every call.

    route "openai": the gateway's OpenAI-compatible endpoint (Bearer auth, as
    LiteLLM's OpenAI route sends it). route "anthropic": the Messages endpoint;
    the gateway takes Bearer auth only, so the header is added to each call.
    Check which one works with `python -m v1compare.gateway_probe`."""
    key = os.environ["QUMUS_ANTHROPIC_API_KEY"]
    base = os.environ["QUMUS_ANTHROPIC_BASE_URL"].rstrip("/")
    prefix = os.environ.get("QUMUS_ANTHROPIC_MODEL_PREFIX", "")
    headers: dict[str, str] = {}
    if model.startswith("gpt"):
        mid, api_base = f"openai/{prefix.replace('anthropic/', 'openai/')}{model}", base + "/v1"
    elif route == "openai":
        mid, api_base = f"openai/{prefix}{model}", base + "/v1"
    else:
        mid, api_base = f"anthropic/{prefix}{model}", base
        headers = {"Authorization": f"Bearer {key}"}
    os.environ["QUMUS_API_KEY"] = key
    os.environ["QUMUS_API_BASE"] = api_base
    os.environ["QUMUS_DEFAULT_MODEL_ID"] = mid
    return mid, headers


def log_tool_calls(path: Path, clock: Any = None) -> None:
    """Append every v1 tool call (any agent, any thread) to a JSONL file: name,
    arguments, the head of the result or the error, and the nesting depth (a
    tool such as execute_processing_task runs an agent that calls tools). v1
    runs its managed agents at verbosity 0, so their calls are otherwise
    invisible."""
    from smolagents.tools import Tool

    orig, depth = Tool.__call__, threading.local()
    path.parent.mkdir(parents=True, exist_ok=True)

    def short(v: Any, n: int) -> str:
        t = v if isinstance(v, str) else json.dumps(v, default=str)
        return t if len(t) <= n else t[:n] + f"... [{len(t)} chars]"

    def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        d = getattr(depth, "n", 0)
        depth.n = d + 1
        rec: dict[str, Any] = {"thread": threading.current_thread().name, "depth": d,
                               "tool": self.name, "args": short(kwargs or list(args), 2000)}
        if clock is not None:
            rec["sim_t"] = round(clock.now(), 1)
        try:
            out = orig(self, *args, **kwargs)
            rec["result"] = short(out, 3000)
            return out
        except BaseException as exc:
            rec["error"] = short(f"{type(exc).__name__}: {exc}", 3000)
            raise
        finally:
            depth.n = d
            with path.open("a") as f:
                f.write(json.dumps(rec) + "\n")

    Tool.__call__ = call


def run_v1_agent(sc: Any, seed: int, workdir: Path, cfg: dict[str, Any], model: str,
                 budget: float, log: list[str]) -> dict[str, Any]:
    from v1compare.bench import V1Bench

    mid, headers = v1_model_env(model, os.environ.get("QUMUS_V1_ROUTE", "openai"))
    bench = V1Bench(workdir / "v1", seed=seed, testbench_config=cfg, build_tools=False,
                    any_thread_is_job=True)
    arm_scenario(bench.triggers, sc)
    meter = CostMeter(model, budget, headers)
    meter.install()
    log_tool_calls(workdir / "v1_tool_calls.jsonl", bench.clock)
    from agents.qumus_agent.qumus_agent import create_qumus_agent

    t0 = bench.clock.now()
    interventions, crashes, answer, stopped = 0, 0, "", None
    agent = create_qumus_agent(model_id=mid, max_steps=80, verbosity_level=1)
    bench._patch_time()
    prompt, reset, replies = TASK, True, 0
    while True:
        box: dict[str, Any] = {}

        def target(p: str = prompt, r: bool = reset, a: Any = agent,
                   out: dict[str, Any] = box) -> None:
            try:
                out["answer"] = a.run(p, reset=r)
            except BaseException as exc:  # noqa: BLE001 - reported below
                out["error"] = exc

        th = threading.Thread(target=target, name="job-v1-agent")
        th.start()
        th.join()
        err = box.get("error")
        if isinstance(err, SimulatedCrash) and crashes == 0:
            crashes += 1
            interventions += 1  # an operator restarts v1 and gives the task again
            log.append(f"[v1 agent] CRASH: {err}")
            bench._dead_threads.clear()
            agent = create_qumus_agent(model_id=mid, max_steps=80, verbosity_level=1)
            bench._patch_time()
            prompt, reset = RESTARTED + TASK, True
            continue
        if err is not None:
            stopped = f"{type(err).__name__}: {err}"
            log.append(f"[v1 agent] stopped: {stopped}")
            log.append("".join(traceback.format_exception(err))[-3000:])
            break
        answer = str(box.get("answer"))
        log.append(f"[v1 agent] answer: {answer}")
        if asks_operator(answer) and replies < MAX_OPERATOR_REPLIES:
            replies += 1
            interventions += 1
            prompt, reset = NO_OPERATOR, False
            continue
        break

    from v1compare.trial import _v1_records

    records, ledger = _v1_records(bench)
    claimed = stopped is None and reports_success(answer)
    s = score("v1", bench.lab.world, claimed_success=claimed, records=records, ledger=ledger)
    return {"score": s.to_dict(), "interventions": interventions, "crashes": crashes,
            "sim_seconds": round(bench.clock.now() - t0, 1),
            "device_commands": len(bench.journal.commands),
            "incidents": bench.journal.incidents[:50], "injected": bench.injected,
            "cost_usd": round(meter.usd, 3), "model_calls": meter.calls,
            "tokens": [meter.tokens_in, meter.tokens_out], "model": mid,
            "tool_choice_relaxed": meter.relaxed,
            "final_answer": answer[-4000:], "stopped": stopped}


# ----- v2 -------------------------------------------------------------------------

def run_v2_agent(sc: Any, seed: int, workdir: Path, cfg: dict[str, Any], model: str,
                 budget: float, log: list[str]) -> dict[str, Any]:
    from qumus2.app import build_services
    from qumus2.ui.recording import attach_recorder
    from v1compare.bench import endless_tape, wrap_fault_injector
    from v1compare.trial import _v2_records

    workdir.mkdir(parents=True, exist_ok=True)
    s = build_services(workdir / "qumus.db", use_openai=True, seed=seed, testbench_config=cfg,
                       run_budget_usd=budget)
    endless_tape(s.lab.world)
    # read-only: bench state and real times, so the trial can be replayed in the UI;
    # best effort, never fails the trial
    rec_info: dict[str, Any] = {}
    try:
        recorder = attach_recorder(s)
    except Exception as exc:  # noqa: BLE001
        recorder, rec_info["recording_error"] = None, f"{type(exc).__name__}: {exc}"
    triggers: list[dict[str, Any]] = []

    class _Announced(list):  # the injected fault also shows in the run's timeline
        def append(self, item: dict[str, Any]) -> None:
            super().append(item)
            try:
                s.store.emit("fault.injected", {"fault": item["fault"],
                                                "params": item["params"]})
            except Exception:  # noqa: BLE001 - display only, never fail the trial
                pass

    injected: list[dict[str, Any]] = _Announced()
    wrap_fault_injector(s.lab, triggers, injected, set())
    arm_scenario(triggers, sc)
    t0 = s.clock.now()

    def evs(kind: str | None = None) -> list[Any]:
        return [e for e in s.store.events_after(0, limit=1_000_000)
                if kind is None or e.kind == kind]

    def last_answer() -> str:
        msgs = [e.payload.get("text", "") for e in evs("agent.message")
                if e.agent == "orchestrator"]
        return msgs[-1] if msgs else ""

    async def drive() -> tuple[int, str | None]:
        interventions, stopped = 0, None
        s.store.add_instruction(TASK, project_id=s.project_id)
        for _ in range(1 + MAX_OPERATOR_REPLIES):
            try:
                await s.driver.wait_idle(timeout=3 * 3600)
            except Exception as exc:  # noqa: BLE001 - timeout or runtime error
                stopped = f"{type(exc).__name__}: {exc}"
                break
            if not asks_operator(last_answer()):
                break
            interventions += 1
            s.store.add_instruction(NO_OPERATOR, project_id=s.project_id)
        await s.runtime.close()
        return interventions, stopped

    interventions, stopped = asyncio.run(drive())
    if recorder:
        rec_info = _export_v2_recording(s, recorder, workdir, sc, seed, log)
    answer = last_answer()
    log.append(f"[v2 agent] answer: {answer}")
    cost = s.store.spent_usd()
    records, ledger = _v2_records(s)
    claimed = stopped is None and reports_success(answer)
    sc_ = score("v2", s.lab.world, claimed_success=claimed, records=records, ledger=ledger)
    return {"score": sc_.to_dict(), "interventions": interventions,
            "crashes": sum(1 for i in injected if i["fault"] == "crash"),
            "sim_seconds": round(s.clock.now() - t0, 1),
            "device_commands": len(evs("device.command")), "incidents": [],
            "injected": injected, "cost_usd": round(cost, 3), "model": s.runtime.creds.model_id,
            "final_answer": answer[-4000:], "stopped": stopped, **rec_info}


def recording_dir(root: Path, base: str, tag: str) -> Path:
    """root/base if free, else root/base-tag, else root/base-tag-2, ..."""
    out, n = root / base, 1
    while out.exists():
        n += 1
        out = root / (f"{base}-{tag}" if n == 2 else f"{base}-{tag}-{n - 1}")
    return out


def _export_v2_recording(s: Any, recorder: Any, workdir: Path, sc: Any, seed: int,
                         log: list[str]) -> dict[str, Any]:
    """recordings/v2-<scenario>-seed<N>/ in the repo, shown in History.
    An existing recording (maybe a committed one) is never overwritten: the new
    one gets the workdir's name as a suffix. Returns {"recording": path} or
    {"recording_error": why}."""
    from qumus2.ui.recording import export_recording

    name = next((k for k, v in SCENARIOS.items() if v is sc), "trial")
    out = recording_dir(Path(__file__).resolve().parent.parent / "recordings",
                        f"v2-{name}-seed{seed}", workdir.name)
    try:
        recorder.stop()
        export_recording(workdir / "qumus.db", out, layout=s.lab.bench,
                         title=f"Get graphene flakes: {sc.title or name} (OpenAI Agents SDK)",
                         description=f"OpenAI Agents SDK on the testbench (v1/v2 comparison trial, "
                                     f"scenario {name}): {sc.description}",
                         meta={"request": TASK, "seed": seed, "scenario": name,
                               "model": s.runtime.creds.model_id})
        log.append(f"[v2 agent] recording: {out}")
        return {"recording": str(out)}
    except Exception as exc:  # noqa: BLE001 - a recording must never fail the trial
        log.append(f"[v2 agent] recording not exported: {exc}")
        return {"recording_error": f"{type(exc).__name__}: {exc}"}


# ----- entry ----------------------------------------------------------------------

def run_agent_trial(system: str, scenario: str, seed: int, workdir: Path, model: str,
                    budget: float) -> dict[str, Any]:
    sc = SCENARIOS[scenario]
    cfg = load_yaml("testbench.yaml")
    log: list[str] = []
    workdir.mkdir(parents=True, exist_ok=True)
    w0 = time.monotonic()
    result: dict[str, Any] = {"system": system, "scenario": scenario, "seed": seed,
                              "mode": "agent"}
    try:
        runner = run_v1_agent if system == "v1" else run_v2_agent
        result.update(runner(sc, seed, workdir, cfg, model, budget, log))
    except BaseException as exc:  # noqa: BLE001 - the harness itself failed: record it
        result["harness_error"] = "".join(traceback.format_exception(exc))[-4000:]
    result["wall_seconds"] = round(time.monotonic() - w0, 1)
    (workdir / f"{system}_agent_log.txt").write_text("\n".join(log), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--system", choices=("v1", "v2"), required=True)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="baseline")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--model", default=None,
                    help="v1: claude-opus-5-5 (default) or gpt-4o; v2 uses QUMUS_OPENAI_MODEL and its configured token rates")
    ap.add_argument("--budget-usd", type=float, default=4.0)
    ap.add_argument("--out", help="write the result JSON here (default: stdout)")
    a = ap.parse_args(argv)
    if a.system == "v1":
        if not os.environ.get("QUMUS_ANTHROPIC_API_KEY"):
            print("QUMUS_ANTHROPIC_API_KEY is not set: v1 agent mode needs its gateway key.")
            return 2
        model = a.model or "claude-opus-5-5"
    else:
        from qumus2.agents.openai_credentials import load_openai_credentials

        creds = load_openai_credentials()
        if creds is None:
            print("QUMUS_OPENAI_API_KEY is not set: configure the v2 model and token rates.")
            return 2
        if a.model and a.model != creds.model_id:
            ap.error("Configure QUMUS_OPENAI_MODEL and its token rates together for v2.")
        model = creds.model_id
    out = Path(a.out).resolve() if a.out else None  # v1's bench chdirs into the workdir
    res = run_agent_trial(a.system, a.scenario, a.seed, Path(a.workdir).resolve(), model,
                          a.budget_usd)
    text = json.dumps(res, indent=2, default=str)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
