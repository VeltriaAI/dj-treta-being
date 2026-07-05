#!/usr/bin/env node
/**
 * Mock replay server — impersonates the daemon's /ws/state endpoint
 * (agent/ws_server.py) so the web UI develops and demos with NO daemon.
 *
 * On connect (mirrors ws_server._ws_handler):
 *   1. state snapshot   {"type":"event","event":"state","data":{...}}
 *   2. billing          {"type":"event","event":"billing","data":{...}}
 *   3. transition_scheduled (optional)
 *   4. thinking ring replay (replay: true), then log ring replay
 *
 * Ongoing loop (~500ms tick): mixxx_status + mixxx_live every tick
 * (daemon does ~5Hz; 2Hz is plenty for dev), state every 4 ticks,
 * thinking every 3 ticks, billing every 8 ticks.
 *
 * Frame payloads live in fixtures.json, derived from the REAL shapes
 * (session.py state_payload, adk_runner billing schema, events.py wire
 * shapes, Mixxx /api/status + /api/live).
 *
 * Usage: npm run mock   (listens on ws://localhost:7779/ws/state)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { WebSocketServer } from 'ws';

const PORT = Number(process.env.MOCK_PORT || 7779);
const fixtures = JSON.parse(
  readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'fixtures.json'), 'utf-8'),
);

const wss = new WebSocketServer({ port: PORT });
console.log(`[mock] /ws/state replay server on ws://localhost:${PORT}/ws/state`);

const event = (name, data, extra = {}) =>
  JSON.stringify({ type: 'event', event: name, data, ...extra });

const now = () => Date.now() / 1000;

let tick = 0;
let thinkIdx = 0;

wss.on('connection', (ws, req) => {
  console.log(`[mock] client connected (path=${req.url})`);

  // ── connect replay: snapshot + rings (same order as ws_server.py) ──
  ws.send(event('state', fixtures.state));
  ws.send(event('billing', fixtures.billing));
  if (fixtures.transition_scheduled) {
    ws.send(event('transition_scheduled', fixtures.transition_scheduled));
  }
  for (const t of fixtures.thinking_ring) ws.send(event('thinking', t, { replay: true }));
  for (const l of fixtures.log_ring) ws.send(event('log', l, { replay: true }));

  ws.on('message', (raw) => {
    // /ws/state is push-only; answer ping like the daemon does.
    try {
      if (JSON.parse(String(raw)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    } catch { /* ignore non-JSON */ }
  });
});

// ── ongoing loop ──
setInterval(() => {
  if (wss.clients.size === 0) return;
  tick += 1;

  // Advance playhead so the UI visibly moves.
  const status = structuredClone(fixtures.mixxx_status);
  const drift = (tick * 0.5) % status.deck1.duration;
  status.deck1.position_seconds = Math.round(drift * 10) / 10;
  status.deck1.remaining_seconds = Math.round((status.deck1.duration - drift) * 10) / 10;

  const live = structuredClone(fixtures.mixxx_live);
  const wobble = 0.15 * Math.sin(tick / 3);
  live.deck1.vu_left = Math.max(0, Math.min(1, live.deck1.vu_left + wobble));
  live.deck1.vu_right = Math.max(0, Math.min(1, live.deck1.vu_right + wobble));

  const frames = [event('mixxx_status', status), event('mixxx_live', live)];

  if (tick % 3 === 0) {
    const t = { ...fixtures.thinking_live[thinkIdx % fixtures.thinking_live.length], ts: now() };
    thinkIdx += 1;
    frames.push(event('thinking', t));
  }
  if (tick % 4 === 0) {
    const st = structuredClone(fixtures.state);
    st.current_track.position = status.deck1.position_seconds;
    st.current_track.remaining = status.deck1.remaining_seconds;
    frames.push(event('state', st));
  }
  if (tick % 8 === 0) {
    const b = structuredClone(fixtures.billing);
    b.calls += Math.floor(tick / 8);
    b.total_cost_usd = Math.round((b.total_cost_usd + 0.0004 * (tick / 8)) * 1e6) / 1e6;
    frames.push(event('billing', b));
  }

  for (const ws of wss.clients) for (const f of frames) ws.send(f);
}, 500);
