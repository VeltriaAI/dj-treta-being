"""v5 audio worker — runs on each worker VM, analyzes tracks in its shard.

Reads its shard from GCS, downloads each track via yt-dlp, runs Essentia +
librosa analysis, writes result rows back to GCS, deletes local audio.

Spawns N parallel processes (one per CPU core) for throughput.

Env vars (set by startup_worker.sh from VM metadata):
    GCP_PROJECT, GCS_BUCKET, DATASET_VERSION, RUN_ID, SHARD_ID,
    KEEP_AUDIO_HOT (optional)

Resumability: each track checkpointed in GCS at
{DATASET_VERSION}/checkpoints/{RUN_ID}/{shard}/{mbid}.done
On restart, worker skips done mbids.
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import polars as pl
from google.cloud import storage

# ── Config from env ───────────────────────────────────────────────────
GCP_PROJECT = os.environ["GCP_PROJECT"]
GCS_BUCKET = os.environ["GCS_BUCKET"]
DATASET_VERSION = os.environ.get("DATASET_VERSION", "v5")
RUN_ID = os.environ["RUN_ID"]
SHARD_ID = int(os.environ["SHARD_ID"])
KEEP_AUDIO_HOT = os.environ.get("KEEP_AUDIO_HOT", "false").lower() == "true"
WORKERS_PER_VM = int(os.environ.get("WORKERS_PER_VM", "4"))
ANALYSIS_VERSION = "essentia-2.1_librosa-0.10_v1"

LOCAL_TMP = Path("/tmp/v5_audio")
LOCAL_TMP.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s [shard=%(shard)s pid=%(process)d] %(message)s",
    level=logging.INFO,
)
log = logging.LoggerAdapter(logging.getLogger("v5"), {"shard": SHARD_ID})

# Lazy GCS client (re-init per process).
_gcs = None


def gcs():
    global _gcs
    if _gcs is None:
        _gcs = storage.Client(project=GCP_PROJECT)
    return _gcs.bucket(GCS_BUCKET)


# ── GCS paths ─────────────────────────────────────────────────────────
def queue_blob(shard_id: int) -> str:
    return f"{DATASET_VERSION}/queue/{RUN_ID}/shard_{shard_id:03d}.parquet"


def result_blob(shard_id: int, worker_id: int) -> str:
    return f"{DATASET_VERSION}/results/{RUN_ID}/shard_{shard_id:03d}_w{worker_id}.parquet"


def checkpoint_prefix(shard_id: int) -> str:
    return f"{DATASET_VERSION}/checkpoints/{RUN_ID}/shard_{shard_id:03d}/"


def audio_blob(mbid: str) -> str:
    return f"{DATASET_VERSION}/audio/{mbid}.m4a"


# ── Audio download ────────────────────────────────────────────────────
def download_audio(video_id: str, dest: Path) -> bool:
    """Download best audio via yt-dlp. Returns True on success."""
    url = f"https://music.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "-f",
        "bestaudio[ext=m4a]/bestaudio",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--socket-timeout",
        "30",
        "--retries",
        "2",
        "--max-filesize",
        "50M",
        "-o",
        str(dest),
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        return r.returncode == 0 and dest.exists() and dest.stat().st_size > 100_000
    except subprocess.TimeoutExpired:
        return False
    except Exception as e:
        log.warning(f"yt-dlp error for {video_id}: {e}")
        return False


# ── Analysis ──────────────────────────────────────────────────────────
KEY_TO_CAMELOT = {
    ("C", "major"): "8B", ("G", "major"): "9B", ("D", "major"): "10B",
    ("A", "major"): "11B", ("E", "major"): "12B", ("B", "major"): "1B",
    ("F#", "major"): "2B", ("C#", "major"): "3B", ("G#", "major"): "4B",
    ("D#", "major"): "5B", ("A#", "major"): "6B", ("F", "major"): "7B",
    ("A", "minor"): "8A", ("E", "minor"): "9A", ("B", "minor"): "10A",
    ("F#", "minor"): "11A", ("C#", "minor"): "12A", ("G#", "minor"): "1A",
    ("D#", "minor"): "2A", ("A#", "minor"): "3A", ("F", "minor"): "4A",
    ("C", "minor"): "5A", ("G", "minor"): "6A", ("D", "minor"): "7A",
}


def analyze(audio_path: Path, mbid: str) -> dict:
    """Run Essentia + librosa. Returns dict of v5 columns."""
    import essentia.standard as es
    import librosa

    sr = 44100
    audio_mono = es.MonoLoader(filename=str(audio_path), sampleRate=sr)()
    if len(audio_mono) < sr * 30:  # less than 30s, skip
        return {"mbid": mbid, "analysis_error": "too_short"}

    duration_ms = int(len(audio_mono) / sr * 1000)

    # BPM + beats
    bpm, beats, beats_conf, _, _ = es.RhythmExtractor2013(method="multifeature")(audio_mono)
    beat_grid = [
        {"t_ms": int(t * 1000), "beat_num": i}
        for i, t in enumerate(beats[:128])
    ]

    # Key
    key_extr = es.KeyExtractor()(audio_mono)
    # KeyExtractor returns (key, scale, strength)
    key_root, key_scale, _key_strength = key_extr
    key_detected = f"{key_root}{'m' if key_scale == 'minor' else ''}"
    key_camelot = KEY_TO_CAMELOT.get((key_root, key_scale), "")

    # LUFS (integrated loudness)
    try:
        lufs = float(es.LoudnessEBUR128()(audio_mono)[2])  # integrated
    except Exception:
        lufs = float("nan")

    # Energy profile — RMS at 10s windows
    win_10s = sr * 10
    energy_profile = []
    for i in range(0, len(audio_mono), win_10s):
        chunk = audio_mono[i : i + win_10s]
        if len(chunk):
            energy_profile.append(float(np.sqrt(np.mean(chunk ** 2))))

    # Waveform RMS at 1s resolution
    win_1s = sr
    waveform_rms = []
    for i in range(0, len(audio_mono), win_1s):
        chunk = audio_mono[i : i + win_1s]
        if len(chunk):
            waveform_rms.append(round(float(np.sqrt(np.mean(chunk ** 2))), 5))

    # Cue point — first beat with confidence > 0.5
    cue_point_ms = int(beats[0] * 1000) if len(beats) else 0

    # Spectral centroid (brightness)
    try:
        sc_mean = float(np.mean(librosa.feature.spectral_centroid(y=audio_mono, sr=sr)))
    except Exception:
        sc_mean = float("nan")

    return {
        "mbid": mbid,
        "bpm_exact": round(float(bpm), 2),
        "bpm_confidence": round(float(beats_conf), 3),
        "key_detected": key_detected,
        "key_camelot": key_camelot,
        "beat_grid_json": json.dumps(beat_grid),
        "energy_profile_json": json.dumps(energy_profile),
        "waveform_rms_json": json.dumps(waveform_rms),
        "cue_point_ms": cue_point_ms,
        "lufs_integrated": round(lufs, 2) if lufs == lufs else None,  # NaN check
        "spectral_centroid": round(sc_mean, 2) if sc_mean == sc_mean else None,
        "duration_ms_exact": duration_ms,
        "analysis_version": ANALYSIS_VERSION,
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "analysis_error": None,
    }


# ── Per-track pipeline ────────────────────────────────────────────────
def process_one(row: dict) -> dict | None:
    mbid = row["mbid"]
    video_id = row["video_id"]
    if not video_id:
        return None

    # Checkpoint check
    ckpt_blob = gcs().blob(checkpoint_prefix(SHARD_ID) + f"{mbid}.done")
    if ckpt_blob.exists():
        return None  # already done

    audio_path = LOCAL_TMP / f"{mbid}.m4a"
    try:
        if not download_audio(video_id, audio_path):
            ckpt_blob.upload_from_string("download_failed")
            return {"mbid": mbid, "analysis_error": "download_failed", "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

        result = analyze(audio_path, mbid)
        result["audio_path"] = None  # default

        # Optional: upload audio
        if KEEP_AUDIO_HOT:
            audio_gcs_path = audio_blob(mbid)
            gcs().blob(audio_gcs_path).upload_from_filename(str(audio_path))
            result["audio_path"] = audio_gcs_path

        ckpt_blob.upload_from_string("ok")
        return result
    except Exception as e:
        log.error(f"{mbid}: {e}\n{traceback.format_exc()}")
        ckpt_blob.upload_from_string(f"error:{type(e).__name__}")
        return {"mbid": mbid, "analysis_error": str(e)[:200], "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    finally:
        try:
            if audio_path.exists():
                audio_path.unlink()
        except Exception:
            pass


# ── Worker process ────────────────────────────────────────────────────
def worker_process(worker_id: int, rows: list[dict]):
    """One process handles a slice of the shard's rows."""
    log.info(f"worker {worker_id} starting on {len(rows)} tracks")
    results: list[dict] = []
    flush_every = 100

    for i, row in enumerate(rows):
        result = process_one(row)
        if result:
            results.append(result)

        if (i + 1) % flush_every == 0:
            _flush(worker_id, results)
            log.info(f"worker {worker_id}: {i + 1}/{len(rows)} done, flushed {len(results)}")
            results = []

    if results:
        _flush(worker_id, results)
    log.info(f"worker {worker_id} DONE")


