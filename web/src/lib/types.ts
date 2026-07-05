/**
 * Wire types for the daemon's /ws/state endpoint (agent/ws_server.py).
 *
 * Envelope (every frame):
 *   { "type": "event", "event": <name>, "data": {...}, "replay"?: true }
 *
 * On connect the server replays: latest `state` snapshot, `billing`,
 * optional `transition_scheduled`, then the thinking + log ring buffers
 * (each frame flagged with replay: true).
 */

export interface Envelope {
  type: 'event' | 'response' | 'error' | 'pong';
  event?: string;
  data?: unknown;
  replay?: boolean;
}

/** state.json shape written by agent/session.py::_write_state (snake_case). */
export interface StateFrame {
  phase: string;
  mood: string;
  mood_profile?: Record<string, unknown> | null;
  tracks_played: number;
  current_track: CurrentTrack;
  next_track: { title: string; deck: number; file_path: string } | null;
  set: SetInfo | Record<string, never>;
  planner_status: 'busy' | 'idle';
  planner_tracks_since?: number;
  agent_busy: boolean;
  relay_enabled?: boolean;
  relay_connected?: boolean;
  recording?: boolean;
  broadcasting?: boolean;
  emergency_count?: number;
  last_command?: string;
  last_command_result?: string;
  billing?: string; // pre-formatted summary string, e.g. "60K tokens $0.0050"
  scheduled_transition?: Record<string, unknown> | null;
  sarathi_mode?: boolean;
  brain_offline?: boolean;
  sources?: { youtube: boolean; treta_originals: boolean };
  producing?: unknown;
  ts?: number;
}

export interface CurrentTrack {
  title: string;
  bpm: number;
  key: number | string;
  remaining: number;
  file_path: string;
  position?: number;
  duration?: number;
  file_bpm?: number;
  deck?: number;
  timeline_compact?: string;
}

export interface SetInfo {
  id: number;
  number: number;
  title: string;
  mood: string;
  genre: string;
  elapsed: number;
  remaining: number;
  target_minutes: number;
  peak_energy: number;
  energy_arc: { t: number; e: number }[];
}

/** Typed think/call events from agent/events.py (NS-002 wire contract). */
export interface ThinkingFrame {
  agent: string;
  type: 'think' | 'call';
  text?: string; // think
  tool?: string; // call
  args?: string; // call (pre-stringified, pre-truncated)
  ts?: number;
  replay?: boolean; // stamped client-side from the envelope
}

/** billing.json shape owned by agent/adk_runner.py::_apply_billing. */
export interface BillingFrame {
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_usd: number;
  calls?: number;
  by_agent?: Record<string, { input: number; output: number; cost: number; calls: number }>;
  session_start?: number;
  key_spend?: number;
  ts?: number;
}

export interface LogFrame {
  ts?: number | null;
  text: string;
}

/** Mixxx /api/status shape, proxied verbatim as `mixxx_status` (~5 Hz). */
export interface MixxxDeckStatus {
  playing: boolean;
  track_loaded: boolean;
  bpm: number;
  file_bpm: number;
  key: number;
  position_seconds: number;
  remaining_seconds: number;
  duration: number;
  title: string;
  sync_enabled?: boolean;
  volume?: number;
  eq_hi?: number;
  eq_mid?: number;
  eq_lo?: number;
}

export interface MixxxStatusFrame {
  deck1: MixxxDeckStatus;
  deck2: MixxxDeckStatus;
  crossfader: number;
}

/** Mixxx /api/live shape, proxied verbatim as `mixxx_live` (~5 Hz). */
export interface MixxxLiveFrame {
  deck1?: { vu_left: number; vu_right: number };
  deck2?: { vu_left: number; vu_right: number };
  master_vu_left?: number;
  master_vu_right?: number;
  crossfader?: number;
  beat_distance?: number;
}

/** The single store the UI consumes. */
export interface LiveState {
  connected: boolean;
  /** ms epoch of the last frame of ANY type; 0 = never. */
  lastFrameTs: number;
  /** true when now - lastFrameTs > STALE_AFTER_MS while data exists. */
  stale: boolean;
  reconnectAttempt: number;
  state: StateFrame | null;
  decks: MixxxStatusFrame | null;
  live: MixxxLiveFrame | null;
  thinking: ThinkingFrame[];
  billing: BillingFrame | null;
  logs: LogFrame[];
  scheduledTransition: Record<string, unknown> | null;
  /** Frames with an event name we don't handle — raw JSON drawer (dev aid). */
  unhandled: { event: string; data: unknown }[];
}
