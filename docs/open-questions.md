# Open questions and defaults chosen (night of 2026-11-08)

> **中文摘要（早上请 Jialei 看一下）：** 夜里没法问你，下面每一条都先按默认做了，都可以改。
> 最重要的三条：① 完整仿真留在本仓库，包成“仿真实验室”窗口（第 1 条）；② 仓库里的 Python 包名暂时还叫
> `qumus2`，没有改成 `qumus_agent`（第 2 条）；③ qumus-v2 的 305 MB 录像没有搬过来（第 3 条）。

Each item: the question, the default used tonight, and what changes if the owner decides otherwise.

1. **Where does the full simulation live?** Default: in this repo, served as the sim lab
   (`qumus2/simlab`) with the same contract as qumus-lab, so the agent runs a whole simulated
   Hall bar without hardware. qumus-lab keeps only its device-level `--demo`.
   Alternative: move it to qumus-lab; then `simlab/` moves there unchanged.
2. **Package name.** Default: keep `qumus2` (all imports, tests, launcher and recordings
   work unchanged). Renaming to `qumus_agent` is a mechanical follow-up.
3. **Recordings.** Default: not copied (305 MB, many from superseded workflows); they stay
   in the frozen qumus-v2 repo. Tests that need one are skipped unless
   `QUMUS_RECORDINGS_DIR` points to a qumus-v2 `recordings/` folder. Hall-bar recordings
   on the Mac (`hallbar-*`) can be opened by History as before.
4. **Whole jobs as window actions.** The sim lab offers each module's flows
   (`search_chip`, `stack_device`, ...) as actions. A job action is accepted while the
   module still runs other jobs (they queue, as in qumus-v2); a single motion command is
   refused (409) while jobs run. ADR 0001 has no queue; the alternative is to refuse and
   let the agent wait.
5. **Job bookkeeping in a flat body.** A job action carries `"job": {"project_id",
   "requested_by"}` next to the flow parameters, and the finished command's
   `last_command.data` holds the answer (e.g. `run_id`). Both are Qumus extensions; a
   client that ignores them still works.
6. **Lab-wide job controls** (`job_resume_all`, `complete_experiment`) are on the
   `stacking` window (the last module of the Hall-bar flow).
7. **Cameras.** The simulated table has one overview camera; both sim windows list it.
   The real lab has one desk camera per module (qumus-lab).
8. **Shared run store (sim only).** The agent and the sim lab use one SQLite file, so job
   progress, samples and images are read directly. With qumus-lab, progress must come from
   module state/events (adaptation plan step 6).
9. **Lease holding.** The agent takes a module's lease at its first command and keeps it
   until it closes. While it holds it, the lab PC debug page can still read and Stop, but
   cannot move things. Jialei may prefer the agent to release between jobs.
10. **Agent heat limit.** The `heat` action has no 40 C cap (that cap is for local L1-L2
    probes); the SafetyGate limits from `config/limits.yaml` apply, and a timeout is
    mandatory (<= 1 h).
11. **Reactor flows** stay in the in-process simulation for now; the real reactor joins
    through its own services (`qumus2/reactor`, [reactor-gaps.md](reactor-gaps.md)).
12. **Module events in recordings** (`module.*` records) are planned, not done yet.
