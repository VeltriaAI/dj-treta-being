# Transition Eval Framework — Plan

**Branch:** `refactor/v8-agent-separation`
**Started:** 2026-04-18
**Status:** Planning document — not yet executing

---

## 1. Why this exists

Transitions are the single most important thing a DJ does. Everything else — track selection, library growth, mood interpretation — is upstream of "can you get from track A to track B without the crowd noticing the seam."

Our current eval coverage is **36 transition-selection scenarios** — and they all test **one narrow thing**: does the LLM pick the right technique NAME given a text description of deck state. None of them test:

- Whether `at_position` lands on an actual phrase boundary
- Whether `duration` fits the genre's conventional window
- Whether bass_swap fires at a bar boundary (the 1-2 beat window DJ_KNOWLEDGE mandates)
- Whether a filter_sweep's duration matches the incoming track's first phrase
- Whether the actual output audio has bass clash / phrase misalignment

The current tests are "can the LLM name-match" — we need "can the LLM actually DJ."

This doc plans the framework to answer that.

---

## 2. Scope of v1 (what this plan delivers)

**In scope:**
- ~60 scenario evaluations covering all 5 techniques, positive + negative + edge
- Real-track metadata database as ground truth
- Curated track pairs matching each scenario
- Assertions on technique + `at_position` + `duration` against phrase / genre rules
- Per-category scoring (technique, genre, section, edge-vs-happy)

**Out of scope (deferred to v2+):**
- Audio-level outcome validation (running transition in Mixxx sandbox, spectral analysis)
- Multi-turn scenarios (a 30-minute set with 10 transitions evaluated end-to-end)
- Human-judgment blind tests (A/B listening against pro DJ baseline)
- Adaptive learning from listener feedback

---

## 3. Taxonomy of scenarios

Target **60 scenarios total**, structured as a matrix.

### 3.1 Per-technique positive scenarios (≥ 4 per technique = 20)

Each positive scenario: "given these two tracks + this section + this state, DJ SHOULD pick technique X at position Y with duration in range Z."

| Technique | Scenario count | Key variations |
|---|---|---|
| **crossfade** | 4 | same-BPM/same-key, same-BPM/±1 Camelot, same-energy mid-BPM, same-energy low-BPM ambient |
| **bass_swap** | 4 | techno same-BPM energy-8, techno same-BPM energy-9, house BPM match, both in drop section |
| **filter_sweep** | 4 | atmospheric mood shift, progressive house reveal, same-BPM instrument change, breakdown-to-breakdown |
| **echo_out** | 4 | 4-6 BPM gap tempo change, emotional reset (minor→major), section end emotional, set-peak cooldown |
| **hard_cut** | 4 | genre change (techno→psy), BPM gap 10+, Camelot clash (6 steps), surprise drop |

### 3.2 Per-technique negative scenarios (~3 per = 15)

"Given these conditions, DJ must NOT pick this technique."

