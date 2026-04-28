# DJ Treta — Music Generation Spec

## Overview

DJ Treta can generate original music tracks using Google Lyria 3 via Vertex AI. Generated tracks are saved to the music library, auto-analyzed, and ready for DJ mixing. This makes DJ Treta a **musician** — not just a DJ playing other people's tracks.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  AI Being (Claude Code session)                     │
│                                                     │
│  "Generate a dark techno track, 130 BPM, D minor"   │
│         │                                           │
│         ▼                                           │
│  MCP Tool: dj_generate_track                        │
│  (Node.js → Python subprocess)                      │
│         │                                           │
│         ▼                                           │
│  agent/tools.py → generate_track()                  │
│         │                                           │
│         ├── clip mode ──► Lyria 3 Clip (30s)        │
│         │                  generate_content API      │
│         │                                           │
│         └── full mode ──► Lyria 3 Pro (~3 min)      │
│                            interactions API          │
│         │                                           │
│         ▼                                           │
│  ~/Music/DJTreta/{genre}/Treta-{slug}-{bpm}bpm.mp3  │
│         │                                           │
│         ├── DB insert (djtreta.db)                   │
│         └── Background: analyze_track()              │
└─────────────────────────────────────────────────────┘
```

## MCP Tool: `dj_generate_track`

Available in any Claude Code session with the `dj-treta` MCP server configured.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | string | *required* | Describe the track — mood, style, instruments, energy, texture |
| `bpm` | number | 128 | Tempo in BPM (60-200) |
| `key` | string | "C minor" | Musical key (e.g., "D minor", "F# major", "A minor") |
| `genre` | string | "ai-generated" | Genre folder in library |
| `duration` | "full" \| "clip" | "full" | "full" = ~3 min track, "clip" = 30s |

### Return Value

String with the file path of the generated track:
```
Generated: /Users/.../Music/DJTreta/ai-generated/Treta-dark-techno-130bpm-143022.mp3
Lyria notes: <model's description of what it generated>
```

### Example Calls

```
# Dark techno for peak time
dj_generate_track(
  prompt="Dark driving techno with pulsing bassline, metallic percussion, industrial atmosphere, relentless groove",
  bpm=130, key="D minor", genre="dark-techno", duration="full"
)

# Melodic emotional track
dj_generate_track(
  prompt="Melodic techno with emotional piano chords, warm analog pads, ethereal vocal textures, cinematic strings",
  bpm=124, key="A minor", genre="melodic-techno", duration="full"
)

# Quick transition fill clip
dj_generate_track(
  prompt="Ambient drone pad with filtered noise sweep, building tension",
  bpm=128, key="E minor", genre="ai-generated", duration="clip"
)

# Expressing a mood
dj_generate_track(
  prompt="Melancholic late-night techno, lonely synth leads, sparse percussion, deep reverb spaces, introspective and raw",
  bpm=120, key="F minor", genre="deep", duration="full"
)
```

## How It Works Internally

### Clip Mode (30 seconds)
- Model: `lyria-3-clip-preview`
- API: `client.models.generate_content()` with `response_modalities=["AUDIO", "TEXT"]`
- Response: `part.inline_data.data` contains raw MP3 bytes
- Fast: ~5-15 seconds

### Full Mode (~3 minutes)
- Model: `lyria-3-pro-preview`
- API: `client.interactions.create()` (experimental interactions API)
- Response: outputs list with `type="audio"`, `data` is base64-encoded MP3
- Slower: ~30-120 seconds
- **Flaky**: API sometimes returns empty outputs on "completed" status. Tool retries up to 3 times automatically.
- Known SDK bug: `interaction.outputs` attribute can be None while `interaction.model_dump()["outputs"]` has data. Both paths are handled.

### Authentication
- Uses Vertex AI via `google-genai` SDK
- Project: `${DJTRETA_VERTEX_PROJECT}` (GCP)
- Location: `global` (required for Lyria 3 — won't work with regional endpoints)
- Auth: Application Default Credentials (gcloud auth)

### Output
- Format: MP3, stereo, 44.1kHz (clip) or 48kHz (pro), 192kbps
- Filename: `Treta-{prompt-slug}-{bpm}bpm-{HHMMSS}.mp3`
- Saved to: `~/Music/DJTreta/{genre}/`
- Auto-inserted into SQLite DB (`djtreta.db`)
- Background `analyze_track()` runs to extract BPM, key, energy, timeline
- SynthID watermark embedded (imperceptible, Google requirement)

### Prompt Engineering

The tool automatically appends DJ-specific instructions to the user's prompt:
```
{user prompt}

