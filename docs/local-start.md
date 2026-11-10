# Open Qumus

On this Mac, open Finder, press **Command + Shift + G**, enter
`~/Projects/Qumus`, and press Return. Double-click **Start Qumus.command**.
Qumus opens at **http://127.0.0.1:8765**. It runs in the background, so the
Terminal window may be closed. After restarting the computer, run the same
launcher again.

The launcher is safe to run any number of times, from any folder or terminal
(Jialei 2026-11-06):

| Situation | What Start does |
| --- | --- |
| Qumus is not running | Starts it in the background and opens the browser. |
| Qumus runs this code version | Only opens the browser. |
| Qumus runs older code, was started by an older launcher, or does not answer | Asks once “Restart it now?” (Return = yes), stops it safely and starts the new one. While it is working on a task the default is no. |
| Another program holds port 8765 | Says which program; nothing is stopped. |

**Stop:** double-click **Stop Qumus.command** (next to Start), or run
`scripts/start_qumus.py --stop`. It sends the same signal as Control+C:
agents pause at a safe point and the lab saves its state.
`scripts/start_qumus.py --status` says whether Qumus runs, since when, which
code version and where its log is (`runs/workspace/logs/`).

**Closing the browser stops it.** When no Qumus page has been open for 15 s,
Qumus stops by itself (again the same as Control+C). The wait lets a reload or
an accidentally closed tab come back. While agents or a laboratory job are
working, it keeps running and stops once that work ends and still no page is
open, so closing a tab does not cut off an unattended run.

How it knows: the running server holds an exclusive lock on
`runs/workspace/qumus-server.lock`, which records its pid, port, code version
and log. The operating system releases the lock whenever the process ends,
however it ends, so the record can never be stale. A server started before
this lock existed is recognised by its command line (`qumus2 serve` on this
workspace database, started in this repository). Other Qumus servers, such as
a scratch replay on another port, are never touched.

`--restart` restarts without asking about the version; `--yes` answers the
question with yes. There is no terminal-bound mode any more.

## On Windows

The brain (this repository) and the simulated laboratory (`qumus-simlab`) run on
Windows 10/11 with Python 3.11 or newer, from two clones side by side
(2026-11-10). Nothing in this section drives real instruments; those are
`qumus-lab`'s.

```bat
git clone https://github.com/papaya2003/qumus-agent.git
git clone https://github.com/papaya2003/qumus-simlab.git
cd qumus-simlab
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd ..\qumus-agent
py -3 scripts\setup_qumus.py
.venv\Scripts\python.exe scripts\check_platform.py
```

`check_platform.py` is a free one-command self-check: scripted agents, no model
calls, no instruments, a temporary folder and a free port for the brain (the
sim lab's own ports 8781-8783 must be free). It starts qumus-simlab from the
clone beside this one and prints PASS/FAIL for install, encoding, start, run,
stop and export.

Not in Git, copied by hand from the Mac: the model gateway's `.env` (never
commit it) and the private Hall-bar target bundle (`target-a518-v1chip`, passed
with `--target-bundle`). A live run is started like on the Mac, with the
environment variables set by `set NAME=value` (cmd) or `$env:NAME="value"`
(PowerShell) instead of `NAME=value` in front of the command. Turn off sleep in
the power settings for long runs: a sleeping computer lets the module leases
expire, and the lab pauses its jobs.

Then double-click **Start Qumus.bat** / **Stop Qumus.bat** in the repository
folder (setup also writes copies next to it, like the Mac `.command` files).

What differs from the Mac, and why:

