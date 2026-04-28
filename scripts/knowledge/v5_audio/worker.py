"""v5 audio worker — full Anyma-tier analysis pipeline.

Per track, in order (each tier wrapped in try/except — failure of one tier
doesn't kill the rest, partial data is better than no data):

  Tier 1: Download (yt-dlp → /tmp/{mbid}.m4a)
  Tier 2: Essentia (BPM, key, beats, LUFS, energy, waveform, spectral)
  Tier 3: Madmom (downbeats, bars, phrase boundaries, sections)
  Tier 4: Structure (drops, build-ups, breakdowns, 8 hot cues — heuristic
          on phrase + energy + spectral flux)
  Tier 5: silero-vad on full mix (vocal segments)
  Tier 6: CLAP embedding (512-dim, vibe similarity)
  Tier 7: musicnn genre/mood tags
  Tier 8: htdemucs stems (drums/bass/vocals/other) → upload Opus 192kbps
  Tier 9: Per-stem features (drum-only beat, bass-only key, etc.)
  Tier 10: basic-pitch MIDI extraction → upload .mid
  Tier 11: Whisper on vocals stem → lyrics + alignment
  Tier 12: Multi-res mel spectrograms → upload .npy
  Final: Upload audio + stems + midi + mel + lyrics to GCS, write parquet
         row, delete /tmp files, checkpoint.

Spawns N parallel processes per VM (configured via WORKERS_PER_VM).

Env vars (set by startup_worker.sh from VM metadata):
    GCP_PROJECT, GCS_BUCKET, DATASET_VERSION, RUN_ID, SHARD_ID, WORKERS_PER_VM

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

# ── Config ────────────────────────────────────────────────────────────
GCP_PROJECT = os.environ["GCP_PROJECT"]
GCS_BUCKET = os.environ["GCS_BUCKET"]
DATASET_VERSION = os.environ.get("DATASET_VERSION", "v5")
RUN_ID = os.environ["RUN_ID"]
SHARD_ID = int(os.environ["SHARD_ID"])
WORKERS_PER_VM = int(os.environ.get("WORKERS_PER_VM", "3"))
ANALYSIS_VERSION = "v5-2026-04-27"

LOCAL_TMP = Path("/tmp/v5_audio")
LOCAL_TMP.mkdir(exist_ok=True)
MODELS_DIR = Path("/opt/models")
MODELS_DIR.mkdir(exist_ok=True, parents=True)

logging.basicConfig(
    format="%(asctime)s [shard=%(shard)s pid=%(process)d] %(message)s",
    level=logging.INFO,
)
log = logging.LoggerAdapter(logging.getLogger("v5"), {"shard": SHARD_ID})

_gcs = None

# Resolve binaries from venv (PATH doesn't include /opt/venv/bin by default
# when worker.py is invoked as `/opt/venv/bin/python worker.py`).
VENV_BIN = "/opt/venv/bin"
YT_DLP = f"{VENV_BIN}/yt-dlp"
DEMUCS_PY = f"{VENV_BIN}/python3"  # for `python -m demucs.separate`
FFMPEG = "ffmpeg"  # system binary, always in PATH


def gcs():
    global _gcs
    if _gcs is None:
        _gcs = storage.Client(project=GCP_PROJECT)
    return _gcs.bucket(GCS_BUCKET)


# ── GCS path layout ───────────────────────────────────────────────────
def queue_blob(shard_id: int) -> str:
    return f"{DATASET_VERSION}/queue/{RUN_ID}/shard_{shard_id:03d}.parquet"


def result_blob(shard_id: int, worker_id: int) -> str:
    return f"{DATASET_VERSION}/results/{RUN_ID}/shard_{shard_id:03d}_w{worker_id}.parquet"


def checkpoint_prefix(shard_id: int) -> str:
    return f"{DATASET_VERSION}/checkpoints/{RUN_ID}/shard_{shard_id:03d}/"


def asset_paths(mbid: str) -> dict:
    """Stable per-track relative paths in GCS — never include bucket name.
    Stems use M4A AAC (not Opus): Essentia 2.1b6 segfaults on opus files
    in this image, M4A AAC is rock-solid for the per-stem analysis tier."""
    return {
        "audio": f"audio/{mbid}.m4a",
        "stem_drums": f"stems/{mbid}/drums.m4a",
        "stem_bass": f"stems/{mbid}/bass.m4a",
        "stem_vocals": f"stems/{mbid}/vocals.m4a",
        "stem_other": f"stems/{mbid}/other.m4a",
        "midi": f"midi/{mbid}.mid",
        "mel": f"mel/{mbid}.npy",
        "lyrics": f"lyrics/{mbid}.json",
    }


# ── Tier 1: Download ──────────────────────────────────────────────────
def download_audio(video_id: str, dest: Path) -> bool:
    url = f"https://music.youtube.com/watch?v={video_id}"
    cmd = [
        YT_DLP,
        "-f",
        "bestaudio[ext=m4a]/bestaudio",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--socket-timeout",
        "30",
        "--retries",
        "2",
        "--max-filesize",
        "60M",
        "-o",
        str(dest),
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        return r.returncode == 0 and dest.exists() and dest.stat().st_size > 100_000
    except Exception:
        return False


# ── Tier 2: Essentia ──────────────────────────────────────────────────
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


def analyze_essentia(audio_path: Path, sr: int = 44100) -> dict:
    import essentia.standard as es

    audio = es.MonoLoader(filename=str(audio_path), sampleRate=sr)()
    duration_ms = int(len(audio) / sr * 1000)

    bpm, beats, conf, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
    beat_times = [float(t) for t in beats]

    key_root, key_scale, key_strength = es.KeyExtractor()(audio)
    key_detected = f"{key_root}{'m' if key_scale == 'minor' else ''}"
    key_camelot = KEY_TO_CAMELOT.get((key_root, key_scale), "")

    # Essentia's EBUR128 needs stereo; we have mono. Use pyloudnorm instead
    # (pure-Python ITU-R BS.1770 LUFS, works on mono/stereo).
    try:
        import pyloudnorm as pyln
        meter = pyln.Meter(sr)
        lufs_int = float(meter.integrated_loudness(audio))
    except Exception:
        lufs_int = float("nan")

    win_1s = sr
    waveform_rms = []
    for i in range(0, len(audio), win_1s):
        chunk = audio[i : i + win_1s]
        if len(chunk):
            waveform_rms.append(round(float(np.sqrt(np.mean(chunk ** 2))), 5))

    win_10s = sr * 10
    energy_profile = []
    for i in range(0, len(audio), win_10s):
        chunk = audio[i : i + win_10s]
        if len(chunk):
            energy_profile.append(float(np.sqrt(np.mean(chunk ** 2))))

    return {
        "bpm_exact": round(float(bpm), 2),
        "bpm_confidence": round(float(conf), 3),
        "key_detected": key_detected,
        "key_camelot": key_camelot,
        "key_strength": round(float(key_strength), 3),
        "lufs_integrated": round(lufs_int, 2) if lufs_int == lufs_int else None,
        "duration_ms_exact": duration_ms,
        "beat_times": beat_times,  # used by structure tier
        "waveform_rms_json": json.dumps(waveform_rms),
        "energy_profile_json": json.dumps(energy_profile),
        "_audio_array": audio,  # passed to downstream tiers
    }


# ── Tier 3: BeatNet (downbeats, bars, phrases) ────────────────────────
# Replaced allin1 → which transitively imports madmom → which doesn't
# install on Python 3.11. BeatNet is pure-PyTorch, no madmom dependency,
# maintained 2024, gives beats + downbeats for any genre.
# Section labels (intro/verse/chorus) are not produced — we keep using
# the heuristic structure tier (drops/builds/breakdowns/hot_cues) which
# already covers the DJ use case.
_BEATNET_MODEL = None


def analyze_madmom(audio_path: Path, beat_times: list[float]) -> dict:
    """Downbeats + bar grid + phrase groupings via BeatNet."""
    try:
        global _BEATNET_MODEL
        if _BEATNET_MODEL is None:
            from BeatNet.BeatNet import BeatNet
            _BEATNET_MODEL = BeatNet(1, mode="offline", inference_model="DBN", plot=[], thread=False)
    except Exception as e:
        return {"downbeats_json": None, "phrases_json": None, "_madmom_error": f"import: {str(e)[:80]}"}

    try:
        # BeatNet returns (n, 2): [time_s, beat_position 1..N]
        out = _BEATNET_MODEL.process(str(audio_path))
    except Exception as e:
        return {"downbeats_json": None, "phrases_json": None, "_madmom_error": f"process: {str(e)[:80]}"}

    downbeats = [{"t_ms": int(t * 1000), "beat_in_bar": int(b)} for t, b in out]
    bars = [d for d in downbeats if d["beat_in_bar"] == 1]

    # Phrase groupings (every N bars)
    phrases = []
    for size in (8, 16, 32):
        for i in range(0, len(bars), size):
            phrases.append({
                "size_bars": size,
                "start_bar": i,
                "start_ms": bars[i]["t_ms"],
                "end_ms": bars[min(i + size, len(bars) - 1)]["t_ms"] if i + size < len(bars) else (bars[-1]["t_ms"] if bars else 0),
            })

    return {
        "downbeats_json": json.dumps(downbeats[:512]),
        "phrases_json": json.dumps(phrases),
        "bar_count": len(bars),
    }


# ── Tier 4: Structure (drops, build-ups, breakdowns, 8 hot cues) ──────
def analyze_structure(audio: np.ndarray, sr: int, beat_times: list[float], madmom_data: dict) -> dict:
    """Heuristic detection of song-structural events."""
    # RMS at 100ms resolution
    win = sr // 10
    rms = []
    for i in range(0, len(audio), win):
        chunk = audio[i : i + win]
        if len(chunk):
            rms.append(float(np.sqrt(np.mean(chunk ** 2))))
    rms = np.array(rms)

    if len(rms) == 0:
        return {"drops_json": None, "hot_cues_json": None}

    # Smooth
    kern = np.ones(20) / 20
    rms_smooth = np.convolve(rms, kern, mode="same")

    # Energy threshold per percentiles
    p20, p50, p80 = np.percentile(rms_smooth, [20, 50, 80])

    # Drop detection: sustained low-energy followed by 1.4x jump within 5 sec
    drops = []
    for i in range(20, len(rms_smooth) - 10):
        if rms_smooth[i] < p20 and rms_smooth[i + 5 : i + 15].mean() > p80 * 1.1:
            drops.append({"t_ms": int(i * 100), "type": "drop"})
            i += 50  # cooldown

    # Build-up: monotonic energy increase over >= 8 sec ending at a drop
    builds = []
    for d in drops:
        end = d["t_ms"] // 100
        start = max(end - 80, 0)
        if rms_smooth[end] > rms_smooth[start] * 1.3:
            builds.append({"start_ms": start * 100, "end_ms": d["t_ms"], "type": "build_up"})

    # Breakdown: low-energy stretch >= 8 sec inside the track
    breakdowns = []
    in_break = False
    bk_start = 0
    for i, v in enumerate(rms_smooth):
        if v < p20 and not in_break:
            in_break, bk_start = True, i
        elif v >= p50 and in_break:
            if i - bk_start >= 80:
                breakdowns.append({"start_ms": bk_start * 100, "end_ms": i * 100, "type": "breakdown"})
            in_break = False

    # 8 hot cues — anchor points pros set:
    # 1. First downbeat
    # 2. Intro end (first phrase ≥ 32 bars OR first vocal/melody)
    # 3. First build_up start
    # 4. First drop
    # 5. First breakdown start
    # 6. Second drop (if exists)
    # 7. Outro start (last 32-bar phrase)
    # 8. Last beat
    cues = []
    if beat_times:
        cues.append({"name": "first_downbeat", "t_ms": int(beat_times[0] * 1000)})
        cues.append({"name": "last_beat", "t_ms": int(beat_times[-1] * 1000)})
    try:
        phrases = json.loads(madmom_data.get("phrases_json") or "[]")
        big_phrases = [p for p in phrases if p["size_bars"] == 32]
        if big_phrases:
            cues.append({"name": "intro_end", "t_ms": big_phrases[0]["end_ms"]})
            cues.append({"name": "outro_start", "t_ms": big_phrases[-1]["start_ms"]})
    except Exception:
        pass
    if builds:
        cues.append({"name": "build_1", "t_ms": builds[0]["start_ms"]})
    if drops:
        cues.append({"name": "drop_1", "t_ms": drops[0]["t_ms"]})
        if len(drops) > 1:
            cues.append({"name": "drop_2", "t_ms": drops[1]["t_ms"]})
    if breakdowns:
        cues.append({"name": "break_1", "t_ms": breakdowns[0]["start_ms"]})

    return {
        "drops_json": json.dumps(drops),
        "build_ups_json": json.dumps(builds),
        "breakdowns_json": json.dumps(breakdowns),
        "hot_cues_json": json.dumps(cues[:8]),
    }


# ── Tier 5: VAD (vocal segments on full mix) ──────────────────────────
def analyze_vad(audio_path: Path) -> dict:
    try:
        import torch
        from silero_vad import load_silero_vad, get_speech_timestamps, read_audio
    except Exception as e:
        return {"vocal_segments_json": None, "_vad_error": str(e)[:80]}

    model = load_silero_vad()
    wav = read_audio(str(audio_path), sampling_rate=16000)
    ts = get_speech_timestamps(wav, model, sampling_rate=16000)
    segs = [{"start_ms": int(s["start"] / 16), "end_ms": int(s["end"] / 16)} for s in ts]
    return {"vocal_segments_json": json.dumps(segs)}


# ── Tier 6: CLAP embedding ────────────────────────────────────────────
def embed_clap(audio_path: Path) -> dict:
    try:
        import laion_clap
        global _CLAP_MODEL
        if "_CLAP_MODEL" not in globals():
            m = laion_clap.CLAP_Module(enable_fusion=False)
            m.load_ckpt()
            _CLAP_MODEL = m
        emb = _CLAP_MODEL.get_audio_embedding_from_filelist([str(audio_path)], use_tensor=False)
        return {"clap_embedding": emb[0].tolist()}
    except Exception as e:
        return {"clap_embedding": None, "_clap_error": str(e)[:80]}


# ── Tier 7: PANNs audio tags (replaces dead musicnn) ──────────────────
# musicnn is abandoned (TF1.x). PANNs (Pretrained Audio NN, AudioSet 527
# classes) is maintained, gives genre + mood + instrument tags in one pass.
_PANNS_MODEL = None


def analyze_musicnn(audio_path: Path) -> dict:
    """Top-10 audio tags via PANNs. Output column kept as `musicnn_tags_json`
    for schema continuity with prior runs."""
    try:
        global _PANNS_MODEL
        if _PANNS_MODEL is None:
            from panns_inference import AudioTagging
            _PANNS_MODEL = AudioTagging(checkpoint_path=None, device="cpu")
        import librosa, numpy as np
        wav, _ = librosa.load(str(audio_path), sr=32000, mono=True)
        wav = wav[None, :]  # (batch=1, samples)
        clipwise_output, _ = _PANNS_MODEL.inference(wav)
        # AudioSet labels — top 10
        from panns_inference.config import labels
        scores = clipwise_output[0]
        top_idx = np.argsort(scores)[-10:][::-1]
        tags = [{"label": labels[i], "score": round(float(scores[i]), 4)} for i in top_idx]
        return {"musicnn_tags_json": json.dumps(tags)}
    except Exception as e:
        return {"musicnn_tags_json": None, "_musicnn_error": str(e)[:80]}


# ── Tier 8: Stems (htdemucs) ──────────────────────────────────────────
def separate_stems(audio_path: Path, mbid: str) -> dict:
    """Run htdemucs, return paths to {drums,bass,vocals,other}.m4a locally."""
    out_dir = LOCAL_TMP / f"stems_{mbid}"
    out_dir.mkdir(exist_ok=True)
    cmd = [
        DEMUCS_PY, "-m", "demucs.separate",
        "-n", "htdemucs",
        "-o", str(out_dir),
        "--mp3",  # demucs native; we re-encode to AAC m4a after
        str(audio_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=600, check=True)
    except Exception as e:
        return {"stems_paths": None, "_stems_error": str(e)[:80]}

    # Demucs writes to {out_dir}/htdemucs/{stem_name}.{wav|mp3}
    stem_subdir = out_dir / "htdemucs" / audio_path.stem
    stems = {}
    for stem_name in ("drums", "bass", "vocals", "other"):
        src = stem_subdir / f"{stem_name}.mp3"
        if not src.exists():
            src = stem_subdir / f"{stem_name}.wav"
        if not src.exists():
            continue
        # re-encode to M4A AAC 192kbps. Original choice was Opus but
        # Essentia 2.1b6 segfaults on Opus files in this image (libopus
        # ABI mismatch). M4A AAC keeps the per-stem analyze tier (which
        # uses Essentia MonoLoader) safe.
        m4a_path = out_dir / f"{stem_name}.m4a"
        try:
            subprocess.run(
                [FFMPEG, "-y", "-i", str(src), "-c:a", "aac", "-b:a", "192k",
                 "-vn", str(m4a_path)],
                capture_output=True, check=True, timeout=120,
            )
            stems[stem_name] = m4a_path
            src.unlink()
        except Exception:
            pass
    return {"stems_paths": stems}


# ── Tier 9: Per-stem features ─────────────────────────────────────────
def analyze_stems(stems_paths: dict, sr: int = 44100) -> dict:
    if not stems_paths:
        return {}
    import essentia.standard as es
    out = {}
    for stem_name, path in stems_paths.items():
        try:
            audio = es.MonoLoader(filename=str(path), sampleRate=sr)()
            rms = float(np.sqrt(np.mean(audio ** 2)))
            out[f"stem_{stem_name}_rms"] = round(rms, 5)
            out[f"stem_{stem_name}_duration_ms"] = int(len(audio) / sr * 1000)
        except Exception:
            pass
    return out


# ── Tier 10: basic-pitch MIDI ─────────────────────────────────────────
def extract_midi(audio_path: Path, mbid: str) -> Path | None:
    """basic-pitch MIDI extraction. Uses lower-level predict() API to avoid
    issues with predict_and_save's TF model loading."""
    try:
        from basic_pitch.inference import predict
        from basic_pitch import ICASSP_2022_MODEL_PATH
        import pretty_midi  # bundled with basic-pitch
    except Exception as e:
        log.warning(f"basic-pitch import failed: {e}")
        return None

    try:
        model_output, midi_data, note_events = predict(
            str(audio_path),
            model_or_model_path=ICASSP_2022_MODEL_PATH,
        )
        out_dir = LOCAL_TMP / f"midi_{mbid}"
        out_dir.mkdir(exist_ok=True)
        midi_path = out_dir / f"{mbid}.mid"
        midi_data.write(str(midi_path))
        return midi_path if midi_path.exists() else None
    except Exception as e:
        log.warning(f"{mbid} basic-pitch predict: {str(e)[:120]}")
        return None