def _flush(worker_id: int, rows: list[dict]):
    """Append rows to this worker's result parquet in GCS (idempotent merge)."""
    if not rows:
        return
    new_df = pl.DataFrame(rows)

    blob = gcs().blob(result_blob(SHARD_ID, worker_id))
    if blob.exists():
        # Download existing, append, re-upload.
        existing_path = LOCAL_TMP / f"existing_w{worker_id}.parquet"
        blob.download_to_filename(str(existing_path))
        existing = pl.read_parquet(existing_path)
        merged = pl.concat([existing, new_df], how="diagonal_relaxed")
        existing_path.unlink()
    else:
        merged = new_df

    out_path = LOCAL_TMP / f"out_w{worker_id}.parquet"
    merged.write_parquet(out_path, compression="zstd")
    blob.upload_from_filename(str(out_path))
    out_path.unlink()


# ── Main ──────────────────────────────────────────────────────────────
def main():
    log.info(f"shard {SHARD_ID} starting (workers={WORKERS_PER_VM}, keep_hot={KEEP_AUDIO_HOT})")

    # Download shard queue
    shard_path = LOCAL_TMP / "shard.parquet"
    gcs().blob(queue_blob(SHARD_ID)).download_to_filename(str(shard_path))
    df = pl.read_parquet(shard_path)
    rows = df.to_dicts()
    log.info(f"shard {SHARD_ID}: {len(rows)} tracks queued")

    # Split rows across processes
    splits: list[list[dict]] = [[] for _ in range(WORKERS_PER_VM)]
    for i, row in enumerate(rows):
        splits[i % WORKERS_PER_VM].append(row)

    procs = []
    for wid in range(WORKERS_PER_VM):
        p = mp.Process(target=worker_process, args=(wid, splits[wid]))
        p.start()
        procs.append(p)
    for p in procs:
        p.join()

    # Mark shard fully done
    done_blob = gcs().blob(f"{DATASET_VERSION}/done/{RUN_ID}/shard_{SHARD_ID:03d}.done")
    done_blob.upload_from_string(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    log.info(f"shard {SHARD_ID} COMPLETE")


if __name__ == "__main__":
    sys.exit(main())
