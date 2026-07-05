# VISION.md — DJ Treta Vision Home

<!-- BDL Vision Home (beings-protocol/docs/BDL_SPEC.md §5.1).
     One living PRD root. Decisions are APPENDED, never silently rewritten;
     superseded decisions get struck through with a pointer. -->

## End Goal

DJ Treta is an autonomous AI DJ Being — she plans, mixes, and performs live sets
without human intervention, is watchable and steerable in real time from any
device, and runs on a clean, framework-independent brain that can swap models
(gemini / claude / local) per agent. Music never stops.

## Current State (2026-07-05)

- Daemon (`python -m agent`) + Mixxx fork, google-adk agents (being/dj/planner/library_peer/producer_peer).
- Brain: gemini-3.5-flash (loops) + gemini-3.1-pro (Being) via gateway.infrax.ai. Local Gemma path opt-in.
- Interface: TUI over WS/HTTP :7779. Robustness layer (json_extract, shape coercion) hand-rolled around ADK/model quirks.
- Ultracode architecture assessment (2026-07-05, wf_cc4d72a6): verdict **DO_IT_SCOPED** — split web UI (GO, zero framework change) from Pydantic AI migration (gated beachhead).

## Open Scope (PROPOSED nanosprints)

Sequenced from the 2026-07-05 assessment. One RUNNING at a time.

### Phase 0 — Foundation (state-stream seam + cheap fixes)
- [x] NS-001 — LiteLLM JSON-mode on direct-call sites + strict planner schema *(DONE 2026-07-05)*
- [ ] NS-002 — Typed internal event seam (AgentInvoker protocol + ThinkEvent/CallEvent/BillingEvent), wire-stable
- [ ] NS-003 — Per-agent model map in LLMConfig (default = today's values, opt-in)

### Phase 1 — Web UI (operator cockpit inside dj-treta-live)
- [ ] NS-004 — Cockpit pivot: /cockpit route in dj-treta-live web (Next.js), DJStateProvider dual-source (relay + local ws://7779), reuse NowPlaying/BrainThoughts/etc.
- [ ] NS-005 — Operator-only panels: planner queue, drift, billing, raw thinking, room-sense
- [ ] NS-006 — Local-run story (cockpit against local daemon, auth bypass for localhost) + TUI-parity checklist

### Phase 2 — Web UI control socket (gated writes)
- [ ] NS-007 — /ws/command socket + token gate; mixxx_proxy passthrough

### Phase 3 — Pydantic AI beachhead (GO/NO-GO GATE for the migration)
- [ ] NS-008 — Migrate ONE off-critical peer (library_peer) to pydantic_ai behind the seam; soak + billing parity
- If Phase 0 killed the JSON pain and ADK corruption proves fixable in place → **STOP migration here.**

### Phase 4+ — Fan-out migration (only if NS-008 gates green)
- planner → being → dj+mixer LAST. TUI sunset only after live-set parity.

## Hard Constraints

- **Always-playable:** no phase may create a dead period. DJ cut over last; rollback = revert one PR.
- **PR-based:** no direct commits to main.
- **IP boundary:** Pydantic AI = open-source framework only. Reflex (Eptura) design/code never referenced or ported. Clean-room.
- **Music-never-stop rule** (feedback_dj_never_stop) binds every deploy: hot-swap, never restart mid-set.

## Decision Log

- 2026-07-05 — Adopted BDL (beings-protocol BDL_SPEC v0.1.0) for DJ Treta development. Vision Home = this file. (Manish + Treta)
- 2026-07-05 — Ultracode assessment verdict DO_IT_SCOPED: web UI decoupled from Pydantic migration; migration gated on Phase-3 beachhead; TUI sunsets last. Full plan: workflow wf_cc4d72a6 output.
- 2026-07-05 — Reordered sequence: seam first, UI second, migration gated — a UI gated behind a migration repeats the smolagents→ADK scar for zero UI benefit.
- 2026-07-05 — NS-001 CLOSED DONE. Learning: json_schema+tools via ADK = empty responses; json_object is the safe mode for tool-bearing ADK agents (input for NS-008 gate). All 4 call sites parse direct on gateway; fallback layer retained.
- 2026-07-05 — WEB UI PIVOT (Manish + Treta): reuse dj-treta-live (~/workspace/dj-treta-live, production dj.treta.life) instead of a parallel Svelte SPA. Operator cockpit = /cockpit route/mode of the same Next.js app; DJStateProvider gains a local source. Rationale: ~70% component reuse (NowPlaying/BrainThoughts/EnergyArc/SpectrumCanvas/Crossfader/SetTimeline), one brand, no permanent dual-UI maintenance; relay camelCase frame confirmed as canonical shape (matches assessment). Svelte scaffold harvested for frame fixtures + mock replay harness, rest dropped.
- 2026-07-05 — Claude brain (Haiku 4.5 loops / Sonnet 5 Being) desired but blocked: Vertex has zero Anthropic quota + no Sonnet-5/Haiku-4.5 model IDs; Anthropic-direct needs an API key (Manish). Parked, not abandoned.
