# v5 Audio Analysis Pipeline

Enriches the v4 knowledge dataset with **exact audio features** (BPM, key, beat
grid, energy profile, waveform, LUFS, cue points) by downloading each track,
running Essentia + librosa, and publishing the result back to HuggingFace.

This subdir is the **complete, reproducible orchestration** — every script,
startup template, and config sample is committed. No secrets, no machine-
specific paths. All runtime state goes through env vars.

## What it produces

`v5/dj_treta_library.parquet` on HuggingFace — v4 schema + Anyma-tier
columns from a 12-tier analysis pipeline:

| Tier | Library | Columns added |
|------|---------|---------------|
| 1 | yt-dlp | (downloads audio to GCS) |
| 2 | Essentia | `bpm_exact`, `bpm_confidence`, `key_detected`, `key_camelot`, `key_strength`, `lufs_integrated`, `duration_ms_exact`, `waveform_rms_json` (1s res), `energy_profile_json` (10s res) |
| 3 | madmom | `downbeats_json` (capped 512), `phrases_json` (8/16/32 bar groupings), `bar_count` |
| 4 | (heuristic) | `drops_json`, `build_ups_json`, `breakdowns_json`, `hot_cues_json` (8 anchors: first_downbeat, intro_end, build_1, drop_1, drop_2, break_1, outro_start, last_beat) |
| 5 | silero-vad | `vocal_segments_json` (start_ms/end_ms list) |
| 6 | laion-CLAP | `clap_embedding` (512-dim vibe vector) |
| 7 | musicnn | `musicnn_tags_json` (top-10 mood/genre tags) |
| 8 | htdemucs | uploads `stems/{mbid}/{drums,bass,vocals,other}.opus`, sets `stem_*_path` |
| 9 | (per stem) | `stem_drums_rms`, `stem_bass_rms`, `stem_vocals_rms`, `stem_other_rms`, durations |
| 10 | basic-pitch | uploads `midi/{mbid}.mid`, sets `midi_path` |
| 11 | faster-whisper | uploads `lyrics/{mbid}.json` (vocal-stem transcription, time-aligned), sets `lyrics_path`, `lyrics_language` |
| 12 | librosa | uploads `mel/{mbid}.npy` (128-bin mel spectrogram, fp16), sets `mel_path` |
| Final | — | `audio_path` = `audio/{mbid}.m4a`, `analyzed_at`, `analysis_version`, `analysis_error` (null on success) |

All `*_path` columns store **relative paths** (no bucket name embedded).
Construct full URI at runtime: `f"gs://{GCS_BUCKET}/{row.audio_path}"`.

## Architecture

```
Local Mac           Coordinator VM        Worker VMs (×32, spot)         GCS
─────────           ────────────────      ──────────────────────         ───
launch_coordinator → coordinator.py
                      │
                      ├── downloads v4 from HF
                      ├── filters priority queue (year>=2020, electronic, video_id)
                      ├── shards → ─────────────────────────────────→ v5/queue/N.pq
                      ├── creates 32 workers ──────────→ worker.py    
                      │                                    │
                      │                                    ├── reads shard ←─ v5/queue/N.pq
                      │                                    ├── per track:
                      │                                    │     yt-dlp → /tmp/{mbid}.m4a
                      │                                    │     Essentia + librosa analysis
                      │                                    │     [optional] upload audio →─→ v5/audio/{mbid}.m4a
                      │                                    │     delete /tmp audio
                      │                                    │     append result row
                      │                                    └── upload shard ──→ v5/results/N.pq
                      │
                      ├── polls v5/results/* until 32 shards present
                      ├── merges shards
                      ├── joins to v4 by mbid
                      ├── pushes ─────────────────────────────────────→ HF v5/dj_treta_library.parquet
                      └── deletes all worker VMs + self
```

Mac just runs `launch_coordinator.sh` once and can sleep.

## Cost estimate (Mumbai, asia-south1)

| Run scope | Compute | Storage (Standard, Mumbai) |
|---|---|---|
| Test (3 VMs × 600 tracks) | ~$0.50 | ~$0.50/mo |
| Priority (32 VMs × 200K) | ~$220 | ~$160/mo (7 TB) |
| **Full (128 VMs × 2.94M)** | **~$4,000** | **~$1,200/mo blended** (103 TB) |

Per-track: ~150-180s at full pipeline (download 30s + Essentia 3s +
madmom 5s + structure 1s + VAD 5s + CLAP 2s + musicnn 3s + htdemucs 120s
+ per-stem 5s + basic-pitch 10s + Whisper 15s + mel 5s).

Privacy: audio + stems + MIDI + lyrics + mels stay in **private GCS**.
Only metadata (BPM/key/embeddings/phrases/cues/segments/etc.) is published
to HuggingFace.

## Files

| File | Purpose |
|------|---------|
| `coordinator.py` | One-shot orchestrator. Runs on coordinator VM. |
| `worker.py` | Per-track worker. Runs on each worker VM (4 parallel processes). |
| `startup_coordinator.sh` | Coordinator VM bootstrap (installs deps, runs coordinator.py). |
| `startup_worker.sh` | Worker VM bootstrap (installs deps, runs worker.py). |
| `launch_coordinator.sh` | Local-Mac one-liner to spin up coordinator VM. |
| `merge_results.py` | Merge result shards → final v5 parquet. Imported by coordinator. |
| `pyproject.toml` | Python deps for both VMs. |
| `config.example.env` | Template for env vars. **Never commit `.env`.** |

## Running it

1. Copy `config.example.env` → `.env` (gitignored), fill in values.
2. Run `./launch_coordinator.sh`. That's it. Mac can sleep.
3. Coordinator pushes v5 to HF when done and deletes everything (including itself).

## Resumability

- **Worker preemption** (spot VMs get killed): each track checkpointed in
  `v5/checkpoints/{shard_id}/{mbid}.done` in GCS. On restart, worker skips
  already-done mbids in its shard.
- **Coordinator crash**: re-running with same `RUN_ID` resumes from
  whatever shards exist in `v5/results/`. Fresh `RUN_ID` starts clean.

## Re-running for a new dataset version

Bump `DATASET_VERSION` env var (default `v5`). Pipeline writes to
`v6/queue/`, `v6/results/`, etc. Original v5 stays intact on HF.
