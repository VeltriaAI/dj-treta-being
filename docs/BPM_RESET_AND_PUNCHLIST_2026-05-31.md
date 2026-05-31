# BPM-Reset Bug + Pre-v11 Musical-Quality Punch-List (2026-05-31)

*From a 5-agent trace workflow (2 independent tempo-path traces + fix-design + what-else sweep → synthesis).
Verified at HEAD `8dfb33a`. Run `wf_d5519e08-ac7`.*

## The "BPM reset is too quick" bug (user-reported)

**Root cause — instant snap, not a glide.** Every transition technique defaults to `bpm_after="anchor"`,
and the DJ prompt/planner never override it, so **every transition uses anchor**. After the blend,
`_apply_bpm_after` (`agent/tools/transitions.py:369`) resets the surviving deck's tempo with a **single
synchronous** `rate_ratio=1.0` HTTP write. During the blend the deck was sync-stretched (e.g. −14%/+11%);
the anchor snaps it back to native **in one frame**, and because this branch sets **no keylock**, the pitch
jumps too. The bigger the prior stretch, the bigger the audible jump. The docstring (`:355-358`) confirms it's
deliberately instant (the gradual `_tempo_ride` was disabled 2026-05-23 for "audible creep").

**Compounding bug — the executor silently drops `bpm_after`.** The scheduled-transition executor
(`agent/transitions.py:14`) reads `toDeck/technique/duration` but **never reads `bpmAfter`/`glideDuration`**
from the schedule file, and dispatches `do_*(to_deck, duration)` with no `bpm_after` (`:100-113`). So even the
deliberate `"keep"` from the user-skip path (`heartbeat.py:860`) is ignored — the scheduler can *only* ever
reach `"anchor"`. Dates to 7f75c06 (2026-04-02).

**The glide exists but is dead.** `_tempo_ride` (`:245-308`) is a stepped, keylocked, jittered ramp —
reachable only via `bpm_after="reset"` or a number, which nothing ever supplies. *"Does she have a smooth
tempo glide? Yes in code, no in practice."* (Also: the `_apply_bpm_after` docstring prose at `:336-340` is
stale — claims it tempo-rides; the code snaps.)

**The fix (~half a day, both in the executor thread which already blocks for the blend):**
- **Fix B (must land first, ~6 lines):** executor reads `bpmAfter`/`glideDuration` and forwards them as
  kwargs to every `do_*` call (`agent/transitions.py:26, :100-113`). Without this the glide stays unreachable.
- **Fix A (~30-line helper):** add `_glide_to_native()` by `_tempo_ride`; route the `"anchor"` branch through
  it — glide `rate_ratio` → 1.0 over ~8 bars (3–10s clamped), keylock ON, steps ≤0.15% each, ~0.5-BPM
  deadband, instant-snap fallback if `/api/status` unreadable (music-never-stop).
- Leave instant (correct): emergency play (`heartbeat.py:1220`) + boot reset (`main.py:511`) — pre-play decks.
- Verify statically: monotonic 0.86→1.0 rate writes, steps ≤0.15%, keylock 1→0, sync not re-enabled.

## Net-new punch-list (beyond the State-of-DJ-Treta audit)

| # | Finding | file:line | Call |
|---|---|---|---|
| 1 | **Anti-churn cooldown blind to the P2 watchdog** — `last_transition_at` stamped only at `transitions.py:152`; the end-of-track watchdog `_auto()` never stamps it → after the most common rescue path the 90s cooldown sees a stale/zero timestamp and P4 mixes the just-started track straight back out. **This is the "transitions too often" complaint.** | `heartbeat.py:~403` | **SHIP — 1 line** |
| 2 | **`energy_max_jump`/`peak_max_consecutive` are dead config** — defined (`config.py:72-73`, `config.yaml:110-111`), zero enforcement. The leapfrog applies cumulative +2/+4/+6/+8 energy steps with no clamp/ceiling. Biggest "no arc / mechanical" gap = the "build don't jump" lesson, uncoded. | `planner_loop.py:314-321, 1003-1052` | **SHIP — medium (A/B listen)** |
| 3 | **P4 DJ invoke has no re-ask throttle** — non-deciding DJ re-invoked every 15s for a whole track (6–10 LLM calls/track) given Flash's ~60% empty-drop. | `heartbeat.py:464-488,748` | **SHIP — small** |
| 4 | **mix_in groove-cue conditional on other deck "playing"** — cold-prep skips it → later mix lands on 20–60s intro silence. | `playback_applier.py:146-171` | **SHIP — small** |
| 5 | **Per-technique duration floors missing** (except echo_out) — a Flash `duration=4` sails through as an abrupt cut dressed as a blend. | `transitions.py:1807-1853, :1392` | **SHIP — tiny** |
| 6 | **FX scream/zap + E2 wet tails are no-ops on unvalidated control keys** — 3/10 techniques can silently lose their character. | `transitions.py:778-788,1335-1340,1454-1470` | **NICE — boot probe** |
| 7 | **Bar/phrase quantize rests on unvalidated `beat_active`/`beat_distance`; waits "fire anyway" on timeout** → off-beat bass swaps. | `transitions.py:142-184` | **NICE — boot probe** |
| 8 | `_tempo_ride` dormant on default path — same root as the BPM bug; folds into the fix above. | `transitions.py:245-308` | **DEFER — fixed by Part 1** |

## Recommended sequence before v11 (Tier-1 energy-typo already done)
1. **P2-watchdog cooldown stamp** (`heartbeat.py:~403`, 1 line) — highest impact:effort; kills the audible churn on the most-travelled path. **= the "transitions too often" fix.**
2. **BPM glide — Fix B then Fix A** (~half day) — the user's reported complaint; resolves #8 too.
3. **P4 re-ask throttle + mix_in-cue hoist** (bundle, small).
4. **`energy_max_jump` enforcement** (medium) — the narrative-arc / "build don't jump" win; A/B listen.
5. **Per-technique duration floors** (tiny insurance).
6. **FX + bar-boundary boot probes** (last; need a live Mixxx, do at v11 dry-run).

Steps 1–3 (~a day) kill both complaints the founder actually hears: too-quick reset + too-frequent transitions.
