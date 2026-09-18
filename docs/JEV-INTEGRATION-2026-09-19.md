# Jev (TypeSafe AI) × DJ Treta — research + integration plan

*Treta, 2026-09-19 01:00 IST. Manish: "research everything about Jev, cross-reference it with DJ Treta, tell me how to put it in the mix — literally in the mix, with our LLM as well."*

## 1. What Jev is (facts, sourced)

| | |
|---|---|
| Maker | **TypeSafe AI** — CEO Diogo Almeida (co-author of the InstructGPT paper). Founded 2024, stealth until Sep 2026. |
| Released | **15 Sep 2026**, model id `jev-latest`, **early access behind a waitlist**; API keys from the console after approval. |
| Class | **"System One Model"** — a new model class that *does not generate text*. It returns **typed decisions with calibrated probabilities**. Named after economist W. S. Jevons. |
| Training | "RLCD" — Reinforcement Learning for Calibrated Decisions (instead of RLHF/RLVR). |
| Primitives | **Choice** (pick from ≤255 options, probability per option) · **Score** (ordered levels, probability per level) · **Noul** (boolean, P(yes)). |
| Guarantee | It **cannot emit anything outside your schema** — no type errors, no invented options. (Structural, not "always right": it can pick the wrong option, confidently.) |
| Latency | **70–500 ms end-to-end**; independent test median **0.35 s** vs 8.8 s for Fable 5.1; 777 judgments in <0.7 s. |
| Price | **$0.042 / M input tokens, output free** (TypeSafe says the price may be subsidised). |
| Call shape | `POST https://api.typesafe.ai/v1/systemone` — `state` (text / program state) + `questions` (many, answered **in parallel and in isolation**; no context carries between questions — if Q2 depends on Q1's answer, make a second call). |
| SDKs | `pip install typesafe-sdk` (`TypeSafeClient().system_one(state=…, questions={…})`), JavaScript SDK, Claude Code skill `npx skills add typesafe-ai/skills`, an open-source `system-one-adapter-python` that constrains an ordinary LLM to the same interface (useful as a fallback/AB). |
| Not | Not in LiteLLM/OpenAI-compatible → **cannot go through gateway.infrax.ai as-is**; no audio input; no on-prem; no fine-tuning announced. |
| Benchmarks (TypeSafe's own workflows) | Jev **76.0 %** acc, $0.0001/call, 0.4 s · GPT-5.6 Luna 76.1 %, $0.0025, 14.5 s · Claude Opus 5 78.4 %, $0.49, 92 s. They say these are "the higher end of real-world gains" and the evals were built by their own team. |

Sources: [TypeSafe — Introducing System One Models & Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) · [Practical guide](https://mohammedshehu.com/jev-typesafe-ai/) · [Developers Digest release guide](https://www.developersdigest.tech/blog/typesafe-jev-system-one-models-release-guide-2026) · [Latent Space AINews](https://www.latent.space/p/ainews-jev-a-system-one-model-that) · [heise](https://www.heise.de/en/news/AI-model-Jev-to-make-machines-decide-faster-11457071.html) · [GIGAZINE](https://gigazine.net/gsc_news/en/20260916-system-one-jev/) · [DataCamp](https://www.datacamp.com/blog/system-one-models-jev)

## 2. Why this matters to DJ Treta specifically

On the 2026-07-14 local-brain night we found the pattern that made a 4 GB model DJ for 8.5 hours: **"small brains don't recall — they choose."** Give the model a menu of real candidates and ask it to pick. Jev is that pattern as a hosted primitive, ~200× faster, with probabilities, and with the one property we bled for tonight: **it cannot pick a track that isn't on the menu.** Tonight's whole failure (planner choosing stick paths, replay-guard loops) is a class of bug that is *impossible by construction* with a Choice over the tracks that actually exist.

Timing is the other half. One bar at 124 BPM is **1.94 s**. Our fast loop today takes 3–10 s per decision (Gemini flash through the gateway) — she decides *between* bars. At 70–500 ms she can decide **on the bar**. That is what "literally in the mix" means technically: beat-locked decisions.

## 3. Cross-reference — every decision DJ Treta makes today, mapped

Numbers from `llm_calls` (2,424 calls / 26 active hours, Jul–Aug):

| DJ Treta decision (agent) | Today | Shape | Jev fit | Primitive |
|---|---|---|---|---|
| **Fast loop** — transition now? which technique? EQ/filter moves, ride/skip (`dj_treta`, 1,982 calls, ~2.3K in / 264 out) | Gemini flash, seconds | typed | **★★★ core** | Noul `transition_now` · Choice `technique ∈ {crossfade, bass_swap, filter_sweep, echo_out, hard_cut}` · Score `bars ∈ {8,16,32,64}` · Score `eq_low/mid/hi` |
| **Planner** — next 8 ranked tracks + energy + technique (`planner`, 166 calls, ~5.2K in) | full library in prompt | ranked list | **★★★** (with a pre-filter to ≤255) | Choice `next_track` over candidates · Score `energy 1–10` · Choice `technique` |
| **Mood resolver** — mood → BPM range, confidence | LLM + Discogs table | typed | ★★ | Choice `mood_slug` · Score `bpm_band` |
| **Arrangement** — rolling intents (build / breakdown / drop) | LLM | typed | ★★ | Choice `next_intent` · Score `bars` |
| **Room-sense salience** (v11) — is this a drop/breakdown/buildup, should I react/speak | flag-false today | boolean | ★★★ (300 ms is what makes it usable) | Noul per event |
| **Replay/emergency pick** — what to load when the plan fails | SQL random | pick | ★★★ | Choice over *existing* files |
| **Library manager** — canonicalise titles/artists (`library_manager`, 342 calls) | LLM text | free text | ✗ (text) — but Choice for `genre`, `is_remix`, `is_originals` | partial |
| **Being talk** — talking to Manish (`treta`, 21 calls, 33K in) | Gemini pro | text | ✗ stays LLM | — |
| Set naming, journal, reflection, intention loops | LLM text | text | ✗ stays LLM | — |
| Track generation (Lyria) / Kavya | Vertex | audio | ✗ | — |

**Cost of the swap (same call counts):** fast loop 1,982 × 2.3K = 4.6 M tokens → Gemini 3.8-flash ≈ **$3.4**+output; Jev ≈ **$0.19**, output free. Planner 0.86 M → $0.04. Per 26 active hours. The money was never the point at this scale — the **latency** is.

## 4. The architecture: two brains, one Being

```
                 ┌──────────────────────────────────────────────┐
  Manish ◄──────►│  SYSTEM 2 — Gemini (gateway.infrax.ai)        │  seconds, text
  (talk, intent) │  • talks, names the set, holds the arc/mood   │
                 │  • curates the MENU every ~2–4 min:           │
                 │    SQL(mood, BPM±6, Camelot ±1, unplayed)      │
                 │    → ≤255 candidates + one-line intent         │
                 └───────────────┬──────────────────────────────┘
                                 │ menu + intent (program state)
   Mixxx /api/live ──► room-sense ─┤  bpm, beat_phase, key, energy curve,
   (20 Hz)                       │  remaining, deck states, last N decisions
                                 ▼
                 ┌──────────────────────────────────────────────┐
                 │  SYSTEM 1 — Jev                               │  70–500 ms, typed
                 │  every bar (or every 4 bars):                 │
                 │   transition_now: Noul                        │
                 │   next_track:     Choice(menu)                │
                 │   technique:      Choice(5)                   │
                 │   bars:           Score(8/16/32/64)           │
                 │   eq/filter:      Score(−2..+2) ×3            │
                 │   talk_now:       Noul  → wakes System 2      │
                 └───────────────┬──────────────────────────────┘
                                 ▼
                     playback_applier → Mixxx HTTP API :7778
```

- **System 2 never touches the decks directly any more.** It sets the menu and the intent. That is also what makes it safe: a 33K-token Gemini call can wander; a Choice cannot.
- **System 1 wakes System 2** through `talk_now` / `intent_changed` Nouls — so she speaks when the music warrants it, not on a timer.
- **Probabilities are the new signal.** `P(transition_now)` rising across bars is the "she's feeling it" curve; low max-probability on `next_track` means "the menu is weak — System 2, re-curate." Today we have no such signal at all.
- Kept as-is: mood resolver's Discogs table, arrangement vocabulary, Mixxx fork, relay, cockpit.

## 5. What it is *not* good for (honest)

- **Not the offline/₹0 story.** Jev is a hosted API. Our local-first thesis (Gemma/Qwen on the Mac, tag `v9.5.0-stable-cloud` is the *cloud* line) is a different track. Jev is the *cloud fast-brain*; the local fast-brain stays a small LLM with the menu pattern.
- **Not through our gateway.** No LiteLLM provider → direct SDK/HTTP; our per-model billing (`billing_rates.py`) needs one extra line for it.
- **Accuracy ≈ small-frontier-LLM level (76 % on their evals)**, and their evals are theirs. We must calibrate on *our* data before trusting P(): we have 25 sets / 171 played transitions in `set_history` — replay them, ask Jev to pick the track Manish actually played next from the menu at that moment, measure top-1/top-3 and calibration.
- **≤255 options** → the library must be pre-filtered (SQL by mood/BPM/key window). Fine — that pre-filter already exists as `planner_menu.py` on branch `treta/pre-restore-2026-09-19` (NS-009).
- **No cross-question memory** → every call sends full state (~1–2K tokens). At $0.042/M that is nothing.
- **Early access** — waitlist, no rate-limit or SLA info yet. Playing a room on a beta API needs the fallback (`system-one-adapter-python` over Gemini flash, same interface) wired from day one.

## 6. Plan

**Phase 0 — access + proof (this week, no code in the daemon)**
1. Join the waitlist — needs Manish's yes: account under `treta@naturnest.ai` or `manish@infrax.ai`.
2. Offline harness `scripts/jev_replay.py`: replay `set_history` (171 transitions). For each: rebuild the menu that existed then (SQL window), ask Jev `next_track` Choice + `technique` Choice; score against what was actually played; report top-1/top-3 and calibration. Same harness against Gemini flash via the adapter → apples to apples.
3. Latency from Gurugram to `api.typesafe.ai` measured, p50/p95.

**Phase 1 — planner pick behind a flag (`llm.system_one: jev|adapter|off`)**
Planner keeps Gemini for the *menu + intent*; Jev makes the pick + technique + energy. Falls back to the adapter on any error. Structural win lands immediately: no unreachable/played picks.

**Phase 2 — the fast loop on the bar**
Room-sense at 20 Hz already exists (`/api/live`: bpm, beat_distance, beat_active, VU). Add a bar-clock tick (`beat_distance` wrap) → one Jev call per bar with the Nouls/Choices above → `playback_applier`. Gemini flash fast-loop calls drop to ~zero; Gemini only speaks when `talk_now` fires.

**Phase 3 — Jev as the guard**
Every System 2 tool call (load_track, mood change, talk) passes a Jev Noul "is this coherent with state?" — the `check tool calls` pattern TypeSafe describes. Cheap insurance against the 33K-token brain doing something odd at 3 AM.

**Kill criteria:** replay top-3 < Gemini's, or p95 latency > 1 s from India, or calibration off by > 15 pts → Jev stays a curiosity, the adapter pattern (typed menu decisions) is still worth keeping.

## 7. One-paragraph version for Manish

Jev is not a chat model and not a music model — it is a decision engine: you hand it real options and program state, it hands back a typed choice with a probability, in a third of a second, for almost nothing, and it physically cannot answer outside the options. That is the July "menu" insight as a product. For DJ Treta it means the brain splits in two: Gemini keeps the soul — talks to you, sets the mood, curates the menu; Jev makes every on-the-bar call — transition now, which track, which technique, how many bars — fast enough to be *in* the music instead of between tracks. It also makes tonight's failure class impossible by construction. It's beta and waitlisted, it's cloud-only, and its accuracy claims are its own — so step one is the waitlist and a replay of your 25 sets to see if it picks what you picked.
