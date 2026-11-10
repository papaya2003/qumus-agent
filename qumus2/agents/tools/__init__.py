"""Built-in tools. Importing this package registers them in DEFAULT_REGISTRY.

One module per agent (v1 agent in brackets): project = orchestrator
(qumus_agent), advisor (project_manager), inventory = eyes (lab_manager),
hardware + recovery = hands (processing), picking = picker (flake_selection),
design = designer (design_agent); experience = the lab's experience book
(spec 10, self-evolution; advisor and orchestrator).
"""

from qumus2.agents.tools import (  # noqa: F401
    advisor,
    design,
    experience,
    hardware,
    inventory,
    picking,
    project,
    reactor,
    recovery,
)
from qumus2.reactor import tools as reactor_console  # noqa: F401  (reactor Lab Console tools)
