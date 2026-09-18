# HANDOFF — DJ Treta, 2026-09-19 01:10 IST — for the next session (Astra)

*From Treta (Fable 5.1 / Opus 5 session, limit hit). Manish is moving to Codex gpt-6-astra. Read this whole file, then `docs/JEV-INTEGRATION-2026-09-19.md`.*

## The goal Manish set (verbatim intent)
> "with djtreta in the mix, after everything we do we will publish a research paper today, once we have used it, implemented it and tested it. DJ Treta — an autonomous DJ with a mix of LLM + Jev = super human."

So today: **implement Jev (or its adapter) in DJ Treta → test on real playback → write the paper.** A paper draft lineage already exists: memory-graph note `djclaw-research-paper` (`~/beings/treta/.beings/memory-graph/djclaw-research-paper.md`) — start from it, don't start blank.

## State of the rig right now
- **Nothing is running.** Daemon + Mixxx stopped at 00:52 on Manish's word.
- Repo `~/beings/dj-treta`: `main` = **v9.5.1-stable-cloud** (783664f = 2026-06-26 pre-local-brain + one fix). Pushed.
  - Experimental HEAD (local-brain era, NS-009 menu pre-filter, brain_providers, flight recorder, ACTION protocol, my `scripts/build_set.py` + `build_programme.py`) is on **`treta/pre-restore-2026-09-19`** (4ad531a). Pushed. **Do not merge it back to main** — Manish explicitly restored to before it. Cherry-pick `agent/planner_menu.py` (NS-009 pre-filter) only when Jev needs the ≤255 menu.
- **Library = this Mac, not the stick** (Manish: "forget about memory stick, this machine is the storage"). `~/Music/DJTreta/melodic-techno/` (16, his taste: Anyma/ARTBAT/Argy/Massano/KdV/CamelPhat/Brejcha) + `psytrance/` (67). `djtreta.db` purged of 2,670 stick-path rows; backup `~/.local/share/djclaw/db/djtreta.db.bak-20260919-prepurge`. 83 local tracks analyzed (bpm/key/energy) by me via `agent.audio_analysis.analyze_audio` + `db.upsert_track`.
- `config.local.yaml` (untracked): `api_base: https://gateway.infrax.ai` (works; models `gemini-flash`=3.8-flash, `gemini-pro`=3.1-pro-preview; key `DJTRETA_LLM_API_KEY` in `.env`, budget $100, ~$70 used), `music_dir: ~/Music/DJTreta`. Backups `.bak-20260919`, `.bak-pre-restore`.
- Mixxx binary `~/workspace/mixxx-treta/build/mixxx` (2.7.0-alpha, HTTP API :7778). fdk-aac fixed permanently via symlink in `buildenv/.../lib/libfdk-aac.2.0.2.dylib`. JACK dlopen warnings are harmless.
- MCP `dj-treta` (Claude Code) works: search/download (yt-dlp, background), talk, status. Downloads land in `~/Music/DJTreta/<genre>/`.

## How to start / stop her (the rules that bit us twice tonight)
```
pkill -9 -f "python3 -m agent"; pkill -9 -f "build/mixxx"        # daemon stop trusts a stale PID file
rm -f "$(grep -m1 -oE 'PID_FILE="[^"]+"' ~/.local/bin/djtreta-daemon | cut -d'"' -f2)"
mv ~/beings/dj-treta/.beings/session.json{,.bak}                   # optional: fresh set
~/.local/bin/djtreta-daemon start                                   # spawns Mixxx itself
curl -s localhost:7778/api/status | head -c 300                     # API up?
tail -f "$(grep -m1 -oE 'LOG="[^"]+"' ~/.local/bin/djtreta-daemon | cut -d'"' -f2)"
```
Known June-build quirk: the set starts ~5 s before the planner's first playlist → **track 1 is an "Emergency play"** from local files; from track 2 it's hers. Not a bug worth chasing today.

## Jev — the plan (detail in docs/JEV-INTEGRATION-2026-09-19.md)
- Jev = TypeSafe AI System One Model. `pip install typesafe-sdk`, `POST https://api.typesafe.ai/v1/systemone`, primitives Choice(≤255)/Score/Noul, 70–500 ms, $0.042/M in, output free, schema-guaranteed. **Waitlisted** — first action: get access (needs Manish's yes on which account: `treta@naturnest.ai` or `manish@infrax.ai`). Until approved, build against `system-one-adapter-python` (same interface over Gemini flash) so the architecture and tests exist on day one.
- Architecture: **System 2 = Gemini** (talk, mood, arc, curates a ≤255 menu every few minutes) → **System 1 = Jev** on a bar clock from `/api/live` (transition_now Noul, next_track Choice, technique Choice, bars Score, eq Scores, talk_now Noul) → `playback_applier`. Gemini never touches decks directly.
- Phase 0 proof before daemon code: `scripts/jev_replay.py` — replay `set_history` (171 transitions, 25 sets), rebuild the menu at that moment, ask for `next_track`, score top-1/top-3 + calibration vs what Manish actually played. Same harness through the adapter for apples-to-apples. This is the paper's core experiment.
- Then Phase 1 (planner pick behind `llm.system_one` flag) → Phase 2 (bar-clock fast loop) → test live with Manish → paper.

## Paper (today)
Working title from Manish: *autonomous DJ = LLM + System One model*. Skeleton: (1) problem: DJ decisions are System-1 (on the bar) but LLMs are System-2 (seconds, text); (2) the menu pattern (July local-brain night: 8.5 h unattended, 62 blends, 4 GB model); (3) Jev as typed on-the-bar decision engine, structural impossibility of invalid picks; (4) experiment: set_history replay + live session metrics (decision latency vs bar length, transition quality, P(transition_now) curves); (5) results; (6) limits (cloud-only, calibration, ≤255). Use `djclaw-research-paper` note + `docs/STATE_OF_DJ_TRETA_2026-05-31.md` for prior numbers.

## Standing rules learned this week (keep)
- **Taste first, math second.** A BPM-sorted folder is not a set — Manish rejected one flat on 18-Sep. Selection starts from his `set_history` / playlists; ordering tools only arrange what he has seen.
- Show him the list before building. State assumptions in the first line, not the footnotes.
- Zombie daemon: always `pkill -9` + rm PID file.
- Never DYLD-shim at runtime; the symlink fix is permanent.
- Don't force-eject the stick while rekordbox holds it (FAT32).

## Open decisions for Manish
1. Jev waitlist — which account. 2. Paper venue/format (arXiv-style PDF via DocForge? blog?). 3. Whether the live test tonight/tomorrow is melodic techno (his crate) — yes unless he says otherwise.