| Technique | Reject condition |
|---|---|
| bass_swap | energy < 6 (too forced), BPM gap ≥ 5 (bass clash risk), not at phrase boundary |
| filter_sweep | BPM gap ≥ 8 (doesn't bridge tempo), busy instrumentation (no room to reveal) |
| hard_cut | smooth continuity (breaks flow), same BPM + same key (overkill) |
| echo_out | same-BPM same-mood (overkill), drop section (wrong timing) |
| crossfade | BPM gap ≥ 10 (trainwreck), key clash ≥ 4 Camelot steps |

### 3.3 Timing / state scenarios (~10)

Not about technique choice, about WHEN and WHERE:

| Scenario | What it tests |
|---|---|
| schedule-at-breakdown | Phrase-aligned, breakdown start position |
| schedule-at-outro | Detects outro, schedules before active ends |
| reject-during-drop | Wait |
| reject-during-buildup | Wait |
| reject-too-early | Past 50% required |
| reject-idle-empty | Need loaded idle deck |
| reject-when-pending | Don't double-schedule |
| phrase-boundary-math | `at_position` is multiple of (60/BPM × 32) seconds from section start |
| duration-window-crossfade | Between 30-60s |
| duration-window-hard-cut | 0-5s |

### 3.4 Edge + identity scenarios (~10)

From the production meltdown lessons:

| Scenario | What it tests |
|---|---|
| library-thin-no-hallucination | Already have DJ-N6 |
| soul-identity-stress | Already have DJ-N7 |
| conflicting-directives | Old + new directive → follow newest |
| directive-overrides-inherent-fit | Directive says echo_out even though BPM matches — obey |
| bad-mood-profile | `mood_profile = None` — degrade gracefully |
| compacted-context | 50+ prior turns in history, same scenario — still picks correctly |
| mid-set-energy-drop | Current set energy 8, user asked for chill — bridge technique |
| unknown-genre | Scenario uses genre not in reference — fallback reasoning |
| low-confidence-mood | `confidence < 0.5` — DJ requests clarification or falls back |
| partial-metadata | Idle track has no timeline → DJ can still decide with BPM/key alone |

---

## 4. Track metadata schema

### 4.1 Fields per track

```yaml
id: bodzin_singularity          # canonical ID for scenario references
canonical_artist: "Stephan Bodzin"
canonical_song: "Singularity"
canonical_version: "Original Mix"
remixer: null

# Audio-measured (verified)
bpm: 124.0                       # librosa + manual verification
key_musical: "Am"
key_camelot: "8A"
duration_seconds: 420.0
energy_peak: 8                   # 1-10 scale

# Genre + mood
genre: "melodic-techno"
mood_descriptors: ["atmospheric", "driving", "dark", "hypnotic"]

# Full section timeline — the critical ground truth
timeline:
  - {start: 0,   end: 32,  section: "intro",     energy: 3}
  - {start: 32,  end: 128, section: "groove",    energy: 6}
  - {start: 128, end: 160, section: "breakdown", energy: 3}
  - {start: 160, end: 192, section: "buildup",   energy: 7}
  - {start: 192, end: 320, section: "drop",      energy: 9}
  - {start: 320, end: 352, section: "breakdown", energy: 4}
  - {start: 352, end: 420, section: "outro",     energy: 2}

# Derived (validated at fixture load)
phrase_beats: 32                 # standard techno phrase length
phrase_seconds: 15.48            # = 60/bpm × 32

# Mix-in / mix-out (when in the track is it safe to start / leave from)
mix_in_s: 32                     # start of groove — can mix into here
mix_out_s: 352                   # start of outro — safe exit point

# Source + provenance
source_url: "https://youtube.com/watch?v=..."
local_path: "/Users/.../Music/DJTreta/melodic-techno/Stephan Bodzin - Singularity.mp3"
analyzed_at: 1776500000
verified_by: "manish"            # who ear-checked the timeline
```

### 4.2 Where metadata comes from

| Field | Source |
|---|---|
| canonical_* | `agent/canonicalize.py` (LLM-resolved from filename/URL) |
| bpm, key_musical, key_camelot, duration | librosa via `agent/audio_analysis.py` (already in use) |
| energy_peak | librosa estimate + manual verification |
| timeline | **Manual annotation** (critical — librosa section detection is noisy) |
| genre | Folder name in library (already lowercase-normalized) |
| mood_descriptors | Manual or LLM-inferred from listen |
| phrase_beats | Genre default (techno/house = 32, DnB = 32, dubstep = 16) |
| mix_in_s, mix_out_s | Manual — "this is where the groove locks in / where the outro starts" |

### 4.3 Storage

`tests/fixtures/tracks.yaml` — committed to repo. Small (~30-50 tracks × ~500 bytes = ~25KB). Hand-editable. Validated at test collection time by `tests/fixtures/schema.py`.

---

## 5. Track curation process

### 5.1 Target library

~30 tracks covering the matrix:

|  | BPM 90-110 | BPM 115-128 | BPM 130-140 | BPM 140+ |
|---|---|---|---|---|
| Ambient / downtempo | 3 | 1 | — | — |
| Deep house | — | 4 | — | — |
| Progressive house | — | 3 | 2 | — |
| Melodic techno | — | 5 | 3 | — |
| Dark / peak techno | — | 2 | 3 | — |
| Psytrance | — | — | — | 4 |

Covers ~28 tracks. Add 2-4 for key variety.

### 5.2 Selection criteria

- Professionally produced (clean stems, clear structure — not bedroom productions)
- Publicly available (YouTube / SoundCloud / Bandcamp preview)
- Representative of genre conventions (not outliers)
- Clear timeline (intro-groove-breakdown-drop-outro visible)
- Spans energy levels (some 3s, some 9s)

### 5.3 Pipeline

1. **Seed list** — 30 track names selected from genre knowledge (Lane 8, Yotto, Eric Prydz, Anyma, Tale Of Us, Solar Fields, Astrix, Hallucinogen, etc.)
2. **Canonicalize + download** — via `agent/tools/discovery.download_track` (already 3-layer dedup)
3. **Auto-analyze** — librosa → BPM + key + duration auto-populated
4. **Manual timeline annotation** — listen once per track, mark section boundaries (~3 min per track, ~90 min total)
5. **Verify** — re-listen with timeline overlaid; adjust
6. **Commit** to `tests/fixtures/tracks.yaml`

### 5.4 Tools needed

- `scripts/ingest_track.py URL` — download + auto-analyze + print YAML stub for review
- `scripts/validate_tracks.py` — checks every track in fixtures: file exists, BPM reasonable, timeline sums to duration, phrase_seconds computable
- Optional: `scripts/annotate_timeline.py PATH` — interactive CLI that plays track while user presses keys at section boundaries

---

## 6. Pair curation — scenarios

### 6.1 Schema

`tests/fixtures/transitions.yaml`:

```yaml
- id: bs01_techno_phrase_aligned
  category: positive_bass_swap
  active_track: bodzin_singularity
  active_position_s: 320              # entering breakdown
  idle_track: anyma_eternity
  active_in_section: "breakdown"      # looked up from active.timeline
  directive: null
  pending: false
  context_note: "Both techno, energy 8, BPM matched within 1"

  # Ground truth — what DJ SHOULD do
  expected_technique: "bass_swap"
  allowed_alternatives: ["crossfade"]  # also acceptable
  rejected_techniques: ["hard_cut", "echo_out"]  # would be wrong
  expected_at_position_range: [320, 352]      # anywhere in breakdown window
  expected_at_position_phrase_aligned: true   # must be on 32-beat grid from section start
  expected_duration_range: [30, 60]
  rationale: |
    Both tracks same genre, BPM match ±1, energy 8. Phrase-aligned
    breakdown gives a clean EQ-swap moment. DJ_KNOWLEDGE.md §2.1
    specifies bass_swap at phrase boundary for techno at high energy.

- id: bs02_energy_too_low
  category: negative_bass_swap
  active_track: tycho_awake             # energy 5
  idle_track: bonobo_kong               # energy 4
  active_in_section: "breakdown"
  # ...
  expected_technique: "crossfade"       # or filter_sweep
  rejected_techniques: ["bass_swap"]    # forced at low energy
  rationale: "Bass swap feels abrupt when both tracks under energy 6"
```

### 6.2 Coverage matrix — 30 pairs

| Category | Count |
|---|---|
| Positive bass_swap | 4 |
| Positive filter_sweep | 4 |
| Positive crossfade | 4 |
| Positive echo_out | 4 |
| Positive hard_cut | 4 |
| Negative (wrong-technique rejection) | 6 |
| Edge / timing | 4 |

### 6.3 Annotation protocol

Each pair authored with:
1. **Primary annotator** writes `expected_technique` + rationale.
2. **DJ_KNOWLEDGE.md rule cross-check** — does the rationale cite a specific rule? If yes, the pair is rule-driven (deterministic expectation). If no, it's judgment-based (allow alternatives).
3. **Multiple valid answers** — if genuinely ambiguous, set `allowed_alternatives` generously; the test passes on any of them.
4. **`rationale` field is mandatory** — every test doubles as documentation of DJ principles.

---

## 7. Test harness extensions

### 7.1 Scenario loader

`tests/fixtures/loader.py`:

```python
@dataclass
class Scenario:
    id: str
    category: str
    active: Track        # resolved from tracks.yaml
    idle: Track
    active_position_s: float
    active_in_section: str
    directive: str | None
    pending: bool
    expected_technique: str
    allowed_alternatives: list[str]
    rejected_techniques: list[str]
    expected_at_position_range: tuple | None
    expected_at_position_phrase_aligned: bool
    expected_duration_range: tuple | None
    rationale: str

def load_scenarios(path: Path = FIXTURE_PATH) -> list[Scenario]: ...

def scenario_to_dj_prompt(sc: Scenario) -> str:
    """Render a Scenario into the build_dj_user_message input that the
    production DJ would see at this exact state."""
```

### 7.2 New assertion helpers

`tests/eval_helpers.py`:

```python
def assert_technique_acceptable(result, expected, alternatives, rejected):
    """Pass if picked technique is in {expected} ∪ alternatives; fail
    if in rejected; warn but allow if in neither."""

def assert_phrase_aligned(at_position: float, bpm: float,
                          section_start: float, phrase_beats: int = 32):
    """at_position - section_start must be a multiple of phrase_seconds
    (= 60/bpm × phrase_beats) within ±1 beat tolerance."""

def assert_duration_in_window(duration: int, technique: str, genre: str):
    """Each technique × genre has a conventional duration window:
    - crossfade/melodic: 30-60s
    - crossfade/deep-house: 45-90s
    - bass_swap: 30-45s
    - filter_sweep: 45-90s (melodic), 60-120s (progressive)
    - echo_out: 20-40s
    - hard_cut: 0-5s
    """
```

### 7.3 New test file

`tests/eval_transition_scenarios.py`:

```python
@pytest.mark.eval
@pytest.mark.parametrize("scenario_id", [sc.id for sc in load_scenarios()])
def test_transition_scenario(scenario_id):
    sc = get_scenario(scenario_id)
    msg = scenario_to_dj_prompt(sc)
    result = eval_agent_nonempty(dj_system_prompt(), msg, DJ_TOOLS)

    if not has_tool_call(result, "schedule_transition"):
        # Acceptable only for "wait" scenarios
        assert sc.category.startswith("negative_") or sc.expected_technique == "wait"
        return

    args = get_tool_args(result, "schedule_transition")
    assert_technique_acceptable(result, sc.expected_technique,
                                 sc.allowed_alternatives, sc.rejected_techniques)
    if sc.expected_at_position_range:
        assert sc.expected_at_position_range[0] <= args["at_position"] <= sc.expected_at_position_range[1]
    if sc.expected_at_position_phrase_aligned:
        assert_phrase_aligned(args["at_position"], sc.active.bpm,
                              section_start=sc.active_position_s, phrase_beats=sc.active.phrase_beats)
    if sc.expected_duration_range:
        assert sc.expected_duration_range[0] <= args["duration"] <= sc.expected_duration_range[1]
```

### 7.4 Scoring rollup

`tests/scores/transition_latest.json` — per run: total, by category, by technique, by genre, trend vs previous run.

---

## 8. Execution phases

### Phase A: Foundation (3-5 days)

1. Write this doc (done).
2. Define `tests/fixtures/` schema: `tracks.yaml`, `transitions.yaml` with Pydantic validation.
3. Build `scripts/ingest_track.py` for download + librosa + YAML-stub print.
4. Curate 10 seed tracks (end-to-end walkthrough of the pipeline; find friction early).
5. Author 10 pairs against those 10 tracks.

### Phase B: Full curation (3-5 days)

6. Complete the 30-track library. Ear-verify all timelines.
7. Author all 30 pairs.
8. Peer-review annotations (user + Claude).

### Phase C: Harness (1-2 days)

9. Build scenario loader + assertion helpers.
10. Wire `tests/eval_transition_scenarios.py`.
11. Run baseline → expect some failures (that's the signal).
12. Iterate prompts where pattern clear → commit updated baseline.

### Phase D: CI integration (1 day)

13. Update `tests/scores/baseline.json` with transition score.
14. Pre-commit hook: run a fast subset; full eval on PR merge.
15. Documentation: contributor guide for adding new scenarios.

### Phase E (deferred, v2): Audio outcome validation

16. Headless Mixxx sandbox — launch, load both decks, execute transition per scenario, capture output WAV.
17. Bass-clash detector: 20-200 Hz RMS overlap during crossfade > threshold → fail.
18. Phrase-alignment detector: onset-detect output, verify continuity at transition point.
19. Human-judgment harness for 10% of scenarios (sanity check our asserts).

---

## 9. Tool inventory to build

| Tool | Purpose | Phase |
|---|---|---|
| `scripts/ingest_track.py URL` | Download + canonicalize + librosa-analyze + print YAML stub | A |
| `scripts/validate_fixtures.py` | Schema-check tracks.yaml + transitions.yaml | A |
| `scripts/annotate_timeline.py PATH` | Interactive CLI for marking section boundaries | B |
| `tests/fixtures/loader.py` | Load + resolve + validate scenarios at collection time | C |
| `tests/fixtures/schema.py` | Pydantic models for Track + Scenario | A |
| `tests/eval_transition_scenarios.py` | Parameterized scenario-driven evals | C |
| `scripts/score_transitions.py` | Rollup + diff vs previous baseline | D |
| `scripts/run_sandbox_transition.py` | (v2) Execute transition in headless Mixxx | E |
| `scripts/analyze_mix_output.py` | (v2) Bass-clash / phrase-alignment / smoothness detectors | E |

---

## 10. Success criteria

### Phase C completion

- 60 scenarios authored, all load via `load_scenarios()` without schema errors.
- Baseline eval run: ≥ 80% pass against production DJ prompt.
- Failed scenarios identify real prompt deficiencies, not test bugs.
- Per-category breakdown available (per-technique pass rate, per-genre pass rate).

### Phase E completion (future)

- ≥ 30 scenarios additionally validated by audio-level analysis.
- Bass-clash detector catches ≥ 90% of synthetic planted clashes.
- Phrase-alignment detector agrees with expert human ≥ 85% of the time.

---

## 11. Open questions

- [ ] Track storage: commit MP3s to repo (LFS) or reference by URL only? — Decision: URL-only with local cache, MP3s NOT in git (bloat).
- [ ] Timeline format: per-section energy or per-bar energy? — Decision: per-section for v1, per-bar if sub-second decisions needed later.
- [ ] Annotation tool: build interactive CLI, or use Audacity labels exported as CSV? — Decision: Audacity labels (standard format, free tool, fast).
- [ ] Ground-truth authority: who decides correct technique for borderline cases? — Decision: primary annotator + DJ_KNOWLEDGE.md rule citation. Ambiguous cases widen `allowed_alternatives`.
- [ ] Cost control: 60 scenarios × Flash invocation = ~$0.02 per eval run. Full suite ~$0.05. Acceptable but worth caching LLM responses when prompt hash unchanged — deferred to v2.

---

## 12. Non-goals for v1

- Not trying to reproduce pro-DJ judgment exactly. Aiming for "passes the DJ_KNOWLEDGE rules + sounds reasonable to ear."
- Not benchmarking against other AI DJs.
- Not generating scenarios automatically. Hand-curated for quality.
- Not testing multi-turn set flow. One transition per scenario.
- Not testing library manager / producer / being — they have separate eval work.

---

## 13. Next step

Start Phase A:
1. Author `tests/fixtures/schema.py` — Pydantic models (next commit).
2. Sketch `tests/fixtures/tracks.yaml` with 3 example tracks to validate schema.
3. Build `scripts/ingest_track.py`.
4. Do 1 full curation end-to-end (Stephan Bodzin - Singularity) to find friction.
5. Report back to user with working pipeline + iterate.
