# Qumus documentation

This is the brain of the Qumus agent system. Start with the
[repository README](../README.md) and the [architecture](architecture.md)
(brain, stations, the record feed and stations.yaml).

| Read | Purpose |
| --- | --- |
| [Architecture](architecture.md) | Brain vs stations, two stores and one feed, stations.yaml, the page |
| [Adaptation plan](adaptation-plan.md) | What changed from qumus-v2 and the steps (history of the split) |
| [Open questions](open-questions.md) | Defaults chosen without the owner |
| [Contract client](contract-client.md) | The client every station uses |
| [Repository instructions](../AGENTS.md) | Development, privacy, runtime and hardware rules |
| [Local startup](local-start.md) | Mac setup, the single launcher, History and recordings |
| [Hall-bar workflow](hallbar-workflow.md) | Steps and owner decisions of the Hall-bar task |
| [OpenAI runtime](runtime.md) | Environments, configuration, budgets and validation |
| [Agent model settings](agent-model-settings.md) | Choosing a model per agent |
| [Context composition](context-composition.md) | How request context is measured and shown |
| [Run data](run-data.md) | Private workspace, checks, archive and relocation rules |

## Reactor

| Read | Purpose |
| --- | --- |
| [Reactor in its own words](reactor-native.md) | Brain reactor tools over the reactor's Lab Console (2026-11-10) |
| [Reactor gaps](reactor-gaps.md) | What still blocks the real reactor station |
| [Reactor model](reactor-model.md) | The simulated apparatus (now in qumus-simlab) and workflow limits |
| [Owner-review workflow contract](reactor-owner-review-20261027.md) | The material-metal workflow contract |
| [Reactor hardware](reactor-hardware.md) | Hardware assumptions for a real reactor |
| [Legacy pair contract](reactor-pair-contract.md) | Two-flake preparation and product lineage |
| [vdW hierarchy](vdw-workflow-hierarchy.md) | Atomic, molecular and assembly boundaries |

The simulated laboratory's own documentation (testbench, simulation contracts,
stacking process v1, material simulation, stage cooling) moved to qumus-simlab.

Project notes, the v1 research notes and the electrode design files are kept
outside Git. Never commit lab data, GDS files, keys or run databases.
