# v5 Audio Analysis Pipeline

Enriches the v4 knowledge dataset with **exact audio features** (BPM, key, beat
grid, energy profile, waveform, LUFS, cue points) by downloading each track,
running Essentia + librosa, and publishing the result back to HuggingFace.

This subdir is the **complete, reproducible orchestration** — every script,
startup template, and config sample is committed. No secrets, no machine-
specific paths. All runtime state goes through env vars.

## What it produces

`v5/dj_treta_library.parquet` on HuggingFace — v4 schema + new columns:

```
bpm_exact            float    — Essentia RhythmExtractor2013
bpm_confidence       float    — 0.0-1.0
key_detected         str      — "Am", "F#m"
key_camelot          str      — "8A", "3B"
beat_grid_json       str      — JSON [{"t_ms": 1234, "beat_num": 0}, …] (first 128 beats)
energy_profile_json  str      — JSON [rms_0..N] at 10s windows
cue_point_ms         int      — first clean downbeat
lufs_integrated      float    — for deck volume normalization
waveform_rms_json    str      — JSON [rms] at 1s resolution
spectral_centroid    float    — brightness fingerprint
duration_ms_exact    int      — actual duration from audio (vs metadata)
analysis_version     str      — e.g. "essentia-2.1_librosa-0.10"
analyzed_at          str      — ISO timestamp
audio_path           str      — relative GCS path "audio/{mbid}.m4a", null if not retained
```

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

## Cost estimate

- Coordinator VM: e2-standard-2, ~$0.07/hr × 24h ≈ **$2**
- Workers: 32 × e2-standard-4 spot, ~$0.08/hr × 18h × 32 ≈ **$46**
- GCS storage during run: ~negligible (queue+results parquets)
- GCS storage if `KEEP_AUDIO_HOT=true` (~5K hot tracks ≈ 30GB) ≈ **$0.60/mo**
- HF upload bandwidth: free
- **Total one-time: ~$50** for ~200K tracks analyzed

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
