# DJ Treta — Decision-Quality Fixes & Evolution Plan

**Branch:** `v10` · **Date:** 2026-05-26 · **Method:** every claim below verified against the actual code + the live local DB (`~/.local/share/djclaw/db/djtreta.db`). No assumptions.

## Verified root causes (with evidence)

1. **The crate is polluted (data, not just logic).** Live DB shows under `genre=melodic-techno`: A.R. Rahman Bollywood remixes (Urvasi Urvasi, The Humma Song @ **99 BPM**, Fana) — the "bollyshit" — and "2 Emotions – Unreborn" @ **152 BPM**. BPM spread 99→152 all tagged melodic-techno. Genre is assigned by **folder**, so the source folders themselves contain off-genre/off-tempo files. Plus **18 duplicate `_all` rows** with `bpm=None` (double-ingested, unanalyzed).

2. **Selection genre-gate uses the raw mood string, not the resolved slug.** `planner_loop.py:232` `slug = self.mood.replace(" ","-")`. Mood "peak-time melodic techno high energy" → slug `peak-time-melodic-techno-high-energy`, which matches **no folder** → 0 candidates from `_topup_playlist_local` → falls through to emergency-load, which grabs *any* track (the 152/Bollywood). Should use `mood_profile.canonical_slug`.

3. **No BPM-range / energy / harmonic gate in selection.** `planner_loop.py:259` ranks only by `score = abs(bpm - cur_bpm)`; `pick_next_candidate` (`playlist_schema.py`) returns lowest rank. `mood_profile.bpm_range`/`energy_range` and `key_camelot` are never used to *filter or rank*. A 152 track in-folder is still eligible.

4. **`mood_profile` never populates at runtime** → there's no band for selection to target. Verified live: `mood_profile` is `None` even minutes after a mood change. `resolve_mood()` works in isolation (main thread *and* a daemon thread — tested), so it's an **orchestration bug**: the `_on_mood_change` async resolver (`main.py:599`) result isn't sticking (candidate causes: the race-guard discarding, a later mood/profile reset, or Session flush). My session-command synchronous resolve patch is **redundant + blocks the command server ~8s** and should be reverted.

5. **`_all` unanalyzed dupes leak into candidates** (`bpm=None` → score 999) — noise + can be loaded with no analysis.

6. **No energy-journey reasoning in selection.** `tracks_played` is a rich list, but its `energy` field is usually `None` (never joined from DB) and **no selector reads the history** — used only as a de-dup set. `arrangement.py`'s energy-targets exist but aren't consumed by the ranker.

7. **No perception in the decision.** `hear_music`/`analyze_track` are registered but the P4 transition decision is built from metadata only; she never listens.

8. **Stranded crossfader can mute a deck → silence → emergency.** Verified: the crossfader code centers correctly (`_XF_CENTER=0.5` → Mixxx `0.0` via `pos*2-1`), but an interrupted transition (e.g. an agent restart mid-blend) leaves it stranded full on one deck, muting the other → silence → emergency-load. Observed live (user found it full on deck 1; centering it cleared the emergency). Needs a **center invariant**, not a code-logic fix.

### Already shipped this session (verify they still hold)
- **Post-transition cooldown** (commit `2bf2416`) — stops back-to-back churn. ✅ committed, needs live re-verify.
- **mix_in cue seek** (commit `fafec27`) — incoming enters on the groove. ✅ verified live.
- **Crash-hardening** — cleaned a poisoned `session.json` (my earlier object-store bug) + guarded `main.py:629` against a non-dict `mood_profile`. ✅ she's running again.
- **Full-log visibility** (commit `db35dbe`) — `force=True` + RotatingFileHandler → `agent.log` (imported libs had made `basicConfig` a no-op, hiding all INFO logs). ✅ confirmed she's on `gemini-3.5-flash` live.

---

## The plan (phased, surgical-first)

### Phase 0 — Crate data integrity
- **Dedup** the `_all` rows (keep the analyzed genre-folder row; drop/merge the `bpm=None` dupes).
- **Re-genre or quarantine off-band tracks**: Bollywood + the 152-BPM track are not melodic-techno. Either retag to their real genre or move out of the melodic-techno set. Decide a rule: a track whose BPM is outside a genre's canonical band shouldn't carry that genre tag.
- Source note: the VM `melodic-techno/` folder is itself polluted — clean there too so re-ingests stay clean.

### Phase 1 — Make the energy/BPM band real (`mood_profile`)
- **Root-cause + fix** why `session.mood_profile` doesn't stick in the daemon (instrument the `_resolve` thread's result write; check the race-guard + any reset; confirm Session flush on reassignment).
- **Revert** the synchronous `change_mood` resolve patch in `commands.py` (keep the async callback as the single path).
- **Verify**: after `change_mood`, `mood_profile` populates with `canonical_slug` + `bpm_range` + `energy_range` within a few seconds.

### Phase 2 — Fix selection (the core musical fix)
- `_topup_playlist_local`: gate by **`mood_profile.canonical_slug`** (not raw mood); add a **hard `bpm_range` filter**; replace BPM-proximity score with a **composite**: `w1·bpm_gap + w2·camelot_distance(cur,cand) + w3·|energy_target − cand_energy|` (`camelot.py` exists). Exclude `bpm=None`/unanalyzed from playable picks.
- **Gate the emergency/fallback loader** by genre + `bpm_range` so it can never grab a 152/Bollywood track to avoid silence.
- `_run_planner`: re-rank the LLM playlist by the same composite after validation.

### Phase 3 — Energy memory + arc
- Backfill `energy` into `tracks_played` history from the DB.
- Feed recent-history energy + `arrangement_plan` energy-target into the composite's energy term so she **builds/holds an arc**, not just nearest-fit.

### Phase 4 — Perception in the decision
- Inject `hear_music(active_deck)` into the P4 transition prompt (a LIVE-AUDIO block) so the *when/how* is informed by what she hears, not just numbers.

### Phase 5 — Re-verify shipped fixes + crossfader invariant
- **Crossfader-center invariant**: re-center the crossfader (`/api/crossfade 0.5` → `0.0`) on agent startup and at the top of each heartbeat if it's drifted off-center while both decks are channel-fader-mixing — so a stranded crossfader (root cause #8) can never mute a deck → silence → emergency.
- Confirm cooldown (no back-to-back) + mix_in (groove entry) survive the new selection path; run a real set and watch energy continuity.

### Phase 6 (later, gated) — Decision-loop redesign
- Collapse the ~8 mechanical heartbeat gates + metadata-only prompt into a single "musical state (energy history + live audio + band) → next action" decision. Do **after** Phases 0–4 prove out.

---

## Verification per phase
- **P0:** DB query shows one row per file; no off-band track carries melodic-techno; no `bpm=None` in the playable set.
- **P1:** live `/http/state.mood_profile` shows `{canonical_slug, bpm_range, energy_range}` after a mood change.
- **P2:** force a set — every selected next-track is in-band (BPM within range, energy near target, harmonically adjacent); a 152/Bollywood track is never loaded.
- **P3:** over a set, observed energy trends along the arc, not random.
- **P4:** transition decisions reference live audio energy.
- **P5:** no back-to-back transitions; incoming enters on the groove.

## Recommended order
**P1 → P2 → P0 → P3 → P4 → P5.** (Make the band real, fix selection to use it, clean the data so the in-band pool is clean, then layer arc + ears, then re-verify.) P0 can run in parallel since it's data-only.

All changes are surgical and local on `v10`; production (djclaw 9.4 on the VM) stays untouched. Each fix: edit → restart local agent (Mixxx stays up, hot) → verify live → commit.
