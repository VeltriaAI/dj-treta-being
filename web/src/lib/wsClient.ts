/**
 * Typed WebSocket client for the DJ Treta daemon's read-only state channel
 * (agent/ws_server.py, path /ws/state).
 *
 * - On connect the server pushes: state snapshot, billing, optional
 *   transition_scheduled, then thinking + log ring replays (replay: true).
 * - Ongoing: state (~2s), billing, thinking, log, mixxx_status + mixxx_live
 *   (~5 Hz), transition_scheduled.
 * - Reconnect: exponential backoff 0.5s -> 8s cap, with jitter.
 * - Staleness: no frame of any kind for > STALE_AFTER_MS while we still hold
 *   data => stale flag (last-known values stay visible).
 *
 * Framework-light on purpose: only depends on svelte/store, so the same
 * module runs under Node for the smoke test (mock/smoke.mjs).
 */

import { writable, type Readable } from 'svelte/store';
import type {
  BillingFrame,
  Envelope,
  LiveState,
  LogFrame,
  MixxxLiveFrame,
  MixxxStatusFrame,
  StateFrame,
  ThinkingFrame,
} from './types';

// Single knob for the endpoint. Override at build/dev time with
// VITE_WS_URL=ws://host:7779/ws/state (kept localhost-only by default —
// the bundle must carry no external network refs).
const envUrl =
  typeof import.meta !== 'undefined' && (import.meta as { env?: Record<string, string> }).env
    ? (import.meta as unknown as { env: Record<string, string | undefined> }).env.VITE_WS_URL
    : undefined;
export const WS_URL: string = envUrl ?? 'ws://localhost:7779/ws/state';

export const STALE_AFTER_MS = 5_000;
const BACKOFF_BASE_MS = 500;
const BACKOFF_CAP_MS = 8_000;
const THINKING_MAX = 300;
const LOGS_MAX = 300;
const UNHANDLED_MAX = 50;

const HANDLED = new Set([
  'state',
  'billing',
  'thinking',
  'log',
  'mixxx_status',
  'mixxx_live',
  'transition_scheduled',
]);

function initialState(): LiveState {
  return {
    connected: false,
    lastFrameTs: 0,
    stale: false,
    reconnectAttempt: 0,
    state: null,
    decks: null,
    live: null,
    thinking: [],
    billing: null,
    logs: [],
    scheduledTransition: null,
    unhandled: [],
  };
}

export interface WsClient {
  store: Readable<LiveState>;
  start(): void;
  stop(): void;
}

/** WebSocket constructor shape — lets Node inject the `ws` package. */
type WSCtor = new (url: string) => {
  onopen: (() => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  close(): void;
};

export function createWsClient(url: string = WS_URL, WsImpl?: WSCtor): WsClient {
  const store = writable<LiveState>(initialState());
  let ws: InstanceType<WSCtor> | null = null;
  let attempt = 0;
  let stopped = true;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let staleTimer: ReturnType<typeof setInterval> | null = null;

  const Ctor: WSCtor =
    WsImpl ?? (globalThis as unknown as { WebSocket: WSCtor }).WebSocket;

  function backoffMs(n: number): number {
    const base = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** n);
    const jitter = base * 0.25 * (Math.random() * 2 - 1); // +/-25%
    return Math.max(BACKOFF_BASE_MS / 2, Math.round(base + jitter));
  }

  function touch(s: LiveState): LiveState {
    s.lastFrameTs = Date.now();
    s.stale = false;
    return s;
  }

  function handleFrame(raw: string): void {
    let env: Envelope;
    try {
      env = JSON.parse(raw) as Envelope;
    } catch {
      return; // not JSON — ignore
    }
    if (env.type !== 'event' || !env.event) return; // response/pong etc.
    const data = env.data;

    store.update((s) => {
      touch(s);
      switch (env.event) {
        case 'state':
          s.state = data as StateFrame;
          if ((data as StateFrame)?.scheduled_transition !== undefined) {
            s.scheduledTransition =
              ((data as StateFrame).scheduled_transition as Record<string, unknown> | null) ?? null;
          }
          break;
        case 'billing':
          s.billing = data as BillingFrame;
          break;
        case 'thinking': {
          const t = { ...(data as ThinkingFrame), replay: env.replay === true };
          s.thinking = [...s.thinking.slice(-(THINKING_MAX - 1)), t];
          break;
        }
        case 'log': {
          const d = data as LogFrame | string;
          const entry: LogFrame =
            typeof d === 'string' ? { ts: null, text: d } : { ts: d.ts ?? null, text: d.text ?? '' };
          s.logs = [...s.logs.slice(-(LOGS_MAX - 1)), entry];
          break;
        }
        case 'mixxx_status':
          s.decks = data as MixxxStatusFrame;
          break;
        case 'mixxx_live':
          s.live = data as MixxxLiveFrame;
          break;
        case 'transition_scheduled':
          s.scheduledTransition = (data as Record<string, unknown>) ?? null;
          break;
        default:
          if (!HANDLED.has(env.event ?? '')) {
            s.unhandled = [
              ...s.unhandled.slice(-(UNHANDLED_MAX - 1)),
              { event: env.event ?? '?', data },
            ];
          }
      }
      return s;
    });
  }

  function connect(): void {
    if (stopped) return;
    let sock: InstanceType<WSCtor>;
    try {
      sock = new Ctor(url);
    } catch {
      scheduleReconnect();
      return;
    }
    ws = sock;

    sock.onopen = () => {
      attempt = 0;
      store.update((s) => {
        s.connected = true;
        s.reconnectAttempt = 0;
        return touch(s);
      });
    };
    sock.onmessage = (ev) => {
      const d = ev.data;
      handleFrame(typeof d === 'string' ? d : String(d));
    };
    const onDown = () => {
      if (ws !== sock) return; // superseded socket
      ws = null;
      store.update((s) => {
        s.connected = false;
        return s;
      });
      scheduleReconnect();
    };
    sock.onclose = onDown;
    sock.onerror = () => {
      // onclose follows onerror in browsers; close defensively for Node ws.
      try {
        sock.close();
      } catch {
        /* already closed */
      }
    };
  }

  function scheduleReconnect(): void {
    if (stopped || reconnectTimer) return;
    const delay = backoffMs(attempt);
    attempt += 1;
    store.update((s) => {
      s.reconnectAttempt = attempt;
      return s;
    });
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  return {
    store,
    start() {
      if (!stopped) return;
      stopped = false;
      connect();
      staleTimer = setInterval(() => {
        store.update((s) => {
          const stale = s.lastFrameTs > 0 && Date.now() - s.lastFrameTs > STALE_AFTER_MS;
          if (stale !== s.stale) s.stale = stale;
          return s;
        });
      }, 1_000);
    },
    stop() {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = null;
      if (staleTimer) clearInterval(staleTimer);
      staleTimer = null;
      try {
        ws?.close();
      } catch {
        /* noop */
      }
      ws = null;
      store.set(initialState());
    },
  };
}

/** App-wide singleton used by the Svelte UI. */
export const client = createWsClient();
export const live = client.store;
