from __future__ import annotations

import sys
from typing import Any, Dict, List

from smolagents import Tool

from agents.physics_expert.physics_expert import (
    FORCE_OPTIONS_PROMPT,
    SYSTEM_PROMPT,
    call_model,
    has_options,
)


MINILAB_PHYSICS_EXPERT_INSTRUCTIONS = """
You are a physics expert providing guidance on fabrication and device physics for a lab that creates and stacks 2D materials. Your advice should be grounded in physics principles, literature when available, and practical fabrication knowledge.

Capability policy:
- This is a guidance role, not a fabrication-execution role. Do not hide scientifically appropriate device designs merely because some steps may need human or external fabrication.
- Qumus can directly execute only its known hardware workflows: exfoliation/search/stacking of available 2D materials and database-backed inventory operations.
- Human operators may provide or approve source/drain electrodes, bottom metal contacts, lithography, deposition, etching, cleaning, annealing, activation, CVD/ALD-grown materials, or other externally fabricated pieces.
- Mark every recommended material, layer, and process with one of these execution tags:
  `qumus_candidate`, `human_provided`, `external_fab`, `literature_reference`, `needs_inventory_check`, or `needs_human_review`.
- Human review decides what is usable. Do not reject a good physics design only because it is outside Qumus hardware; annotate the dependency instead.

Advice rules:
- Provide physics-driven device structures, target thicknesses, size/coverage requirements, contact/gate choices, activation or post-process notes, and the reason each item matters.
- If a recommendation uses Ni, Pd, Ti/Au, Cr/Au, EBL, evaporation, etching, annealing, cleaning, CVD, ALD, or any other non-Qumus step, keep it in the plan and tag it as `human_provided` or `external_fab`.
- For Qumus-controlled exfoliation parameters only, respect the operating bounds supplied by the project manager/Qumus. For human/external steps, provide literature-style targets and mark them for human approval.
- For hBN exfoliation done by Qumus, prefer room temperature and low peel speed to preserve clean interfaces unless lab history strongly suggests otherwise.
- Treat device examples from the user, literature, or history as evidence, not as built-in templates. Do not force a requested device into a fixed graphene/ReSe2 recipe.
- Design the fabrication order for the current goal. Do not constrain the device to one clean-room visit. Identify every clean-room operation explicitly, including lithography, deposition, etch, metallization, passivation, contacts/electrodes, and alignment masks; mention batching only as an optional convenience when it does not compromise device quality.

DEVICE_GUIDANCE_REQUEST output contract:
Return a JSON-compatible object with these exact top-level keys whenever Qumus asks for device guidance:
- `device_name`
- `research_goal`
- `workflow_strategy`
- `workflow_plan`
- `overall_structure`
- `layer_stack`
- `material_requirements`
- `contact_scheme`
- `gate_scheme`
- `activation_or_postprocess`
- `flake_selection_targets`
- `layout_constraints`
- `fabrication_steps`
- `cleanroom_minimization_plan`
- `history_evidence`
- `literature_evidence`
- `capability_annotations`
- `open_questions_for_human`
- `needs_human_review`
- `confidence`

`workflow_strategy` must be a short explanation of why the chosen order fits the physics, fabrication dependencies, available Qumus actions, and clean-room process dependencies.
`workflow_plan` must be an ordered list generated for the current request, not a selection from a fixed enum. Each stage should include:
`stage`, `actor`, `goal`, `inputs_needed`, `outputs_expected`, `dependencies`, `execution_tag`, and `cleanroom_session_id` when relevant.
`cleanroom_minimization_plan` should name which steps require clean-room work, what each step produces, and which steps may be batched if the operator chooses. Do not set a one-session target unless the operator explicitly asks for it.

Use `layer_stack` as an ordered list from top to bottom. For each layer, include:
`order`, `role`, `material`, `target_thickness`, `coverage_or_size`, `contact_or_exposure_notes`, `execution_tag`, and `rationale`.

Keep `workflow_plan` physically consistent with `layer_stack`. If source/drain contacts are listed below the channel material in the stack, treat them as pre-patterned contacts that the channel overlaps unless the request explicitly specifies vias, windows, edge contacts, or post-encapsulation contact access. Do not schedule S/D metallization after a dielectric/top gate fully encapsulates the channel without naming the access method.

Available Qumus-searchable material labels include `Gr`, `hBN`, and `ReSe2` when corresponding raw carriers/chips exist. Graphene is `Gr` plus monolayer evidence. A graphite gate is the categorical database match `type_label='Gr'` and `thickness_label='manylayer'`; never request or filter graphite gates by a nanometer thickness because graphite thickness is not stored that way.
"""


