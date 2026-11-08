"""Tool contract (spec 6.1, 6.3). CONTRACT for the SDK-migration workstream.

A Qumus tool is a plain async Python function plus metadata:

    @qumus_tool("place_chip_on_transfer_stage", "Move a chip ...",
                agents={"hands"}, kind="hardware", refs={"chip_id": "chip"})
    async def place_chip(args: PlaceChipArgs, ctx: ToolContext) -> ToolResult: ...

- `args` is a pydantic model: it is both the JSON schema shown to the model and
  the validation done before the call (bad args never reach the function).
- `agents` = which agents may call it. Enforced twice: the SDK `tools` list per
  agent, and the PreToolUse policy (agents/policy.py).
- `refs` = argument -> sample kind. Ids must exist in the sample ledger; the model
  cannot invent chip/flake ids.
- `requires` = names of cross-tool rules (agents/policy.py RULES).
- The function returns a ToolResult, never a bare string.

The same function runs under the OpenAI adapter (agents/openai_runtime.py)
and under the scripted runtime (agents/scripted.py),
always through agents/gateway.py, so hooks behave the same in both.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Literal

from pydantic import BaseModel

from qumus2.runtime.jobs import JobService
from qumus2.runtime.store import RunStore

MCP_SERVER = "qumus"
ToolKind = Literal["query", "state", "hardware", "job", "recovery", "notify"]
ToolStatus = Literal["ok", "corrected", "needs_attention", "denied", "error", "pending", "paused"]


def mcp_name(tool: str) -> str:
    return f"mcp__{MCP_SERVER}__{tool}"


def short_name(name: str) -> str:
    prefix = f"mcp__{MCP_SERVER}__"
    return name[len(prefix):] if name.startswith(prefix) else name


class ToolResult(BaseModel):
    status: ToolStatus
    summary: str  # one or two sentences the model reads first
    data: dict[str, Any] = {}
    images: list[str] = []  # file paths; the SDK adapter attaches them as image content

    def to_text(self) -> str:
        import json

        body = {"status": self.status, "summary": self.summary,
                "data": _for_model(self.data)}
        if self.images:
            body["images"] = [_file_name(p) for p in self.images]
        return json.dumps(body, ensure_ascii=False, default=str)


#: testbench ground-truth ids (simulator flake ids carried in ledger props and
#: detections, for scoring and tests): never shown to the model
_SIM_KEYS = frozenset({"sim_id"})
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff")


def _file_name(v: str) -> str:
    """An image file by its name only: the images themselves are attached, and
    an absolute path into some run directory made the model doubt a result
    (stage-1 acceptance E10, E3b). The records keep the full path."""
    return PurePath(v).name if PurePath(v).is_absolute() else v


def _for_model(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _for_model(x) for k, x in v.items() if k not in _SIM_KEYS}
    if isinstance(v, (list, tuple)):
        return [_for_model(x) for x in v]
    if isinstance(v, str) and v.lower().endswith(_IMAGE_SUFFIXES):
        return _file_name(v)
    return v


@dataclass
class ToolContext:
    store: RunStore
    jobs: JobService
    agent: str
    project_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Any, ToolContext], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Handler
    agents: frozenset[str]
    kind: ToolKind
    refs: dict[str, str] = field(default_factory=dict)
    requires: tuple[str, ...] = ()
    model_visible: bool = True

    @property
    def mcp_name(self) -> str:
        return mcp_name(self.name)

    def json_schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(short_name(name))

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def for_agent(self, agent: str) -> list[ToolSpec]:
        return [t for t in self._tools.values() if agent in t.agents and t.model_visible]


#: tools registered by @qumus_tool at import time
DEFAULT_REGISTRY = ToolRegistry()


def qumus_tool(name: str, description: str, *, agents: set[str] | frozenset[str],
               kind: ToolKind, refs: dict[str, str] | None = None,
               requires: tuple[str, ...] = (),
               model_visible: bool = True,
               registry: ToolRegistry | None = None) -> Callable[[Handler], Handler]:
    from qumus2.agents.roster import ALL_AGENTS, RESERVED

    unknown = set(agents) - set(ALL_AGENTS) - set(RESERVED)
    if unknown:
        raise ValueError(f"tool {name}: unknown agents {unknown}")

    def deco(fn: Handler) -> Handler:
        import inspect

        hints = inspect.get_annotations(fn, eval_str=True)
        args_model = hints.get("args")
        if not (isinstance(args_model, type) and issubclass(args_model, BaseModel)):
            raise TypeError(f"tool {name}: first parameter must be annotated with a BaseModel")
        (registry or DEFAULT_REGISTRY).add(ToolSpec(
            name=name, description=description, args_model=args_model, handler=fn,
            agents=frozenset(agents), kind=kind, refs=refs or {}, requires=requires, model_visible=model_visible))
        return fn

    return deco


def default_registry() -> ToolRegistry:
    import qumus2.agents.tools  # noqa: F401 - registers built-in tools

    return DEFAULT_REGISTRY
