"""Real audio analysis using librosa — no LLM guessing.

Returns ground-truth: BPM, key, energy curve, sections, mix points.
Gemini is NOT used here — only signal processing.
"""

import json
import numpy as np
import librosa


# Key detection — chroma to musical key mapping
KEY_NAMES = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
KEY_PROFILES = {
    'major': [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
    'minor': [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
}


def analyze_audio(file_path: str) -> dict:
    """Full audio analysis using librosa.

    Returns:
        {
            "bpm": float,
            "key": str (e.g. "Dm", "Am"),
            "duration_seconds": float,
            "energy_peak": int (1-10),
            "energy_curve": list of (time, energy) tuples,
            "timeline": list of section dicts,
            "mix_in_seconds": float,
            "mix_out_seconds": float,
            "beat_count": int,
        }
    """
    # Load audio (mono, 22050 Hz default)
    y, sr = librosa.load(file_path, sr=22050, mono=True)
    duration = librosa.get_duration(y=y, sr=sr)

    # BPM detection
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    beat_times = librosa.frames_to_time(beats, sr=sr)

    # Key detection via chroma
    key = _detect_key(y, sr)

    # Energy analysis (RMS)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=512)

    # Normalize energy to 1-10
    rms_norm = rms / (rms.max() + 1e-8)

    # Compute sections from energy curve
    sections = _detect_sections(rms_norm, rms_times, duration, bpm)

    # Energy peak (overall)
    energy_peak = int(np.ceil(np.percentile(rms_norm, 90) * 10))
    energy_peak = max(1, min(10, energy_peak))

    # Energy curve (sampled every 5 seconds for compact representation)
    energy_curve = _sample_energy(rms_norm, rms_times, interval=5.0)

    # Mix points
    mix_in = _find_mix_in(sections)
    mix_out = _find_mix_out(sections, duration)

    return {
        "bpm": round(bpm, 1),
        "key": key,
        "duration_seconds": round(duration, 1),
        "energy_peak": energy_peak,
        "energy_curve": energy_curve,
        "timeline": sections,
        "mix_in_seconds": round(mix_in, 1),
        "mix_out_seconds": round(mix_out, 1),
        "beat_count": len(beat_times),
    }


def _detect_key(y, sr) -> str:
    """Detect musical key using Krumhansl-Schmuckler algorithm."""
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_avg = np.mean(chroma, axis=1)

    best_corr = -1
    best_key = "Am"

    for mode_name, profile in KEY_PROFILES.items():
        for shift in range(12):
            shifted_profile = np.roll(profile, shift)
            corr = np.corrcoef(chroma_avg, shifted_profile)[0, 1]
            if corr > best_corr:
                best_corr = corr
                root = KEY_NAMES[shift]
                suffix = "m" if mode_name == "minor" else ""
                best_key = f"{root}{suffix}"

    return best_key


def _detect_sections(rms_norm, rms_times, duration, bpm) -> list:
    """Detect track sections from energy curve.

    Uses energy thresholds and temporal smoothing to find:
    intro, buildup, drop/main, breakdown, outro
    """
    # Smooth energy over ~4 seconds for section detection
    window = max(1, int(4.0 / (rms_times[1] - rms_times[0]) if len(rms_times) > 1 else 1))
    smooth = np.convolve(rms_norm, np.ones(window) / window, mode='same')

    # Sample energy every 2 seconds
    step = max(1, int(2.0 / (rms_times[1] - rms_times[0]) if len(rms_times) > 1 else 1))
    samples = [(rms_times[i], smooth[i]) for i in range(0, len(smooth), step) if i < len(rms_times)]

    if not samples:
        return [{"start": 0, "end": round(duration), "section": "main", "energy": 5}]

    # Classify each 2-second window
    segments = []
    for t, e in samples:
        energy_level = max(1, min(10, int(np.ceil(e * 10))))
        segments.append({"time": round(t, 1), "energy": energy_level})

    # Merge consecutive segments with same classification into sections
    sections = []
    current_section = _classify_section(segments[0]["energy"], 0, duration)
    section_start = 0
    prev_energy = segments[0]["energy"]

    for seg in segments[1:]:
        section_type = _classify_section(seg["energy"], seg["time"], duration)
        # Merge if same type or similar energy (±2)
        if section_type == current_section and abs(seg["energy"] - prev_energy) <= 3:
            prev_energy = seg["energy"]
            continue
        # New section
        sections.append({
            "start": round(section_start),
            "end": round(seg["time"]),
            "section": current_section,
            "energy": prev_energy,
        })
        section_start = seg["time"]
        current_section = section_type
        prev_energy = seg["energy"]

    # Close last section
    sections.append({
        "start": round(section_start),
        "end": round(duration),
        "section": current_section,
        "energy": prev_energy,
    })

    # Merge very short sections (<8s) into neighbors
    merged = []
    for s in sections:
        if merged and (s["end"] - s["start"]) < 8:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(s)

    return merged if merged else sections


def _classify_section(energy: int, time: float, duration: float) -> str:
    """Classify a moment into a section type based on energy and position."""
    position_ratio = time / duration if duration > 0 else 0

    # Position-based heuristics
    if position_ratio < 0.1:
        return "intro"
    if position_ratio > 0.88:
        return "outro"

    # Energy-based
    if energy <= 3:
        return "breakdown"
    elif energy <= 5:
        if position_ratio < 0.3:
            return "intro"
        return "breakdown"
    elif energy <= 7:
        return "buildup"
    else:
        return "drop"


def _sample_energy(rms_norm, rms_times, interval=5.0) -> list:
    """Sample energy curve at regular intervals. Returns [(time, energy_1_10), ...]"""
    if len(rms_times) == 0:
        return []
    result = []
    t = 0
    max_t = rms_times[-1]
    while t <= max_t:
        idx = np.searchsorted(rms_times, t)
        idx = min(idx, len(rms_norm) - 1)
        energy = max(1, min(10, int(np.ceil(rms_norm[idx] * 10))))
        result.append((round(t, 1), energy))
        t += interval
    return result


def _find_mix_in(sections) -> float:
    """Best time to start mixing INTO this track — end of intro or first breakdown."""
    for s in sections:
        if s["section"] == "intro":
            return s["end"]
        if s["section"] == "breakdown" and s["start"] < 60:
            return s["start"]
    return sections[0]["end"] if sections else 30


def _find_mix_out(sections, duration) -> float:
    """Best time to start mixing OUT of this track — last breakdown or start of outro."""
    for s in reversed(sections):
        if s["section"] == "outro":
            return s["start"]
        if s["section"] == "breakdown" and s["start"] > duration * 0.6:
            return s["start"]
    return max(duration - 30, duration * 0.8)
