# Qumus documentation

This is the agent system. Start with the [repository README](../README.md), the
[architecture](architecture.md) (three repositories and the window rules) and the
[adaptation plan](adaptation-plan.md) (what changed from qumus-v2 and what comes next).

## Agent system and lab windows

| Read | Purpose |
| --- | --- |
| [Architecture](architecture.md) | Agent side vs lab modules, contract v1, where things are |
| [Adaptation plan](adaptation-plan.md) | Kept / changed parts, old file to new role, steps |
| [Open questions](open-questions.md) | Defaults chosen without the owner |
| [Contract client](contract-client.md) | The client every window uses |
| [Reactor gaps](reactor-gaps.md) | What still blocks the reactor window |

## Simulated Hall-bar workflow (from qumus-v2)

The hBN/graphene Hall bar on the eight-electrode chip runs in the simulated
laboratory with the OpenAI Agents SDK runtime. Start with
the [Hall-bar workflow](hallbar-workflow.md). Dated reports from earlier rounds
are in [historical documents](archive/README.md); they do not describe current
behavior.

| Read | Purpose |
| --- | --- |
| [Repository instructions](../AGENTS.md) | Development, privacy, runtime and hardware rules |
| [Local startup](local-start.md) | Mac setup, the single launcher, History and recordings |
| [Hall-bar workflow](hallbar-workflow.md) | Steps, owner decisions, save points and where the code is |
| [Stacking process (v1)](stacking-process-v1.md) | The version 1 stacking sequence the simulation follows |
| [Simulation contracts](sim-contracts.md) | Agent/job/step/event/UI interfaces, evidence rules and known apparatus-model differences |
| [Testbench](testbench.md) | Simulated laboratory, faults and limitations |
| [OpenAI runtime](runtime.md) | Environments, configuration, budgets and validation |
| [Agent model settings](agent-model-settings.md) | Choosing a model per agent |
| [Context composition](context-composition.md) | How request context is measured and shown |
| [Run data](run-data.md) | Private workspace, checks, archive and relocation rules |
| [Current workstreams](workstreams.md) | Workstream map; live task status is in the GitHub issues |
| [Recording catalog](../recordings/README.md) | Recordings in History and how they are made |

## Reactor and material model (implemented, not the current workflow)

The reactor is a later v2 goal. Its code stays in the program and keeps its
own documentation.

| Read | Purpose |
| --- | --- |
| [Reactor model](reactor-model.md) | Simulated apparatus, hBN/2D-material/metal workflow and limits |
| [Owner-review workflow contract](reactor-owner-review-20261027.md) | The material-metal workflow contract |
| [Reactor hardware](reactor-hardware.md) | Hardware assumptions for a future real reactor |
| [Legacy pair contract](reactor-pair-contract.md) | Two-flake preparation and product lineage |
| [Material simulation](material-simulation.md) | Flake geometry, morphology profiles and local reaction |
| [vdW hierarchy](vdw-workflow-hierarchy.md) | Atomic, molecular and assembly boundaries |
| [Stage cooling](stage-cooling-20261028.md) | Thermal assumptions for ordinary stages and the reactor |

## Outside this repository

Project notes, the v1 research notes and the electrode design files are kept
outside Git. Never commit lab data, GDS files, keys or run databases.
