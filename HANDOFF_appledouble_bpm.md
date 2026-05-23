# Handoff — AppleDouble (`._`) load failures, BPM drift, library state

**Date:** 2026-05-22
**Context:** Local practice rig (local Mixxx + DDJ controller + local agent). **Production (dj.treta.life) is healthy and untouched** — everything below is local-only.

---

## TL;DR

The local agent keeps dropping into **emergency mode** and Mixxx throws **"could not be loaded"** dialogs. Root cause: macOS **AppleDouble sidecar files** (`._Track.mp3`) created because the music library lives on an **exFAT SSD** (`/Volumes/PortableSSD/DJTreta`, symlinked into `~/Music/DJTreta`). These 4 KB stubs carry a `.mp3` extension, so the agent's scanners/resolvers/analyzer all pick them up and choke.

**The only permanent fix is reformatting the SSD to APFS** (kills `._` files at the source). Everything else is whack-a-mole — macOS regenerates `._` files within minutes of any write to exFAT.

---

## The three code paths that trip on `._` files

All match `._foo.mp3` because it ends in `.mp3` and (critically) **sorts before `foo.mp3`** since `.` < letters:

1. **Library scanners** (`db.sync_library`, `session` context, `validate`, `tools/perception` ×2, `main` track count, `tools/library.list_library_tracks`) — index `._` files into the candidate list.
2. **Load resolver** (`agent/playback_applier.py:resolve_track_path`) — fuzzy match returns `._Track.mp3` first → handed to Mixxx → **"could not be loaded"** dialog.
3. **Analyzer** (`agent/audio_analysis.py:analyze_audio`) — librosa crashes on the 4 KB stub (`Illegal Audio-MPEG-Header 0x00000000 at offset 4092`) → track never gets BPM/key → **emergency mode**.

---

## Current state of fixes — SCATTERED, needs consolidation

### Clone repo `~/workspace/dj-treta-bpm-anchor`, branch `fix/bpm-drift-mood-anchor` (PUSHED to origin, commit `eb1f2fa`)
The **canonical, complete, tested** fix:
- `agent/audio_files.py` — new `is_audio_file()` helper (rejects any dotfile)
- All 7 scanner sites routed through it
- `tests/test_audio_files.py` — 4 passing cases (covers the exact `._` files from the incident)
- `cli.py` — `reset --hard` symlink fix (clears symlinked-genre contents instead of crashing `rmtree` on a symlink)

### Local running repo `~/beings/dj-treta`, branch `fix/typed-directives-v1` (UNCOMMITTED band-aids)
Separate, partial fixes made directly to the running tree:
- `agent/audio_analysis.py` — dotfile guard at top of `analyze_audio()` (raises `ValueError` for dotfiles). **Loaded in the running agent** (edited before the 5:13PM restart).
- `agent/playback_applier.py` — dotfile guards in `resolve_track_path` paths #3 and #4. **NOT yet loaded** (edited after the last restart) → needs an agent restart.

### NOT done
- The clone branch's full fix is **not merged into the local running repo**.
- BPM-drift anchor fix (the branch's namesake) — **not written yet**. See below.

---

## ⚠️ Coordination note

A second Claude session is also editing `~/beings/dj-treta` (local HEAD moved to `fc20166 "sarathi: Phase 2"`; `scripts/knowledge/v5_audio/*` changed; `.beings/dj-treta/` untracked appeared). **Avoid colliding** — coordinate before committing to local.

---

## Recommended actions (in order)

### 1. Root fix — reformat SSD to APFS (ends the whole class of bug)
Library is ~10-20 disposable tracks + a re-pullable knowledge cache, so cost is low.
```
# VERIFY the volume/disk first:
diskutil list
# Then (DESTRUCTIVE — wipes the SSD):
diskutil eraseVolume APFS DJTreta /Volumes/PortableSSD
```
After: re-download library onto the clean APFS volume → no `._` files ever again. None of the code patches below are needed if you do this, though they're still good hygiene.

### 2. Consolidate the code fix onto ONE branch
Port the analyzer guard (`audio_analysis.py`) and the load-resolver guard (`playback_applier.py`) **into the clone branch `fix/bpm-drift-mood-anchor`** so it has the complete fix: scanners + resolver + analyzer + symlink + tests. Then PR + deploy. Don't leave band-aids stranded on `fix/typed-directives-v1`.

### 3. Restart the local agent
Whatever code you settle on, the running agent must restart to load it (it was last restarted 5:13PM and does NOT have the `playback_applier.py` load-path fix):
```
# in the agent's terminal: Ctrl+C, then
python3 -m agent     # or djtreta / djclaw
```

### 4. Still-pending BPM-drift anchor fix (the branch's original purpose)
**Symptom:** decks accumulate tempo drift (observed deck playing at **+20.3% rate**). **Cause:** every transition in `agent/tools/transitions.py` calls `/api/sync` then `_apply_bpm_after(deck, bpm_after="keep")`. `"keep"` disables sync but **leaves the rate where sync stuck it** (transitions.py:~121). A half-detected/slower track becomes the tempo anchor and every subsequent track sync-pulls toward it → cumulative drift.
**Fix:** change the default from `"keep"` → anchor to the mood profile's BPM-range center. `MoodProfile.bpm_range` already exists (`agent/mood_resolver.py`). Roughly:
```python
def _apply_bpm_after(deck, bpm_after="anchor", glide_duration=60, mood_profile=None):
    _mixxx_post(...sync_enabled=0)
    if bpm_after == "keep": return
    if bpm_after == "anchor":
        rng = (mood_profile or {}).get("bpm_range")
        target = sum(rng)/2 if rng else None   # None → reset to native file_bpm
        _tempo_ride(deck, target_bpm=target, duration_s=glide_duration)
        return
    # existing "reset"/numeric paths unchanged
```
Thread `session.mood_profile` through the 7 transition entrypoints.

---

## Reference: file/path facts
- Library (real): `/Volumes/PortableSSD/DJTreta/<genre>/*.mp3` (exFAT)
- Symlinked into: `~/Music/DJTreta/<genre>` → SSD
- Local DB: `~/.local/share/djclaw/db/djtreta.db` (was poisoned with `._` rows; pruned to 10 real tracks on 2026-05-22)
- Local agent log: `/private/var/folders/.../T/dj-treta-daemon.log`
- Mixxx local HTTP: `http://127.0.0.1:7778` (`/api/status`, `/api/load`, `/api/play`, `/api/control`)
- Agent WS: `127.0.0.1:7779`
- Quick `._` cleanup (band-aid): `find /Volumes/PortableSSD/DJTreta ~/Music/DJTreta/ -name '._*' -delete; dot_clean /Volumes/PortableSSD/DJTreta`
