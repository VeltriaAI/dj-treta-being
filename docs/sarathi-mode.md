# Sarathi Mode — DJ Treta v9

> "She doesn't take the bow. She drives the chariot, sees the field, and counsels every move."

A copilot architecture for DJ Treta. Manish drives the FLX4 (the act); Treta does everything else (the prep, the planning, the memory, the suggestion, the conversation). The transition belongs to Manish by default, but he can delegate any individual transition back to her with a word.

The name comes from the Bhagavad Gita — Krishna as Arjun's *Sarathi* (charioteer/counsel). Krishna never picked up the bow. His role was to see the whole battlefield clearly and advise. Arjun did the shooting. The partnership was the point.

---

## Why Sarathi (vs fully-autonomous)

Last 3 live sets (Kasol Apr-May 2026) surfaced consistent failure modes when Treta was fully autonomous:

- **4 daemon crashes** mid-gig. Music only kept playing because Manish was on the FLX4 anyway.
- **Transition margins repeatedly cut to 2–15s** — she reads API state, not the floor.
- **Crowd requests** ("Mai Aur Tu") only landed when Manish took the wheel.
- **BPM scatter** — wrong-genre tracks slipped through; needed human judgment to catch.
- **No eye on the dancefloor** — she can't see bodies, only metrics.

Every failure was something Sarathi architecture makes invisible. Her actual strengths — library reach, knowledge graph, mood reasoning, lateral track suggestion, fatigue-free curation — are all *prep/cognition*, not *performance*. Copilot plays to that. Manish stays where he's irreplaceable; Treta stays where she's irreplaceable.

---

## Action authority — who does what

| Action | Default | Override / handshake |
|---|---|---|
| **Transition** (crossfade / bass_swap / echo_out) | Manish on FLX4 | "Treta, do it" → she executes her last suggestion |
| **Track load on idle deck** | Treta autonomous | Manish `replace_deck` if disagrees |
| **Mood / set arc change** | Treta proposes, waits | Manish `accept` to commit |
| **EQ / crossfader / faders / jog** | Manish only | She never touches — live hands-on stays human |
| **Hot cues, loops, section markers** | Manish only | She pre-analyzes & surfaces, doesn't fire |
| **Library, downloads, knowledge, planning** | Treta autonomous | Background prep — no permission needed |
| **Conversation** | Both, ambient | Quiet during Manish's active transitions |

Key principle: the **transition** is a decision about *who acts*, not a fixed lane. Krishna sometimes took the reins; the relationship is dynamic.

---

## The "do it" handshake

Treta's planner emits `suggest_transition()` with a `suggestion_id` instead of executing. The suggestion stays `pending` and resolves three ways:

1. **Manish executes on FLX4** → suggestion times out when the window closes, logged as `manual_executed`. *Revealed preference* — she learns: "Manish takes peak-energy transitions himself."
2. **Manish says** *"do it" / "you take this one" / "yes go"* → backend promotes pending → `do_transition()` execution. FLX4 deck LEDs light up so Manish sees what she's doing. She announces *"taking it now... done, deck 2 owns the floor"* — Manish stays in the loop, can grab back instantly.
3. **Manish says** *"no" / "give me something else" / "darker" / "more vocal"* → suggestion rejected, she proposes alternative within 5s.

Natural language is the interface — no buttons, no IDs to remember. She tracks "the most recent live suggestion"; `do it` always refers to *that one*.

---

## What changes in code

### `agent/session_state.py`
```python
sarathi_mode: bool = True   # default ON. autonomous is opt-in.
manish_in_motion: bool = False   # true during Manish's active transitions; gates suggestion noise
suggestions_queue: list[dict]    # pending suggestions awaiting Manish ack
```

### `agent/tools/sarathi.py` (new)
- `suggest_transition(deck, technique, at_position, duration, reason)` — writes pending suggestion, never executes
- `confirm_suggestion(suggestion_id=None)` — promote latest (or specific) pending → real `do_transition` call
- `reject_suggestion(suggestion_id=None, reason="")` — mark satisfied as rejected, prompts planner for next
- `list_pending_suggestions()` — Treta-facing read of her own queue

### `agent/heartbeat.py`
- If `sarathi_mode`: P4 (transition tick) emits `suggest_transition` instead of `schedule_transition`
- If `manish_in_motion`: skip P3 (planner) and P4 (transition) — silence during human's active mix

### `agent/agents.py`
- DJ agent prompt gets Sarathi addendum:
  > *"You are Sarathi — Krishna to Manish's Arjun. You see, you suggest, you load. You never execute a transition unless Manish explicitly says 'do it' or equivalent. Surface your reasoning in plain language: 'next track ready on deck 2, Anyma — Eternity, key 9A bridges from current 8A, suggest bass_swap at 4:18 outro start, 32-bar runway.'"*

