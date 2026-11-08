> Historical development evidence, superseded by the integrated optional reactor and current workstreams. Old startup/release instructions are not current.

# Overnight delivery goal

Deadline: 2026-10-26 08:00 America/New_York.
Baseline: main at 27e0566. Implementation branch:
`codex/vdw-reactor-simulation`.

## Latest owner scope change

This section overrides the original Goal's integration/release wording. Develop vdW
only in an isolated branch and worktree. Keep the canonical everyday checkout
`/Users/jialei/Projects/Qumus/v2` on the working main revision 27e0566 with its
launcher and existing recordings unchanged. Do not merge, release or deploy the
reactor extension until the owner has discussed open details with the experimental
colleagues and explicitly authorizes integration.

Exclude vdW from the group-meeting presentation and demonstration. The report
covers verified stable v2 accomplishments and evidence-based improvements over v1.
Review code, recordings, documentation, scientific content and slide formatting.
Preserve previous working recordings byte-for-byte and provide rollback steps.
The original Goal's automatic merge/update sentence is superseded by this scope.

The owner further clarified that these are two independent deliverables. The
stable-results report is the primary complete delivery for the morning. Reactor
development is a partial experimental advance to the extent supported by known
information, with explicit open questions for laboratory colleagues. A complete
reactor integration or a new paid reactor recording is not required for this
morning, and must not displace preparation and review of the stable-results talk.

## Deliverables

1. A vdW reactor extension of the Qumus testbench, agent team and UI, available
   only in the isolated experimental branch/worktree.
2. Updated contracts, model assumptions, operating instructions and acceptance evidence.
3. Separate experimental simulated recordings with a documented return to the
   previous working version. Stable presentation demos remain unchanged.
4. A concise English scientific presentation, editable PPTX and PDF, a five-minute
   English script with a ten-minute expansion, and demo/fallback instructions.

The presentation emphasizes demonstrated improvements from v1 to v2. Claims and
numbers must be supported by inspected implementation or executed comparisons.
Simulation results must not be presented as real laboratory performance.

## Owner-confirmed apparatus

- Manually cut, dedicated-size chips enter a new Storage Stage.
- A robotic arm places a chip into a 3D-printed carrier called the slot.
- The slot has exactly two planar translation axes. One axis provides both
  insertion/retraction and fine positioning, and the other fine lateral motion.
- The slot and chip do not rotate. The threaded drive produces linear motion.
- A rectangular reactor contains a circular passage and an observation window above it.
- The microscope stays fixed in the plane and moves vertically for focus.
- Temperature and gas flow are controllable. Microscope observations inform
  adjustments during the reaction.

## Authorized simulation development

The owner explicitly authorizes synthetic bench geometry and material-growth
parameters, literature research, a probabilistic growth model, paid Qumus model
API acceptance within configured budgets and regenerated simulated recordings.
Earlier permission to merge/update the reactor is revoked by the latest scope.

Unknown physical parameters do not block the testbench. Chosen geometry,
operating envelopes, kinetics and virtual sensors must be identified as
simulation assumptions. Literature motivates model structure and plausible
scales, rather than certifying this apparatus or a real material recipe.
No real hardware operation is involved.

## Implementation and review

- Extend devices, limits, SafetyGate, clock-driven simulation, closed-loop jobs,
  sample reservations, observation evidence and restart handling together.
- Add the experimental Reactor role with normal ToolGateway permissions and
  checked delegation outcomes. Reuse the existing app; preserve the stable launcher.
- Keep commands, measured readings and inferred growth estimates distinct.
- Couple growth to accumulated temperature/flow history, with reproducible
  stochastic variation. Extra image requests must not redraw growth outcomes.
- Exercise useful success, poor-growth, control-adjustment, fault, stop/resume
  and restart cases. Review the final diff separately from implementation.
- Run relevant regression checks, Ruff and pytest before pushing. Report actual
  results and any CI infrastructure failures accurately.
- Review every presentation slide for scientific claims, evidence, legibility,
  wrapping, alignment and consistency. Render and inspect the final deck.
- Prefer a few substantive slides over a feature catalog. Keep a usable report
  draft early and reserve the final hour for validation and packaging.

## Recording and rollback requirements

- Preserve existing recording folders and their media byte-for-byte.
- Create new recordings under new names. Retain provider/model and scripted
  operator-input provenance and run the recording integrity checks.
- Verify that previous recommended recordings still open and replay correctly.
- Retain the baseline Git revision and provide exact software rollback steps.
  Store new private run data separately so rollback does not discard it.
- Keep the single everyday launcher. Temporary development/replay checks do not
  replace it or constitute a reactor deployment.

## Completion evidence

The final delivery must identify the actual code revision, tested behavior,
recording entry points, rollback instructions, final presentation files and
remaining limitations. A deadline, a final agent message or a run status alone
does not prove that the requested outcome succeeded.

Preserve `v1compare/v1` against its manifest. Never commit private environment
files, billing, run databases or raw laboratory materials, or expose private
source/data publicly.