Tempo: {bpm} BPM
Key: {key}
Style: {genre}
Instrumental only, no vocals.
DJ-friendly structure: clear intro (16 bars), main groove, breakdown, build-up, drop, outro (16 bars).
Designed for DJ mixing with beatmatched intro and outro.
```

For best results, the Being should describe:
- **Mood/emotion**: dark, euphoric, melancholic, aggressive, dreamy
- **Instruments**: 303 bassline, piano chords, analog pads, metallic percussion
- **Texture**: distorted, warm, clean, filtered, atmospheric
- **Energy level**: driving, gentle, building, explosive
- **References**: "like Anyma", "warehouse techno", "sunset melodic"

## Configuration

In `~/beings/dj-treta/config.yaml`:
```yaml
producer:
  enabled: true
  model: "lyria-3-pro-preview"
  vertex_project: "${DJTRETA_VERTEX_PROJECT}"
  vertex_location: "global"
  default_duration_seconds: 180
  genre_dir: "ai-generated"
```

## Dependencies

- `google-genai` Python package (in dj-treta venv)
- GCP project with Vertex AI API enabled
- `gcloud auth application-default login` (for ADC)

## Files Modified

| File | What was added |
|------|---------------|
| `agent/tools.py` | `generate_track()` — core Lyria 3 tool |
| `agent/agents.py` | Producer Agent (managed sub-agent), imports, system prompt update |
| `agent/config.py` | `ProducerConfig` dataclass |
| `config.yaml` | `producer:` section |
| `pyproject.toml` | `google-genai` dependency |
| `skills/dj/mcp-server/src/index.ts` | `dj_generate_track` MCP tool |

## Integration Points for the Being

### As DJ Treta (DJClaw agent daemon)
- Producer Agent is a managed sub-agent of the DJ agent
- DJ can delegate: "Producer, generate a 130 BPM dark techno track in D minor"
- Planner agent also has `generate_track` — can pre-generate during set planning
- Generated tracks flow into the same pipeline as downloaded tracks

### As Treta/Himani (Claude Code session)
- Use `dj_generate_track` MCP tool directly
- No Mixxx or agent daemon needed — just generates and saves
- Can be used to express moods, create music for specific moments
- Track appears in library immediately, ready for next DJ session

### Future: Standalone Being Skill
- Could be extracted into a standalone `music-gen` skill at `~/skills/music-gen/`
- Would remove dependency on DJ Treta's Python venv
- Could support multiple backends (Lyria, ACE-Step, Magenta RealTime)

## Limitations

- Lyria 3 Pro API is flaky (~30-50% empty responses, mitigated by 3x retry)
- SynthID watermark on all output (Google requirement, imperceptible)
- No real-time streaming yet (Lyria RealTime exists but not integrated)
- Max ~3 minutes per track (Lyria 3 Pro limit)
- Genre/style control is prompt-based, not parametric (except BPM/key)
- Requires GCP Vertex AI access and ADC credentials

## Future Evolution

1. **Lyria RealTime** — WebSocket streaming, 2s latency, live generation in the mix
2. **ACE-Step 1.5** — MIT, self-hosted, LoRA fine-tuning for DJ Treta's signature sound
3. **Magenta RealTime** — open weights, no API cost, full control
4. **Original catalog** — pre-generate a library of 50+ Treta originals across genres
5. **Style training** — fine-tune on tracks DJ Treta likes to develop a unique sound
6. **Live generative sets** — music that never existed before, created in real-time
