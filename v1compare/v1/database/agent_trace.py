"""
database/agent_trace.py

Live instrumentation for smolagents agents.

Usage
-----
1. Wrap a run call to get structured database records:

    from database.agent_trace import traced_run

    result = traced_run(
        agent,
        task="search for graphene flakes",
        agent_name="qumus_agent",
        model_id="gpt-4o",
        session_entry_id=317,   # experiment_session.id — optional
        parent_run_id=None,     # set for nested sub-agent runs
    )

2. The step_callback is wired automatically for you inside traced_run.
   You do NOT need to modify step_callbacks on the agent object directly.

Callback contract (smolagents)
------------------------------
ActionStep fields used:
  - step_number      : int (1-based)
  - timing.total     : float seconds
  - token_usage      : TokenUsage | None  (.input_tokens, .output_tokens)
  - tool_calls       : list[ToolCall] | None  (.name, .arguments)
  - observations     : str | None  (tool response text)
  - error            : AgentError | None
  - is_final_answer  : bool
  - model_output     : str | None  (LLM thought / raw output)
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_json(obj: Any) -> Optional[dict]:
    """Try to return obj as a plain dict; return None if not possible."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        try:
            parsed = json.loads(obj)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _ms(seconds: Optional[float]) -> Optional[int]:
    return int(seconds * 1000) if seconds is not None else None


# ---------------------------------------------------------------------------
# Core: step_callback
# ---------------------------------------------------------------------------

def make_trace_callback(run_id: int):
    """
    Returns a smolagents step_callback(step, agent=...) that writes
    AgentStep + ToolCall + ToolCallResponse rows to experiment_base.db.

    run_id: the AgentRun.id this callback belongs to.
    """
    from database.db_utils import experiment_session_scope
    from database.models import AgentStep, ToolCall as DBToolCall, ToolCallResponse

    def _callback(step, agent=None):  # noqa: ANN001
        # Import here to avoid circular-import at module load time
        from smolagents.memory import ActionStep

        if not isinstance(step, ActionStep):
            # We only handle ActionStep (tool-call / error / final-answer steps)
            return

        try:
            # ---- resolve timing & tokens ----
            duration_sec = getattr(getattr(step, "timing", None), "total", None)
            token_usage = getattr(step, "token_usage", None)
            input_tokens = getattr(token_usage, "input_tokens", None)
            output_tokens = getattr(token_usage, "output_tokens", None)

            # ---- determine step_type ----
            if step.is_final_answer:
                step_type = "final_answer"
            elif step.error is not None and not step.tool_calls:
                step_type = "error"
            else:
                step_type = "tool_call"

            # ---- thought text ----
            thought = getattr(step, "model_output", None)
            if thought and isinstance(thought, list):
                # CodeAgent sometimes returns a list of dicts
                thought = str(thought)

            with experiment_session_scope() as session:
                # 1. Write AgentStep
                db_step = AgentStep(
                    run_id=run_id,
                    step_index=step.step_number - 1,  # convert to 0-based
                    agent_name=agent.name if agent else None,
                    thought=thought,
                    step_type=step_type,
                    llm_input_tokens=input_tokens,
                    llm_output_tokens=output_tokens,
                    duration_ms=_ms(duration_sec),
                    timestamp=_utcnow(),
                )
                session.add(db_step)
                session.flush()  # get db_step.id

                # 2. Write ToolCall + ToolCallResponse for each tool invoked
                if step.tool_calls:
                    for tc in step.tool_calls:
                        arguments = tc.arguments
                        if not isinstance(arguments, dict):
                            arguments = _safe_json(arguments) or {"raw": str(arguments)}

                        tc_status = "error" if step.error else "success"

                        db_tc = DBToolCall(
                            step_id=db_step.id,
                            run_id=run_id,
                            tool_name=tc.name,
                            arguments_json=arguments,
                            status=tc_status,
                            called_at=_utcnow(),
                        )
                        session.add(db_tc)
                        session.flush()

                        # Observation / error text
                        if step.error:
                            obs_text = f"ERROR: {step.error}"
                            err_text = str(step.error)
                        else:
                            obs_text = step.observations
                            err_text = None

                        db_resp = ToolCallResponse(
                            tool_call_id=db_tc.id,
                            output_text=obs_text,
                            output_json=_safe_json(obs_text),
                            error_message=err_text,
                            duration_ms=_ms(duration_sec),
                            returned_at=_utcnow(),
                        )
                        session.add(db_resp)

        except Exception as exc:  # never crash the agent
            logger.warning("agent_trace callback error: %s", exc, exc_info=True)

    return _callback


# ---------------------------------------------------------------------------
# Public API: traced_run
# ---------------------------------------------------------------------------

def traced_run(
    agent,
    task: str,
    *,
    agent_name: Optional[str] = None,
    model_id: Optional[str] = None,
    parent_run_id: Optional[int] = None,
    session_entry_id: Optional[int] = None,
    **run_kwargs,
) -> Any:
    """
    Wraps agent.run(task) with full AgentRun tracing.

    Creates an AgentRun record, registers a step_callback, calls agent.run(),
    closes the AgentRun record, and optionally links experiment_session.

    Returns the same value as agent.run().
    """
    from database.db_utils import experiment_session_scope
    from database.models import AgentRun, ExperimentSession

    resolved_name = agent_name or getattr(agent, "name", "unknown_agent")
    resolved_model = model_id or _infer_model_id(agent)

    # ---- open AgentRun ----
    with experiment_session_scope() as session:
        run = AgentRun(
            parent_run_id=parent_run_id,
            agent_name=resolved_name,
            model_id=resolved_model,
            task_prompt=task,
            status="running",
            started_at=_utcnow(),
        )
        session.add(run)
        session.flush()
        run_id = run.id

    # ---- register step_callback ----
    callback = make_trace_callback(run_id)
    from smolagents.memory import ActionStep
    agent.step_callbacks.register(ActionStep, callback)

    # ---- run ----
    final_result = None
    error_msg = None
    status = "success"
    try:
        final_result = agent.run(task, **run_kwargs)
    except Exception as exc:
        error_msg = str(exc)
        status = "error"
        raise
    finally:
        # ---- close AgentRun ----
        step_count = _count_action_steps(agent)
        with experiment_session_scope() as session:
            run = session.get(AgentRun, run_id)
            if run:
                run.final_result = str(final_result) if final_result is not None else None
                run.status = status
                run.error_message = error_msg
                run.total_steps = step_count
                run.completed_at = _utcnow()

            # Link experiment_session → agent_run if provided
            if session_entry_id is not None:
                es = session.get(ExperimentSession, session_entry_id)
                if es:
                    es.agent_run_id = run_id

        # Remove the callback so it doesn't fire on subsequent runs of the
        # same agent object (e.g. if the server reuses a long-lived agent)
        _deregister_callback(agent, ActionStep, callback)

    return final_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_model_id(agent) -> Optional[str]:
    try:
        return agent.model.model_id
    except AttributeError:
        return None


def _count_action_steps(agent) -> Optional[int]:
    try:
        from smolagents.memory import ActionStep
        return sum(1 for s in agent.memory.steps if isinstance(s, ActionStep))
    except Exception:
        return None


def _deregister_callback(agent, step_cls, callback):
    """Remove a previously registered callback from the agent's registry."""
    try:
        cbs = agent.step_callbacks._callbacks.get(step_cls, [])
        if callback in cbs:
            cbs.remove(callback)
    except Exception:
        pass
