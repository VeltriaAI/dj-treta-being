#!/usr/bin/env python3
"""Build an ordered DJ set from a folder of tracks — offline, no Mixxx needed.

Usage:
    .venv/bin/python scripts/build_set.py <folder> [--name NAME] [--out DIR]

What it does
  1. Analyzes every track (librosa BPM/key/energy/mix-in/mix-out) and caches
     results in <folder>/.set-analysis.json so re-runs are instant.
  2. Cross-checks BPM/key against a reference catalogue (label metadata pulled
     from rekordbox/Spotify) when a title matches — label BPM wins over
     librosa when they disagree, and the disagreement is reported.
  3. Orders the set: ascending BPM, and inside each BPM band a greedy
     Camelot walk (each next track is the most harmonically compatible
     remaining one). No anchors — the whole set is one climb.
  4. Writes  <out>/<name>.m3u8  (import into rekordbox: File > Import >
     Playlist, or Mixxx: File > Load Playlist), <name>.md (the sheet) and
     <name>.json (machine-readable).

Born 2026-09-18 — Manish: "use dj treta to just get the tracks, rest you can
do yourself, feel free to evolve the mcp as needed or build tools as you need."
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.audio_analysis import analyze_audio  # noqa: E402
from agent.camelot import KEY_TO_CAMELOT, get_compatible_keys  # noqa: E402

AUDIO_EXT = {".mp3", ".flac", ".wav", ".m4a", ".aiff", ".ogg"}

# Reference catalogue — (bpm, camelot) as shown by rekordbox's Spotify metadata
# for the psy-fucking-love playlist, 2026-09-18. Keys are lowercase fragments
# that must appear in the (lowercased) filename.
REFERENCE: dict[str, tuple[float, str]] = {
    "sonic masala": (142, "11A"),
    "adhana": (138, "12B"),
    "the tribe": (160, "12B"),
    "mandala": (143, "1A"),
    "namaste": (138, "10B"),
    "deep jungle walk": (138, "4B"),
    "sahara": (145, "1A"),
    "pranava": (138, "9B"),
    "bungee jump": (138, "12B"),
    "universe inside me": (138, "9B"),
    "liquid hook": (135, "10B"),
    "be right": (142, "2B"),
    "wake up": (138, "12B"),
    "spiritual beings": (142, "10B"),
    "valley of stevie": (140, "6B"),
    "come with us": (142, "4B"),
    "the light": (135, "11A"),
    "frenchman in mumbai": (142, "5B"),
    "brief history of goa": (142, "12B"),
    "we are aliens": (142, "4B"),
    "shiva's india": (145, "10B"),
    "mescaline": (148, "12B"),
    "smoke": (145, "5B"),
    "out of reality": (140, "6B"),
    "telemetry": (131, "12A"),
    "starfield": (126, "9B"),
    "beyond influence": (140, "12A"),
    "liquid connective": (140, "11B"),
    "titan": (138, "9B"),
    "are we dreaming": (142, "7B"),
    "drumville": (142, "7B"),
    "burning stones": (138, "6B"),
    "dawn till dusk": (137, "7B"),
    "parvati valley": (143, "4B"),
    "indian spirit": (147, "10A"),
    "high on acid": (145, "9B"),
    "free tibet": (138, "12A"),
    "bazinga": (144, "9B"),
    "starfall": (142, "9B"),
    "forest cure": (144, "11B"),
    "rearranged": (145, "8A"),
    "albert balbert": (146, "10A"),
    "ready to get high": (138, "12B"),
    "story of d.m.t": (142, "12B"),
    "third eye (symbolic": (142, "11A"),
    "psychedelic trance": (145, "4A"),
    "zone tempest": (150, "9B"),
    "psychedelic traveller": (142, "11B"),
    "becoming insane": (145, "11B"),
    "deeply disturbed": (145, "12B"),
    "in front of me": (145, "9A"),
    "gravity waves": (147, "11A"),
    "power of now": (137, "9B"),
    "consciousness": (138, "7B"),
    "deep into nature": (137, "9B"),
    # GMS / Space Tribe — rekordbox search metadata, 2026-09-18
    "gms - juice": (148, "9B"),
    "full on": (142, "11A"),
    "conscious revolution": (145, "9B"),
    "into the 4th dimension": (147, "9B"),
    "time for a revolution": (147, "11B"),
    "all systems go": (147, "7B"),
}

CHANNEL_PREFIXES = [
    "iboga records music - ", "psychedelic universe - ", "dacru records - ",
    "nano records - ", "tip world records - ", "trancentral - ",
    "mutant disco records - ", "liquid soul official - ", "cross - ",
    "zone tempest (official) - ", "unitedbeatsrecords - ", "sacred technology - ",
    "yserecordings - ", "astral13ized - ", "skazi asher swissa - ", "johan öberg - ",
]


def clean_title(path: Path) -> str:
    name = path.stem
    low = name.lower()
    for p in CHANNEL_PREFIXES:
        if low.startswith(p):
            name = name[len(p):]
            low = name.lower()
            break
    name = re.sub(r"\s*-\s*topic\s*-\s*", " - ", name, flags=re.I)
    # "Astrix - Astrix - Deep Jungle Walk" -> "Astrix - Deep Jungle Walk"
    parts = [s.strip() for s in name.split(" - ")]
    dedup: list[str] = []
    for s in parts:
        if not dedup or dedup[-1].lower() != s.lower():
            dedup.append(s)
    name = " - ".join(dedup)
    name = re.sub(r"\s*\((official|music video|video|visuali[sz]ation)[^)]*\)", "", name, flags=re.I)
    return name.strip()


def normalize_bpm(bpm: float, lo: float = 120.0, hi: float = 170.0) -> float:
    """Fold librosa half/double/3:2 tempo errors into the genre window."""
    if not bpm:
        return 0.0
    if lo <= bpm <= hi:
        return round(bpm, 1)
    mid = (lo + hi) / 2
    cands = [bpm * f for f in (2, 0.5, 1.5, 1 / 1.5, 4, 0.25, 3, 1 / 3)]
    inside = [c for c in cands if lo <= c <= hi]
    if inside:
        return round(min(inside, key=lambda c: abs(c - mid)), 1)
    return round(bpm, 1)


def camelot_score(c1: str, c2: str) -> int:
    """Camelot-native compatibility: 10 same, 8 adjacent/relative, 5 two steps, 2 far, 5 unknown."""
    if not c1 or not c2:
        return 5
    if c1 == c2:
        return 10
    comp = get_compatible_keys(c1)
    if c2 in comp:
        return 8
    for c in comp:
        if c2 in get_compatible_keys(c):
            return 5
    return 2


def camelot_label(c1: str, c2: str) -> str:
    if not c1 or not c2:
        return "key ?"
    if c1 == c2:
        return "same key"
    if c2 in get_compatible_keys(c1):
        return "relative" if c1[:-1] == c2[:-1] else "adjacent"
    return "2 steps" if camelot_score(c1, c2) == 5 else "key jump"


def musical_to_camelot(key: str) -> str | None:
    if not key:
        return None
    return KEY_TO_CAMELOT.get(key) or KEY_TO_CAMELOT.get(key.replace("♭", "b").replace("♯", "#"))


def reference_for(path: Path) -> tuple[str, float, str] | None:
    low = path.name.lower().replace("’", "'")
    for frag, (bpm, cam) in REFERENCE.items():
        if frag in low:
            return frag, float(bpm), cam
    return None


def load_cache(folder: Path) -> dict:
    f = folder / ".set-analysis.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return {}
    return {}


def save_cache(folder: Path, cache: dict) -> None:
    (folder / ".set-analysis.json").write_text(json.dumps(cache, indent=1))


def analyze_folder(folder: Path, refresh: bool = False) -> list[dict]:
    files = sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in AUDIO_EXT and not p.name.startswith(".")
    )
    cache = {} if refresh else load_cache(folder)
    rows: list[dict] = []
    for i, p in enumerate(files, 1):
        key = p.name
        st = p.stat()
        entry = cache.get(key)
        if entry and entry.get("size") == st.st_size:
            rows.append(entry)
            continue
        t0 = time.time()
        print(f"[{i:2d}/{len(files)}] analyzing {p.name[:70]} ...", flush=True)
        try:
            a = analyze_audio(str(p))
        except Exception as e:  # keep going; report at the end
            print(f"      !! failed: {e}", flush=True)
            a = {}
        entry = {
            "file": key,
            "size": st.st_size,
            "title": clean_title(p),
            "librosa_bpm": normalize_bpm(float(a.get("bpm") or 0)),
            "librosa_key": a.get("key") or "",
            "librosa_camelot": musical_to_camelot(a.get("key") or "") or "",
            "energy_peak": a.get("energy_peak"),
            "duration": round(float(a.get("duration_seconds") or 0), 1),
            "mix_in": round(float(a.get("mix_in_seconds") or 0), 1),
            "mix_out": round(float(a.get("mix_out_seconds") or 0), 1),
        }
        cache[key] = entry
        save_cache(folder, cache)
        print(f"      bpm={entry['librosa_bpm']} key={entry['librosa_key']}/{entry['librosa_camelot']} "
              f"energy={entry['energy_peak']} ({time.time()-t0:.1f}s)", flush=True)
        rows.append(entry)
    return rows


def resolve(rows: list[dict], folder: Path) -> list[dict]:
    """Merge reference catalogue with analysis; label metadata wins."""
    out = []
    for r in rows:
        p = folder / r["file"]
        ref = reference_for(p)
        r = dict(r)
        if ref:
            frag, bpm, cam = ref
            r["bpm"], r["camelot"], r["source"] = bpm, cam, "label"
            r["bpm_disagree"] = abs(bpm - (r["librosa_bpm"] or bpm)) > 3
            r["key_disagree"] = bool(r["librosa_camelot"]) and r["librosa_camelot"] != cam
        else:
            r["bpm"], r["camelot"], r["source"] = r["librosa_bpm"], r["librosa_camelot"], "librosa"
            r["bpm_disagree"] = r["key_disagree"] = False
        out.append(r)
    return out


def order_set(tracks: list[dict]) -> list[dict]:
    """Ascending BPM; inside each band, greedy Camelot walk from the last placed track."""
    remaining = sorted(tracks, key=lambda t: (t["bpm"], t["title"]))
    ordered: list[dict] = []
    while remaining:
        band_bpm = remaining[0]["bpm"]
        band = [t for t in remaining if t["bpm"] == band_bpm]
        remaining = [t for t in remaining if t["bpm"] != band_bpm]
        while band:
            if ordered:
                prev = ordered[-1]["camelot"]
                band.sort(key=lambda t: (-camelot_score(prev, t["camelot"]), t["title"]))
            nxt = band.pop(0)
            ordered.append(nxt)
    return ordered


def transition_note(prev: dict | None, cur: dict) -> str:
    if not prev:
        return "opener"
    d = cur["bpm"] - prev["bpm"]
    harm = camelot_label(prev["camelot"], cur["camelot"])
    tempo = "hold" if d == 0 else (f"+{d:g}" if d > 0 else f"{d:g}")
    return f"{tempo} bpm · {harm}"


def write_outputs(ordered: list[dict], folder: Path, out_dir: Path, name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # M3U8 — absolute paths so rekordbox/Mixxx resolve them on the stick
    lines = ["#EXTM3U"]
    for t in ordered:
        lines.append(f"#EXTINF:{int(t['duration'])},{t['title']}")
        lines.append(str((folder / t["file"]).resolve()))
    (out_dir / f"{name}.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Sheet
    total = sum(t["duration"] for t in ordered)
    md = [f"# {name}", "", f"{len(ordered)} tracks · {total/3600:.1f} h · built {time.strftime('%Y-%m-%d %H:%M')}", "",
          "| # | BPM | Key | Track | Transition | Energy | Src |", "|--:|--:|:--|:--|:--|:--:|:--|"]
    prev = None
    for i, t in enumerate(ordered, 1):
        flag = ""
        if t["bpm_disagree"]:
            flag += f" ⚠bpm(librosa {t['librosa_bpm']})"
        if t["key_disagree"]:
            flag += f" ⚠key(librosa {t['librosa_camelot']})"
        md.append(f"| {i} | {t['bpm']:g} | {t['camelot']} | {t['title']} | {transition_note(prev, t)} | "
                  f"{t['energy_peak'] or ''} | {t['source']}{flag} |")
        prev = t
    (out_dir / f"{name}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (out_dir / f"{name}.json").write_text(json.dumps(ordered, indent=1), encoding="utf-8")
    print("\n".join(md))
    print(f"\nwrote {out_dir / (name + '.m3u8')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--name", default=None)
    ap.add_argument("--out", default=None, help="output dir (default: <folder>/../_SETS)")
    ap.add_argument("--refresh", action="store_true", help="ignore analysis cache")
    args = ap.parse_args()
    folder = Path(args.folder).expanduser().resolve()
    name = args.name or f"{time.strftime('%Y-%m-%d')}-{folder.name.lower()}"
    out_dir = Path(args.out).expanduser() if args.out else folder.parent / "_SETS"
    rows = analyze_folder(folder, refresh=args.refresh)
    tracks = resolve(rows, folder)
    ordered = order_set(tracks)
    write_outputs(ordered, folder, out_dir, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
