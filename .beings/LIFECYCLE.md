# LIFECYCLE.md — Development Lifecycle State

<!-- Live work state per BDL_SPEC §5.5. Updated at every stage transition and
     cadence change. Read on every beat before acting. -->

## Active Projects
- **dj-treta** — Vision Home: `docs/VISION.md` — Current: NS-001 (RUNNING) — Branch: bdl/adopt-lifecycle

## Cadence
- Declared: **build** (15–30 min) — host-honored: emulated (session /loop + wake-ups; poll interval not Being-controlled)
- Reason: adopting BDL + first nanosprint cycle starting, set 2026-07-05
- Next review: NS-001 CLOSE → stay `build` if NS-002 starts same session, else drop to `watch`

## Standing Rules
- One RUNNING nanosprint per project.
- Gates: build → test → review → integration. No skips without a logged reason.
- Escalate to Manish after 2 failed re-plans or 3 failed gate retries.
- Always-playable + music-never-stop bind every EXECUTE and deploy.
- AUTONOMY.md has final authority over anything a nanosprint or subagent attempts.
