# Private run data

`runs/` is local, private and ignored by Git. A push does not back it up.
Scrubbed simulation replays belong in `recordings/`; moving raw runs into an
archive does not make them safe to publish.

## Layout

```text
runs/
  workspace/
    twin/                current launcher: independent-focus laboratory twin
    integrated/          preserved preceding integrated workspace and checkpoints
                         original two-arm files remain directly under workspace/
  checks/
    manual/              direct CLI serve/lab/export defaults
    model/               check_model.py live model checks
    stream/              probe_model_stream.py transport checks
    acceptance/          historical acceptance source runs (recorder retired)
    demos/               record_demo.py source runs
    v1compare/           v1/v2 comparison results
  archive/
    2026-10-26/          prior reviews, migration trials and Claude session evidence
  openai-budget.db       persistent shared phase ledger; never reset during cleanup
  layout.json            local relocation map; preserve with data and ledger
  README.md              local inventory and preservation receipt location
```

`scripts/record_hallbar_night.py` writes the raw run to
`runs/checks/<name>`. It then exports the recording to `recordings/<name>`,
which is where History lists it. The export clones each photo (APFS
`clonefile` on macOS, otherwise a hard link) instead of copying it, so a
recording takes almost no extra disk space. Run photos are never rewritten.

Checks use distinct output folders and keep failed attempts as evidence.
The microscope-mechanics upgrade uses a fresh `workspace/twin/` database rather
than overlaying older stage-Z-focus checkpoints. Raw historical sessions can be
exported and replayed without resuming their device state. No old files or
spending scopes are reset by this new workspace.
Reports such as `chaos.md` and `not-verifiable.md` also go under `checks/`.
Use descriptive names for new runs; original dated experiment names stay intact.
The word `openai` remains in the budget filename and SDK session files because
those identify an actual adapter/ledger format, not a second workspace.

## Archived Claude data

The current application has no Claude SDK execution path and does not create
`runs/claude`. Its former `projects/` tree contains unique session transcripts,
subagent records and tool-result images. These are historical run evidence,
preserved under `archive/2026-10-26/claude/`. The obsolete CLI identity/settings
file, its backup, cleanup timestamp and empty session directory are removed.
No Claude configuration is loaded from the archive.

Earlier raw databases, migration trials, screenshots and diagnostic scripts
are also archived. Old root databases and their shared images/timeline are
kept together in `archive/2026-10-26/legacy-root/`. Archived scripts are evidence,
not supported entry points; use the tracked current scripts for new checks.

## Relocation and continuity

Stop the application and lab process before moving an existing workspace.
Verify there are no active turns, pending instructions or unfinished jobs.
Keep each database with its WAL/SHM sidecars, SDK sessions, images, timeline,
bench-state file and billing snapshot. Never move a database while it is open.

On the owner's existing checkout, the 26 October move is recorded in
`runs/layout.json`: `version: 1`, `original_root` (the prior absolute runs path),
and `moves` (old relative path to new relative path). Do not edit or remove it
as a cache. Old stored image paths resolve through this map; the original
OpenAI budget scope remains stable. Original event payloads and ledger rows
are retained. New clones have no historical moves and need no manifest.

Use the new path explicitly when exporting an archived database, for example:

```sh
.venv/bin/python -m qumus2 export --db runs/workspace/qumus.db --out recordings/my-run --last-session
```

Exporting private data for publication still requires the normal simulation
and secret-scrubbing checks. The relocation resolver never allows an image
outside the selected run directory to be served or exported.

## What can be removed

Regenerable Python caches, lint/test caches and empty output directories can
be deleted when their processes are stopped. Environments can be rebuilt with
`scripts/setup_qumus.py`; `.venv-v1compare` must remain isolated. Editor hiding
only changes the file view; it never removes these environments.

Preserve unique runs, failed attempts, original images, SDK sessions, budget
ledgers, the relocation map and reactor evidence. A replay is a presentation
export, not a complete replacement for its raw source run. Do not deduplicate
images across runs: identical pixels can still belong to separate observations.
