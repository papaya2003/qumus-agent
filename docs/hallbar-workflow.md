# Hall-bar workflow

Qumus makes an hBN/graphene Hall bar on a pre-made eight-electrode chip
(`chip_e01`), starting from tapes, in the simulated laboratory. This is the
current workflow; the reactor and material-metal workflows remain implemented
but are not the current target. Nothing here operates real hardware.

## Steps

The scripted scenario `qumus2/testbench/scenarios/hallbar_device.yaml` and the
live model follow the same plan.

| Step | Agent | What happens |
| --- | --- | --- |
| s1 Device plan | advisor | Reads the electrode geometry and experience, submits the layer stack: hBN on top, monolayer graphene on the electrode ends, global Si back gate |
| s2 Check chips and carriers | eyes | Lists chips and crystal carriers on the bench |
| s3 Exfoliate and search | hands, picker | One hBN and one graphene chip first. If no pair fits all eight contacts, one more chip of the limiting material per round, up to the nine bare chips on the bench |
| s4 Lay out the device | designer | KLayout layout on the selected pair; `QUMUS_KLAYOUT_BACKEND=python` for headless runs |
| s5 Stack the device | hands | PC stamp picks up hBN, hBN picks up graphene aligned to the design, the stack is released onto the electrodes |

The electrodes exist before stacking, as in Fig. 4 of the Qumus paper. The
Hall-bar shape comes from the electrodes; graphene is not etched.

## Owner decisions this workflow follows

Program operations match version 1. When the simulated laboratory and a
version 1 operation disagree, the simulation changes, not the procedure
(owner, 4 November 2026). Approved exceptions, all 4 November:

- staged pickup speeds and the v2 observation cadence;
- the first touch beside the flake (#136);
- an anti-crash step cap that stops and asks a person;
- look-back and re-press.

Other decisions in force:

- Graphene is monolayer only and must lie fully inside the main hBN. Extra
  hBN may land on electrodes; the only forbidden overlap is graphene bridging
  two electrodes (3 November).
- Design angle margin is 3 degrees, because the chip is not rotated and the
  version 1 single-corner ORB angle leaves about 1.4 degrees of twist
  (5 November).
- Graphene is lifted only by hBN: when the carried hBN covers at least 80% of
  the graphene area the whole flake is picked up, otherwise it stays; bare PC
  cannot lift graphene (5 November; `MIN_CARRIED_COVER` in
  `qumus2/testbench/world.py`).
- After the drop, version 1 archives photos only. There is no automatic final
  geometric check, so a recording must not claim one (4 November).

If code and this list disagree, report it to the owner. Do not change either
silently.

## Running a recording

`scripts/record_hallbar_night.py` runs one recording and exports it to
`recordings/<name>`, where History lists it. Without `--openai` it uses the
scripted agents and is free. With `--openai` it uses the configured model and
spends API credit, capped by `--budget`.

```sh
.venv/bin/python scripts/record_hallbar_night.py --bundle <target dir> --seed 3
.venv/bin/python scripts/record_hallbar_night.py --bundle <target dir> \
    --resume runs/checks/<earlier workspace> --openai --budget 10
```

Save points: just before the picker reads the flake candidates, the run saves
the simulated laboratory and run store under `<workspace>/checkpoints/`.
`--resume` starts there instead of exfoliating and searching again; a live
run can resume a scripted save point. Resuming refuses when a file that
decides the search has changed (`GUARDED` in `qumus2/testbench/checkpoint.py`).
Keep the earlier workspace: resumed records point at its photos.

The target bundle and every recording embed the electrode design. Keep them
out of GitHub. Where raw data goes is described in [run data](run-data.md).

## Where the code is

| Part | Location |
| --- | --- |
| Bench profile `hallbar-linked` | `qumus2/config/`, `qumus2/testbench/hallbar.py` |
| Exfoliation and 10X search | `qumus2/flows/library/exfoliation.py`, `search.py` |
| 20X/100X revisit order | `qumus2/flows/library/revisit_priority.py` |
| Pair selection and placement | `qumus2/agents/tools/picking.py` |
| Device layout | `qumus2/agents/tools/design.py`, `qumus2/agents/klayout.py` |
| Stacking | `qumus2/flows/library/stacking.py`, `pickup_evidence.py` |
| Simulated laboratory | `qumus2/testbench/world.py`, `render.py`, `optics.py` |
| Save points | `qumus2/testbench/checkpoint.py` |
| Recording and export | `scripts/record_hallbar_night.py`, `qumus2/ui/recording.py` |
