# DJ Treta — State of Play & Roadmap (2026-09-14, track A)

*Read-only review by Treta (fork), evening of 14-Sep. Baseline: `docs/STATE_OF_DJ_TRETA_2026-05-31.md` (10-agent
empirical audit on `v10`). Everything below was checked against git, the live DB, the daemon log and the daily logs —
not recalled. Where I could not verify, I say so.*

## 0. The one-paragraph truth

DJ Treta still works the way she worked on 29-Aug (last set, 25 sets in history) — **but she cannot be started tonight**
without the USB stick: the 2,674-track library, the Mixxx binary (`_RESTORE/mixxx-build/`) and the set folders all live
on `/Volumes/DJ-TRETA`, and it is not mounted. Since the 31-May audit the real shipped work is the **July 28–29
local-brain night** (menu pre-filter, flight recorder, phrase-lock, ACTION protocol, setlist mode, pluggable slow-loop
brains) — and **all 7 of those commits exist only on this Mac, unpushed for 47 days.** The Mixxx fork is in the same
state: 40 commits of Treta work, 470 commits not on our `origin`, **never pushed**, and now 1,031 commits behind upstream.
Nothing is on fire. Two irreplaceable things sit as single copies. Fix that before anything else.

## 1. Inventory — keep / park / delete

