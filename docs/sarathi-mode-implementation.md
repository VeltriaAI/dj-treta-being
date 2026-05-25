# Sarathi Mode — Implementation Plan (v9)

Companion to `sarathi-mode.md` (the design). This is the build plan, grounded in the actual current code (researched 2026-05-22).

---

## The key architectural insight

There is already a clean execution seam:

```
DJ agent calls schedule_transition()           [transitions.py:1166]
  → writes runtime/scheduled-transition.json    [transitions.py:1358]
    → heartbeat P3 reads + executes it          [heartbeat.py:265-290]
      → calls do_transition/do_bass_swap/...     [transitions.py:143/296/673]
        → Mixxx crossfader moves
```

**Sarathi intercepts at the seam.** In Sarathi mode:
- DJ agent calls `suggest_transition()` (new) instead of `schedule_transition()` → writes a `transition_suggestion` directive + broadcasts to the WS dialog channel. Does **NOT** write `scheduled-transition.json`.
- When Manish says "do it" → `confirm_suggestion()` writes `scheduled-transition.json` with the suggested params → heartbeat P3 executes it **unchanged**.

So the actual transition executors (`do_transition` etc.) and the P3 executor need **zero changes**. We only redirect *who triggers the write* to `scheduled-transition.json`.

---

## Heartbeat priority cascade — what changes

Current cascade (`heartbeat.py:_heartbeat` @ line 139):

| Priority | What it does | Line | Sarathi change |
|---|---|---|---|
| P1 | Silence emergency recovery | 210 | **KEEP unchanged** — music never stops, even if Manish missed it |
| P2 | Auto-transition safety net (`remaining < 30`) | 222-263 | **Tighten to `remaining < 12`** in sarathi — gives Manish the 12-30s window to do it himself; below 12s Treta rescues |
| P3 | Execute `scheduled-transition.json` | 265-290 | **KEEP unchanged** — this is the confirm-executor; `confirm_suggestion` writes the file, P3 runs it |
| P3.5 | `_execute_signals` (skip→crossfade etc) | 292-301 | **Gate**: in sarathi, mechanical signals emit suggestions, not executions (except emergency) |
| P4 | DJ agent creative invoke (already gated by `dj_paused`) | 303-340 | **Branch**: in sarathi, DJ agent prompt instructs `suggest_transition` not `schedule_transition`; also skip if `manish_in_motion` |

Existing precedent: `dj_paused` flag at line 324 already shows the gate pattern. Sarathi adds two sibling flags.

---

## File-by-file changes

### 1. `agent/session_state.py` — flags + reuse directive queue
- `_FIELD_DEFAULTS` (line 124) already has `dj_paused`, `planner_paused`, `self_schedule`, `reflections`. **Add:**
  ```python
  "sarathi_mode": True,        # default ON — autonomous is opt-in
  "manish_in_motion": False,   # true during Manish's active transition; suppresses P4 + suggestions
  "manish_motion_until": 0.0,  # timestamp; manish_in_motion auto-clears after this
  ```
- **No new queue** — reuse `session.directives` with `kind="transition_suggestion"`. `add_directive` (351), `mark_satisfied` (445), `find_active_directive` (416) all work as-is.

### 2. `agent/tools/sarathi.py` — NEW (model on `tools/suggestions.py`)
- `suggest_transition(to_deck, technique, at_position, duration, reason, track_title="")` → `add_directive(kind="transition_suggestion", target="manish", payload={...}, ttl_seconds=window)`. Broadcasts to WS dialog channel. Returns suggestion_id. **Never writes scheduled-transition.json.**
- `confirm_suggestion(suggestion_id=None)` → resolve latest (or specific) pending; `mark_satisfied`; write `scheduled-transition.json` with the payload params (so P3 executes); broadcast "executing" to WS. Logs to treta_thoughts.
- `reject_suggestion(suggestion_id=None, reason="")` → `mark_satisfied` as rejected; set replan flag; broadcast.
- `list_pending_suggestions()` → Treta + UI read of active `transition_suggestion` directives.
- Reuses `_session()`, `_log_thought()` helpers from suggestions.py (copy or import).