def _build_system_prompt() -> str:
    return (
        SYSTEM_PROMPT.strip()
        + "\n\n"
        + "Additional mini-lab instructions:\n"
        + MINILAB_PHYSICS_EXPERT_INSTRUCTIONS.strip()
    )


class ProjectManagerPhysicsExpertTool(Tool):
    name = "physics_expert_tool"
    description = (
        "Consults the physics expert for fabrication and device-physics reasoning, "
        "augmented with editable mini-lab-specific instructions. When the Claude Agent "
        "SDK is available it answers with live internet search; otherwise it uses the "
        "configured chat model. Pass setup_context so the expert grounds advice in the "
        "current bench state."
    )
    inputs = {
        "query": {
            "type": "string",
            "description": "The physics or fabrication question to ask the expert.",
        },
        "setup_context": {
            "type": "string",
            "description": (
                "Optional current-lab-setup text (from get_lab_setup) so the expert only "
                "recommends materials/devices achievable on this bench."
            ),
            "nullable": True,
            "default": "",
        },
        "use_web": {
            "type": "boolean",
            "description": (
                "If true, strongly prefer the internet-search (Claude Agent SDK) backend. "
                "Use for 'which material', 'state of the art', or literature-grounded questions."
            ),
            "nullable": True,
            "default": False,
        },
        "force_assumptions": {
            "type": "boolean",
            "description": "If true, force the expert to proceed with reasonable assumptions.",
            "nullable": True,
            "default": False,
        },
        "clear_history": {
            "type": "boolean",
            "description": "If true, clear the tool's internal conversation history first.",
            "nullable": True,
            "default": False,
        },
        "allow_sdk_fallback": {
            "type": "boolean",
            "description": (
                "If false, NEVER use the Claude Agent SDK (web-search) backend — not even the "
                "opportunistic upgrade when the chat model mentions 'literature'. Set false when "
                "the answer must come from provided project defaults + lab history only "
                "(e.g. a DEVICE_GUIDANCE_REQUEST that matches a fixed project stack default)."
            ),
            "nullable": True,
            "default": True,
        },
    }
    output_type = "string"

    # Hard cap on Claude Agent SDK (web-search) subprocess spawns per tool
    # instance. One ProjectManagerPhysicsExpertTool is built per qumus_agent run,
    # so this is "at most N web searches per run". The project-manager agent tends
    # to re-call this tool across its reasoning steps; each spawn is a multi-minute
    # billed `claude` subprocess. 1 is enough for a material/device Q&A.
    SDK_SPAWN_BUDGET = 1
    # Hard cap on web-search `forward()` calls per run. After the first one has
    # produced the literature-grounded answer, every further web-search call
    # returns that same answer with a "stop calling" directive instead of doing
    # more work — the project-manager agent tends to re-call this tool to
    # "refine" a survey it already has.
    WEB_CALL_BUDGET = 1

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.messages: List[Dict[str, str]] = [{"role": "system", "content": _build_system_prompt()}]
        # Within-run cache of SDK (web-search) answers, keyed on (query, setup_context).
        self._sdk_cache: Dict[tuple[str, str], str] = {}
        self._sdk_spawns = 0
        self._last_sdk_text: str | None = None
        self._web_calls = 0

    def _transcript(self) -> str:
        lines = []
        for msg in self.messages:
            if msg["role"] == "system":
                continue
            lines.append(f"{msg['role'].capitalize()}: {msg['content']}")
        return "\n".join(lines)

    def _try_sdk(self, query: str, setup_context: str) -> str | None:
        cache_key = (query.strip(), (setup_context or "").strip())
        cached = self._sdk_cache.get(cache_key)
        if cached is not None:
            print(
                "[physics_expert_tool] reusing the cached web-search answer for an identical "
                "query in this run (no new claude subprocess).",
                file=sys.stderr,
            )
            return cached
        if self._sdk_spawns >= self.SDK_SPAWN_BUDGET:
            # Budget spent for this run — do not spawn another `claude` subprocess.
            # Reuse the first SDK answer if we have one; otherwise signal the caller
            # to fall back to the plain chat model.
            print(
                f"[physics_expert_tool] Claude Agent SDK web-search budget "
                f"({self.SDK_SPAWN_BUDGET}/run) already spent; "
                + ("reusing the first web-search answer." if self._last_sdk_text
                   else "using the plain chat model for this call."),
                file=sys.stderr,
            )
            return self._last_sdk_text
        try:
            from agents.physics_expert.sdk_agent import run_claude_sdk_physics_expert
        except Exception as exc:
            print(
                f"[physics_expert_tool] Claude Agent SDK backend import failed ({exc}); "
                "falling back to the plain chat model (no web/literature search).",
                file=sys.stderr,
            )
            return None
        self._sdk_spawns += 1
        result = run_claude_sdk_physics_expert(
            query,
            system_prompt=_build_system_prompt(),
            setup_context=setup_context or "",
            conversation_context=self._transcript(),
        )
        if result.get("status") == "ok" and result.get("text"):
            text = str(result["text"])
            self._sdk_cache[cache_key] = text
            self._last_sdk_text = text
            return text
        print(
            "[physics_expert_tool] web search requested but the Claude Agent SDK backend is "
            f"unavailable (status={result.get('status')!r}: {result.get('reason', 'unknown')}). "
            "Falling back to the plain chat model — the answer will NOT be literature-grounded. "
            "Set ANTHROPIC_API_KEY (or ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN) to enable it.",
            file=sys.stderr,
        )
        return None

    def forward(
        self,
        query: str,
        setup_context: str = "",
        use_web: bool = False,
        force_assumptions: bool = False,
        clear_history: bool = False,
        allow_sdk_fallback: bool = True,
    ) -> str:
        if clear_history:
            self.messages = [{"role": "system", "content": _build_system_prompt()}]
            self._sdk_cache.clear()

        wants_web = bool(use_web and allow_sdk_fallback)

        # One web-search call per run: after the first, hand back the same answer
        # and tell the agent to stop calling this tool.
        if wants_web and self._web_calls >= self.WEB_CALL_BUDGET and self._last_sdk_text:
            print(
                f"[physics_expert_tool] web-search call budget "
                f"({self.WEB_CALL_BUDGET}/run) already used - returning the first "
                "answer; do not call physics_expert_tool again.",
                file=sys.stderr,
            )
            directive = (
                "[physics_expert_tool: the single web-search call for this run was already "
                "made. The complete physics-expert answer is below. Use it as-is to write the "
                "final response and DO NOT call physics_expert_tool again this run.]\n\n"
            )
            self.messages.append({"role": "user", "content": query})
            self.messages.append({"role": "assistant", "content": self._last_sdk_text})
            return directive + self._last_sdk_text

        effective_query = query
        if setup_context.strip():
            effective_query = (
                "CURRENT LAB SETUP:\n" + setup_context.strip() + "\n\nQUESTION:\n" + query
            )

        self.messages.append({"role": "user", "content": effective_query})

        response: str | None = None
        if wants_web:
            self._web_calls += 1
            response = self._try_sdk(query, setup_context)

        if response is None:
            # No "opportunistic upgrade" to the SDK: the plain chat model answers
            # unless the caller explicitly asked for web search via use_web=True.
            # (The old heuristic fired the multi-minute SDK web search whenever the
            # answer merely contained the word "literature" — which a
            # DEVICE_GUIDANCE_REQUEST always does — and made the tool hang.)
            response = call_model(self.messages, use_web=False)

        self.messages.append({"role": "assistant", "content": response})

        if force_assumptions and not has_options(response):
            self.messages.append({"role": "user", "content": FORCE_OPTIONS_PROMPT})
            response = call_model(self.messages, use_web=False)
            self.messages.append({"role": "assistant", "content": response})

        return response
