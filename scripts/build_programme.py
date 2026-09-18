#!/usr/bin/env python3
"""Cut a multi-hour programme out of analyzed track folders.

Usage:
    .venv/bin/python scripts/build_programme.py programme.json

programme.json:
{
  "name": "2026-09-18-night",
  "out": "/Volumes/DJ-TRETA/_SETS",
  "blocks": [
    {"name": "H1 Bolly Techno — warm-up", "folder": "/Volumes/DJ-TRETA/Bollywood/Techno",
     "bpm_min": 100, "bpm_max": 124, "minutes": 60, "bpm_window": [100, 150]},
    ...
  ]
}

Each block: take every track in `folder` whose BPM is within [bpm_min, bpm_max],
order it ascending (BPM, then Camelot walk — same rule as build_set.py), then
sample evenly along that order until `minutes` is filled, so a 60-minute cut
of a 4-hour pool still climbs from the bottom of the band to the top.

BPM sources, in precedence order:
  1. filename prefix  "120 - Song Name.mp3"   (the pack compiler's tempo)
  2. reference catalogue (label metadata, see build_set.REFERENCE)
  3. DJ Treta's library DB (~/.local/share/djclaw/db/djtreta.db)
  4. librosa (folded into the block's bpm_window)
Key comes from the same chain (prefix has none), energy from DB/librosa.

Outputs in <out>/<name>/:  <name>.m3u8 (all blocks), one .m3u8 per block,
<name>.md (the sheet), <name>.json.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.build_set import (  # noqa: E402
    AUDIO_EXT, camelot_score, clean_title, load_cache, musical_to_camelot, normalize_bpm,
    order_set, reference_for, transition_note,
)

DB_PATH = Path.home() / ".local/share/djclaw/db/djtreta.db"
PREFIX_RE = re.compile(r"^\s*(\d{2,3})\s*[-_. ]")


def bpm_from_filename(name: str) -> float | None:
    m = PREFIX_RE.match(name)
    if not m:
        return None
    v = int(m.group(1))
    return float(v) if 85 <= v <= 175 else None


def db_lookup(path: Path) -> dict:
    if not DB_PATH.exists():
        return {}
    try:
        con = sqlite3.connect(str(DB_PATH))
        row = con.execute(
            "select bpm, key_camelot, energy_peak, duration_seconds from tracks where path=?",
            (str(path),),
        ).fetchone()
        con.close()
    except Exception:
        return {}
    if not row:
        return {}
    bpm, cam, energy, dur = row
    return {"bpm": bpm, "camelot": cam, "energy": energy, "duration": dur}


def collect(folder: Path, bpm_window: tuple[float, float]) -> list[dict]:
    cache = load_cache(folder)
    rows: list[dict] = []
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() not in AUDIO_EXT or p.name.startswith("."):
            continue
        a = cache.get(p.name, {})
        db = db_lookup(p)
        ref = reference_for(p)
        lib_bpm = normalize_bpm(float(a.get("librosa_bpm") or 0), *bpm_window) if a.get("librosa_bpm") else None
        lib_cam = a.get("librosa_camelot") or ""

        # ---- BPM precedence ----
        pre = bpm_from_filename(p.name)
        if pre:
            bpm, src = pre, "prefix"
        elif ref:
            bpm, src = ref[1], "label"
        elif db.get("bpm"):
            bpm, src = normalize_bpm(float(db["bpm"]), *bpm_window), "db"
        elif lib_bpm:
            bpm, src = lib_bpm, "librosa"
        else:
            continue  # nothing to go on — skip silently, reported in sheet footer

        # ---- key precedence ----
        cam = (ref[2] if ref else None) or db.get("camelot") or lib_cam or ""
        energy = db.get("energy") or a.get("energy_peak")
        duration = float(a.get("duration") or db.get("duration") or 0)
        rows.append({
            "file": p.name, "path": str(p), "title": clean_title(p),
            "bpm": float(bpm), "camelot": cam, "source": src,
            "librosa_bpm": lib_bpm, "librosa_camelot": lib_cam,
            "bpm_disagree": bool(lib_bpm and abs(lib_bpm - float(bpm)) > 3),
            "key_disagree": bool(lib_cam and cam and lib_cam != cam),
            "energy_peak": energy, "duration": duration,
            "mix_in": a.get("mix_in"), "mix_out": a.get("mix_out"),
        })
    return rows


def song_core(title: str) -> str:
    """'Ace Ventura & Lish - The Light (Astrix Remix)' -> 'the light' — to keep two
    versions of one song out of the same hour."""
    s = re.sub(r"[\(\[].*?[\)\]]", "", title.lower())
    s = re.split(r"\s*-\s*", s)[-1]
    s = re.sub(r"\b(remix|rmx|original mix|extended mix|official|vs\.?|feat\.?)\b.*", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def sample_to_minutes(ordered: list[dict], minutes: float, fill: str = "even") -> list[dict]:
    """Pick tracks along the ordered pool until `minutes` is filled.

    fill="even" — spread evenly across the pool (a 1-hour cut still climbs bottom→top)
    fill="top"  — take the highest-BPM tracks first (peak-time block), then re-ascend
    """
    target = minutes * 60
    if not ordered:
        return []
    if fill == "top":
        picked: list[dict] = []
        for t in reversed(ordered):
            if sum(x["duration"] or 360 for x in picked) >= target:
                break
            picked.append(t)
        return order_set(picked)
    total = sum(t["duration"] or 360 for t in ordered)
    if total <= target:
        return ordered
    avg = total / len(ordered)
    n = max(2, int(round(target / avg)))
    if n >= len(ordered):
        return ordered
    # Sample by BPM VALUE, not by index: evenly spaced tempo targets across the
    # band, nearest unused track to each (ties -> best Camelot with the previous
    # pick). A sparse low end no longer gets skipped, so the climb has no cliffs.
    lo, hi = ordered[0]["bpm"], ordered[-1]["bpm"]
    targets = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    pool = list(ordered)
    picked: list[dict] = []
    for tg in targets:
        if not pool:
            break
        prev = picked[-1]["camelot"] if picked else ""
        used = {song_core(x["title"]) for x in picked}
        cands = [t for t in pool if song_core(t["title"]) not in used] or pool
        best = min(cands, key=lambda t: (abs(t["bpm"] - tg), -camelot_score(prev, t["camelot"]), t["title"]))
        picked.append(best)
        pool.remove(best)
    picked = order_set(picked)
    while sum(t["duration"] or 360 for t in picked) > target * 1.08 and len(picked) > 2:
        picked.pop(len(picked) // 2)
    return picked


def write(prog: dict, blocks_out: list[tuple[dict, list[dict], int]]) -> None:
    name = prog["name"]
    out = Path(prog.get("out", "/Volumes/DJ-TRETA/_SETS")).expanduser() / name
    out.mkdir(parents=True, exist_ok=True)
    master = ["#EXTM3U"]
    md = [f"# {name}", "", f"built {time.strftime('%Y-%m-%d %H:%M')}", ""]
    js = []
    for bi, (spec, tracks, pool_n) in enumerate(blocks_out, 1):
        mins = sum(t["duration"] for t in tracks) / 60
        lines = ["#EXTM3U"]
        for t in tracks:
            lines.append(f"#EXTINF:{int(t['duration'])},{t['title']}")
            lines.append(t["path"])
            master.append(f"#EXTINF:{int(t['duration'])},[{bi}] {t['title']}")
            master.append(t["path"])
        safe = re.sub(r"[^\w\-]+", "_", spec["name"]).strip("_")
        (out / f"{bi}-{safe}.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")
        md += [f"## {bi}. {spec['name']}  —  {len(tracks)} tracks · {mins:.0f} min  "
               f"(pool {pool_n} tracks in {spec['bpm_min']}–{spec['bpm_max']} bpm)", "",
               "| # | BPM | Key | Track | Transition | E | Src |", "|--:|--:|:--|:--|:--|:--:|:--|"]
        prev = None
        for i, t in enumerate(tracks, 1):
            flag = (" ⚠bpm" if t["bpm_disagree"] else "") + (" ⚠key" if t["key_disagree"] else "")
            md.append(f"| {i} | {t['bpm']:g} | {t['camelot'] or '?'} | {t['title']} | "
                      f"{transition_note(prev, t)} | {t['energy_peak'] or ''} | {t['source']}{flag} |")
            prev = t
        md.append("")
        js.append({"block": spec, "tracks": tracks})
    (out / f"{name}.m3u8").write_text("\n".join(master) + "\n", encoding="utf-8")
    (out / f"{name}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (out / f"{name}.json").write_text(json.dumps(js, indent=1), encoding="utf-8")
    print("\n".join(md))
    print(f"\nwrote {out}/")


def main() -> int:
    prog = json.loads(Path(sys.argv[1]).read_text())
    results = []
    for spec in prog["blocks"]:
        folder = Path(spec["folder"]).expanduser()
        window = tuple(spec.get("bpm_window", [100, 170]))
        pool = [t for t in collect(folder, window) if spec["bpm_min"] <= t["bpm"] <= spec["bpm_max"]]
        ordered = order_set(pool)
        cut = sample_to_minutes(ordered, float(spec.get("minutes", 60)), spec.get("fill", "even"))
        results.append((spec, cut, len(pool)))
    write(prog, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
