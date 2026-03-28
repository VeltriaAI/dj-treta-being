# Relay Protocol Specification — DJ Treta Being → dj.treta.life

> This document defines the WebSocket relay protocol between the DJ Treta Being (Mac, local) and the dj.treta.life server (GCP VM). Use this as the implementation blueprint on the server side.

---

## Connection

```
Being → WSS → wss://dj.treta.life/ws/relay

Headers:
  Authorization: Bearer <RELAY_TOKEN>

Push rate: 3Hz (configurable via config.yaml relay.push_hz)
Reconnect: auto-reconnect on disconnect with 3s backoff
Ping: interval=20s, timeout=10s
```

## Message Format

Every push is a JSON object. Server receives ~3 messages/second.

```json
{
  "phase": "playing" | "offline",
  "activeDeck": 1 | 2,
  "currentTrack": {
    "title": "Running",
    "artist": "Anyma & Meg Myers",
    "bpm": 126.8,
    "key": "Am (8A)",
    "energy": 7,
    "duration": 388.0,
    "elapsed": 124.5,
    "remaining": 263.5
  },
  "nextTrack": {
    "title": "Moon",
    "artist": "Anima Ft. Sheera"
  } | null,
  "mood": "driving",
  "perception": {
    "energy": 7.2,
    "energyDirection": "rising" | "building" | "steady" | "falling" | "dropping",
    "beatPhase": "kick" | "offbeat" | "between" | "silent",
    "density": 6.5,
    "mood": "driving" | "hypnotic" | "euphoric" | "melancholic" | "dark" | "chill" | "dreamy" | "groovy" | "energetic" | "intense" | "silent"
  },
  "vu": {
    "masterLeft": 0.456,
    "masterRight": 0.432,
    "deck1Left": 0.389,
    "deck1Right": 0.371,
    "deck2Left": 0.0,
    "deck2Right": 0.0
  },
  "crossfader": -0.85,
  "set": {
    "id": "set-20260328-2110",
    "number": 2,
    "title": "Lost Transmission #2",
    "mood": "melodic-techno",
    "genre": "melodic-techno",
    "status": "live" | "finished",
    "elapsed": 3600,
    "remaining": 3600,
    "targetDuration": 7200,
    "tracksPlayed": 12,
    "peakEnergy": 8.5,
    "energyArc": [
      {"time": 0, "energy": 3},
      {"time": 300, "energy": 5},
      {"time": 600, "energy": 7}
    ],
    "startedAt": 1774712444.11
  },
  "brain": {
    "lastDecision": "Transitioned to Deck 2 over 45s. Deck 1 ejected."
  },
  "waveform": {
    "low": [0.1, 0.3, 0.5, ...],
    "mid": [0.2, 0.4, 0.6, ...],
    "high": [0.05, 0.1, 0.15, ...],
    "data_size": 3842
  } | null,
  "history": [
    {
      "title": "Running",
      "artist": "Anyma & Meg Myers",
      "playedAt": "22:15",
      "energy": 7
    }
  ],
  "listeners": 0,
  "timestamp": 1774716000000
}
```

---

## Field Details

### `phase`
- `"playing"` — at least one deck is playing
- `"offline"` — nothing playing or Mixxx not responding

### `currentTrack`
- `title` — parsed from Mixxx (uploader prefix stripped)
- `artist` — parsed from `"Uploader - Artist - Title"` format
- `bpm` — real-time BPM from Mixxx (may differ from file BPM due to pitch/sync)
- `key` — Camelot format: `"Am (8A)"`, `"Dm (7A)"`, etc.
- `energy` — 0-10, derived from VU meter loudness (PerceptionEngine)
- `duration`, `elapsed`, `remaining` — seconds

### `nextTrack`
- Track loaded on idle deck, or `null` if idle deck empty
- Only sent if different from current track

### `perception`
Derived from VU meter history (PerceptionEngine, 300-reading buffer):

| Field | Description |
|-------|-------------|
| `energy` | 0-10, from master VU average × 12 |
| `energyDirection` | Trend over last 20 readings: building/rising/steady/falling/dropping |
| `beatPhase` | From beat_active + beat_distance: kick/offbeat/between/silent |
| `density` | 0-10, inverse coefficient of variation of VU history |
| `mood` | Derived from BPM + energy: hypnotic, driving, euphoric, etc. |

### `vu`
Raw VU meter values from Mixxx `/api/live` endpoint. Range: 0.0 - 1.0+

### `crossfader`
- `-1.0` = full Deck 1
- `0.0` = center
- `1.0` = full Deck 2

### `set`
Full set metadata. Updated every push.

| Field | Description |
|-------|-------------|
| `id` | Unique: `"set-YYYYMMDD-HHMM"` |
| `number` | Sequential: 1, 2, 3... |
| `title` | AI-generated name (e.g., "Neural Drift #3") |
| `mood` | Set mood (from user or auto-decided) |
| `genre` | Genre tag for archive UI |
| `status` | `"live"` during play, `"finished"` when ended |
| `elapsed` | Seconds since set started |
| `remaining` | Seconds until target duration |
| `targetDuration` | Target set length in seconds |
| `tracksPlayed` | Number of tracks played in this set |
| `peakEnergy` | Highest energy reached (0-10) |
| `energyArc` | Array of `{time, energy}` samples (last 20) |
| `startedAt` | Unix timestamp when set started |