# ── Tier 11: Whisper on vocals stem ───────────────────────────────────
def transcribe_vocals(vocals_path: Path) -> dict | None:
    try:
        from faster_whisper import WhisperModel
        global _WHISPER_MODEL
        if "_WHISPER_MODEL" not in globals():
            _WHISPER_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, info = _WHISPER_MODEL.transcribe(str(vocals_path), beam_size=1)
        segs = [{"start_ms": int(s.start * 1000), "end_ms": int(s.end * 1000), "text": s.text.strip()}
                for s in segments]
        return {"language": info.language, "segments": segs}
    except Exception:
        return None


# ── Tier 12: Multi-res mel spectrograms ───────────────────────────────
def compute_mel(audio: np.ndarray, sr: int = 44100) -> np.ndarray | None:
    try:
        import librosa
        # 128-bin mel, 22kHz nyquist, 1024 FFT, 512 hop
        mel = librosa.feature.melspectrogram(y=audio, sr=sr, n_mels=128, n_fft=1024, hop_length=512)
        mel_db = librosa.power_to_db(mel, ref=np.max)
        return mel_db.astype(np.float16)  # half precision for storage
    except Exception:
        return None


# ── Master per-track pipeline ─────────────────────────────────────────
def process_one(row: dict) -> dict | None:
    mbid = row["mbid"]
    video_id = row["video_id"]
    if not video_id:
        return None

    bucket = gcs()
    ckpt_blob = bucket.blob(checkpoint_prefix(SHARD_ID) + f"{mbid}.done")
    if ckpt_blob.exists():
        return None

    audio_path = LOCAL_TMP / f"{mbid}.m4a"
    paths = asset_paths(mbid)
    result = {"mbid": mbid, "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "analysis_version": ANALYSIS_VERSION}

    try:
        # Tier 1
        if not download_audio(video_id, audio_path):
            result["analysis_error"] = "download_failed"
            ckpt_blob.upload_from_string("download_failed")
            return result

        # Tier 2
        try:
            ess = analyze_essentia(audio_path)
            audio_arr = ess.pop("_audio_array")
            beat_times = ess.pop("beat_times")
            result.update(ess)
        except Exception as e:
            log.warning(f"{mbid} essentia: {e}")
            audio_arr, beat_times = None, []

        # Tier 3
        try:
            mm = analyze_madmom(audio_path, beat_times)
            result.update({k: v for k, v in mm.items() if not k.startswith("_")})
            mm_data = mm
        except Exception as e:
            log.warning(f"{mbid} madmom: {e}")
            mm_data = {}

        # Tier 4
        if audio_arr is not None:
            try:
                struct = analyze_structure(audio_arr, 44100, beat_times, mm_data)
                result.update(struct)
            except Exception as e:
                log.warning(f"{mbid} structure: {e}")

        # Tier 5
        try:
            result.update(analyze_vad(audio_path))
        except Exception as e:
            log.warning(f"{mbid} vad: {e}")

        # Tier 6
        try:
            result.update(embed_clap(audio_path))
        except Exception as e:
            log.warning(f"{mbid} clap: {e}")

        # Tier 7
        try:
            result.update(analyze_musicnn(audio_path))
        except Exception as e:
            log.warning(f"{mbid} musicnn: {e}")

        # Tier 8
        stems_data = separate_stems(audio_path, mbid)
        stems_paths = stems_data.get("stems_paths") or {}

        # Upload stems
        for stem_name, local_path in stems_paths.items():
            gcs_path = paths[f"stem_{stem_name}"]
            try:
                bucket.blob(gcs_path).upload_from_filename(str(local_path))
                result[f"stem_{stem_name}_path"] = gcs_path
            except Exception as e:
                log.warning(f"{mbid} upload stem {stem_name}: {e}")

        # Tier 9
        try:
            result.update(analyze_stems(stems_paths))
        except Exception as e:
            log.warning(f"{mbid} stems analyze: {e}")

        # Tier 10
        midi_path = extract_midi(audio_path, mbid)
        if midi_path and midi_path.exists():
            try:
                bucket.blob(paths["midi"]).upload_from_filename(str(midi_path))
                result["midi_path"] = paths["midi"]
            except Exception:
                pass

        # Tier 11 — Whisper on vocals stem
        if "vocals" in stems_paths:
            tx = transcribe_vocals(stems_paths["vocals"])
            if tx:
                lyrics_local = LOCAL_TMP / f"{mbid}_lyrics.json"
                lyrics_local.write_text(json.dumps(tx))
                try:
                    bucket.blob(paths["lyrics"]).upload_from_filename(str(lyrics_local))
                    result["lyrics_path"] = paths["lyrics"]
                    result["lyrics_language"] = tx.get("language")
                    lyrics_local.unlink()
                except Exception:
                    pass

        # Tier 12 — mel spectrogram
        if audio_arr is not None:
            mel = compute_mel(audio_arr)
            if mel is not None:
                mel_local = LOCAL_TMP / f"{mbid}_mel.npy"
                np.save(mel_local, mel)
                try:
                    bucket.blob(paths["mel"]).upload_from_filename(str(mel_local))
                    result["mel_path"] = paths["mel"]
                    mel_local.unlink()
                except Exception:
                    pass

        # Final: upload the audio itself
        try:
            bucket.blob(paths["audio"]).upload_from_filename(str(audio_path))
            result["audio_path"] = paths["audio"]
        except Exception as e:
            log.warning(f"{mbid} upload audio: {e}")

        ckpt_blob.upload_from_string("ok")
        return result

    except Exception as e:
        log.error(f"{mbid} fatal: {e}\n{traceback.format_exc()}")
        result["analysis_error"] = str(e)[:200]
        ckpt_blob.upload_from_string(f"error:{type(e).__name__}")
        return result
    finally:
        # Clean up local audio + stems + temp files
        try:
            if audio_path.exists():
                audio_path.unlink()
            for d in LOCAL_TMP.glob(f"stems_{mbid}*"):
                shutil.rmtree(d, ignore_errors=True)
            for d in LOCAL_TMP.glob(f"midi_{mbid}*"):
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


# ── Worker process ────────────────────────────────────────────────────
def worker_process(worker_id: int, rows: list[dict]):
    log.info(f"worker {worker_id} starting on {len(rows)} tracks")
    results: list[dict] = []
    flush_every = 25  # flush more often since each track is heavier

    for i, row in enumerate(rows):
        result = process_one(row)
        if result:
            results.append(result)

        if (i + 1) % flush_every == 0:
            _flush(worker_id, results)
            log.info(f"worker {worker_id}: {i + 1}/{len(rows)} done")
            results = []

    if results:
        _flush(worker_id, results)
    log.info(f"worker {worker_id} DONE")


def _flush(worker_id: int, rows: list[dict]):
    if not rows:
        return
    new_df = pl.DataFrame(rows)
    blob = gcs().blob(result_blob(SHARD_ID, worker_id))
    if blob.exists():
        existing = LOCAL_TMP / f"existing_w{worker_id}.parquet"
        blob.download_to_filename(str(existing))
        merged = pl.concat([pl.read_parquet(existing), new_df], how="diagonal_relaxed")
        existing.unlink()
    else:
        merged = new_df
    out = LOCAL_TMP / f"out_w{worker_id}.parquet"
    merged.write_parquet(out, compression="zstd")
    blob.upload_from_filename(str(out))
    out.unlink()


def main():
    # Use spawn (fresh interpreter) instead of fork — fork inherits parent's
    # locked threadpool state from polars/numpy/google-cloud-storage and
    # deadlocks the child workers. This is a known C-extension+fork issue.
    mp.set_start_method("spawn", force=True)

    # Smoke-test critical binaries on startup. Fail loud rather than
    # silently mark every track as download_failed.
    for bin_path in (YT_DLP, FFMPEG):
        check_cmd = [bin_path, "--version"] if "/" in bin_path else ["which", bin_path]
        if subprocess.run(check_cmd, capture_output=True).returncode != 0:
            log.error(f"FATAL: binary missing or broken: {bin_path}")
            sys.exit(1)
    log.info(f"binaries OK: {YT_DLP}, {FFMPEG}")

    log.info(f"shard {SHARD_ID} starting (workers={WORKERS_PER_VM})")
    shard_path = LOCAL_TMP / "shard.parquet"
    gcs().blob(queue_blob(SHARD_ID)).download_to_filename(str(shard_path))
    df = pl.read_parquet(shard_path)
    rows = df.to_dicts()
    log.info(f"shard {SHARD_ID}: {len(rows)} tracks")

    splits = [[] for _ in range(WORKERS_PER_VM)]
    for i, row in enumerate(rows):
        splits[i % WORKERS_PER_VM].append(row)

    procs = []
    for wid in range(WORKERS_PER_VM):
        p = mp.Process(target=worker_process, args=(wid, splits[wid]))
        p.start()
        procs.append(p)
    for p in procs:
        p.join()

    gcs().blob(f"{DATASET_VERSION}/done/{RUN_ID}/shard_{SHARD_ID:03d}.done").upload_from_string(
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    log.info(f"shard {SHARD_ID} COMPLETE")


if __name__ == "__main__":
    sys.exit(main())
