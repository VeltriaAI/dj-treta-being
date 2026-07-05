#!/usr/bin/env node
/**
 * Smoke test for AC2/AC3: exercises the REAL wsClient store (bundled from
 * src/lib/wsClient.ts via esbuild) against the mock replay server.
 *
 * Asserts:
 *   AC2 — connect -> store holds state, decks, billing, thinking (with
 *         ring-replay frames flagged replay: true).
 *   AC3 — mock server killed -> connected=false + stale flag flips while
 *         last-known state stays visible; server restart -> reconnected.
 *
 * Run: npm run smoke   (spawns its own mock server on port 7791)
 */
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import WebSocket from 'ws';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const PORT = 7791;
const URL = `ws://localhost:${PORT}/ws/state`;

// ── bundle wsClient.ts for Node ──
const outDir = mkdtempSync(join(tmpdir(), 'djt-smoke-'));
const bundle = join(outDir, 'wsClient.mjs');
execFileSync(
  join(webRoot, 'node_modules', '.bin', 'esbuild'),
  [join(webRoot, 'src', 'lib', 'wsClient.ts'), '--bundle', '--format=esm', `--outfile=${bundle}`],
  { stdio: 'inherit' },
);
const { createWsClient, STALE_AFTER_MS } = await import(bundle);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const startMock = () =>
  spawn(process.execPath, [join(here, 'replay.mjs')], {
    env: { ...process.env, MOCK_PORT: String(PORT) },
    stdio: 'ignore',
  });

let failures = 0;
const check = (name, cond) => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}`);
  if (!cond) failures += 1;
};

async function waitFor(get, pred, timeoutMs = 10_000, step = 100) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (pred(get())) return true;
    await sleep(step);
  }
  return false;
}

// ── AC2: connect + snapshot + live frames ──
let mock = startMock();
await sleep(400);

const client = createWsClient(URL, WebSocket);
let snap = null;
client.store.subscribe((v) => (snap = v));
client.start();

check('connects', await waitFor(() => snap, (s) => s.connected));
check('state snapshot received', await waitFor(() => snap, (s) => s.state?.phase === 'playing'));
check('billing received', await waitFor(() => snap, (s) => (s.billing?.total_cost_usd ?? 0) > 0));
check('thinking ring replayed (replay=true)', await waitFor(
  () => snap, (s) => s.thinking.length >= 5 && s.thinking[0].replay === true));
check('deck frames flow (mixxx_status)', await waitFor(
  () => snap, (s) => s.decks?.deck1?.playing === true && typeof s.decks?.deck1?.bpm === 'number'));
check('mixxx_live flows', await waitFor(() => snap, (s) => (s.live?.deck1?.vu_left ?? -1) >= 0));
check('live thinking arrives (replay=false)', await waitFor(
  () => snap, (s) => s.thinking.some((t) => !t.replay), 5_000));
check('not stale while streaming', snap.stale === false);

// ── AC3: kill server -> disconnect + staleness, restart -> reconnect ──
const stateBeforeKill = snap.state;
mock.kill('SIGKILL');
check('disconnect detected', await waitFor(() => snap, (s) => !s.connected, 5_000));
check('reconnect attempts back off', await waitFor(() => snap, (s) => s.reconnectAttempt >= 2, 6_000));
check('stale flag raised after gap', await waitFor(
  () => snap, (s) => s.stale === true, STALE_AFTER_MS + 4_000, 250));
check('last-known state still visible while stale', snap.state !== null && snap.state === stateBeforeKill);

mock = startMock();
check('reconnects after restart', await waitFor(() => snap, (s) => s.connected, 15_000, 250));
check('fresh frames clear staleness', await waitFor(() => snap, (s) => s.stale === false, 5_000));

client.stop();
mock.kill('SIGKILL');
console.log(failures === 0 ? '\nSMOKE: all checks passed' : `\nSMOKE: ${failures} failure(s)`);
process.exit(failures === 0 ? 0 : 1);