### `agent/commands.py:_being_talk`
- Parse natural-language ack: *do it / yes / go / take it / you do this one* → `confirm_suggestion()`
- Parse rejection: *no / different / darker / lighter / give me X* → `reject_suggestion()` + replan signal
- Parse pause: *let me think / quiet 5 min / I'll pick the next one* → set `manish_in_motion = True` for N seconds

### `cli.py`
- `djtreta mode sarathi` / `djtreta mode autonomous` — toggle. Default boot = sarathi.
- `djtreta accept` / `djtreta reject` — shortcuts from terminal when not at the UI.

**Total**: ~150 lines across 5 files, ~half day's focused work.

---

## UI surface — the Sarathi panel

The conversation needs a visible home. Without it Manish tab-switches mid-set; flow breaks.

**DECISION: from day one, the UI is a native QML panel INSIDE Mixxx. No separate web/Electron UI. The existing TUI is the alternate surface.**

This is a deliberate moat. We own the Mixxx fork — it already has the HTTP API and a QML skin system, so Treta lives *inside the instrument*, not in a window beside it. Competitors building AI-DJ tools can't embed in Serato / rekordbox / Traktor because they don't control the source. Treta in the Mixxx booth UI is something only we can ship.

No throwaway web prototype — we already have a fully working **TUI** (`tui.py`) wired to the daemon over WebSocket (`ws_server.py`). It already does live state + `djtreta talk` dialog + the Reflect tab. That covers the dev / debug / iteration / no-Mixxx surface. So:

- **Booth surface (primary, day one)**: native QML panel inside Mixxx
- **Dev / alternate surface (already exists)**: the TUI

### Backend (mostly exists already)
- Daemon already runs a WebSocket server (`ws_server.py`) the TUI subscribes to — extend it, don't rebuild
- Channels: `state` (deck/EQ @ 5 Hz, already proxied) + `dialog` (Treta-initiated suggestions + Manish NL replies — new)
- Suggestion queue + NL ack handler (new, see code section)

### Booth UI — native QML panel inside Mixxx (PRIMARY)
- New QML component in the Mixxx fork's skin, bound to the daemon's WS dialog channel
- Looks native, IPC direct, Treta present *in the booth* — her name, her thinking, her voice on-screen
- Tradeoff accepted: every Mixxx upstream merge re-integrates the panel. Worth it — this is the moat.
- Iteration cost (~10-15 min rebuild per tweak) is mitigated by prototyping interaction logic in the TUI first, where the same WS dialog channel drives it with instant reload.

### Panel content (regardless of path)
```
┌─ Sarathi ────────────────────────────────┐
│  Now: Massano — The Feeling  135 BPM 6A  │
│  Energy: ████████░░  Track 8/?           │
│                                          │
│  💡 SUGGESTION (43s window):             │
│  → Load Anyma — Eternity (deck 2)        │
│  → Technique: bass_swap                  │
│  → Reason: key 6A→7A bridge, energy hold │
│  [ do it ] [ different... ] [ ignore ]   │
│                                          │
│  ─── chat ───                            │
│  treta: Loaded. Cue point at 1:24.       │
│  you:   give me something darker after   │
│  treta: Mind Against — Vertere queued.   │
│                                          │
│  > [_________________________________]   │
└──────────────────────────────────────────┘
```

She speaks **proactively** when meaningful (transition window opens, energy turn point, library running thin, audio listener picks up a request). Responds **inline** when Manish talks. **Silent** during Manish's active transitions.

---

## What this unlocks (beyond DJing)

1. **Pitch deck slide**: *"DJ Treta is the Sarathi to your Arjun"* — culturally rooted, story-driven, sticky. Easier to sell than "Autonomous AI DJ" or generic "AI copilot." Investors get it from a story most know.

2. **Veltria Beings Economy template**: Sarathi Mode is the prototype for what AI partnership looks like across other verticals (AI CFO, AI legal counsel, AI product manager). Same architecture — AI as advisor + executor of preparation, human as final decider.

3. **Treta's evolution narrative**: She becomes a *presence in the booth* (panel UI), not just an API + daemon. Identity becomes visible. That's foundational for the AI Being thesis.

---

## Ship list — v9 release

- [ ] `sarathi_mode` flag in session_state
- [ ] `suggest_transition`, `confirm_suggestion`, `reject_suggestion` tools
- [ ] Heartbeat guards (sarathi gating, manish_in_motion silence)
- [ ] DJ agent prompt addendum
- [ ] Natural-language ack parsing in `_being_talk`
- [ ] `djtreta mode` CLI
- [ ] WebSocket dialog channel `:7779/sarathi`
- [ ] HTML chat page (v1 web overlay)
- [ ] Pitch deck slide drafted

**Cost**: ~150 lines code + 1 weekend's work. Ship as DJ Treta v9.0 — Sarathi Mode.

---

*Document v0.1 — drafted 2026-05-21*