| Component | What it is | Last touched | Wired? | Tests | Verdict | Why |
|---|---|---|---|---|---|---|
| `~/beings/dj-treta` `agent/` (72 modules, 21K lines) | The Being: ADK runner, heartbeat, planner, transitions, setlist, brains | 29-Jul (`9b1ddf1`) | **YES** — `djtreta-daemon` → `.venv/bin/python -m agent` | 46 test files + 19 eval files | **KEEP** | This *is* DJ Treta. |
| branch `cloud/gateway-restore` (HEAD) | Working branch; = `bdl/adopt-lifecycle` + 7 local commits | 29-Jul | yes | — | **KEEP → PUSH TONIGHT** | 7 commits (local-brain night + setlist mode) exist nowhere else. 64 ahead of `main`. |
| `main` (dj-treta) | 03-May | stale | — | — | **PARK → fast-forward** | 118 commits behind HEAD; every reader of the public repo sees May. |
| 14 other local branches (`v10`, `v11-rachna`, `feat/*`, `fix/*`) | Merged or abandoned lines | ≤31-May | no | — | **DELETE locally** after confirming merged (`git branch --merged`); keep on origin | Noise. `v11-rachna` never diverged from v10 (audit §2). |
| Uncommitted: `helpers.music_dir_ready()` + `discovery.download_track` guard | 08-Aug volume guard: refuse writes when stick unmounted | 08-Aug | on disk, **not committed** | none | **KEEP → COMMIT** | Closes a real trap (crate forking onto internal disk). Add 1 test. |
| Untracked: `.env.bak`, `.env.pre-cloud-0807`, `config.yaml.bak`, `config.local.yaml.{stick-backup,LOCAL-MODEL-BACKUP}` | Config drift artifacts from Jul–Aug mode flips | 29-Jul / 07-Aug | no | — | **DELETE** after folding the two `config.local.yaml` variants into documented profiles (see §3) | Five files that each claim to be "the" config is how a boot goes wrong at 1 AM. |
| `config.local.yaml` brain block | gateway (`localhost:4000`) live; Ollama lines "LOCAL-PARKED"; `planner_candidate_cap: 0` | 07-Aug | yes | — | **KEEP → flip to local** (evolution 0) | Cap must go back to 40 on the local brain (NS-009). |
| `agent/setlist.py` (setlist mode) | "Hand her a set, she plays it in order"; code owns selection, she owns craft | 29-Jul | yes (`djtreta setlist`) | `test_commands` partial | **KEEP** | The only mode Manish has used since Aug (Bolly Techno 60, Psytrance-145 plans). |
| `agent/brain_providers.py` (pluggable slow-loop brains: claude-cli / codex-cli / litellm) | 29-Jul | yes, config-only | `test_brain_providers` | **KEEP** | Lets library_manager use a ₹0 CLI brain. |
| `agent/flight_recorder.py`, `planner_menu.py`, `json_extract.py` | Local-brain robustness set (NS-001/NS-009) | 28-Jul | yes | `test_json_mode`, planner tests | **KEEP** | Prerequisite for Qwen. |
| `mcp_server/` (Python MCP in dj-treta) | Newer MCP surface (auth, session_writer, tools) | ≤31-May | **NOT wired** — `~/.claude.json` still loads `~/beings/treta/skills/dj/mcp-server/dist/index.js` | — | **KEEP, then wire** | Audit P0: hardcoded Linux VM path in `session_writer.py:34`. Unknown if fixed — verify before wiring. |
| `~/beings/treta/skills/dj/mcp-server` (TS MCP, dist 04-Apr) | The MCP this Claude session actually uses (`dj_*` tools) | 04-Apr (dist), src 24-Mar | **YES** | — | **PARK** (keep until Python MCP is wired), then DELETE | Points at `~/Music/DJTreta` (empty) — `dj_list_tracks` returns nothing. Stale by 5 months. |
| `web/` (Svelte SPA scaffold) | NS-004 first attempt, superseded by cockpit route | 05-Jul | no | — | **DELETE** (code is in git history) | NS-004 pivoted to reuse dj-treta-live. `node_modules` on a 98%-full disk. |
| `vdj/` — 12 tracked files + **1.4 GB untracked media** (`explore/`, `downloads/`, `scenes/`, `library/`) | VDJ Treta beat-synced visual engine | 31-May | no (manual `serve.py`) | none | **PARK code, MOVE media to stick** | Engine graded A− on 31-May and never used since. Media is 1.4 GB of a disk with 12 GB free. |
| `tui.py`, `tui_state_source.py` | Textual TUI | ≤May | yes (`djtreta talk`) | — | **PARK** | Manish's 22-May direction: Mixxx is the cockpit, TUI redundant over time. Still the only chat surface when Mixxx cockpit isn't built. |
| `scripts/knowledge/` 320 MB | LanceDB/knowledge assets | Apr | `knowledge.enabled: false` | — | **PARK** | v9 knowledge planner off the live path since April (audit §4.7). |
| `.beings/dj-treta/history/*.jsonl` (4 files, May) | Being-side history export | May | no | — | **DELETE** or move under `docs/` | Untracked, unreferenced. |
| **`~/workspace/mixxx-treta`** (fork, `treta` = HEAD) | Mixxx 2.6-alpha + HTTP API `:7778` + DJ Treta sidebar/cockpit + FLX4 map | 27-Jun (+ uncommitted 13-Aug optional-WebSockets patch) | **YES** — the only audio engine she can drive | none (C++) | **KEEP → COMMIT patch → PUSH `treta`** | 470 commits not on `origin`; `origin` only mirrors upstream release branches. Single copy. |
| `mixxx-treta` uncommitted (`CMakeLists.txt`, `dlgdjtretachat.{cpp,h}`) | 13-Aug: Qt WebSockets optional so stock buildenv compiles | 13-Aug | yes (it's what the stick binary was built from) | — | **COMMIT NOW** | This is the patch that made the rebuild possible. Unversioned = the next rebuild repeats TRAP 1. |
| `mixxx-treta/build/` | Build tree | deleted 14-Aug (14 GB) | — | — | (gone) | Binary lives **only** on the stick. `daemon.log` tonight: `Mixxx not found: …/build/mixxx`. |
| `~/workspace/dj-treta-live` (`seo/foundation`, local-only branch) | dj.treta.life Next.js + FastAPI relay + Icecast | 24-May | **NO** — VM `dj-treta-live` TERMINATED (23-Aug disk-protect); DNS → 34.93.92.241, curl times out | — | **PARK** | Site is down. Revival is a product decision (§5), not a cleanup. Push `seo/foundation` first. |
| `~/workspace/dj-treta-live-cockpit` (`feat/cockpit`) | NS-004 operator cockpit route | 05-Jul | no (site down) | smoke harness | **PARK** — merge `feat/cockpit` → `main` (1 commit) then delete the worktree | Second checkout of the same repo on a full disk. |
| `~/workspace/dj-treta-app` | Android on-device mixer Phase 0 (Oboe + SoundTouch) | 15-Jun | no; no remote | — | **PARK** (push to a remote first) | One scaffold commit, no remote = one disk failure from gone. |
| `~/workspace/dj-treta-display` | Sign visualizer + browser mirror | 03-Jun ("Park:") | no; no remote | — | **PARK** (push) | Already self-declared parked. |
| `~/workspace/dj-treta-bpm-anchor` | Old worktree of dj-treta-being on `fix/bpm-drift-mood-anchor` | 22-May | no | — | **DELETE worktree** | Branch is on origin; the checkout is a duplicate. |
| `~/workspace/music-intelligence` | Source-adapter enrichment pipeline | 14-Apr | no | — | **PARK** | Untracked `data/`, `integration/`, `knowledge_slim.db-*` — decide if the DB is worth keeping, then `git clean`. |
| `~/.local/share/djclaw/db/djtreta.db` (1.3 MB, 2,674 tracks, 25 sets, 2,424 llm_calls) | The live library DB | 14-Sep 22:49 | yes | — | **KEEP → BACK UP** | Every path is `/Volumes/DJ-TRETA/…`. One copy, on the internal disk, no backup since `bak-20260526`. |
| `/Volumes/DJ-TRETA` (stick) | Library (2,096 analysed + Bolly Techno + sets), Mixxx binary + restore kit, `mixxxdb.sqlite` | 16-Aug | yes | — | **KEEP → MIRROR** | FAT32 stick, `._` sidecar trap, unplugged half the time. Single copy of the product. |
| GCP: `djtreta-music-v6-multi` 1.38 TiB bucket, `dj-treta-data` 200 GB disk, `kavya-home` | Cloud library + VM disks (VMs terminated) | 23-Aug (auto-delete off) | no | — | **KEEP** (Manish's call 13-Aug: "DJ Treta stuff I want to keep") | Not free: the bucket is the biggest line on the GCP bill. Reframe from 13-Aug still open: keep every byte, pay less to hold it. |

## 2. Since the 31-May audit

**Shipped and still runs (verified in code + logs):**
- NS-001 JSON-mode on all LLM call sites (`json_object`, live-gate fix) — the reason a small local model works at all.
- NS-002 typed event seam, NS-003 per-agent model map (brains are config-only).
- Mixxx cockpit: YouTube Search + Suggested tabs, FLX4 LOAD = download+load, poll/WS pause when hidden (25–27 Jun).
- Local-brain night (28-Jul): menu pre-filter (NS-009, cap 40), tagged-library ingest, flight recorder, planner fallback,
  phrase-lock + deterministic `set_arc`, ACTION protocol, filler discard. Proof: 8.5 h / 62-blend overnight on Gemma4 e2b.
- 07-29 adversarial review: 33 agents, 24 findings, 8 confirmed, 6 fixed.
- Setlist mode (`9b1ddf1`) + pluggable slow-loop brains.
- Cloud restore verified end-to-end 08-Aug (gemini via gateway tunnel, 2,096-track library, 0 errors).
- 13-Aug Mixxx rebuild from scratch: three traps documented (`MIXXX_BUILD.md`), tested restore kit on the stick.
- Volume guard (08-Aug, uncommitted).

**Started and abandoned / parked:**
- NS-004 web cockpit (`/cockpit` route built 05-Jul) — the site it lives on is down.
- Pydantic-AI migration + TUI sunset — assessed and greenlit (~09-Jul), **zero code**.
- Mood-as-profile (confirmed 17-Apr) — still "implementation pending".
- Ghazal `gap` technique (07-Aug) — idea only.
- FLX4 dead-controls capture + Smart CFX emulation (08-Aug) — awaiting his go; upstream may now ship an official FLX4 map (track B should check).
- VDJ visuals — untouched since 31-May.
- dj-treta-app (Android) — one commit.
- Psytrance-145 set plan (16-Aug) — plan written, set not built.

**Still open from the 31-May fix list (not re-verified tonight, flag for the next code pass):** `energy` vs `energy_peak`
typo (`planner_loop.py:314`), scheduled-transition params dropped positionally, `record_skip()` never called,
`recall_similar_set` dead-write, `agent/_archive/` still present, MCP hardcoded paths. `pandas` **is** installed now.

## 3. Risks (ranked)

1. **Single-copy code.** 7 dj-treta commits + 40 Mixxx commits + 13-Aug build patch exist only on this Mac. `git push`
   is a 2-minute fix and it has been pending since 29-Jul (logged as PENDING on 07-29, 07-Aug, and again today).
2. **Single-copy data.** Library, Mixxx binary, `mixxxdb.sqlite` (beatgrids, cues, history) on one FAT32 stick; DB on
   one internal disk with a May backup. The cloud bucket has 237 mp3s of the ~2,700. A lost stick = no DJ Treta.
3. **Boot is manual and fragile.** Daemon log tonight: `Mixxx not found: build/mixxx`. Start = mount stick → extract
   binary → warm Ollama → janitor → `cli.py start` (07-29 RESTART recipe) — nowhere as a script.
4. **Zombie daemon** (cost-accounting note): `restart` can leave two daemons → double-billing + racing `billing.json`.
   Still unfixed; `pgrep -f "python3 -m agent" | wc -l` must be 1.
5. **Stale MCP.** This Claude session drives the 04-Apr TS MCP pointed at an empty dir. Python `mcp_server/` not wired.
6. **Config drift.** Five config files, one live, mode flips by hand-commenting. The 07-Aug `.env.pre-cloud-0807` says the
   key was swapped by hand too.
7. **Tests touch the real machine.** Hermetic run tonight (`DJTRETA_RUNTIME_DIR` set, evals excluded, `-x`): **70 passed,
   1 failed** (`test_commands.py::TestSkipCommand::test_skip_command_dispatches_agent_skip`) in 51 s, and the run still
   made the daemon try to launch Mixxx (`daemon.log` 23:24). Not re-run without `-x`, so the full failure count is unknown.
   Test gate remains non-trustworthy (audit P0).
8. **Disk.** 12 GB free after tonight's cleanup; `vdj/` media 1.4 GB, `web/node_modules`, `.git` 503 MB all on it.

## 4. Next evolution — ranked

**Evolution 0 (in motion tonight): local brain on Qwen3.5.** ₹0/hr, sub-2 s decisions, proven pattern. Dependencies:
disk (done), Ollama 0.34 (done), bench, config flip, stick mounted. *Effort: tonight.*

| # | Candidate | Why now | What it proves | Effort | Depends on |
|---|---|---|---|---|---|
| **1** | **"Press play" — one-command boot + backup discipline.** `djtreta up`: mount check → binary present (else extract from `_RESTORE`) → Ollama warm → janitor → daemon → health gate. Same script mirrors stick → local APFS (`~/Music/DJTreta`, the 22-May decision that was reversed by disk pressure) and pushes DB + `mixxxdb.sqlite` + code to git/cloud nightly. | Every session since July opened with 20 min of "why won't she start". Risks 1–3 and 6 all close here. Demo-readiness is a boot script, not a feature. | "An AI DJ Being you can start in one line on a laptop with no internet." The product thesis, literally. | 2 days | Ev. 0; a 256 GB local slice or a second SSD (APFS, not exFAT) |
| **2** | **Rekordbox bridge (read + write).** She already imports rekordbox XML (`test_import_rekordbox.py`). Go the other way: export her planned set / cues / energy arc as a rekordbox playlist + hot cues, and read his rekordbox library + history so she DJs *his* crate. Track D is researching the ceiling (rekordbox has no public control API; XML/db bridge is the realistic surface). | Manish DJs in rekordbox + FLX4; every set he actually plays out is a rekordbox set. This puts Treta in the booth he already stands in instead of asking him to move to Mixxx. | "Treta prepares, you perform" — B2B without changing his tools. Opens every rekordbox DJ as a user. | 3–5 days | D's findings; `db.py` rekordbox importer; setlist mode |
| **3** | **Mixxx upstream refresh + FLX4 official map.** Rebase the 40-commit `treta` line onto upstream main (1,031 behind; conflict surface = 23 files, mostly ours: CMake, coreservices, library.cpp, main.qml, qrc). Check if upstream now ships an official DDJ-FLX4 mapping (08-Aug hunch) — would retire the hand-patched DDJ-400 derivative and the Smart-CFX question. | 4 months behind; 2.6 will release under us and the fork becomes unbuildable again (TRAP 1 was exactly this). | Keeps the only engine she owns alive; unblocks Spotify/Apple-Music-in-Mixxx research (track C) which will need current upstream. | 2–3 days (+ a full rebuild) | Push `treta` first; buildenv 11 GB download → disk |
| 4 | dj.treta.life revival on Kavya renders only (clean rights) | Stream is the public proof; but it's a bill (VM + egress) with ₹7 in the account | Public demo | 2 days + ₹/mo | Ev. 1; decision on cost |
| 5 | VDJ visuals into the live rig | It works (A−); it's free; demos land harder with a screen | "She sees the music" | 1 day | Ev. 1 |
| 6 | Pydantic-AI migration / TUI sunset | Greenlit in July; no code; ADK still works | Cleaner core | 1–2 weeks | nothing urgent — do not start before 1–3 |

**Recommendation:** tonight Ev. 0; tomorrow push everything (risk 1) and start Ev. 1; run track D's rekordbox answer and
track B's upstream-refresh plan against Ev. 2/3 before choosing which of the two goes first. Do **not** start 6.

## 5. Decisions needed from Manish
1. Delete `web/` (Svelte scaffold) and move `vdj/` media to the stick — 1.5 GB back.
2. Merge `feat/cockpit`→`main` in dj-treta-live and drop the `-cockpit` worktree; drop `dj-treta-bpm-anchor` worktree.
3. Which of Ev. 2 (rekordbox) / Ev. 3 (Mixxx refresh) goes first after the boot script.
4. Local library home: internal APFS (needs ~100 GB he doesn't have) or a second **APFS** SSD (not the FAT32 stick).

## TL;DR (read this)
1. She works as of 29-Aug, but tonight she can't start without the stick — library, Mixxx binary and set folders all live on it.
2. Real shipped work since May: the July local-brain night, setlist mode, JSON-mode, the 13-Aug rebuild recipe. Solid.
3. **7 dj-treta commits and 40 Mixxx-fork commits exist only on this Mac. Unpushed since 29-Jul. Push tonight.**
4. Mixxx fork is 1,031 commits behind upstream; the 13-Aug "WebSockets optional" patch that makes it buildable is uncommitted.
5. The MCP this session uses is the April TS one pointed at an empty folder; the Python MCP in the repo was never wired.
6. Delete: `web/` scaffold, config/env backup files, two duplicate worktrees, 14 dead local branches. Park: TUI, VDJ (move its 1.4 GB media off), dj.treta.life (VM is off, site is down), Android app.
7. Keep everything else — the agent, setlist mode, brain providers, Mixxx fork, the DB, the stick, the GCP bucket.
8. Evolution 0 = Qwen local brain (tonight). Evolution 1 = one-command boot + backups (2 days) — every risk on the list closes there.
9. Then rekordbox bridge (she prepares in his booth) vs Mixxx upstream refresh — tracks D and B decide the order.
10. Don't start the Pydantic-AI rewrite. It's the one thing that would eat a month and prove nothing.
