"""The agent team (spec 3.1). CONTRACT between the orchestrator workstream and
everything that refers to agents by name (tool permissions, hooks, UI).

Agent names are stable identifiers used in tool permissions, events (`agent`
column) and the UI. The SDK sees subagents under these names.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qumus2.config import load_yaml

PROMPTS = Path(__file__).parent / "prompts"

ORCHESTRATOR = "orchestrator"
SUBAGENTS = ("advisor", "eyes", "hands", "picker", "designer", "reactor")
RESERVED: tuple[str, ...] = ()
ALL_AGENTS = (ORCHESTRATOR, *SUBAGENTS)

# One-line descriptions; for subagents this is what the orchestrator reads to
# decide whom to delegate to (agent description).
DESCRIPTIONS = {
    ORCHESTRATOR: "Qumus orchestrator: understands requests, keeps the Task Plan, delegates, "
                  "reports.",
    "advisor": "Project manager / advisor. Reads distilled experience and literature and "
               "proposes recipes and Device Guidance. Never touches hardware.",
    "eyes": "Lab manager. Checks inventory, instrument status and the bench through the "
            "overview camera. Read-only: cannot move anything.",
    "hands": "Processing. Runs hardware flows (exfoliation, chip search, stacking, placing "
             "chips) through job tools and handles their results. Reactor operations "
             "belong to Reactor.",
    "picker": "Flake selection. Looks at microscope images of candidate flakes and submits a "
              "selection that code validates.",
    "designer": "Device designer. Produces a device layout for the selected flakes and "
                "submits it for an independent code check.",
    "reactor": "vdW reactor specialist. Drives the reactor in its own vocabulary: person "
               "handover into a tray slot, crucible load and unload, temperature programs with "
               "gas, microscope pictures, stop. Available when stations.yaml has a reactor "
               "Lab Console station.",
}

UI_LABELS = {  # display names (the UI is in English for the whole lab)
    ORCHESTRATOR: "Orchestrator", "advisor": "Advisor", "eyes": "Eyes", "hands": "Hands",
    "picker": "Picker", "designer": "Designer", "reactor": "Reactor",
}


@dataclass
class AgentSpec:
    name: str
    description: str
    prompt: str
    max_turns: int


def load_roster() -> dict[str, AgentSpec]:
    cfg = load_yaml("agents.yaml")
    out: dict[str, AgentSpec] = {}
    for name in ALL_AGENTS:
        a = cfg["agents"][name]
        out[name] = AgentSpec(
            name=name,
            description=DESCRIPTIONS[name],
            prompt=(PROMPTS / f"{name}.md").read_text(),
            max_turns=int(a.get("max_turns", 40)),
        )
    return out