### 3. `agent/tools/__init__.py` + `agent/agents.py`
- Re-export the 4 new tools.
- Wire `suggest_transition`, `confirm_suggestion`, `reject_suggestion`, `list_pending_suggestions` into the DJ agent + Being agent toolsets (`agents.py`, ~950 lines — find `being_tools` + dj agent tool list, same place the suggestions tools were wired in commit d500a36).
- **DJ agent prompt addendum** (conditional on sarathi_mode): *"You are Sarathi — Krishna to Manish's Arjun. When a transition window opens you SUGGEST via suggest_transition (never schedule_transition). State your reasoning plainly: track, key bridge, BPM gap, technique, runway. Manish executes on the FLX4, or says 'do it' and you fire it. You still load tracks, plan, manage library autonomously."*

### 4. `agent/heartbeat.py`
- P2 (line 238): add `sarathi_emergency = 12 if session.sarathi_mode else 30`; change `remaining < 30` → `remaining < sarathi_emergency`.
- P4 (line 324, beside `dj_paused` check): add `elif session.manish_in_motion and time.time() < session.manish_motion_until: skip`.
- P4 DJ invoke: pass `sarathi_mode` into the DJ prompt builder so the agent gets the suggest-not-schedule instruction.
- P3.5 `_execute_signals` (line 612): in sarathi, route user_skip → suggestion (unless emergency).

### 5. `agent/commands.py` — `_being_talk` NL ack parsing (~line 163)
- Before invoking the Being agent, intercept short control phrases:
  - `do it / yes / go / take it / you do this one / fire it` → `confirm_suggestion()` (most recent pending)
  - `no / nope / different / darker / lighter / something else / give me X` → `reject_suggestion()` + replan
  - `i've got this / let me / i'll take it / quiet` → set `manish_in_motion=True`, `manish_motion_until=now+90`
- Longer messages still flow to the Being agent normally.

### 6. `agent/ws_server.py` — dialog channel
- Existing event types: `state`, `thinking`, `log`, `transition_scheduled`, `billing` (lines 122-144). Model on `transition_scheduled` (line 133).
- **Add event type** `transition_suggestion` (Treta→client): `{type:"event", event:"transition_suggestion", data:{id, track, technique, at_position, reason, window_s}}`.
- **Add event type** `suggestion_resolved` (broadcast on confirm/reject so UI clears it).
- Confirm/reject from client reuse the existing `/ws/command` path → map to `confirm_suggestion`/`reject_suggestion` (or just route through `_being_talk` NL parse).

### 7. `cli.py` — `djtreta mode`
- Add `elif cmd == "mode":` (~line 679 dispatch block). Writes `runtime_path("mode.txt")` = `"sarathi"|"autonomous"` (consistent with existing `mood.txt` pattern).
- Daemon reads `mode.txt` at heartbeat start → sets `session.sarathi_mode`. Or send via `command.json` (COMMAND_FILE, line 36) as a `{"command":"set_mode","args":{"mode":"sarathi"}}` handled in `commands.py:_handle_command`.
- Add `djtreta accept` / `djtreta reject` shortcuts → write command.json → `confirm_suggestion`/`reject_suggestion`.

### 8. `tui.py` — surface suggestions (dev/alternate surface)
- Subscribe to the new `transition_suggestion` WS event.
- Render a SUGGESTION panel (model on the existing Reflect tab added in commit d500a36).
- Keybind: `a` = accept, `r` = reject (sends via /ws/command).
- This is where we tune the interaction loop before building the QML panel.

### 9. Mixxx QML panel — booth UI (PRIMARY, separate phase)
- New QML component in `~/workspace/mixxx-treta/res/skins/` (or controllers QML).
- Connects to daemon WS `/ws/state` + new dialog events.
- Renders the Sarathi panel (mockup in design doc).
- Built after TUI proves the interaction. Requires `-DHTTPAPI=ON` build (already fixed) + QML skin work.

---

## Build phases & sequencing

### Phase 0 — guardrails (do first, ~30 min)
Already half-done this session: the `_ensure_mixxx` launch-lock + process-check fix (uncommitted in `main.py`). Commit it. Add the HTTPAPI build-flag check to daemon boot.

### Phase 1 — backend core (~3 hrs)
- session flags (1)
- `tools/sarathi.py` (2)
- tool wiring + DJ prompt addendum (3)
- heartbeat gates (4)
- **Verify**: with `sarathi_mode=True`, run a set — confirm DJ agent emits `transition_suggestion` directives (visible in `list_pending_suggestions()`), `scheduled-transition.json` is NOT written by the agent, music keeps playing via P2 safety net at <12s.

