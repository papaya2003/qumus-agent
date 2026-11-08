from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Optional, Sequence, List

import litellm
from sqlalchemy import text
from smolagents import Tool

from common.llm_config import DEFAULT_MODEL_ID, get_api_base_for_model, get_api_key_for_model, resolve_model_id
from common.material_semantics import GRAPHITE_CODE
from database.db_utils import init_db, material_session_scope


@dataclass(frozen=True)
class FieldSpec:
    column: str
    keywords: tuple[str, ...]
    friendly_name: str
    units: Optional[str] = None


@dataclass
class FilterClause:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class AggregateSpec:
    func: str
    column: str
    label: str
    friendly_field: str
    units: Optional[str] = None


@dataclass
class QueryPlan:
    intent: Literal["list", "count", "summary", "metric"]
    sql: str
    params: dict[str, Any]
    result_mode: Literal["rows", "scalar"]
    filters: list[str]
    where_clause: str
    order_by: Optional[str] = None
    limit: Optional[int] = None
    count_sql: Optional[str] = None
    aggregate: Optional[AggregateSpec] = None


@dataclass
class LLMQueryPlan:
    sql: str
    intent: Literal["list", "count", "summary", "metric"]
    reasoning: Optional[str] = None
    model: Optional[str] = None


class NaturalLanguageQueryPlanner:
    """Heuristic planner that converts simple NL chip questions into SQL."""

    _LABEL_KEYWORDS: dict[str, tuple[str, ...]] = {
        "bare_silicon_chip": ("bare silicon", "bare chip", "left table"),
        "exfoliated_chip": ("exfoliated", "finished chip", "right table"),
        "electrode_chip": ("electrode chip", "device chip", "bottom electrode chip"),
        "raw_crystal_carrier": ("raw crystal", "raw crystals", "raw crystal carrier", "raw carrier", "carrier", "mother tape"),
    }

    _NUMERIC_FIELDS: tuple[FieldSpec, ...] = (
        FieldSpec("pickup_left_angle_deg", ("pickup left angle", "left pickup angle"), "left pickup angle", "degrees"),
        FieldSpec("pickup_left_x_mm", ("pickup left x", "left pickup x"), "left pickup X position", "mm"),
        FieldSpec("pickup_left_y_mm", ("pickup left y", "left pickup y"), "left pickup Y position", "mm"),
        FieldSpec("pickup_right_angle_deg", ("pickup right angle", "right pickup angle"), "right pickup angle", "degrees"),
        FieldSpec("pickup_right_x_mm", ("pickup right x", "right pickup x"), "right pickup X position", "mm"),
        FieldSpec("pickup_right_y_mm", ("pickup right y", "right pickup y"), "right pickup Y position", "mm"),
        FieldSpec("stage_position_x", ("stage x", "stage position x"), "stage X", "mm"),
        FieldSpec("stage_position_y", ("stage y", "stage position y"), "stage Y", "mm"),
        FieldSpec("width_mm", ("width", "chip width"), "width", "mm"),
        FieldSpec("height_mm", ("height", "chip height"), "height", "mm"),
        FieldSpec("angle_deg", ("angle", "orientation"), "angle", "degrees"),
        FieldSpec("confidence", ("confidence", "score", "probability"), "confidence", None),
        FieldSpec("size_area_um", ("area", "size", "flake area", "sq um"), "area", "um^2"),
    )

    _STRING_FIELDS: tuple[FieldSpec, ...] = (
        FieldSpec("table_coordinate_left", ("left table coordinate", "table coordinate", "coordinate", "slot", "position"), "left table coordinate"),
        FieldSpec("table_coordinate_right", ("right table coordinate", "right coordinate", "right slot"), "right table coordinate"),
        FieldSpec("notes", ("note", "notes", "comment", "annotation"), "notes"),
    )

    _AGGREGATE_KEYWORDS: dict[str, tuple[str, bool]] = {
        "average": ("avg", True),
        "avg": ("avg", True),
        "mean": ("avg", True),
        "minimum": ("min", False),
        "minimums": ("min", False),
        "minimum value": ("min", False),
        "min": ("min", False),
        "maximum": ("max", False),
        "max": ("max", False),
        "sum": ("sum", True),
        "total": ("sum", True),
    }

    _LIST_COLUMNS: tuple[str, ...] = (
        "id",
        "label",
        "table_coordinate_left",
        "table_coordinate_right",
        "current_slot_id",
        "stage_position_x",
        "stage_position_y",
        "pickup_left_x_mm",
        "pickup_left_y_mm",
        "pickup_left_angle_deg",
        "pickup_right_x_mm",
        "pickup_right_y_mm",
        "pickup_right_angle_deg",
        "width_mm",
        "height_mm",
        "confidence",
        "last_updated_date",
    )

    _SUPERLATIVE_ORDER: dict[str, tuple[str, bool]] = {
        "highest": ("DESC", False),
        "largest": ("DESC", False),
        "biggest": ("DESC", False),
        "widest": ("DESC", False),
        "longest": ("DESC", False),
        "lowest": ("ASC", False),
        "smallest": ("ASC", False),
        "narrowest": ("ASC", False),
        "shortest": ("ASC", False),
        "earliest": ("ASC", True),
        "oldest": ("ASC", True),
        "latest": ("DESC", True),
        "newest": ("DESC", True),
        "recent": ("DESC", True),
    }

    WEEKDAY_INDEX: dict[str, int] = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    MONTH_INDEX: dict[str, int] = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }

    def __init__(self) -> None:
        self._param_counter = 0

    def plan(self, question: str, intent_hint: Optional[str], known_labels: Sequence[str]) -> QueryPlan:
        cleaned = question.strip()
        if not cleaned:
            raise ValueError("question must not be empty")
        normalized = self._normalize(cleaned)
        self._param_counter = 0
        filters: list[FilterClause] = []

        intent = self._infer_intent(normalized, intent_hint)
        label_filter = self._infer_label_filter(normalized, known_labels)
        if label_filter:
            filters.append(label_filter)
        stage_filter = self._infer_stage_filter(normalized)
        if stage_filter:
            filters.append(stage_filter)
        filters.extend(self._extract_chip_id_filters(normalized))
        coordinate = self._extract_coordinate_filter(cleaned)
        if coordinate:
            filters.append(coordinate)
        slot_filter = self._extract_slot_id_filter(normalized)
        if slot_filter:
            filters.append(slot_filter)
        filters.extend(self._extract_numeric_filters(cleaned))
        filters.extend(self._extract_temporal_filters(cleaned, normalized))
        filters.extend(self._extract_text_filters(cleaned))
        table_filter = self._extract_table_filter(normalized)
        if table_filter:
            filters.append(table_filter)

        where_clause = self._build_where(filters)
        aggregate = self._detect_aggregate(normalized, intent)
        order_by = self._infer_order(normalized, aggregate)
        limit = self._extract_limit(normalized)
        filter_descriptions = [f.description for f in filters if f.description]

        if aggregate:
            sql = f"SELECT {aggregate.func.upper()}({aggregate.column}) AS value FROM chip {where_clause}"
            return QueryPlan(
                intent="metric",
                sql=sql,
                params=self._merge_params(filters),
                result_mode="scalar",
                filters=filter_descriptions,
                where_clause=where_clause,
                aggregate=aggregate,
            )

        if intent == "count":
            sql = f"SELECT COUNT(*) AS count FROM chip {where_clause}"
            return QueryPlan(
                intent="count",
                sql=sql,
                params=self._merge_params(filters),
                result_mode="scalar",
                filters=filter_descriptions,
                where_clause=where_clause,
            )

        # summary quickly piggybacks on list output plus aggregates
        select_cols = ", ".join(self._LIST_COLUMNS)
        if not order_by:
            order_by = "last_updated_date DESC"
        limit_clause = f" LIMIT {limit or 25}"
        sql = f"SELECT {select_cols} FROM chip {where_clause} ORDER BY {order_by}{limit_clause}"
        count_sql = f"SELECT COUNT(*) AS count FROM chip {where_clause}"
        return QueryPlan(
            intent=intent,
            sql=sql,
            params=self._merge_params(filters),
            result_mode="rows",
            filters=filter_descriptions,
            where_clause=where_clause,
            order_by=order_by,
            limit=limit or 25,
            count_sql=count_sql,
        )

    def _infer_intent(self, normalized: str, hint: Optional[str]) -> Literal["list", "count", "summary"]:
        if hint:
            hint_norm = hint.strip().lower()
            if hint_norm in {"list", "count", "summary"}:
                return hint_norm  # type: ignore[return-value]
        if any(token in normalized for token in ("how many", "count", "number of", "total chips")):
            return "count"
        if normalized.startswith("list ") or normalized.startswith("show "):
            return "list"
        if any(token in normalized for token in ("which", "show me", "give me", "what are", "find all")):
            return "list"
        return "summary"

    def _infer_label_filter(self, normalized: str, known_labels: Sequence[str]) -> Optional[FilterClause]:
        for label, keywords in self._LABEL_KEYWORDS.items():
            for keyword in keywords:
                if keyword in normalized:
                    return FilterClause(sql="label = :label", params={"label": label}, description=f"label = {label}")
        for entry in known_labels:
            if not entry:
                continue
            slug = entry.lower()
            if slug and slug in normalized:
                return FilterClause(sql="label = :label", params={"label": entry}, description=f"label = {entry}")
        return None

    def _infer_stage_filter(self, normalized: str) -> Optional[FilterClause]:
        if "left table" in normalized or "left side" in normalized:
            return FilterClause(sql="stage_position_x < 0", description="stage: left")
        if "right table" in normalized or "right side" in normalized:
            return FilterClause(sql="stage_position_x >= 0", description="stage: right")
        return None

    def _extract_chip_id_filters(self, normalized: str) -> list[FilterClause]:
        filters: list[FilterClause] = []
        for match in re.finditer(r"chip\s*(?:id|#)?\s*(\d+)", normalized):
            param = self._next_param("chip_id")
            filters.append(
                FilterClause(
                    sql=f"id = :{param}",
                    params={param: int(match.group(1))},
                    description=f"chip id = {match.group(1)}",
                )
            )
        return filters

    def _extract_coordinate_filter(self, text: str) -> Optional[FilterClause]:
        match = re.search(r"(?:slot|coordinate|position)\s+([A-Za-z]\d{1,2})", text, re.IGNORECASE)
        if match:
            value = match.group(1).upper()
            return FilterClause(
                sql="COALESCE(table_coordinate_left, table_coordinate_right) = :coordinate",
                params={"coordinate": value},
                description=f"table coordinate = {value}",
            )
        return None

    def _extract_slot_id_filter(self, normalized: str) -> Optional[FilterClause]:
        match = re.search(r"\bslot\s+(?:number\s*)?(\d+)", normalized)
        if match:
            param = self._next_param("slot_id")
            return FilterClause(
                sql=f"current_slot_id = :{param}",
                params={param: int(match.group(1))},
                description=f"slot id = {match.group(1)}",
            )
        return None

    def _extract_table_filter(self, normalized: str) -> Optional[FilterClause]:
        match = re.search(r"\btable(?:\s+number)?\s+(\d+)", normalized)
        if match:
            param = self._next_param("table_id")
            return FilterClause(
                sql=f"table_id = :{param}",
                params={param: int(match.group(1))},
                description=f"table id = {match.group(1)}",
            )
        return None

    def _extract_numeric_filters(self, original_text: str) -> list[FilterClause]:
        filters: list[FilterClause] = []
        cleaned = original_text.replace("°", " ")
        for field in self._NUMERIC_FIELDS:
            for keyword in field.keywords:
                filters.extend(self._match_numeric_condition(cleaned, keyword, field))
        return filters

    def _match_numeric_condition(self, text: str, keyword: str, field: FieldSpec) -> list[FilterClause]:
        filters: list[FilterClause] = []
        escaped = re.escape(keyword)
        comp_words = r"(?:>=|<=|>|<|=|==|greater than or equal to|less than or equal to|greater than|less than|over|under|above|below|at least|at most|not less than|not more than|more than|fewer than|equal to|equals)"
        value_pattern = r"(-?\d+(?:\.\d+)?)"
        pattern_keyword_first = re.compile(
            rf"{escaped}[^0-9]*?(?P<op>{comp_words})\s*(?P<value>{value_pattern})",
            re.IGNORECASE,
        )
        pattern_value_first = re.compile(
            rf"(?P<value>{value_pattern})\s*(?P<op>{comp_words})\s*(?:of\s+)?{escaped}",
            re.IGNORECASE,
        )
        between_pattern = re.compile(
            rf"{escaped}[^0-9]*(?:between|from)\s*(?P<low>{value_pattern})\s*(?:and|to)\s*(?P<high>{value_pattern})",
            re.IGNORECASE,
        )

        for match in between_pattern.finditer(text):
            low = float(match.group("low"))
            high = float(match.group("high"))
            if high < low:
                low, high = high, low
            low_param = self._next_param(f"{field.column}_low")
            high_param = self._next_param(f"{field.column}_high")
            filters.append(
                FilterClause(
                    sql=f"{field.column} BETWEEN :{low_param} AND :{high_param}",
                    params={low_param: low, high_param: high},
                    description=f"{field.friendly_name} between {low} and {high}",
                )
            )
        for pattern in (pattern_keyword_first, pattern_value_first):
            for match in pattern.finditer(text):
                op = self._normalize_operator(match.group("op"))
                value = float(match.group("value"))
                param = self._next_param(field.column)
                filters.append(
                    FilterClause(
                        sql=f"{field.column} {op} :{param}",
                        params={param: value},
                        description=f"{field.friendly_name} {op} {value}",
                    )
                )
        return filters

    def _extract_temporal_filters(self, text: str, normalized: str) -> list[FilterClause]:
        filters: list[FilterClause] = []
        date_field = self._infer_date_field(normalized)
        now = datetime.utcnow()
        between_match = re.search(r"between\s+([^,?]+?)\s+and\s+([^,?]+)", normalized)
        if between_match:
            start = self._parse_date_phrase(between_match.group(1).strip(), now)
            end = self._parse_date_phrase(between_match.group(2).strip(), now)
            if start and end:
                start_dt, end_dt = sorted([start, end])
                filters.append(
                    FilterClause(
                        sql=f"{date_field} BETWEEN :{date_field}_start AND :{date_field}_end",
                        params={
                            f"{date_field}_start": self._start_of_day(start_dt).isoformat(),
                            f"{date_field}_end": (self._start_of_day(end_dt) + timedelta(days=1)).isoformat(),
                        },
                        description=f"{date_field} between {start_dt.date()} and {end_dt.date()}",
                    )
                )
                return filters

        since_phrase = self._extract_keyword_phrase(normalized, "since")
        if since_phrase:
            dt = self._parse_date_phrase(since_phrase, now)
            if dt:
                filters.append(
                    FilterClause(
                        sql=f"{date_field} >= :{date_field}_since",
                        params={f"{date_field}_since": self._start_of_day(dt).isoformat()},
                        description=f"{date_field} since {dt.date()}",
                    )
                )

        after_phrase = self._extract_keyword_phrase(normalized, "after")
        if after_phrase:
            dt = self._parse_date_phrase(after_phrase, now)
            if dt:
                filters.append(
                    FilterClause(
                        sql=f"{date_field} > :{date_field}_after",
                        params={f"{date_field}_after": self._start_of_day(dt).isoformat()},
                        description=f"{date_field} after {dt.date()}",
                    )
                )

        before_phrase = self._extract_keyword_phrase(normalized, "before")
        if before_phrase:
            dt = self._parse_date_phrase(before_phrase, now)
            if dt:
                filters.append(
                    FilterClause(
                        sql=f"{date_field} < :{date_field}_before",
                        params={f"{date_field}_before": self._start_of_day(dt).isoformat()},
                        description=f"{date_field} before {dt.date()}",
                    )
                )

        relative_match = re.search(r"(?:past|last)\s+(\d+)\s+(day|days|week|weeks|month|months)", normalized)
        if relative_match:
            value = int(relative_match.group(1))
            unit = relative_match.group(2)
            delta = self._relative_delta(value, unit)
            filters.append(
                FilterClause(
                    sql=f"{date_field} >= :{date_field}_window",
                    params={f"{date_field}_window": (now - delta).isoformat()},
                    description=f"{date_field} in last {value} {unit}",
                )
            )
        elif "past week" in normalized or "last week" in normalized:
            filters.append(
                FilterClause(
                    sql=f"{date_field} >= :{date_field}_week",
                    params={f"{date_field}_week": (now - timedelta(days=7)).isoformat()},
                    description=f"{date_field} in last week",
                )
            )
        elif "past month" in normalized or "last month" in normalized:
            filters.append(
                FilterClause(
                    sql=f"{date_field} >= :{date_field}_month",
                    params={f"{date_field}_month": (now - timedelta(days=30)).isoformat()},
                    description=f"{date_field} in last month",
                )
            )

        return filters

    def _infer_date_field(self, normalized: str) -> str:
        if any(token in normalized for token in ("scan", "scanned", "detected", "updated", "refresh")):
            return "last_updated_date"
        if any(token in normalized for token in ("created", "creation", "exfoliated", "fabricated")):
            return "creation_date"
        return "last_updated_date"

    def _extract_text_filters(self, text: str) -> list[FilterClause]:
        filters: list[FilterClause] = []
        for field in self._STRING_FIELDS:
            for keyword in field.keywords:
                pattern = re.compile(
                    rf"{re.escape(keyword)}\s+(?:like|contains|containing|with|matching)\s+['\"]?([^'\"]+)",
                    re.IGNORECASE,
                )
                match = pattern.search(text)
                if match:
                    param = self._next_param(field.column)
                    value = match.group(1).strip()
                    filters.append(
                        FilterClause(
                            sql=f"{field.column} LIKE :{param}",
                            params={param: f"%{value}%"},
                            description=f"{field.friendly_name} contains '{value}'",
                        )
                    )
                    break
        return filters

    def _detect_aggregate(
        self,
        normalized: str,
        intent: Literal["list", "count", "summary"],
    ) -> Optional[AggregateSpec]:
        if intent in {"list", "count"}:
            return None
        base_mentions = self._find_field_mentions(normalized)
        for keyword, (func, always) in self._AGGREGATE_KEYWORDS.items():
            if keyword not in normalized:
                continue
            if not always and any(term in normalized for term in ("which", "list", "show", "give me")):
                continue
            field = self._find_field_near_keyword(normalized, keyword) or base_mentions
            spec = field if isinstance(field, FieldSpec) else None
            if spec:
                return AggregateSpec(
                    func=func,
                    column=spec.column,
                    label=f"{func.upper()} of {spec.friendly_name}",
                    friendly_field=spec.friendly_name,
                    units=spec.units,
                )
        return None

    def _find_field_mentions(self, normalized: str) -> Optional[FieldSpec]:
        for spec in self._NUMERIC_FIELDS:
            for keyword in spec.keywords:
                if keyword in normalized:
                    return spec
        return None

    def _find_field_near_keyword(self, normalized: str, keyword: str) -> Optional[FieldSpec]:
        try:
            idx = normalized.index(keyword)
        except ValueError:
            return None
        tail = normalized[idx:]
        for spec in self._NUMERIC_FIELDS:
            for phrase in spec.keywords:
                if phrase in tail:
                    return spec
        return None

    def _infer_order(self, normalized: str, aggregate: Optional[AggregateSpec]) -> Optional[str]:
        if aggregate:
            return None
        for word, (direction, is_time) in self._SUPERLATIVE_ORDER.items():
            if word not in normalized:
                continue
            if is_time:
                column = "creation_date" if direction == "ASC" else "last_updated_date"
                return f"{column} {direction}"
            spec = self._find_field_near_keyword(normalized, word)
            if spec:
                return f"{spec.column} {direction}"
        return None

    def _extract_limit(self, normalized: str) -> Optional[int]:
        match = re.search(r"(?:top|first|last)\s+(\d{1,3})", normalized)
        if match:
            return int(match.group(1))
        range_match = re.search(r"show\s+(\d{1,3})\s+(?:chips|entries|records)", normalized)
        if range_match:
            return int(range_match.group(1))
        if any(word in normalized for word in ("highest", "lowest", "largest", "smallest")) and "which" in normalized:
            return 1
        return None

    def _normalize(self, text: str) -> str:
        normalized = text.lower()
        normalized = normalized.replace("°", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip()

    def _build_where(self, filters: list[FilterClause]) -> str:
        if not filters:
            return ""
        clauses = [f.sql for f in filters]
        return "WHERE " + " AND ".join(clauses)

    def _merge_params(self, filters: list[FilterClause]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for clause in filters:
            params.update(clause.params)
        return params

    def _extract_keyword_phrase(self, normalized: str, keyword: str) -> Optional[str]:
        pattern = re.compile(rf"{keyword}\s+([^,.!?]+)")
        match = pattern.search(normalized)
        if not match:
            return None
        phrase = match.group(1)
        terminators = [" and ", " but ", " or ", ";"]
        for term in terminators:
            idx = phrase.find(term)
            if idx != -1:
                phrase = phrase[:idx]
                break
        return phrase.strip()

    def _parse_date_phrase(self, phrase: str, now: datetime) -> Optional[datetime]:
        cleaned = phrase.strip().lower()
        if not cleaned:
            return None
        cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", cleaned)
        if cleaned in {"today"}:
            return now
        if cleaned in {"yesterday"}:
            return now - timedelta(days=1)
        if cleaned in {"tomorrow"}:
            return now + timedelta(days=1)
        if cleaned.startswith("last "):
            target = cleaned.replace("last ", "", 1)
            if target in self.WEEKDAY_INDEX:
                return self._previous_weekday(now, self.WEEKDAY_INDEX[target])
            if target == "week":
                return now - timedelta(days=7)
            if target == "month":
                return now - timedelta(days=30)
            if target == "year":
                return now - timedelta(days=365)
        if cleaned in self.WEEKDAY_INDEX:
            return self._previous_weekday(now, self.WEEKDAY_INDEX[cleaned])
        absolute = self._parse_absolute_date(cleaned, now)
        if absolute:
            return absolute
        ago_match = re.match(r"(\d+)\s+(day|week|month|year)s?\s+ago", cleaned)
        if ago_match:
            value = int(ago_match.group(1))
            return now - self._relative_delta(value, ago_match.group(2) + "s")
        return None

    def _parse_absolute_date(self, cleaned: str, now: datetime) -> Optional[datetime]:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
        month_match = re.match(r"(?P<month>[a-z]+)\s+(?P<day>\d{1,2})(?:,\s*(?P<year>\d{4}))?", cleaned)
        if month_match:
            month_name = month_match.group("month")
            month = self.MONTH_INDEX.get(month_name)
            if not month:
                return None
            day = int(month_match.group("day"))
            year = int(month_match.group("year") or now.year)
            candidate = datetime(year=year, month=month, day=day)
            if not month_match.group("year") and candidate > now:
                candidate = candidate.replace(year=year - 1)
            return candidate
        return None

    def _relative_delta(self, value: int, unit: str) -> timedelta:
        unit = unit.lower()
        if unit.startswith("day"):
            return timedelta(days=value)
        if unit.startswith("week"):
            return timedelta(weeks=value)
        if unit.startswith("month"):
            return timedelta(days=30 * value)
        if unit.startswith("year"):
            return timedelta(days=365 * value)
        return timedelta(days=value)

    def _previous_weekday(self, now: datetime, target_weekday: int) -> datetime:
        delta = (now.weekday() - target_weekday) % 7 or 7
        return now - timedelta(days=delta)

    def _start_of_day(self, dt: datetime) -> datetime:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    def _normalize_operator(self, op: str) -> str:
        normalized = op.strip().lower()
        if normalized in {">=", "greater than or equal to", "at least", "not less than"}:
            return ">="
        if normalized in {"<=", "less than or equal to", "at most", "not more than"}:
            return "<="
        if normalized in {">", "greater than", "more than", "over", "above"}:
            return ">"
        if normalized in {"<", "less than", "below", "under", "fewer than"}:
            return "<"
        return "="

    def _next_param(self, seed: str) -> str:
        self._param_counter += 1
        key = seed.replace(".", "_")
        return f"{key}_{self._param_counter}"


class LLMFallbackPlanner:
    """LLM-backed planner that emits SQL and textual summaries for complex questions."""

    _SCHEMA_DESCRIPTION = """
    Tables:
    - chip(id INTEGER PRIMARY KEY, label TEXT, creation_date DATETIME, last_updated_date DATETIME,
           stage_position_x FLOAT, stage_position_y FLOAT,
           corner_location_x FLOAT, corner_location_y FLOAT, corner_image_id INTEGER,
           pickup_left_x_mm FLOAT, pickup_left_y_mm FLOAT, pickup_left_angle_deg FLOAT,
           pickup_right_x_mm FLOAT, pickup_right_y_mm FLOAT, pickup_right_angle_deg FLOAT,
           angle_deg FLOAT, width_mm FLOAT, height_mm FLOAT, confidence FLOAT,
           table_coordinate_left TEXT, table_coordinate_right TEXT,
           current_slot_id INTEGER, notes TEXT, metadata JSON)
    /* NOTE: 'inventory' refers to the 'chip' table. chip.corner_image_id references image.id for the chip-corner image path. */
    - flake(id INTEGER PRIMARY KEY, chip_id INTEGER, type_label TEXT, type_index INTEGER,
            location_x FLOAT, location_y FLOAT, date_created DATETIME,
            size_area_um FLOAT, thickness_layers FLOAT,
            cleanliness TEXT, is_used BOOLEAN, device_id INTEGER, image_id INTEGER, image_20x_id INTEGER, mask_path TEXT, mask_20x_path TEXT, metadata JSON)
    /* NOTE: flake.image_id references the preferred 100x optical image in m_100_results; flake.image_20x_id references the paired 20x optical image; flake.mask_path stores the matching segmented 100x image path in segmented_m_100; flake.mask_20x_path stores the matching segmented 20x image path in segmented_m_20. Real-device design consumes mask_20x_path only. */
    - image(id INTEGER PRIMARY KEY, path TEXT, type TEXT, creation_date DATETIME, source TEXT, metadata JSON)
    - device(id INTEGER PRIMARY KEY, name TEXT, chip_id INTEGER, flake_id INTEGER,
             status TEXT, created_at DATETIME, updated_at DATETIME, notes TEXT, image_id INTEGER)
    - bottom_electrode(id INTEGER PRIMARY KEY, chip_id INTEGER,
             location_x FLOAT, location_y FLOAT,
             created_at DATETIME, updated_at DATETIME,
             image_id INTEGER, image_20x_id INTEGER)
    - chip_slot(id INTEGER PRIMARY KEY, label TEXT, x_left_mm FLOAT, y_left_mm FLOAT,
                x_right_mm FLOAT, y_right_mm FLOAT, updated_at DATETIME, occupied_by INTEGER)
    - chip_move(id INTEGER PRIMARY KEY, chip_id INTEGER, from_slot_id INTEGER, to_slot_id INTEGER,
                source TEXT, correlation_id TEXT, notes TEXT, created_at DATETIME)
    """.strip()

    def __init__(self, *, model_id: Optional[str] = None) -> None:
        self._model_id = resolve_model_id(model_id or DEFAULT_MODEL_ID)
        self._api_key: Optional[str] = None
        self._api_base: Optional[str] = None
        self._last_error: Optional[str] = None
        self._setup_client()

    def _setup_client(self) -> None:
        self._api_key = get_api_key_for_model(self._model_id)
        self._api_base = get_api_base_for_model(self._model_id)
        if not self._api_key:
            self._last_error = "QUMUS_API_KEY is not set"
            return
        self._last_error = None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def plan(self, question: str, intent_hint: Optional[str]) -> Optional[LLMQueryPlan]:
        if not self.available:
            return None
        system_prompt = (
            "You translate operator questions into SQLite queries over the lab materials schema. "
            "All responses must be valid JSON with keys: sql (string), intent (list|count|summary|metric), "
            "reasoning (short string). Only SELECT statements are allowed.\n"
            "CRITICAL MATERIAL RULES: 'Gr' is the graphite material code. "
            "'graphene' is NOT equal to 'Gr'; graphene means type_label='Gr' AND "
            "(thickness_layers=1 OR thickness_label IN ('1', 'monolayer', '1L')). "
            "A 'graphite gate' means type_label='Gr' AND thickness_label='manylayer'; "
            "never apply a graphite nanometer-thickness filter. "
            "If the user asks for 'graphite' (or 'Gr'), filter by type_label='Gr' without forcing monolayer unless requested. "
            "hBN is NOT monolayer/bilayer in this lab schema; treat hBN thickness as bin labels such as 0-10/10-20/20-30/30-40/40+. "
            "The stored thickness_label values are unitless: use '30-40', never '30-40nm', in SQL filters."
        )
        user_prompt = f"Schema:\n{self._SCHEMA_DESCRIPTION}\n" f"Intent hint: {intent_hint or 'unknown'}\n" f"Question: {question}\n" "Respond with JSON only."
        try:
            completion_kwargs: dict[str, Any] = {
                "model": self._model_id,
                "api_key": self._api_key,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if self._api_base:
                completion_kwargs["api_base"] = self._api_base
            response = litellm.completion(**completion_kwargs)
        except Exception as exc:  # pragma: no cover - API failure
            self._last_error = str(exc)
            return None
        content = self._extract_content(response)
        payload = self._parse_json(content)
        if not payload:
            return None
        sql = (payload.get("sql") or "").strip()
        if not sql.lower().startswith("select"):
            return None
        intent = (payload.get("intent") or "summary").lower()
        if intent not in {"list", "count", "summary", "metric"}:
            intent = "summary"
        reasoning = payload.get("reasoning")
        return LLMQueryPlan(sql=sql, intent=intent, reasoning=reasoning, model=self._model_id)

    def summarize(self, question: str, plan: LLMQueryPlan, rows: list[dict[str, Any]]) -> Optional[str]:
        if not self.available:
            return None
        preview = rows[: min(len(rows), 10)]
        user_payload = json.dumps(
            {"rows": preview, "total_rows": len(rows)},
            ensure_ascii=False,
        )
        summary_prompt = (
            "Provide a concise natural language answer (<=120 words) grounded only in the supplied SQL results. "
            "Do not invent data and mention if zero rows were returned. "
            "If the results include an image path for a flake, you MUST explicitly include it in your response exactly as: 'Optical image: <path>'."
        )
        user_prompt = f"Question: {question}\nSQL: {plan.sql}\nResults JSON: {user_payload}\n" "Summarize the findings."
        try:
            completion_kwargs: dict[str, Any] = {
                "model": self._model_id,
                "api_key": self._api_key,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": summary_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if self._api_base:
                completion_kwargs["api_base"] = self._api_base
            response = litellm.completion(**completion_kwargs)
        except Exception as exc:  # pragma: no cover
            self._last_error = str(exc)
            return None
        content = self._extract_content(response)
        return content or None

    @staticmethod
    def _extract_content(response: Any) -> str:
        try:
            choices = response.get("choices") if isinstance(response, dict) else response.choices
            message = choices[0].get("message") if isinstance(choices[0], dict) else choices[0].message
            content = message.get("content") if isinstance(message, dict) else message.content
            return (content or "").strip()
        except Exception:  # pragma: no cover - defensive
            return ""

    def _parse_json(self, content: str) -> Optional[dict[str, Any]]:
        if not content:
            return None
        candidates = [content]
        start = content.find("{")
        end = content.rfind("}")
        if 0 <= start < end:
            candidates.append(content[start : end + 1])
        for chunk in candidates:
            try:
                data = json.loads(chunk)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
        return None

    def _normalize(self, text: str) -> str:
        normalized = text.lower()
        normalized = normalized.replace("°", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip()

    def _build_where(self, filters: list[FilterClause]) -> str:
        if not filters:
            return ""
        clauses = [f.sql for f in filters]
        return "WHERE " + " AND ".join(clauses)

    def _merge_params(self, filters: list[FilterClause]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for clause in filters:
            params.update(clause.params)
        return params

    def _extract_keyword_phrase(self, normalized: str, keyword: str) -> Optional[str]:
        pattern = re.compile(rf"{keyword}\s+([^,.!?]+)")
        match = pattern.search(normalized)
        if not match:
            return None
        phrase = match.group(1)
        terminators = [" and ", " but ", " or ", ";"]
        for term in terminators:
            idx = phrase.find(term)
            if idx != -1:
                phrase = phrase[:idx]
                break
        return phrase.strip()

    def _parse_date_phrase(self, phrase: str, now: datetime) -> Optional[datetime]:
        cleaned = phrase.strip().lower()
        if not cleaned:
            return None
        cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", cleaned)
        if cleaned in {"today"}:
            return now
        if cleaned in {"yesterday"}:
            return now - timedelta(days=1)
        if cleaned in {"tomorrow"}:
            return now + timedelta(days=1)
        if cleaned.startswith("last "):
            target = cleaned.replace("last ", "", 1)
            if target in self.WEEKDAY_INDEX:
                return self._previous_weekday(now, self.WEEKDAY_INDEX[target])
            if target == "week":
                return now - timedelta(days=7)
            if target == "month":
                return now - timedelta(days=30)
            if target == "year":
                return now - timedelta(days=365)
        if cleaned in self.WEEKDAY_INDEX:
            return self._previous_weekday(now, self.WEEKDAY_INDEX[cleaned])
        absolute = self._parse_absolute_date(cleaned, now)
        if absolute:
            return absolute
        ago_match = re.match(r"(\d+)\s+(day|week|month|year)s?\s+ago", cleaned)
        if ago_match:
            value = int(ago_match.group(1))
            return now - self._relative_delta(value, ago_match.group(2) + "s")
        return None

    def _parse_absolute_date(self, cleaned: str, now: datetime) -> Optional[datetime]:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
        month_match = re.match(r"(?P<month>[a-z]+)\s+(?P<day>\d{1,2})(?:,\s*(?P<year>\d{4}))?", cleaned)
        if month_match:
            month_name = month_match.group("month")
            month = self.MONTH_INDEX.get(month_name)
            if not month:
                return None
            day = int(month_match.group("day"))
            year = int(month_match.group("year") or now.year)
            candidate = datetime(year=year, month=month, day=day)
            if not month_match.group("year") and candidate > now:
                candidate = candidate.replace(year=year - 1)
            return candidate
        return None

    def _relative_delta(self, value: int, unit: str) -> timedelta:
        unit = unit.lower()
        if unit.startswith("day"):
            return timedelta(days=value)
        if unit.startswith("week"):
            return timedelta(weeks=value)
        if unit.startswith("month"):
            return timedelta(days=30 * value)
        if unit.startswith("year"):
            return timedelta(days=365 * value)
        return timedelta(days=value)

    def _previous_weekday(self, now: datetime, target_weekday: int) -> datetime:
        delta = (now.weekday() - target_weekday) % 7 or 7
        return now - timedelta(days=delta)

    def _start_of_day(self, dt: datetime) -> datetime:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    def _normalize_operator(self, op: str) -> str:
        normalized = op.strip().lower()
        if normalized in {">=", "greater than or equal to", "at least", "not less than"}:
            return ">="
        if normalized in {"<=", "less than or equal to", "at most", "not more than"}:
            return "<="
        if normalized in {">", "greater than", "more than", "over", "above"}:
            return ">"
        if normalized in {"<", "less than", "below", "under", "fewer than"}:
            return "<"
        return "="

    def _next_param(self, seed: str) -> str:
        self._param_counter += 1
        key = seed.replace(".", "_")
        return f"{key}_{self._param_counter}"


class DatabaseQueryTool(Tool):
    name = "query_database"
    description = (
        "Answer natural language questions about the lab materials database. Uses heuristic NL-to-SQL planning "
        "for common chip queries and falls back to an LLM planner for complex joins or unseen request types."
    )
    inputs = {
        "question": {"type": "string", "description": "Natural language database question.", "nullable": True},
        "intent": {
            "type": "string",
            "description": "Optional hint: count | list | summary.",
            "nullable": True,
        },
        "table": {
            "type": "string",
            "description": "Optional explicit table to query; when provided the tool bypasses NL planning.",
            "nullable": True,
        },
        "columns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional list of columns to select when using structured queries.",
            "nullable": True,
        },
        "filters": {
            "type": "object",
            "description": "Optional explicit filters (dict or list of filter specs) for structured queries.",
            "nullable": True,
        },
        "order_by": {
            "type": "string",
            "description": "Optional ORDER BY clause for structured queries (e.g., 'area_um2 DESC').",
            "nullable": True,
        },
        "limit": {
            "type": "integer",
            "description": "Optional LIMIT for structured queries.",
            "nullable": True,
        },
    }
    output_type = "string"

    _LLM_TRIGGER_KEYWORDS: tuple[str, ...] = (
        "device",
        "devices",
        "instrument",
        "carrier",
        "polygon",
        "chip move",
        "movement",
        "join",
        "flake",
        "flakes",
    )

    def __init__(self, *, fallback_model: Optional[str] = None) -> None:
        super().__init__()
        init_db()
        self._planner = NaturalLanguageQueryPlanner()
        self._fallback = LLMFallbackPlanner(model_id=fallback_model)
        self._known_labels: tuple[str, ...] = ()

    def forward(
        self,
        question: Optional[str] = None,
        intent: Optional[str] = None,
        table: Optional[str] = None,
        columns: Optional[List[str]] = None,
        filters: Optional[Any] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> str:
        structured_query = any(value is not None for value in (table, columns, filters, order_by, limit))
        if structured_query:
            return self._run_structured_query(
                question=question or "Structured database query",
                intent=intent or "list",
                table=table,
                columns=columns,
                filters=filters,
                order_by=order_by,
                limit=limit,
            )

        if not question:
            raise RuntimeError("Database query requires a question.")
        question = question.strip()
        if not question:
            raise RuntimeError("Database query requires a non-empty question.")

        flake_image_payload = self._handle_flake_image_query(question)
        if flake_image_payload:
            return flake_image_payload

        flake_payload = self._handle_flake_size_query(question)
        if flake_payload:
            return flake_payload

        labels = self._get_known_labels()
        plan: Optional[QueryPlan] = None
        planner_error: Optional[str] = None
        try:
            plan = self._planner.plan(question, intent, labels)
        except Exception as exc:
            planner_error = str(exc)

        if plan and not self._should_use_llm(question, plan):
            return self._respond_with_plan(question, plan)

        fallback_payload = self._run_llm_pipeline(question, intent)
        if fallback_payload:
            return fallback_payload

        if plan:
            return self._respond_with_plan(question, plan)

        if planner_error:
            raise RuntimeError(f"Unable to build database query: {planner_error}")
        raise RuntimeError("Unable to build database query.")

    def _handle_flake_image_query(self, question: str) -> Optional[str]:
        normalized = question.lower()
        if "flake" not in normalized:
            return None
        if not any(keyword in normalized for keyword in ("image", "photo", "picture", "optical")):
            return None

        index_match = re.search(r"flake\s*(?:number|#)?\s*(\d+)", normalized)
        if not index_match:
            return None

        type_index = int(index_match.group(1))
        target_type, graphene_only, graphite_gate_only, requested_material = self._infer_flake_material_filter(normalized)

        params: dict[str, Any] = {"type_index": type_index}
        filters = [f"type_index = {type_index}"]
        where_clause = "WHERE f.type_index = :type_index"
        if target_type:
            params["type_label"] = target_type.lower()
            filters.append(f"type_label = {target_type}")
            where_clause += " AND LOWER(f.type_label) = :type_label"
            if graphene_only:
                where_clause += " AND (f.thickness_layers = 1 OR f.thickness_label IN ('1', 'monolayer', '1L'))"
                filters.append("graphene-only (Gr + monolayer)")
            elif graphite_gate_only:
                where_clause += " AND LOWER(f.thickness_label) = 'manylayer'"
                filters.append("graphite-gate-only (Gr + manylayer)")

        sql = f"""
            SELECT
                f.id,
                f.type_label,
                f.type_index,
                f.chip_id,
                COALESCE(c.table_coordinate_left, c.table_coordinate_right) AS table_coordinate,
                f.location_x,
                f.location_y,
                f.size_width_um,
                f.size_length_um,
                i.path AS image_path
            FROM flake AS f
            LEFT JOIN chip AS c ON c.id = f.chip_id
            LEFT JOIN image AS i ON i.id = f.image_id
            {where_clause}
            LIMIT 1
        """.strip()

        rows = self._run_sql(sql, params)
        serialized_rows = [self._serialize_row(row) for row in rows]
        if not serialized_rows:
            label_text = requested_material or target_type or "that"
            answer = f"There is no specific image recorded for {label_text} flake #{type_index}."
        else:
            row = serialized_rows[0]
            label_text = row.get("type_label") or target_type or "flake"
            chip_id = row.get("chip_id")
            slot = (
                row.get("table_coordinate_left")
                or row.get("table_coordinate_right")
                or row.get("table_coordinate")
                or "unknown slot"
            )
            answer = f"{label_text} flake #{row.get('type_index')} on chip {chip_id} at {slot} is recorded."
            if row.get("image_path"):
                answer += f" Optical image: {row['image_path']}."
            else:
                answer += " However, no optical image is stored for this flake."

        payload = {
            "question": question,
            "intent": "summary",
            "filters": filters,
            "sql": sql,
            "parameters": params,
            "rows": serialized_rows,
            "total_count": len(serialized_rows),
            "answer": answer,
            "source": "flake_image_rule",
        }
        return json.dumps(payload, ensure_ascii=False)

    def _handle_flake_size_query(self, question: str) -> Optional[str]:
        normalized = question.lower()
        if "flake" not in normalized:
            return None
        if not any(
            keyword in normalized
            for keyword in (
                "largest",
                "biggest",
                "size",
                "area",
                "larger",
                "greater",
                "smaller",
                "less",
                "over",
                "under",
            )
        ):
            return None

        target_type, graphene_only, graphite_gate_only, requested_material = self._infer_flake_material_filter(normalized)
        chip_id_match = re.search(r"chip\s*(?:id|#)?\s*(\d+)", normalized)
        chip_scope_requested = any(
            phrase in normalized
            for phrase in (
                "this round",
                "current round",
                "that round",
                "latest round",
                "last round",
                "most recent round",
                "latest exfoliation",
                "most recent exfoliation",
                "newly exfoliated chip",
                "newest exfoliated chip",
                "latest exfoliated chip",
                "most recently exfoliated chip",
                "newest chip",
                "latest chip",
            )
        )

        threshold_match = re.search(
            r"(?:area|size|flakes?)?.{0,30}?"
            r"(>=|<=|>|<|at least|at most|greater than or equal to|less than or equal to|greater than|less than|more than|fewer than|over|under|above|below|larger than|smaller than)"
            r"\s*(\d+(?:\.\d+)?)",
            normalized,
            re.IGNORECASE,
        )

        filter_specs: list[dict[str, Any]] = []
        threshold: Optional[float] = None
        operator: Optional[str] = None
        if threshold_match:
            operator = self._normalize_comparison_operator(threshold_match.group(1))
            threshold = float(threshold_match.group(2))
            if threshold < 0:
                return None
            filter_specs.append(
                {
                    "column": "f.size_area_um",
                    "operator": operator,
                    "value": threshold,
                }
            )
        if target_type:
            filter_specs.append({"column": "f.type_label", "operator": "LIKE", "value": target_type})

        where_clause, params, filters = self._build_structured_where(filter_specs)
        scope_clause = ""
        scope_params: dict[str, Any] = {}
        scope_description: Optional[str] = None

        if chip_id_match:
            chip_id = int(chip_id_match.group(1))
            scope_clause = "f.chip_id = :scoped_chip_id"
            scope_params["scoped_chip_id"] = chip_id
            scope_description = f"chip_id = {chip_id}"
        elif chip_scope_requested:
            scope_clause = (
                "f.chip_id = ("
                "SELECT c_latest.id FROM chip AS c_latest "
                "WHERE c_latest.label = 'exfoliated_chip' "
                "ORDER BY c_latest.creation_date DESC, c_latest.last_updated_date DESC, c_latest.id DESC "
                "LIMIT 1)"
            )
            scope_description = "latest exfoliation round only (newest exfoliated chip)"

        if scope_clause:
            if where_clause:
                where_clause += f" AND {scope_clause}"
            else:
                where_clause = f"WHERE {scope_clause}"
            params.update(scope_params)
            if scope_description:
                filters.append(scope_description)
        
        if graphene_only:
            if where_clause:
                where_clause += " AND (f.thickness_layers = 1 OR f.thickness_label IN ('1', 'monolayer', '1L'))"
            else:
                where_clause = "WHERE (f.thickness_layers = 1 OR f.thickness_label IN ('1', 'monolayer', '1L'))"
            filters.append("graphene-only (Gr + monolayer)")
        elif graphite_gate_only:
            if where_clause:
                where_clause += " AND LOWER(f.thickness_label) = 'manylayer'"
            else:
                where_clause = "WHERE LOWER(f.thickness_label) = 'manylayer'"
            filters.append("graphite-gate-only (Gr + manylayer)")
        row_limit = 25 if threshold is not None else 5

        sql = f"""
            SELECT
                f.id,
                f.type_label,
                f.type_index,
                f.chip_id,
                f.location_x,
                f.location_y,
                f.size_area_um,
                f.thickness_layers,
                f.thickness_label,
                f.cleanliness,
                f.image_id,
                i.path AS image_path,
                COALESCE(c.table_coordinate_left, c.table_coordinate_right) AS table_coordinate,
                c.stage_position_x,
                c.stage_position_y
            FROM flake AS f
            LEFT JOIN chip AS c ON c.id = f.chip_id
            LEFT JOIN image AS i ON i.id = f.image_id
            {where_clause}
            ORDER BY f.size_area_um DESC NULLS LAST
            LIMIT {row_limit}
        """.strip()

        rows = self._run_sql(sql, params)
        serialized_rows = [self._serialize_row(row) for row in rows]
        total_count = len(serialized_rows)

        mat_text = requested_material or target_type or "matching"
        if threshold is not None:
            max_sql = """
                SELECT MAX(f.size_area_um) AS max_area_um
                FROM flake AS f
                WHERE (:type_label IS NULL OR f.type_label = :type_label)
            """.strip()
            max_params: dict[str, Any] = {"type_label": target_type}
            if graphene_only:
                max_sql += " AND (f.thickness_layers = 1 OR f.thickness_label IN ('1', 'monolayer', '1L'))"
            elif graphite_gate_only:
                max_sql += " AND LOWER(f.thickness_label) = 'manylayer'"
            if scope_clause:
                max_sql += f" AND {scope_clause}"
                max_params.update(scope_params)
            max_rows = self._run_sql(max_sql, max_params)
            max_area = max_rows[0].get("max_area_um") if max_rows else None

            if not serialized_rows:
                if max_area is None:
                    answer = f"No {mat_text} flakes with recorded area were found."
                else:
                    answer = (
                        f"No {mat_text} flakes satisfy area {operator} {threshold:g} µm². "
                        f"Largest recorded area is {float(max_area):.2f} µm²."
                    )
            else:
                largest = serialized_rows[0]
                area = largest.get("size_area_um")
                area_text = f"{float(area):.2f}" if isinstance(area, (int, float)) else "unknown"
                answer = (
                    f"Found {total_count} {mat_text} flake(s) with area {operator} {threshold:g} µm². "
                    f"Largest match is flake #{largest.get('type_index')} on chip {largest.get('chip_id')} "
                    f"with area {area_text} µm²."
                )
        elif not serialized_rows:
            mat_text = target_type or "any"
            answer = f"No {mat_text} flakes have recorded dimensions."
        else:
            largest = serialized_rows[0]
            mat_text = target_type or largest.get("type_label") or "flake"
            area = largest.get("size_area_um")
            area_text = f"{area:.1f}" if isinstance(area, (int, float)) else "unknown"
            chip_id = largest.get("chip_id")
            slot = (
                largest.get("table_coordinate_left")
                or largest.get("table_coordinate_right")
                or largest.get("table_coordinate")
                or "unknown slot"
            )
            location = (
                f"({largest.get('location_x')}, {largest.get('location_y')})"
                if largest.get("location_x") is not None and largest.get("location_y") is not None
                else "unknown location"
            )
            answer = (
                f"On the newest exfoliated chip (chip {chip_id}), the largest {mat_text} flake is "
                f"#{largest.get('type_index')} at {location} in {slot} (area ≈ {area_text} µm²)."
                if chip_scope_requested and not chip_id_match
                else f"Largest {mat_text} flake is #{largest.get('type_index')} on chip {chip_id} at {location} "
                f"in {slot} (area ≈ {area_text} µm²)."
            )
            if largest.get("image_path"):
                answer += f" Optical image: {largest['image_path']}."

        payload = {
            "question": question,
            "intent": "summary",
            "filters": filters,
            "sql": sql,
            "parameters": params,
            "rows": serialized_rows,
            "total_count": total_count,
            "answer": answer,
            "aggregate": "flake_area_threshold" if threshold is not None else "flake_area",
            "source": "flake_rule",
        }
        return json.dumps(payload, ensure_ascii=False)

    def _respond_with_plan(self, question: str, plan: QueryPlan) -> str:
        rows = self._run_sql(plan.sql, plan.params)
        serialized_rows = [self._serialize_row(row) for row in rows]
        total_count = None
        if plan.count_sql:
            count_rows = self._run_sql(plan.count_sql, plan.params)
            if count_rows:
                total_count = count_rows[0].get("count")
        answer = self._render_answer(question, plan, serialized_rows, total_count)
        returned_count = len(serialized_rows)
        payload = {
            "question": question,
            "intent": plan.intent,
            "filters": plan.filters,
            "sql": plan.sql,
            "parameters": plan.params,
            "rows": serialized_rows,
            "total_count": total_count,
            "returned_count": returned_count,
            "limit_applied": plan.limit,
            "answer": answer,
            "aggregate": plan.aggregate.label if plan.aggregate else None,
            "source": "rule_based",
        }
        return json.dumps(payload, ensure_ascii=False)

    def _run_structured_query(
        self,
        *,
        question: str,
        intent: str,
        table: Optional[str],
        columns: Optional[Sequence[str]],
        filters: Optional[Any],
        order_by: Optional[str],
        limit: Optional[int],
    ) -> str:
        if not table:
            raise RuntimeError("Structured queries require 'table' to be specified.")
        safe_table = self._sanitize_identifier(table)
        select_clause = self._sanitize_columns(columns)
        where_clause, params, filter_descriptions = self._build_structured_where(filters)
        order_clause = self._build_order_clause(order_by)
        limit_clause = self._build_limit_clause(limit)

        sql_parts = [f"SELECT {select_clause}", f"FROM {safe_table}"]
        if where_clause:
            sql_parts.append(where_clause)
        if order_clause:
            sql_parts.append(order_clause)
        if limit_clause:
            sql_parts.append(limit_clause)
        sql = " ".join(sql_parts)

        count_sql = f"SELECT COUNT(*) AS count FROM {safe_table}"
        if where_clause:
            count_sql = f"{count_sql} {where_clause}"

        plan = QueryPlan(
            intent=intent or "list",
            sql=sql,
            params=params,
            result_mode="rows",
            filters=filter_descriptions,
            where_clause=where_clause or "",
            order_by=order_by,
            limit=limit,
            count_sql=count_sql,
            aggregate=None,
        )
        return self._respond_with_plan(question, plan)

    def _build_structured_where(self, filters: Optional[Any]) -> tuple[str, dict[str, Any], list[str]]:
        normalized = self._normalize_filters(filters)
        if not normalized:
            return "", {}, []

        clauses: list[str] = []
        params: dict[str, Any] = {}
        descriptions: list[str] = []
        param_index = 0
        for spec in normalized:
            column = self._sanitize_identifier(spec["column"])
            operator = spec["operator"]
            value = spec["value"]

            # The UI and agent prompts commonly describe hBN bins with units
            # (for example, ``30-40nm``), while material_base stores the
            # categorical label without units (``30-40``).  Normalize at the
            # database boundary so an otherwise valid inventory row is not
            # reported as missing because of display-unit formatting.
            if column.rsplit(".", 1)[-1].lower() == "thickness_label":
                value = self._normalize_thickness_label_filter_value(value)

            if operator == "IN":
                values = self._ensure_sequence(value)
                if not values:
                    raise RuntimeError(f"Filter for {column} uses IN but no values were provided.")
                placeholders = []
                for item in values:
                    param_name = f"p_{param_index}"
                    params[param_name] = item
                    placeholders.append(f":{param_name}")
                    param_index += 1
                clause = f"{column} IN ({', '.join(placeholders)})"
                descriptions.append(f"{column} IN {list(values)}")
            elif operator in {"IS NULL", "IS NOT NULL"}:
                clause = f"{column} {operator}"
                descriptions.append(clause)
            else:
                if value is None and operator in {"=", "=="}:
                    clause = f"{column} IS NULL"
                    descriptions.append(clause)
                    clauses.append(clause)
                    continue
                if value is None and operator in {"!=", "<>"}:
                    clause = f"{column} IS NOT NULL"
                    descriptions.append(clause)
                    clauses.append(clause)
                    continue
                param_name = f"p_{param_index}"
                params[param_name] = value
                clause = f"{column} {operator} :{param_name}"
                descriptions.append(f"{column} {operator} {value!r}")
                param_index += 1

            clauses.append(clause)

        where_clause = "WHERE " + " AND ".join(clauses)
        return where_clause, params, descriptions

    @classmethod
    def _normalize_thickness_label_filter_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            text_value = value.strip().replace("\u2013", "-").replace("\u2014", "-")
            range_match = re.fullmatch(
                r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(?:nm)?",
                text_value,
                flags=re.IGNORECASE,
            )
            if range_match:
                low = f"{float(range_match.group(1)):g}"
                high = f"{float(range_match.group(2)):g}"
                return f"{low}-{high}"

            plus_match = re.fullmatch(
                r"(\d+(?:\.\d+)?)\s*\+\s*(?:nm)?",
                text_value,
                flags=re.IGNORECASE,
            )
            if plus_match:
                lower_bound = f"{float(plus_match.group(1)):g}"
                return f"{lower_bound}+"
            return value

        if isinstance(value, Sequence):
            return [cls._normalize_thickness_label_filter_value(item) for item in value]
        return value

    def _normalize_filters(self, filters: Optional[Any]) -> list[dict[str, Any]]:
        if filters is None:
            return []
        if isinstance(filters, str):
            parsed = json.loads(filters)
            return self._normalize_filters(parsed)
        if isinstance(filters, dict):
            normalized: list[dict[str, Any]] = []
            operator_map = {
                "eq": "=",
                "ne": "!=",
                "neq": "!=",
                "gt": ">",
                "gte": ">=",
                "ge": ">=",
                "lt": "<",
                "lte": "<=",
                "le": "<=",
                "like": "LIKE",
                "ilike": "ILIKE",
                "in": "IN",
                "is_null": "IS NULL",
                "is_not_null": "IS NOT NULL",
            }

            for key, value in filters.items():
                if isinstance(value, dict):
                    for raw_operator, raw_value in value.items():
                        op_key = str(raw_operator).strip().lower()
                        operator = operator_map.get(op_key)
                        if not operator:
                            raise RuntimeError(f"Unsupported operator '{raw_operator}' in filter for '{key}'.")
                        normalized.append({
                            "column": key,
                            "operator": operator,
                            "value": raw_value,
                        })
                    continue

                # Django/ORM-style suffix in the key: "size_area_um__gte": 864
                column = key
                operator = "IN" if self._should_use_in_operator(value) else "="
                if "__" in key:
                    base, _, suffix = key.rpartition("__")
                    mapped = operator_map.get(suffix.strip().lower())
                    if base and mapped:
                        column, operator = base, mapped

                normalized.append({"column": column, "operator": operator, "value": value})
            return normalized
        if isinstance(filters, Sequence):
            normalized: list[dict[str, Any]] = []
            for entry in filters:
                if not isinstance(entry, dict):
                    raise RuntimeError("Filter entries must be dictionaries.")
                column = entry.get("column")
                value = entry.get("value")
                operator = entry.get("operator", "=").strip().upper()
                if not column:
                    raise RuntimeError("Filter entries must include 'column'.")
                if isinstance(column, str) and "__" in column and operator == "=":
                    base, _, suffix = column.rpartition("__")
                    mapped = operator_map.get(suffix.strip().lower())
                    if base and mapped:
                        column, operator = base, mapped
                if operator == "=" and self._should_use_in_operator(value):
                    operator = "IN"
                if operator == "IN" and isinstance(value, str):
                    value = [value]
                if operator not in {"=", "!=", "<>", "<", ">", "<=", ">=", "LIKE", "ILIKE", "IN", "IS NULL", "IS NOT NULL"}:
                    raise RuntimeError(f"Unsupported operator '{operator}' in filter.")
                normalized.append({"column": column, "operator": operator, "value": value})
            return normalized
        raise RuntimeError("Filters must be provided as a dict, list of dicts, or JSON string.")

    @staticmethod
    def _should_use_in_operator(value: Any) -> bool:
        if value is None or isinstance(value, str):
            return False
        if isinstance(value, Sequence):
            return len(list(value)) > 1
        return False

    def _sanitize_columns(self, columns: Optional[Sequence[str]]) -> str:
        if columns is None:
            return "*"
        if isinstance(columns, str):
            columns = [columns]
        sanitized: list[str] = []
        for raw in columns:
            column = raw.strip()
            if not column:
                continue
            sanitized.append(self._sanitize_identifier(column))
        return ", ".join(sanitized) if sanitized else "*"

    def _sanitize_identifier(self, identifier: str) -> str:
        token = identifier.strip()
        if token == "*":
            return "*"
        alias_parts = re.split(r"\s+AS\s+", token, flags=re.IGNORECASE)
        if len(alias_parts) == 2:
            base = self._sanitize_identifier(alias_parts[0])
            alias = self._sanitize_identifier(alias_parts[1])
            return f"{base} AS {alias}"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\.]*", token):
            raise RuntimeError(f"Invalid identifier '{identifier}'.")
        return token

    def _build_order_clause(self, order_by: Optional[str]) -> str:
        if not order_by:
            return ""
        clauses: list[str] = []
        for raw_part in order_by.split(","):
            part = raw_part.strip()
            if not part:
                continue
            tokens = part.split()
            column = self._sanitize_identifier(tokens[0])
            direction = ""
            if len(tokens) > 1:
                dir_token = tokens[1].upper()
                if dir_token not in {"ASC", "DESC"}:
                    raise RuntimeError(f"Invalid ORDER BY direction '{tokens[1]}'.")
                direction = f" {dir_token}"
            clauses.append(column + direction)
        if not clauses:
            return ""
        return "ORDER BY " + ", ".join(clauses)

    def _build_limit_clause(self, limit: Optional[int]) -> str:
        if limit is None:
            return ""
        if not isinstance(limit, int) or limit <= 0:
            raise RuntimeError("LIMIT must be a positive integer.")
        return f"LIMIT {limit}"

    def _ensure_sequence(self, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, Sequence):
            return list(value)
        return [value]

    @staticmethod
    def _normalize_comparison_operator(op: str) -> str:
        normalized = op.strip().lower()
        if normalized in {">=", "greater than or equal to", "at least", "not less than"}:
            return ">="
        if normalized in {"<=", "less than or equal to", "at most", "not more than"}:
            return "<="
        if normalized in {">", "greater than", "more than", "over", "above", "larger than"}:
            return ">"
        if normalized in {"<", "less than", "below", "under", "fewer than", "smaller than"}:
            return "<"
        return "="

    @staticmethod
    def _infer_flake_material_filter(
        normalized_question: str,
    ) -> tuple[Optional[str], bool, bool, Optional[str]]:
        """
        Returns:
        - canonical type_label filter
        - graphene_only (requires monolayer)
        - graphite_gate_only (requires the categorical manylayer label)
        - requested material label for human-readable responses
        """
        if re.search(r"\bgraphene\b", normalized_question):
            return GRAPHITE_CODE, True, False, "graphene"
        if re.search(r"\bgraphite\s+(?:back\s+|top\s+|bottom\s+)?gate\b", normalized_question):
            return GRAPHITE_CODE, False, True, "graphite gate"
        if re.search(r"\bgraphite\b", normalized_question) or re.search(r"\bgr\b", normalized_question):
            return GRAPHITE_CODE, False, False, "graphite"
        if "boron nitride" in normalized_question or re.search(r"\bhbn\b", normalized_question):
            return "hBN", False, False, "hBN"
        if re.search(r"\bwte-?2\b", normalized_question):
            return "WTe2", False, False, "WTe2"
        return None, False, False, None

    def _run_llm_pipeline(self, question: str, intent: Optional[str]) -> Optional[str]:
        if not self._fallback.available:
            return None
        llm_plan = self._fallback.plan(question, intent)
        if not llm_plan:
            return None
        rows = self._run_sql(llm_plan.sql, {})
        serialized_rows = [self._serialize_row(row) for row in rows]
        answer = self._fallback.summarize(question, llm_plan, serialized_rows)
        if not answer:
            answer = self._default_llm_summary(llm_plan, serialized_rows)
        payload = {
            "question": question,
            "intent": llm_plan.intent,
            "filters": [],
            "sql": llm_plan.sql,
            "parameters": {},
            "rows": serialized_rows,
            "total_count": len(serialized_rows) if llm_plan.intent == "list" else None,
            "answer": answer,
            "aggregate": None,
            "source": "llm_fallback",
            "reasoning": llm_plan.reasoning,
            "model": llm_plan.model or self._fallback.model_id,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _default_llm_summary(self, plan: LLMQueryPlan, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "LLM fallback executed the query but no rows were returned."
        preview = "; ".join(self._describe_generic_row(row) for row in rows[: min(5, len(rows))])
        return f"LLM fallback returned {len(rows)} rows. Sample: {preview}"

    def _describe_generic_row(self, row: dict[str, Any]) -> str:
        preferred_keys = ("id", "label", "name", "table_coordinate_left", "table_coordinate_right", "current_slot_id", "status")
        parts: list[str] = []
        for key in preferred_keys:
            if key in row and row[key] is not None:
                parts.append(f"{key}={row[key]}")
        if not parts:
            for key, value in row.items():
                parts.append(f"{key}={value}")
                if len(parts) >= 3:
                    break
        return ", ".join(parts)

    def _should_use_llm(self, question: str, plan: QueryPlan) -> bool:
        normalized = question.lower()
        if any(keyword in normalized for keyword in self._LLM_TRIGGER_KEYWORDS):
            return True
        if plan.intent == "summary" and not plan.filters:
            if any(token in normalized for token in ("per device", "by device", "by flake", "grouped", "join")):
                return True
        return False

    def _render_answer(
        self,
        question: str,
        plan: QueryPlan,
        rows: list[dict[str, Any]],
        total_count: Optional[int],
    ) -> str:
        if plan.result_mode == "scalar":
            if plan.intent == "count":
                value = rows[0]["count"] if rows else 0
                return self._format_scalar_answer(value, plan.filters)
            if plan.aggregate:
                value = rows[0]["value"] if rows else None
                return self._format_aggregate_answer(value, plan.aggregate, plan.filters)
            return "No result available."

        if not rows:
            scope = " with " + ", ".join(plan.filters) if plan.filters else ""
            return f"No chips match the request{scope}."

        preview = []
        for row in rows[: min(5, len(rows))]:
            preview.append(self._describe_row(row))
        returned = len(rows)
        if total_count is not None:
            if plan.limit and total_count > returned:
                total_text = f"Showing {returned} of {total_count} chips (limit {plan.limit})"
            else:
                total_text = f"{total_count} chips match"
        else:
            total_text = f"{returned} chips returned"
        return f"{total_text}. Examples: " + "; ".join(preview)

    def _format_scalar_answer(self, value: Any, filters: list[str]) -> str:
        filter_text = ""
        if filters:
            filter_text = " when " + ", ".join(filters)
        return f"{value} chips match{filter_text}."

    def _format_aggregate_answer(self, value: Any, aggregate: AggregateSpec, filters: list[str]) -> str:
        if value is None:
            scope = " under the requested filters" if filters else ""
            return f"No data to compute {aggregate.label}{scope}."
        unit = f" {aggregate.units}" if aggregate.units else ""
        scope = " when " + ", ".join(filters) if filters else ""
        return f"{aggregate.label} = {value}{unit}{scope}."

    def _describe_row(self, row: dict[str, Any]) -> str:
        label = row.get("label") or "chip"
        slot = (
            row.get("table_coordinate_left")
            or row.get("table_coordinate_right")
            or row.get("table_coordinate")
            or row.get("current_slot_id")
        )
        last_update = row.get("last_updated_date")
        fragments = [f"#{row.get('id')}", label]
        if slot:
            fragments.append(f"slot {slot}")
        if last_update:
            fragments.append(f"updated {last_update}")
        return " ".join(str(part) for part in fragments if part)

    def _serialize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        serialized: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                serialized[key] = value.isoformat()
            elif key in ("image_path", "path") and isinstance(value, str):
                normalized_path = value.replace("\\", "/")
                if "images/" in normalized_path:
                    normalized_path = "images/" + normalized_path.split("images/", 1)[1]
                serialized[key] = normalized_path
            else:
                serialized[key] = value
        return serialized

    def _get_known_labels(self) -> tuple[str, ...]:
        if self._known_labels:
            return self._known_labels
        with material_session_scope() as session:
            rows = session.execute(text("SELECT DISTINCT label FROM chip WHERE label IS NOT NULL"))  # type: ignore[arg-type]
            labels = [row[0] for row in rows if row[0]]
            self._known_labels = tuple(sorted({label for label in labels if isinstance(label, str)}))
        return self._known_labels

    @staticmethod
    def _validate_and_rewrite_sql(sql: str, table_hint: Optional[str] = None) -> str:
        """
        Validate SQL against known schema and auto-correct common column name errors.
        
        Common LLM mistakes:
        - chip_id / flake_id → id (in their respective tables)
        - location → stage_position_x, stage_position_y (chip table) or location_x, location_y (flake table)
        - status → (doesn't exist, should be removed or use is_used for flake table)
        - current_stage → (doesn't exist)
        """
        # Define actual schema for each table
        CHIP_COLUMNS = {
            'id', 'label', 'creation_date', 'last_updated_date',
            'stage_position_x', 'stage_position_y',
            'corner_location_x', 'corner_location_y', 'corner_image_id',
            'pickup_left_x_mm', 'pickup_left_y_mm', 'pickup_left_angle_deg',
            'pickup_right_x_mm', 'pickup_right_y_mm', 'pickup_right_angle_deg',
            'angle_deg', 'width_mm', 'height_mm', 'confidence',
            'present',
            'table_coordinate_left', 'table_coordinate_right',
            'current_slot_id', 'is_searched', 'notes', 'metadata'
        }
        
        FLAKE_COLUMNS = {
            'id', 'chip_id', 'type_label', 'type_index',
            'location_x', 'location_y', 'date_created',
            'size_width_um', 'size_length_um', 'thickness_layers',
            'size_area_um', 'cleanliness', 'is_used', 'device_id', 'metadata', 'image_id', 'image_20x_id', 'mask_path', 'mask_20x_path'
        }
        
        # Column rewrites (common mistakes → correct)
        COLUMN_REWRITES = {
            'chip_id': 'id',  # In chip table context
            'flake_id': 'id',  # In flake table context
        }
        
        # Detect which table is being queried
        sql_lower = sql.lower()
        is_chip_query = 'from chip' in sql_lower or 'from "chip"' in sql_lower
        is_flake_query = 'from flake' in sql_lower or 'from "flake"' in sql_lower
        
        # Auto-rewrite common mistakes
        rewritten_sql = sql

        # LLM-generated SQL may use human-facing hBN labels such as
        # '30-40nm'.  The database stores thickness bins as '30-40', so
        # canonicalize quoted range/plus labels before executing the query.
        def normalize_quoted_thickness(match: re.Match[str]) -> str:
            quote = match.group("quote")
            raw_label = match.group("label")
            normalized_label = DatabaseQueryTool._normalize_thickness_label_filter_value(raw_label)
            return f"{quote}{normalized_label}{quote}"

        rewritten_sql = re.sub(
            r"(?P<quote>['\"])(?P<label>\d+(?:\.\d+)?\s*(?:[-\u2013\u2014]\s*\d+(?:\.\d+)?|\+)\s*nm)(?P=quote)",
            normalize_quoted_thickness,
            rewritten_sql,
            flags=re.IGNORECASE,
        )
        
        # Handle chip_id and flake_id based on context
        if is_chip_query and re.search(r'\bchip_id\b', sql, re.IGNORECASE):
            rewritten_sql = re.sub(r'\bchip_id\b', 'id', rewritten_sql, flags=re.IGNORECASE)
            print(f"[SQL_REWRITE] Replaced 'chip_id' with 'id' in chip table query", flush=True)
            
        if is_flake_query and re.search(r'\bflake_id\b', sql, re.IGNORECASE):
            rewritten_sql = re.sub(r'\bflake_id\b', 'id', rewritten_sql, flags=re.IGNORECASE)
            print(f"[SQL_REWRITE] Replaced 'flake_id' with 'id' in flake table query", flush=True)
        
        # Remove non-existent columns with helpful warnings
        nonexistent_patterns = [
            (r'\bstatus\b', 'status'),
            (r'\bcurrent_stage\b', 'current_stage'),
        ]
        
        for pattern, col_name in nonexistent_patterns:
            if re.search(pattern, rewritten_sql, re.IGNORECASE):
                # Try to remove the column from SELECT clause
                # Pattern: SELECT ..., status, ... or SELECT status, ... or SELECT ..., status FROM
                rewritten_sql = re.sub(
                    r',\s*' + pattern + r'\s*,', ',', rewritten_sql, flags=re.IGNORECASE
                )  # Middle position
                rewritten_sql = re.sub(
                    r'SELECT\s+' + pattern + r'\s*,', 'SELECT ', rewritten_sql, flags=re.IGNORECASE
                )  # First position
                rewritten_sql = re.sub(
                    r',\s*' + pattern + r'\s+FROM', ' FROM', rewritten_sql, flags=re.IGNORECASE
                )  # Last position
                
                # Try to remove from WHERE clause
                rewritten_sql = re.sub(
                    r'WHERE\s+' + pattern + r'\s*=\s*[^\s]+\s+AND\s+', 'WHERE ', rewritten_sql, flags=re.IGNORECASE
                )
                rewritten_sql = re.sub(
                    r'AND\s+' + pattern + r'\s*=\s*[^\s]+', '', rewritten_sql, flags=re.IGNORECASE
                )
                rewritten_sql = re.sub(
                    r'WHERE\s+' + pattern + r'\s*=\s*[^\s]+', '', rewritten_sql, flags=re.IGNORECASE
                )
                
                print(f"[SQL_REWRITE] Removed non-existent column '{col_name}'", flush=True)
        
        # Handle ambiguous 'location' references
        if re.search(r'\blocation\b', rewritten_sql, re.IGNORECASE):
            if is_chip_query:
                # For chip table, there's no single 'location' column
                print(f"[SQL_ERROR] Ambiguous column 'location' in chip table. Use 'stage_position_x', 'stage_position_y', or 'table_coordinate_left/right'", flush=True)
                raise ValueError(
                    "Column 'location' does not exist in chip table. "
                    "Use 'stage_position_x', 'stage_position_y' for coordinates, "
                    "or 'table_coordinate_left/right' for slot position."
                )
            elif is_flake_query:
                # For flake table, suggest location_x and location_y
                print(f"[SQL_ERROR] Ambiguous column 'location' in flake table. Use 'location_x' and 'location_y'", flush=True)
                raise ValueError(
                    "Column 'location' does not exist in flake table. "
                    "Use 'location_x' and 'location_y' instead."
                )
        
        return rewritten_sql

    @staticmethod
    def _run_sql(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        safe_sql = sql.strip().lower()
        if not (safe_sql.startswith("select") or safe_sql.startswith("with")):
            raise ValueError("Only SELECT queries are permitted.")
        
        # Validate and rewrite SQL before execution
        try:
            validated_sql = DatabaseQueryTool._validate_and_rewrite_sql(sql)
        except ValueError as e:
            print(f"[SQL_VALIDATION_ERROR] {e}", flush=True)
            raise RuntimeError(f"Database query validation failed: {e}")
        
        with material_session_scope() as session:
            try:
                rows = session.execute(text(validated_sql), params).mappings().all()
                return [dict(row) for row in rows]
            except Exception as e:
                # Provide helpful error message
                error_msg = str(e)
                print(f"[SQL_EXECUTION_ERROR] {error_msg}", flush=True)
                print(f"[SQL_QUERY] {validated_sql}", flush=True)
                print(f"[SQL_PARAMS] {params}", flush=True)
                raise RuntimeError(f"Database query failed: {error_msg}")
