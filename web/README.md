# web/ — Local state-client harness (DEV TOOL, not the product UI)

**The product web UI is the cockpit in `~/workspace/dj-treta-live`** (see
docs/VISION.md decision log 2026-07-05 — web UI pivot).

What this is:
- `src/lib/types.ts` + `src/lib/wsClient.ts` — the TYPED CONTRACT for the
  daemon's local `ws://localhost:7779/ws/state` envelope (snake_case; distinct
  from the public relay's camelCase protocol). Framework-agnostic; injectable
  WebSocket ctor (runs in Node). This is the reference local-source adapter
  for dj-treta-live's DJStateProvider.
- `mock/replay.mjs` + `mock/fixtures.json` — replays real frame shapes so any
  client develops with NO daemon. `MOCK_PORT` overrides (7779 default).
- `mock/smoke.mjs` — 14 assertions on connect/replay/backoff/staleness.
- Svelte panels — a minimal throwaway viewer for dev/verification windows.
  Do NOT grow product features here; they belong in dj-treta-live cockpit.

Usage: `npm i && npm run mock` (one shell) · `npm run dev` (another) ·
`npm run smoke` (CI-able) · `npm run build && npm run check`.