### `waveform`
- Sent **only once per track change** (not every push)
- `null` on subsequent pushes = frontend keeps previous data
- 3842 points per channel: `low`, `mid`, `high`
- Used for scrolling waveform visualization

### `history`
Last 20 tracks played. Each: title, artist, time played, energy level.

### `brain`
Neural processing data for the sidebar panel:

```json
{
  "lastDecision": "Transitioned to Deck 2 over 45s.",
  "currentIntent": "Buildup in progress. Energy climbing at 7.2. Tension building.",
  "transitionAnalysis": "Bass swap locked at 126.8 BPM. Key: Am (8A) → Dm (7A). Crossfader curve: S-type.",
  "processingLoad": 59
}
```

| Field | UI Element | Description |
|-------|-----------|-------------|
| `currentIntent` | CURRENT INTENT card | What the AI is thinking right now. Changes based on perception: breakdown/buildup/drop detection, energy direction, track timing. |
| `transitionAnalysis` | TRANSITION ANALYSIS card | Technical transition details: BPM lock, key compatibility (Camelot), crossfader curve type. Shows "No track loaded on standby deck" if idle deck empty. |
| `processingLoad` | Processing Load bar | 0-100%. Higher during transitions, buildups, drops. Based on tension + event detection. |
| `lastDecision` | Scrollable log | Last brain decision text (300 chars max). |

---

## Set Lifecycle Events

The being manages sets automatically:

```
1. djtreta start
   → set.status = "live", set.title = AI-generated
   → Broadcast enabled (Shoutcast)
   → Recording started (Mixxx)

2. During set
   → set.elapsed increases
   → set.tracksPlayed increases
   → set.energyArc populated
   → set.peakEnergy updated

3. Set ends (target duration reached OR djtreta stop)
   → set.status = "finished"
   → Recording stopped
   → New set auto-starts (if not stopping)

4. djtreta stop
   → set.status = "finished"
   → Broadcast disabled
   → phase = "offline"
```

---

## Server Responsibilities

### What the server should do with this data:

1. **Fan-out to browsers** — push state to all connected `/ws/state` clients
2. **Record Icecast stream** — when `set.status == "live"`, record the audio from Icecast
3. **Store set metadata** — when `set.status` changes to `"finished"`:
   - Save set metadata to PostgreSQL (title, mood, genre, duration, tracks, energy)
   - Tag the recording file with set ID
   - Make available in archive
4. **Track history** — store `history[]` entries for archive tracklist
5. **Waveform** — cache waveform data per track (only arrives once per track change)

### Recording strategy:
```
Being broadcasts → Mixxx Shoutcast → Icecast (:8000) → Nginx /stream → Browsers
                                                     ↘ Server ffmpeg captures → /recordings/{set_id}.ogg
```

Server starts ffmpeg capture when it receives `set.status == "live"`.
Server stops capture when `set.status == "finished"`.
Recording file named: `{set_id}.ogg` (e.g., `set-20260328-2110.ogg`).

### Archive API needed:
```
GET  /api/sets                    → list all finished sets (pagination)
GET  /api/sets/:id                → set details + tracklist
GET  /api/sets/:id/audio          → recorded audio file (OGG stream)
GET  /api/sets/stats              → total sets, tracks, hours
POST /api/sets/:id/sync           → being pushes final set data (optional)
```

---

## Configuration (Being side)

```yaml
# config.yaml
relay:
  enabled: true
  server_url: "wss://dj.treta.life/ws/relay"
  token: ""  # from .env: DJTRETA_RELAY_TOKEN
  push_hz: 3

# .env (gitignored)
DJTRETA_RELAY_TOKEN=dj-treta-prod-2026-secret
```

---

## Source Files

| File | What |
|------|------|
| `agent/relay.py` | RelayEngine + PerceptionEngine (being side) |
| `agent/main.py` | Set manager, recording, broadcast control |
| `agent/db.py` | SQLite: sets table, set_history table |
| `agent/config.py` | RelayConfig, SetsConfig, BroadcastConfig |

---

## Notes for Server Implementer

1. **Waveform is heavy** — 3842 × 3 numbers. Only sent once per track. Cache on server, don't re-broadcast every push.
2. **Phase "offline"** means being is down or Mixxx not responding. Stop recording.
3. **Set auto-cycles** — when one set ends, a new one starts immediately. Don't assume silence between sets.
4. **Energy arc** — server should sample and store the full energy timeline for archive sparkline visualization.
5. **Relay token** — same token used by old relay agent. No auth changes needed on server.
6. **Backwards compatible** — message format is a superset of old relay format. New fields: `set.*`, `waveform` improvements.

---

*Written as a handoff spec for the dj.treta.life server implementation.*
