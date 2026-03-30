# Building Mixxx with HTTP API

DJClaw requires a forked version of Mixxx with an HTTP API. This is the only non-standard dependency.

## Quick Version

```bash
# Clone the fork
git clone https://github.com/VeltriaAI/mixxx.git ~/workspace/mixxx-treta
cd ~/workspace/mixxx-treta
git checkout feature/http-api

# Build (macOS)
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
cmake --build . --parallel $(sysctl -n hw.ncpu)

# Verify HTTP API
./mixxx &
curl http://localhost:7778/api/status
# Should return JSON with deck1, deck2, crossfader, etc.
```

## macOS Dependencies

```bash
brew install cmake qt@6 protobuf libid3tag libmad portaudio \
  rubberband soundtouch vamp-plugin-sdk taglib chromaprint \
  fftw libshout libebur128 hidapi
```

## What the HTTP API Adds

The fork adds an embedded HTTP server (port 7778) to Mixxx with these endpoints:

| Endpoint | Method | What |
|----------|--------|------|
| `/api/status` | GET | Both decks: BPM, key, position, remaining, volume, EQ, sync, crossfader |
| `/api/live` | GET | VU meters (per-deck + master), beat phase, peak indicator |
| `/api/deck/{n}/track_info` | GET | Title, artist, file path, waveform summary |
| `/api/load` | POST | Load track onto deck |
| `/api/play` | POST | Play deck |
| `/api/pause` | POST | Pause deck |
| `/api/volume` | POST | Set deck volume |
| `/api/crossfade` | POST | Set crossfader position |
| `/api/eq` | POST | Set EQ band (hi/mid/lo) |
| `/api/filter` | POST | Set quick-effect filter |
| `/api/sync` | POST | Enable/disable beat sync |
| `/api/transition` | POST | C++ S-curve crossfade (20fps, smooth) |
| `/api/control` | POST | Raw Mixxx control (any group/key/value) |

## Config

In DJClaw's `config.yaml`:

```yaml
mixxx:
  url: "http://localhost:7778"
  auto_start: true
  binary: "~/workspace/mixxx-treta/build/mixxx"
  resource_path: "~/workspace/mixxx-treta/res"
  settings_path: "~/Library/Application Support/Mixxx"
```

When `auto_start: true`, DJClaw launches Mixxx automatically on `djclaw start`.

## Linux (planned)

Linux build follows standard Mixxx build process. The HTTP API code is cross-platform. Audio routing (PulseAudio/PipeWire + JACK) needs testing. Contributions welcome.