### Phase 2 — conversation (~2 hrs)
- NL ack parsing in `_being_talk` (5)
- `djtreta mode` + `accept`/`reject` CLI (7)
- **Verify**: `djtreta mode sarathi`; let her suggest; `djtreta accept` → P3 executes the transition; `djtreta talk "darker"` → she rejects + replans.

### Phase 3 — WS dialog + TUI (~3 hrs)
- WS dialog channel (6)
- TUI suggestion panel + accept/reject keys (8)
- **Verify**: TUI shows live suggestions; `a`/`r` keys drive confirm/reject; tune the interaction feel here.

### Phase 4 — Mixxx QML panel (separate, ~1 week)
- The booth UI (9). Only after Phase 3 locks the interaction pattern.

**Phases 0-3 = the functional Sarathi Mode, ~1 day + testing. Phase 4 = the shipped booth experience.**

---

## Risks & mitigations

- **Music stops if Manish forgets AND P2 emergency misfires** → P2 stays as hard safety net at `remaining<12`; P1 silence-recovery is the final catch. Test the 12s window explicitly.
- **Suggestion spam during Manish's mix** → `manish_in_motion` gate; auto-set when crossfader moves without a confirmed suggestion (revealed: he's driving), auto-clear after 90s.
- **Stale suggestions pile up** → `transition_suggestion` directives carry TTL = transition window; `expire_stale()` (already called at heartbeat top, line 151) retires them.
- **Mode confusion (which mode am I in?)** → TUI + QML panel show mode badge; `djtreta status` prints it.
- **Default mode** → ship with `sarathi_mode=True` default. Autonomous becomes the opt-in (`djtreta mode autonomous`). This matches the strategic direction: copilot is primary.

---

## Manual-transition detection (the FLX4 ↔ Treta feedback loop)

Manish does transitions physically on the DDJ-FLX4. The FLX4 writes to the same Mixxx ControlObjects that Treta reads via `/api/status` (established 2026-05-16 — FLX4 + Treta's virtual MIDI controller both bind the same ControlObjects). So **Treta sees Manish's physical moves for free** — no extra wiring.

### `agent/heartbeat.py` — new `_detect_manual_transition()` (~40 lines)
Runs near the top of `_heartbeat()`, compares this tick's status to last tick's:

- **Crossfader moved significantly** (Δ > 0.15) AND no confirmed suggestion in flight → infer Manish is driving:
  - set `session.manish_in_motion = True`, `manish_motion_until = now + 90`
  - suppress P4 + new suggestions for the window (gate already added in P4)
- **Transition completed** (crossfader settled at opposite end + active/idle deck roles swapped vs last tick) →
  - log `manual_executed` to energy_arc + `_record_playing_tracks()`
  - if a pending `transition_suggestion` existed, `mark_satisfied` it with note "manish_executed_manually"
  - **revealed preference**: store_thought(agent_id="treta:learning", "Manish took this transition himself at energy=X, BPM=Y, track N/M") — over time she learns which transitions he always grabs (peak drops, crowd requests) and stops suggesting on those
- **EQ knobs moved** (lo/mid/hi Δ on a deck) → log as Manish's live sculpting (already observed working in Kasol sets); don't treat as transition, just note it in her awareness

### Why this matters
Without detection, she's a passive suggester firing into the void. With it, she's a partner who *notices*: "you took the last three yourself, I'll stop suggesting on peak transitions and focus on prepping deeper cuts for when you want them." That noticing — adapting to his revealed behavior — is the difference between a tool and a Sarathi.

Added to **Phase 1** (backend core) since it shares the heartbeat status-poll path.

---

## What does NOT change (important)
- `do_transition`, `do_bass_swap`, `do_echo_out` — untouched. P2/P3 + confirm path all reuse them.
- Heartbeat P1 (silence), P3 (scheduled exec) — untouched.
- Planner, library loop, knowledge graph, mood resolver — untouched. She still does all of this autonomously.
- The directive queue + `scheduled-transition.json` mechanism — reused, not replaced.

*Implementation plan v0.1 — 2026-05-22*
