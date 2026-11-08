from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import text
from smolagents import Tool

from common.llm_config import DEFAULT_MODEL_ID, get_api_base_for_model, get_api_key_for_model, resolve_model_id
from database.db_utils import experiment_session_scope


def _litellm_completion(**kwargs):
    import litellm

    litellm.drop_params = True
    return litellm.completion(**kwargs)


class ExperimentQueryTool(Tool):
    name = "query_experiments"
    description = (
        "Query the experiment history database to find past runs, their outcomes, "
        "parameters used, and success/failure status. Use this to inform decisions about "
        "what to try next based on what has worked or failed before."
    )
    inputs = {
        "question": {"type": "string", "description": "Natural language question about past experiments or runs."},
    }
    output_type = "string"
    _FORBIDDEN_SQL = re.compile(
        r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|vacuum|pragma)\b",
        flags=re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        model_id: Optional[str] = None,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._model = resolve_model_id(model_id or DEFAULT_MODEL_ID)
        self._api_key = api_key or get_api_key_for_model(self._model)
        self._api_base = api_base if api_base is not None else get_api_base_for_model(self._model)
        if not self._api_key:
            raise RuntimeError("QUMUS_API_KEY not set.")

    def _completion_text(self, *, temperature: float, messages: list[dict[str, str]]) -> str:
        kwargs: dict[str, object] = {
            "model": self._model,
            "api_key": self._api_key,
            "messages": messages,
        }
        if not self._temperature_is_deprecated():
            kwargs["temperature"] = temperature
        if self._api_base:
            kwargs["api_base"] = self._api_base
        resp = _litellm_completion(**kwargs)
        return (resp.choices[0].message.content or "").strip()

    def _temperature_is_deprecated(self) -> bool:
        model = self._model.lower()
        return model.startswith("anthropic/claude-") and re.search(r"-5(?:$|-)", model) is not None

    def forward(self, question: str) -> str:
        schema = self._introspect_schema()
        schema_mode = self._detect_schema_mode(schema)
        schema_text = self._render_schema(schema)

        sql = self._template_sql(question, schema_mode, schema)
        if not sql:
            sql = self._generate_sql(question, schema_text)

        if not sql:
            return json.dumps(
                {
                    "error": "Could not generate a valid SQL query.",
                    "question": question,
                    "schema_mode": schema_mode,
                    "available_tables": sorted(schema.keys()),
                }
            )

        try:
            safe_sql = self._sanitize_sql(sql, schema)
            rows = self._run_sql(safe_sql)
        except Exception as exc:
            return json.dumps(
                {
                    "error": f"SQL execution failed: {exc}",
                    "question": question,
                    "sql": sql,
                    "schema_mode": schema_mode,
                    "available_tables": sorted(schema.keys()),
                },
                ensure_ascii=False,
                default=str,
            )

        answer = self._summarize(question, safe_sql, rows, schema_mode)
        return json.dumps({
            "question": question,
            "schema_mode": schema_mode,
            "sql": safe_sql,
            "rows": rows[:20],
            "total_rows": len(rows),
            "answer": answer,
        }, ensure_ascii=False, default=str)

    def _introspect_schema(self) -> dict[str, list[str]]:
        with experiment_session_scope() as session:
            table_rows = session.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                )
            ).all()

            schema: dict[str, list[str]] = {}
            for (table_name,) in table_rows:
                pragma_rows = session.execute(text(f'PRAGMA table_info("{table_name}")')).all()
                columns = [str(row[1]) for row in pragma_rows]
                schema[str(table_name)] = columns
            return schema

    def _detect_schema_mode(self, schema: dict[str, list[str]]) -> str:
        tables = set(schema.keys())
        if {"experiment_workflow", "experiment_run", "experiment_message"}.issubset(tables):
            return "workflow"
        if {"agent_run", "agent_step", "tool_call"}.issubset(tables):
            return "trace"
        return "unknown"

    def _render_schema(self, schema: dict[str, list[str]]) -> str:
        lines: list[str] = []
        for table in sorted(schema.keys()):
            cols = ", ".join(schema[table])
            lines.append(f"{table}({cols})")
        return "\n".join(lines)

    def _generate_sql(self, question: str, schema_text: str) -> Optional[str]:
        sql = self._completion_text(
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You translate natural language questions into SQLite SELECT queries "
                        "over the experiment history schema below. "
                        "Rules: (1) only SELECT/CTE queries; (2) prefer explicit LIMIT <= 200; "
                        "(3) use only listed tables/columns; (4) no markdown or explanation. "
                        "Return ONLY the raw SQL string, no markdown, no explanation.\n\n"
                        + schema_text
                    ),
                },
                {"role": "user", "content": question},
            ],
        ).strip("`")
        # Strip markdown code fence if present
        if sql.lower().startswith("sql"):
            sql = sql[3:].strip()
        return sql

    def _has_col(self, schema: dict[str, list[str]], table: str, col: str) -> bool:
        return col in schema.get(table, [])

    def _model_filter_clause(self, question: str, schema_mode: str, schema: dict[str, list[str]]) -> Optional[str]:
        q = question.lower()
        if schema_mode == "trace":
            if not self._has_col(schema, "agent_run", "model_id"):
                return None
            model_col = "lower(model_id)"
        elif schema_mode == "workflow":
            if not self._has_col(schema, "experiment_run", "model_id"):
                return None
            model_col = "lower(model_id)"
        else:
            return None

        # Common family aliases; can be extended.
        families = {
            "qwen": ["qwen"],
            "gpt": ["gpt", "openai"],
            "claude": ["claude", "anthropic"],
            "gemini": ["gemini", "google"],
            "deepseek": ["deepseek"],
            "grok": ["grok", "xai"],
        }
        for family, needles in families.items():
            if family in q:
                like_parts = [f"{model_col} LIKE '%{n}%'" for n in needles]
                return "(" + " OR ".join(like_parts) + ")"
        return None

    def _template_sql(self, question: str, schema_mode: str, schema: dict[str, list[str]]) -> Optional[str]:
        q = question.lower()
        asks_params = any(word in q for word in ("parameter", "parameters", "temperature", "voltage", "hold_v", "target_t"))
        asks_exfoliation = any(word in q for word in ("exfoliat", "graphene", "graphite", "gr", "hbn", "rese2"))

        if asks_params and asks_exfoliation:
            if schema_mode == "trace" and "agent_run" in schema:
                conditions = ["lower(task_prompt) LIKE '%exfoliat%'"]
                if any(word in q for word in ("graphene", "graphite", "gr")):
                    conditions.append("(lower(task_prompt) LIKE '%graphene%' OR lower(task_prompt) LIKE '%graphite%' OR task_prompt LIKE '%''Gr''%')")
                elif "hbn" in q or "boron nitride" in q:
                    conditions.append("(lower(task_prompt) LIKE '%hbn%' OR task_prompt LIKE '%''hBN''%')")
                elif "rese2" in q:
                    conditions.append("(lower(task_prompt) LIKE '%rese2%' OR task_prompt LIKE '%''ReSe2''%')")

                where_parts: list[str] = []
                if self._has_col(schema, "agent_run", "parent_run_id"):
                    where_parts.append("parent_run_id IS NULL")
                where_parts.extend(conditions)
                where_clause = f" WHERE {' AND '.join(where_parts)}"

                select_cols: list[str] = ["id"]
                for c in ("agent_name", "status", "task_prompt", "final_result", "started_at"):
                    if self._has_col(schema, "agent_run", c):
                        select_cols.append(c)
                order_col = "started_at" if self._has_col(schema, "agent_run", "started_at") else "id"
                return (
                    f"SELECT {', '.join(select_cols)} FROM agent_run"
                    f"{where_clause} ORDER BY {order_col} DESC LIMIT 50"
                )

            if schema_mode == "workflow" and "experiment_run" in schema:
                select_cols = ["id"]
                for c in ("run_type", "status", "parameters", "result", "result_summary", "started_at"):
                    if self._has_col(schema, "experiment_run", c):
                        select_cols.append(c)
                where_clause = ""
                if self._has_col(schema, "experiment_run", "run_type"):
                    where_clause = " WHERE lower(run_type) LIKE '%exfoliat%'"
                order_col = "started_at" if self._has_col(schema, "experiment_run", "started_at") else "id"
                return (
                    f"SELECT {', '.join(select_cols)} FROM experiment_run"
                    f"{where_clause} ORDER BY {order_col} DESC LIMIT 50"
                )

        if any(phrase in q for phrase in ("how many experiments", "number of experiments", "total experiments", "experiment count")):
            if schema_mode == "workflow" and "experiment_run" in schema:
                where_parts: list[str] = []
                model_filter = self._model_filter_clause(question, schema_mode, schema)
                if model_filter:
                    where_parts.append(model_filter)
                where_clause = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
                return f"SELECT COUNT(*) AS total_experiments FROM experiment_run{where_clause}"
            if schema_mode == "trace" and "agent_run" in schema:
                where_parts: list[str] = []
                if self._has_col(schema, "agent_run", "parent_run_id"):
                    where_parts.append("parent_run_id IS NULL")
                model_filter = self._model_filter_clause(question, schema_mode, schema)
                if model_filter:
                    where_parts.append(model_filter)
                where_clause = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
                return f"SELECT COUNT(*) AS total_experiments FROM agent_run{where_clause}"
            if "experiment_session" in schema:
                return "SELECT COUNT(*) AS total_experiments FROM experiment_session"

        if schema_mode == "trace":
            if (
                any(word in q for word in ("latest", "recent", "last"))
                and "experiment_session" in schema
                and "created_at" in schema["experiment_session"]
            ):
                cols = ["es.id", "es.created_at"]
                if self._has_col(schema, "experiment_session", "prompt"):
                    cols.append("es.prompt")
                if self._has_col(schema, "experiment_session", "final_text"):
                    cols.append("es.final_text")
                if "agent_run" in schema and self._has_col(schema, "experiment_session", "agent_run_id"):
                    if self._has_col(schema, "agent_run", "status"):
                        cols.append("ar.status")
                    if self._has_col(schema, "agent_run", "agent_name"):
                        cols.append("ar.agent_name")
                    return (
                        f"SELECT {', '.join(cols)} "
                        "FROM experiment_session es "
                        "LEFT JOIN agent_run ar ON ar.id = es.agent_run_id "
                        "ORDER BY es.created_at DESC LIMIT 20"
                    )
                return (
                    f"SELECT {', '.join(cols)} "
                    "FROM experiment_session es ORDER BY es.created_at DESC LIMIT 20"
                )

            if any(word in q for word in ("failure", "failed", "error")) and "agent_run" in schema:
                cols = ["id"]
                for c in ("agent_name", "status", "error_message", "started_at", "completed_at"):
                    if self._has_col(schema, "agent_run", c):
                        cols.append(c)
                status_pred = "lower(status) = 'error'" if self._has_col(schema, "agent_run", "status") else "0=1"
                error_pred = "error_message IS NOT NULL" if self._has_col(schema, "agent_run", "error_message") else "0=1"
                order_col = "started_at" if self._has_col(schema, "agent_run", "started_at") else "id"
                return (
                    f"SELECT {', '.join(cols)} FROM agent_run "
                    f"WHERE {status_pred} OR {error_pred} "
                    f"ORDER BY {order_col} DESC LIMIT 50"
                )

            if (
                any(phrase in q for phrase in ("success rate", "how often", "failure rate"))
                and "agent_run" in schema
                and self._has_col(schema, "agent_run", "agent_name")
                and self._has_col(schema, "agent_run", "status")
            ):
                where_clause = "WHERE parent_run_id IS NULL " if self._has_col(schema, "agent_run", "parent_run_id") else ""
                return (
                    "SELECT agent_name, COUNT(*) AS total_runs, "
                    "SUM(CASE WHEN lower(status) = 'success' THEN 1 ELSE 0 END) AS success_runs, "
                    "ROUND(100.0 * SUM(CASE WHEN lower(status) = 'success' THEN 1 ELSE 0 END) / COUNT(*), 2) AS success_rate_pct "
                    "FROM agent_run "
                    f"{where_clause}"
                    "GROUP BY agent_name "
                    "ORDER BY total_runs DESC LIMIT 20"
                )

        if schema_mode == "workflow":
            if any(word in q for word in ("latest", "recent", "last")) and "experiment_run" in schema:
                cols = ["id"]
                for c in ("session_id", "workflow_id", "run_type", "status", "result_summary", "started_at", "completed_at"):
                    if self._has_col(schema, "experiment_run", c):
                        cols.append(c)
                order_col = "started_at" if self._has_col(schema, "experiment_run", "started_at") else "id"
                return f"SELECT {', '.join(cols)} FROM experiment_run ORDER BY {order_col} DESC LIMIT 20"

            if any(word in q for word in ("failure", "failed", "error")) and "experiment_run" in schema:
                predicate = "lower(status) IN ('error', 'failed', 'failure')" if self._has_col(schema, "experiment_run", "status") else "1=1"
                order_col = "started_at" if self._has_col(schema, "experiment_run", "started_at") else "id"
                return (
                    "SELECT * FROM experiment_run "
                    f"WHERE {predicate} "
                    f"ORDER BY {order_col} DESC LIMIT 50"
                )

            if any(phrase in q for phrase in ("success rate", "how often", "failure rate")) and "experiment_run" in schema:
                if self._has_col(schema, "experiment_run", "run_type") and self._has_col(schema, "experiment_run", "status"):
                    return (
                        "SELECT run_type, COUNT(*) AS total_runs, "
                        "SUM(CASE WHEN lower(status) = 'success' THEN 1 ELSE 0 END) AS success_runs, "
                        "ROUND(100.0 * SUM(CASE WHEN lower(status) = 'success' THEN 1 ELSE 0 END) / COUNT(*), 2) AS success_rate_pct "
                        "FROM experiment_run GROUP BY run_type ORDER BY total_runs DESC LIMIT 20"
                    )
                if self._has_col(schema, "experiment_run", "status"):
                    return (
                        "SELECT status, COUNT(*) AS count FROM experiment_run "
                        "GROUP BY status ORDER BY count DESC LIMIT 20"
                    )

        return None

    def _sanitize_sql(self, sql: str, schema: dict[str, list[str]]) -> str:
        cleaned = (sql or "").strip().strip("`")
        if not cleaned:
            raise ValueError("Generated SQL is empty.")
        if cleaned.lower().startswith("sql"):
            cleaned = cleaned[3:].strip()

        # Single statement only.
        if cleaned.count(";") > 1 or (";" in cleaned and not cleaned.endswith(";")):
            raise ValueError("Only a single SQL statement is allowed.")
        cleaned = cleaned.rstrip(";").strip()
        cleaned = re.sub(r"\bilike\b", "LIKE", cleaned, flags=re.IGNORECASE)

        if self._FORBIDDEN_SQL.search(cleaned):
            raise ValueError("Only read-only SELECT queries are allowed.")

        lowered = cleaned.lower()
        if not (lowered.startswith("select") or lowered.startswith("with")):
            raise ValueError("Query must start with SELECT or WITH.")

        table_matches = re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b", lowered)
        unknown_tables = [t for t in table_matches if t not in schema]
        if unknown_tables:
            raise ValueError(f"Query references unknown tables: {sorted(set(unknown_tables))}")

        if not re.search(r"\blimit\s+\d+\b", lowered, flags=re.IGNORECASE):
            cleaned += " LIMIT 200"
        return cleaned

    def _run_sql(self, sql: str) -> list[dict[str, Any]]:
        with experiment_session_scope() as session:
            rows = session.execute(text(sql)).mappings().all()
            return [dict(r) for r in rows]

    def _summarize(self, question: str, sql: str, rows: list[dict], schema_mode: str) -> str:
        if rows and len(rows) == 1 and "total_experiments" in rows[0]:
            value = rows[0].get("total_experiments")
            return f"Total experiments recorded: {value}. Next step: segment by status/agent to assess quality, not just volume."

        preview = rows[:10]
        try:
            return self._completion_text(
                temperature=0.2,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarize the SQL query results in ≤150 words. "
                            "Be concrete: mention specific values, counts, and success/failure rates when relevant. "
                            "If no rows, say so clearly. Then provide a one-line recommended next step."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Schema mode: {schema_mode}\n"
                            f"Question: {question}\nSQL: {sql}\nResults: {json.dumps(preview, default=str)}"
                        ),
                    },
                ],
            )
        except Exception:
            if not rows:
                return "No matching experiment records were found. Next step: broaden filters or verify logging coverage."
            return f"Found {len(rows)} matching row(s). Next step: inspect top records and aggregate success/failure by run type."
