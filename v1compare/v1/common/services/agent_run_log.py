import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from smolagents import ActionStep


class AgentRunLogger:
    """
    Unified management of agent logs and trajectory exports.

    Directory layout (created at runtime):
    logs/agent_log/
      ├── run.jsonl                 # unified streaming log
      └── prev_trajs_sessions/      # per-run temp files before flush
    """

    def __init__(self):
        # base project dir: .../Qumus
        base = Path(__file__).resolve().parents[3]
        self.logs_root = os.path.join(base, "logs", "agent_log")
        self.sessions_dir = os.path.join(self.logs_root, "prev_trajs_sessions")
        for d in [self.logs_root, self.sessions_dir]:
            os.makedirs(d, exist_ok=True)

        self.prev_trajs_path = os.path.join(self.logs_root, "prev_trajs.jsonl")
        self.complete_trajs_path = os.path.join(self.logs_root, "complete_trajs.jsonl")
        self.error_corr_path = os.path.join(self.logs_root, "error_corrections.jsonl")
        self.run_log_path = os.path.join(self.logs_root, "run.jsonl")

    # ---------- database ----------
    def append_complete_traj(self, question: str, prev_traj: dict[str, Any], result: dict[str, Any] | str) -> None:
        """Export a full trajectory in one shot → write to complete_trajs.jsonl"""
        record = {
            "ts": time.time(),
            "question": question,
            "traj": prev_traj,
            "result": result,
        }
        self._append_jsonl(self.complete_trajs_path, record)

    def append_complete_traj_from_memory(self, agent, question: str, result: dict[str, Any] | str) -> None:
        """Read each step from agent.memory and write to complete_trajs.jsonl (one shot)."""
        steps: list[dict[str, Any]] = []
        try:
            for step in getattr(agent.memory, "steps", []) or []:
                if isinstance(step, ActionStep):
                    # Safely serialize action_output
                    action_out = getattr(step, "action_output", None)
                    try:
                        json.dumps(action_out)
                    except TypeError:
                        action_out = str(action_out)

                    # Safely serialize observations
                    obs = getattr(step, "observations", None)
                    try:
                        json.dumps(obs)
                    except TypeError:
                        obs = str(obs)

                    entry: dict[str, Any] = {
                        "step": getattr(step, "step_number", None),
                        "observations": obs,
                        "action_output": action_out,
                        "error": str(getattr(step, "error", "")) or None,
                    }
                    calls = []
                    for tc in getattr(step, "tool_calls", []) or []:
                        name = getattr(tc, "name", None)
                        args = getattr(tc, "arguments", None)
                        try:
                            json.dumps(args)
                            calls.append({"name": name, "arguments": args})
                        except Exception:
                            calls.append({"name": name, "arguments": str(args)})
                    if calls:
                        entry["tool_calls"] = calls
                    steps.append(entry)
        except Exception:
            # Even if a single step fails to serialize, do not block overall write
            pass

        prev_traj = {"steps": steps}
        self.append_complete_traj(question=question, prev_traj=prev_traj, result=result)

    # ---------- incremental session recording ----------
    def start_prev_traj_session(self, question: str) -> str:
        session_id = f"sess_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        path = os.path.join(self.sessions_dir, f"{session_id}.jsonl")
        meta = {"ts": time.time(), "type": "meta", "question": question, "session_id": session_id}
        self._append_jsonl(path, meta)
        # Also write a meta line to prev_trajs.jsonl (for incremental tracking)
        self._append_jsonl(self.prev_trajs_path, {"ts": time.time(), "type": "meta", "question": question, "session_id": session_id})
        return session_id

    def append_prev_traj_step(self, session_id: str, step_entry: dict[str, Any]) -> None:
        path = os.path.join(self.sessions_dir, f"{session_id}.jsonl")
        rec = {"ts": time.time(), "type": "step", **step_entry}
        self._append_jsonl(path, rec)
        # Also append incremental steps to prev_trajs.jsonl
        inc = {"ts": time.time(), "type": "step", "session_id": session_id, **step_entry}
        self._append_jsonl(self.prev_trajs_path, inc)

    def finalize_prev_traj_session(self, session_id: str, result: dict[str, Any] | str) -> None:
        path = os.path.join(self.sessions_dir, f"{session_id}.jsonl")
        steps: list[dict[str, Any]] = []
        question = ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") == "meta":
                        question = obj.get("question", "")
                    elif obj.get("type") == "step":
                        # Strip type and ts; keep core content
                        obj.pop("type", None)
                        obj.pop("ts", None)
                        steps.append(obj)
        except FileNotFoundError:
            pass
        # Output full trajectory to complete_trajs.jsonl
        self.append_complete_traj(question=question, prev_traj={"steps": steps}, result=result)

    def append_error_correction(self, rule: str, data: dict[str, Any] | None = None) -> None:
        record = {
            "ts": time.time(),
            "rule": rule,
            "data": data or {},
        }
        self._append_jsonl(self.error_corr_path, record)

    # ---------- logs ----------
    def append_run_log(self, event: str | None = None, payload: dict[str, Any] | None = None) -> None:
        record = {"ts": time.time()}
        if event is not None:
            record["event"] = event
        if payload:
            record.update(payload)
        self._append_jsonl(self.run_log_path, record)

    # ---------- utils ----------
    @staticmethod
    def _append_jsonl(path: str, obj: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def get_paths(self) -> dict[str, str]:
        return {
            "agent_log_root": self.logs_root,
            "prev_trajs": self.prev_trajs_path,
            "complete_trajs": self.complete_trajs_path,
            "error_corrections": self.error_corr_path,
            "run_log": self.run_log_path,
        }


class TrajectoryRecorder:
    """
    Collect per-step actions/observations from agent step callbacks and write to prev_trajs.jsonl when done.
    Usage:
      rec = get_trajectory_recorder(); rec.start(question)
      agent.step_callbacks.append(logger.step_callback)
      ... after task completion ...
      logger.flush(result={"final_answer": answer})
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.question: str | None = None
        self.steps: list[dict[str, Any]] = []
        self.start_ts: float | None = None

    def start(self, question: str) -> None:
        self.reset()
        self.question = question
        self.start_ts = time.time()

    def step_callback(self, memory_step, agent=None):
        try:
            # Safely serialize action_output
            action_out = getattr(memory_step, "action_output", None)
            try:
                json.dumps(action_out)
            except TypeError:
                action_out = str(action_out)

            # Safely serialize observations
            obs = getattr(memory_step, "observations", None)
            try:
                json.dumps(obs)
            except TypeError:
                obs = str(obs)

            entry: dict[str, Any] = {
                "step": getattr(memory_step, "step_number", None),
                "observations": obs,
                "action_output": action_out,
                "error": str(getattr(memory_step, "error", "")) or None,
            }
            calls = []
            for tc in getattr(memory_step, "tool_calls", []) or []:
                name = getattr(tc, "name", None)
                args = getattr(tc, "arguments", None)
                try:
                    json.dumps(args)
                    calls.append({"name": name, "arguments": args})
                except Exception:
                    calls.append({"name": name, "arguments": str(args)})
            if calls:
                entry["tool_calls"] = calls
            self.steps.append(entry)
        except Exception:
            # Do not block main flow
            pass

    def flush(self, result: dict[str, Any] | str | None = None) -> None:
        try:
            ds = AgentRunLogger()
            ds.append_complete_traj(
                question=self.question or "",
                prev_traj={"steps": self.steps},
                result=result if result is not None else {},
            )
        finally:
            self.reset()


_global_recorder: TrajectoryRecorder | None = None


def get_trajectory_recorder() -> TrajectoryRecorder:
    global _global_recorder
    if _global_recorder is None:
        _global_recorder = TrajectoryRecorder()
    return _global_recorder