| | macOS | Windows |
| --- | --- | --- |
| Launcher | `Start/Stop Qumus.command` (zsh) | `Start/Stop Qumus.bat` (`scripts\qumus_launcher.bat`) |
| Environment | `.venv/bin/python` | `.venv\Scripts\python.exe` (qumus-simlab's too) |
| Workspace lock | `flock`, inherited by the server | byte-range lock (`msvcrt`) that the server takes itself (`start_qumus.py --serve-holding-lock`) |
| Stop | HTTP stop request (token in the lock file), else SIGINT then SIGTERM | HTTP stop request, else `taskkill /T /F` (a signal from outside cannot be caught on Windows) |
| Keep awake | `caffeinate -dimsu -w <pid>` | power settings: never sleep while plugged in |
| Text files | UTF-8 | UTF-8 everywhere (`encoding="utf-8"` on every read/write; the launcher also sets `PYTHONUTF8=1`), because the Windows default is the locale code page (GBK) |

Checked without a Windows machine: every text read/write in both repositories
names UTF-8 (tests scan all calls; ruff rule PLW1514 catches new ones), the
brain starts under a non-UTF-8 default encoding, POSIX-only calls are scanned
for, and the Windows code paths are exercised by tests that pretend to be
Windows plus a full self-check with the Windows start path
(`check_platform.py --handoff`). Only a real Windows run can confirm:
double-clicking the `.bat` files, that the background server survives closing
the window, the `msvcrt` lock and `taskkill` fallback. Run `check_platform.py`
there once.

Run folders still record photo paths as absolute paths of the computer that
made them, so a run folder copied between the Mac and a Windows computer does
not resume; exported recordings (History) are self-contained and do move.

## Current local versions

| Version | Entry | Purpose |
| --- | --- | --- |
| Current Qumus | http://127.0.0.1:8765 | Everyday laboratory, saved work, History and recordings; start with the root launcher. |

This page is not half of a v1/v2 benchmark pair: frozen v1 lives under
`v1compare/v1` and uses its isolated comparison environment. It has no second everyday web launcher.

## Saved workspaces

The normal launcher opens the fresh Hall-bar workspace at
`runs/workspace/hallbar-linked-owner-20261101/qumus.db`, with the owner-imported eight-contact wafer,
material tapes and six blank source chips. No hBN or graphene flakes are
preloaded. It retains the three-arm simulated bench, normal error model and
shared safety limits. The launcher requires exactly one existing private bundle
under `targets/<source-sha256>/target.json`, with its original GDS sibling.
Missing or ambiguous bundles block startup; the synthetic target is not a fallback.
The first two arm frames are linked through version1 storage stage2. Board widths
and the provisional450 mm reach remain simulation assumptions.

The current presentation candidate exposes the map and imported target; complete
stacking and the new Astra recording remain pending local acceptance. The intended
short goal for the accepted experiment is:
“Make an hBN/graphene Hall device on the supplied eight-electrode wafer, starting from tapes.”
Simulation cannot contribute real laboratory learning.

There is one entry point (Jialei 2026-11-04): the launcher always opens this
workspace, and recordings are watched in **History**. There is no separate
playback page and no launcher switch for older workspaces; their folders under
`runs/workspace/` are kept untouched.

The launcher repairs the two states that used to block startup:

- **Closed without Control+C.** A leftover `qumus.db-wal` is copied to
  `runs/workspace/backups/wal-<time>/` and checkpointed into the database, then
  startup continues. This only happens while the launcher holds the workspace
  lock and nothing holds port 8765.
- **Made by older code or for another target.** When the saved bench geometry,
  saved laboratory state or imported target no longer matches, the whole
  workspace is moved intact to `runs/workspace/archive/<name>-<time>/`, the
  target bundle is kept, and a fresh workspace starts. The terminal prints why.

Developers can still inspect an old database read-only with
`qumus2 export --db <old qumus.db> --out <folder> --last-session`; the result
appears in History once it is under `recordings/`.

## KLayout Designer setup

Eight-contact design creation uses the existing KlayoutClaw bridge at
`http://localhost:8767/mcp`, separately from Qumus on port8765. Install KLayout
with KlayoutClaw0.6, then apply the small repository patch below from the repository
root. It adds atomic new-layout execution so a design cannot overwrite a user's
active layout. The exact original/patched hashes guard this specific extension;
an unknown plugin build is refused and an already patched file is left alone.
These hashes target this workspace's configured bridge; other upstream releases
or local modifications need a reviewed patch rather than bypassing the guard.
Backups stay outside the auto-loaded `pymacros` directory.

```sh
(
  set -e
  plugin="$HOME/.klayout/pymacros/klayoutclaw_server.lym"
  patch_file="$PWD/scripts/patches/klayoutclaw-0.6-atomic-new-layout-v1.patch"
  before="ffa53c8337c8ec8bdcc6746aa2c78b4b7655ff679c78a27188d32d4ef4f96a50"
  after="c2c86a9ffe5856643f47720fa68cbac11e3badb7546378a7e62674978d4c5fea"
  current="$(shasum -a 256 "$plugin")"; current="${current%% *}"
  if [ "$current" = "$after" ]; then
    echo "Atomic layout extension is already installed."; exit 0
  fi
  if [ "$current" != "$before" ]; then
    echo "Unknown KlayoutClaw plugin bytes; patch refused." >&2; exit 1
  fi
  cd "$HOME/.klayout/pymacros"
  patch --dry-run -p0 < "$patch_file"
  backup_dir="$HOME/.klayout/qumus-backups"
  backup="$backup_dir/klayoutclaw-0.6-$before.lym.backup"
  mkdir -p "$backup_dir"
  if [ -e "$backup" ]; then cmp -s "$plugin" "$backup"; else cp -p "$plugin" "$backup"; fi
  patch -p0 < "$patch_file"
  current="$(shasum -a 256 "$plugin")"; current="${current%% *}"
  if [ "$current" != "$after" ]; then
    cp -p "$backup" "$plugin"
    echo "Patched hash differs; original plugin restored." >&2; exit 1
  fi
  echo "Disk patch verified. The running application was not changed."
)
```

On a fresh installation, save your own layouts before restarting KLayout to
load the extension. Do not rerun the full server macro while its listener is
active. This Mac's existing bridge was already updated in place; it needs no
restart for this extension. Designer checks the live `execute_script` schema for
`atomic_new_layout` before creating anything and requires its
`atomic-new-layout-v1` confirmation. Missing capability, connection or readback
blocks acceptance without a batch fallback. Saved historical batch artifacts
remain readable. The adapter's native GDS viewport is visual evidence; it does
not certify a physical device or replace the full stacking checks.

## Watch a demo

1. Open **History** in the Qumus sidebar.
2. Under **Demo recordings**, choose a recording.
3. Review its **Results**, **Progress**, **Conversation**, or **Experience**.
4. Press **Play recording**, or choose a key moment on the right.
5. Press **Back to live** to return to your current workspace.

Playback makes no model requests and costs nothing. The experience shown in
an individual demo belongs to that recording; it does not replace the live
lab's notebook. Simulated runs do not count as real experimental experience.
There is no separate review website or per-recording server to start.

### What to show

History distinguishes **Accepted delivery recordings** from **Historical recordings**.
The new Hall-bar Astra recording remains pending until local process acceptance,
actual model execution and replay checks pass. Earlier recordings remain
accessible as history; they do not certify this optimization. Each demo retains
its original live model decisions, scripted operator inputs and fixture metadata.
Playback is always free. See the [catalog](../recordings/README.md).

For this round's available results, open **History → This round · diagnostic
evidence**, select a diagnostic, and start with **Evidence**. This shows the
owner's eight fixed electrode ends, original microscope images and the actual
source-selection failure. **Play recording** replays the corresponding scripted
preparation/search run. Both diagnostics stopped before stacking and made zero
model calls; neither is the requested Astra recording. The optical animation is
separately labeled as an illustration, not a recorded transfer. **Back to live**
returns to the current workspace. These local diagnostic exports contain no
new laboratory learning and do not require another server or launcher.

When a live job is waiting for a person, open its job record in Results.
After inspecting and resolving the reported problem, enter your name and what
you reviewed under **Review this job hold**, then select **Record human review**.
This only parks the job for normal recovery; it does not restart instruments or
declare the experiment successful. Ask Qumus to recover the job as a separate
instruction. Unsafe states and stale reviews remain blocked. Continue alone
does not release a human-required hold, and replay offers no review action.

Open **History → Current run → Context** for live total context usage, or
select a past run and its **Context** tab.
Context also reports cache-token hit share, request-hit frequency and
known/unknown usage coverage from provider receipts. A missing cache field is
unknown, not a zero hit. These measurements alone do not identify whether
a low hit rate originates at the gateway or in prompt composition.
Context debugging is kept out of Results. **Actual usage** fits the usage axis;
**Full capacity** includes recorded model limits. Switching smoothly zooms the
scale (or switches immediately when reduced motion is preferred). Agent rows
align with the bars; filter by agent and scroll horizontally through long runs.
Slim bars show each request's reported input plus output as one total. The closed,
rounded navigator scrolls through requests. Capacity segments use each request's
recorded model, session and limit; gaps and model changes remain separate.
Select a bar or agent mark to see
its caller, target model, token counts, capacity ratio, remaining space and retry
identity in the right panel. Details are not duplicated below the chart.
**Settings → Model** automatically matches the administrator table by full model
ID on the Token Station gateway. The chart shows used tokens (input + output)
and remaining space after each reported response. Context limit is read-only and
shows its gateway catalog source. Table entries of zero and unmatched models stay unknown.
The right-hand **Context composition** separates measured input/output totals
from approximate input-source categories. New requests record those categories;
older recordings without them keep an explicit missing-data message.
Limits are remembered separately for each model and gateway. Missing usage stays
unknown. Older saved token reports remain
visible, but character-only recordings cannot be converted into tokens.

Results uses **left category → middle cards → right details**. Lab uses the same
navigation pattern. Progress preserves tool → job → lab step hierarchy. Problems
keeps recovered refusals as history; seeking before recovery shows them unresolved.
The simulated bench diagram follows recorded state; original images stay available.

### Review mother-tape materials

Open **Lab → Stock → Mother tapes & daughter tape**. Each mother tape shows
its inventory material, storage location and human review state. The current
simulation declares **Gr** in `left_slide_1` and **hBN** in `left_slide_2`;
these labels do not mean a person has confirmed the physical material.

In the live workspace, open a mother tape, enter the reviewing person's name
and an optional label/preparation-record note, then choose **Confirm … label**
or **Identity uncertain**. Use physical labels or preparation records, not
camera appearance. An uncertain tape is excluded from new exfoliation jobs.
The review is refused while the tape is reserved/in use or the model is busy.
This form reviews the current inventory label; it does not rename or replace
a physical tape. Historic demos show recorded labels and remain read-only.

**Problems** spans the app directly above the bottom status bar. Expand it
for wrapped messages and recovered history. Scenario switching is immediate;
the slide animation was removed after visual review.

### Choose a model or check spending

Settings is a full page. Choose the default for Qumus and the team, then optionally
choose a custom model beside a specialist. **Use default model** follows future
default changes. **Use default for everyone** and **Discard changes** edit the
draft; **Save settings** saves the complete team. Finish or resolve the current
Task Plan first.

Open **Settings → Model** in the live workspace. Choose a conversational model
from the gateway catalog, then **Save settings**. Changes take effect from the next
plan. Conservative spending rates are under **Budget guard rates**.
All catalog model types are listed. Specialized decision/image/video entries can
be selected for inspection, but need their own API adapter before they can run
agent conversations.
This saves the selection across restarts and starts fresh conversation context;
the lab, history and accumulated budget remain. Finish or resolve unfinished
work first. Switching itself makes no model request. Recorded replies stay
unchanged. The catalog distinguishes administrator registrations, website entries,
and the current account's model list. Administrator-only entries can be inspected;
they do not establish permission to call a model. Catalog presence does not prove
successful access or tool/image compatibility.

Dated administrator quotes appear alongside their source and account-applicability
status. They do not silently replace the conservative budget guard rates or
establish a current account invoice. Unknown aliases and capacities stay unknown.

An explicitly sourced zero nominal input/output quote may be saved as zero.
A missing quote stays unknown; neither nominal zero nor a catalog listing proves
the account's billed rate. Cache-read rates need their own matching source.
These rates enforce a local estimated budget; they are not an invoice. For a fair
model comparison, use the same task and initial bench state in separate trials;
switching alone does not reset the bench or run a comparison.

**Settings → Cost & billing** shows a dated, scoped snapshot from
Token Station and links to its Usage page. The normal model API key cannot read
that signed-in billing endpoint, so this snapshot does not refresh itself.
It covers the gateway account, not an individual demo. When the snapshot includes
per-model token totals, Settings estimates workspace cost using each model's
average billed USD per total token. Cache and output mix may change that estimate;
requests with unreported usage, unknown gateway identity or no calibration are
excluded. If no request can be calibrated, the workspace estimate is unavailable. The conservative
budget guard remains separate. Replay explicitly says it is free.

## Work with Qumus

Send a short goal in **Progress**, for example:
“Find a graphene monolayer. Show me the candidates before choosing.”
Send follow-up messages while it works; Qumus reads them at a tool boundary.
Use **Stop** to pause and **Continue** to resume. Review photos in **Lab** and
**Results**; the current bench and all images are simulated.

Sending a task uses model credit. The launcher uses OpenAI Agents SDK, a
persisted software testbench, and an estimated cumulative workspace cap of
$40. The current first Hall-bar recording has a separate cumulative estimated
USD100 cap, including qualification and retries, enforced by its campaign
adapter. It does not change the everyday workspace cap. Historical ledger entries and unknown
reservations remain intact. Configured-rate estimates are not actual provider
charges; actual billing is on Token Station's Usage page. Opening the app or
watching a demo does not call the model.

To shut down Qumus, close its browser pages (it stops about 15 s later)
or double-click **Stop Qumus.command**. Restarting restores the same workspace
and may resume unfinished testbench jobs.

## VS Code

Open the folder `~/Projects/Qumus` with **File → Open Folder**. Choose
**Terminal → New Terminal** and run:

```sh
./"Start Qumus.command"
```

The same command also works in a terminal opened inside `v2`, which has its
own `Start Qumus.command` and `Stop Qumus.command` running the same launcher.
`.venv/bin/python scripts/start_qumus.py` from `v2` is equivalent. The
terminal is free again once Qumus is up. No VS Code extension is needed.

## Where things belong

| Location in `~/Projects/Qumus` | Contents |
| --- | --- |
| `Start Qumus.command` | The only everyday launcher (also in `v2/`). |
| `Stop Qumus.command` | Stops Qumus from anywhere (also in `v2/`). |
| `START HERE.txt` | Short instructions. |
| `v2/` | The active Git repository. |
| `v2/recordings/` | Self-contained, scrubbed simulation demos shown in History. |
| `v2/runs/` | Private live databases, original test records, images, and cost ledger. |
| `v2/scripts/` | Developer utilities, not a second set of desktop launchers. |
| `notes/` | Current planning, confirmed issues and v1 research; start with `notes/README.md`. |
| `archive/` | Earlier plans, handoffs, exports, backup bundles and validation logs; the 2026-10-26 cleanup has its own manifest. |
| `v2/docs/archive/` | Version-controlled historical workstream, architecture and migration records. |
| `worktrees/vdw-reactor-simulation/` | Preserved integration worktree; main now contains the optional module. |

Keep `v2/.env` private. Use `v2/.venv` for the current application;
`v2/.venv-v1compare` is retained for frozen v1 comparison dependencies. Do not move either
environment by renaming its directory: executable entry points contain absolute paths.
See [setup](runtime.md) to rebuild one.

The launcher and editor settings can be regenerated with
`python3 scripts/setup_qumus.py --local-only --refresh-launcher` from `v2/`.
For a fresh clone, omit `--local-only` to create the modern environment.
See [run data](run-data.md) for current, check and archived data locations.

Only `v2/` is this Git repository. The root launcher, `notes/` and `archive/`
are local files and are not included in a push of `v2/main`. The compatibility
`Qumus-v2` entry is a symlink, not another active checkout. The older
`work/archived-development-copy-20261025` retains historical code and run data;
its unused environments were removed on 26 October. Do not launch it.

## Troubleshooting

- **Cannot connect:** start `Start Qumus.command`; Qumus stops by itself about
  15 s after its last page closed.
- **Port 8765 in use:** start `Start Qumus.command` again: it opens or
  restarts a Qumus it recognises, and names any other program on the port.
- **Model/key error:** check `v2/.env` against `v2/.env.example`. Never paste
  the key into an issue or chat.
- **Missing image:** the recording must include its `media/` folder. Keep it
  together with `recording.json`; replays do not fetch images from the gateway.
- **Missing environment:** rebuild it using the setup guide.

The old command-line playback (`qumus2 replay`) and the terminal-bound start
were removed on 2026-11-06: History is the only way to watch recordings and the
launcher is the only way to start Qumus.
