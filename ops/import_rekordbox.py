"""Import a Rekordbox `collection.xml` export into the DJ Treta library DB.

E3 (Library Ingestion & Analysis Coverage). A DJ who already analyzed their
crate in Rekordbox has BPM, key, beatgrids, cue points (with colors + loops)
and playlist structure baked into the export. We parse all of it and
`upsert_track()` it straight into our DB — **zero librosa passes**, so a
1,000+ track library ingests in seconds instead of hours.

Run:
    python3 -m ops.import_rekordbox /path/to/rekordbox.xml
    # or via the unified entrypoint:
    python3 -m ops.ingest_library /path/to/rekordbox.xml

Rekordbox XML format (DeviceSQL export, schema as of Rekordbox 6):
    <DJ_PLAYLISTS>
      <COLLECTION Entries="N">
        <TRACK TrackID=".." Location="file://localhost/..." Name=".." Artist=".."
               Genre=".." AverageBpm="128.00" Tonality="Am" TotalTime="360">
          <TEMPO Inizio="0.025" Bpm="128.00" Metro="4/4" Battito="1"/>
          <POSITION_MARK Name="Intro" Type="0" Start="0.025" Num="0"
                         Red="40" Green="226" Blue="20"/>
          <POSITION_MARK Name="Loop" Type="4" Start="120.0" End="135.0" Num="2"/>
        </TRACK>
      </COLLECTION>
      <PLAYLISTS>
        <NODE Type="0" Name="ROOT" Count="..">
          <NODE Type="0" Name="Sets" Count="..">          <!-- folder -->
            <NODE Type="1" Name="Warmup" KeyType="0" Entries="..">  <!-- playlist -->
              <TRACK Key="123"/>
            </NODE>
          </NODE>
        </NODE>
      </PLAYLISTS>
    </DJ_PLAYLISTS>

POSITION_MARK Type codes (Rekordbox): 0=Cue, 1=Fade-In, 2=Fade-Out,
3=Load, 4=Loop. A loop has both Start and End. Loop length in beats is
derived from (End-Start) / beat_seconds, where beat_seconds = 60/bpm.

Colors: hot cues carry Red/Green/Blue 0-255 attributes. Memory cues
(Num="-1") typically don't; we leave color None for those.
"""
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from agent.audio_analysis import map_section_to_block, block_color
from agent.camelot import KEY_TO_CAMELOT
from agent.db import (
    upsert_track,
    replace_track_cues,
    get_track_id_by_path,
    upsert_import_playlist,
    add_track_to_playlist,
)

SOURCE = "rekordbox"

# Rekordbox POSITION_MARK Type → our cue kind.
_MARK_KIND = {"0": "cue", "1": "fade-in", "2": "fade-out", "3": "load", "4": "loop"}


def _location_to_path(location: str) -> str:
    """Rekordbox stores Location as a file URL: file://localhost/Users/...
    Return a real filesystem path (URL-decoded)."""
    if not location:
        return ""
    loc = location
    for prefix in ("file://localhost", "file://"):
        if loc.startswith(prefix):
            loc = loc[len(prefix):]
            break
    return urllib.parse.unquote(loc)


def _rgb_to_hex(track_el) -> str | None:
    r, g, b = track_el.get("Red"), track_el.get("Green"), track_el.get("Blue")
    if r is None or g is None or b is None:
        return None
    try:
        return f"#{int(r):02X}{int(g):02X}{int(b):02X}"
    except (TypeError, ValueError):
        return None


def _parse_cues(track_el, bpm: float, duration: float) -> list[dict]:
    """Extract POSITION_MARK elements → our cue dicts with section mapping."""
    beat_seconds = (60.0 / bpm) if bpm else 0.0
    cues = []
    for m in track_el.findall("POSITION_MARK"):
        start = _as_float(m.get("Start"))
        if start is None:
            continue
        type_code = m.get("Type", "0")
        kind = _MARK_KIND.get(type_code, "cue")
        is_loop = kind == "loop"
        loop_len_beats = None
        if is_loop:
            end = _as_float(m.get("End"))
            if end is not None and beat_seconds:
                loop_len_beats = round((end - start) / beat_seconds, 2)
        cues.append({
            "cue_index": _as_int(m.get("Num")),
            "name": m.get("Name") or "",
            "start_seconds": round(start, 3),
            "kind": kind,
            "is_loop": is_loop,
            "loop_length_beats": loop_len_beats,
            "color": _rgb_to_hex(m),
        })
    return _assign_sections(cues, duration)


def _assign_sections(cues: list[dict], duration: float) -> list[dict]:
    """Map cues → our section markers. First cue → mix_in (START); last/outro
    cue → mix_out (BREAK); loop cues → LOOP; the rest → timeline sections.
    The block/color come from the START/BREAK/LOOP/DROP vocabulary."""
    if not cues:
        return cues
    ordered = sorted(cues, key=lambda c: c["start_seconds"])
    n = len(ordered)
    for i, c in enumerate(ordered):
        if c["is_loop"]:
            c["section"] = "mix_loop"  # marker; block resolves to LOOP below
            block = "LOOP"
        elif i == 0:
            c["section"] = "mix_in"
            block = "START"
        elif i == n - 1 or (duration and c["start_seconds"] > duration * 0.85):
            c["section"] = "mix_out"
            block = "BREAK"
        else:
            c["section"] = "timeline"
            block = "DROP"
        # store the resolved block on the cue color if Rekordbox gave none
        if not c.get("color"):
            c["color"] = block_color(block)
    return ordered


def _build_timeline(cues: list[dict], duration: float) -> list[dict]:
    """Build a section timeline (start/end/section/block/color/energy) from
    cues so the planner + waveform have a sectioned view without librosa."""
    if not cues:
        return []
    ordered = sorted(cues, key=lambda c: c["start_seconds"])
    timeline = []
    for i, c in enumerate(ordered):
        end = ordered[i + 1]["start_seconds"] if i + 1 < len(ordered) else duration
        block = map_section_to_block(c.get("section", ""), is_loop=c["is_loop"])
        timeline.append({
            "start": round(c["start_seconds"], 1),
            "end": round(end or c["start_seconds"], 1),
            "section": c.get("section", "timeline"),
            "block": block,
            "color": block_color(block),
            "energy": 5,  # Rekordbox carries no energy curve; neutral default
        })
    return timeline


def _track_mix_points(cues: list[dict], duration: float):
    """Resolve mix_in / mix_out seconds from cues for schedule_transition."""
    mix_in = mix_out = None
    for c in cues:
        if c.get("section") == "mix_in" and mix_in is None:
            mix_in = c["start_seconds"]
        if c.get("section") == "mix_out":
            mix_out = c["start_seconds"]
    if mix_in is None and cues:
        mix_in = min(c["start_seconds"] for c in cues)
    if mix_out is None and duration:
        mix_out = max(duration - 30, duration * 0.8)
    return mix_in, mix_out


def _beatgrid(track_el, bpm: float):
    """First TEMPO marker → beatgrid anchor (sec) + bar length (sec)."""
    tempo = track_el.find("TEMPO")
    anchor = _as_float(tempo.get("Inizio")) if tempo is not None else None
    grid_bpm = (_as_float(tempo.get("Bpm")) if tempo is not None else None) or bpm
    bar_seconds = (60.0 / grid_bpm) * 4 if grid_bpm else None
    return anchor, bar_seconds


def parse_collection(xml_path: str) -> dict:
    """Parse a Rekordbox collection.xml. Returns:
        {"tracks": [track_dict...], "playlists": [pl_dict...],
         "trackid_to_path": {rb_id: fs_path}}
    Pure parse — no DB writes (so it's unit-testable without a DB).
    """
    root = ET.parse(xml_path).getroot()
    collection = root.find("COLLECTION")
    tracks = []
    trackid_to_path = {}
    if collection is not None:
        for t in collection.findall("TRACK"):
            path = _location_to_path(t.get("Location", ""))
            if not path:
                continue
            bpm = _as_float(t.get("AverageBpm")) or 0.0
            duration = _as_float(t.get("TotalTime")) or 0.0
            key_musical = (t.get("Tonality") or "").strip()
            cues = _parse_cues(t, bpm, duration)
            anchor, bar_seconds = _beatgrid(t, bpm)
            mix_in, mix_out = _track_mix_points(cues, duration)
            tracks.append({
                "rb_id": t.get("TrackID"),
                "path": path,
                "title": t.get("Name") or Path(path).stem,
                "artist": t.get("Artist") or "",
                "genre": t.get("Genre") or "",
                "bpm": round(bpm, 1) if bpm else None,
                "key_musical": key_musical or None,
                "key_camelot": KEY_TO_CAMELOT.get(key_musical, "") or None,
                "duration_seconds": round(duration, 1) if duration else None,
                "mix_in_seconds": round(mix_in, 1) if mix_in is not None else None,
                "mix_out_seconds": round(mix_out, 1) if mix_out is not None else None,
                "timeline": _build_timeline(cues, duration),
                "cues": cues,
                "has_loop_cue": any(c["is_loop"] for c in cues),
                "beatgrid_anchor_seconds": anchor,
                "beatgrid_bar_seconds": bar_seconds,
            })
            trackid_to_path[t.get("TrackID")] = path

    playlists = _parse_playlists(root)
    return {"tracks": tracks, "playlists": playlists,
            "trackid_to_path": trackid_to_path}


def _parse_playlists(root) -> list[dict]:
    """Walk the PLAYLISTS NODE tree → flat list of {name, path, is_folder,
    track_rb_ids}. Type 0 = folder, Type 1 = playlist."""
    out = []
    pl_root = root.find("PLAYLISTS")
    if pl_root is None:
        return out

    def walk(node, prefix):
        name = node.get("Name", "")
        is_folder = node.get("Type") == "0"
        path = f"{prefix}/{name}" if prefix else name
        # Skip the synthetic ROOT node in the path.
        if name and name != "ROOT":
            track_ids = [c.get("Key") for c in node.findall("TRACK") if c.get("Key")]
            out.append({"name": name, "path": path, "is_folder": is_folder,
                        "track_rb_ids": track_ids})
        child_prefix = path if (name and name != "ROOT") else prefix
        for child in node.findall("NODE"):
            walk(child, child_prefix)

    top = pl_root.find("NODE")
    walk(top if top is not None else pl_root, "")
    return out


def import_collection(xml_path: str, verbose: bool = True) -> dict:
    """Parse + write a Rekordbox collection into the DB. Returns a summary."""
    parsed = parse_collection(xml_path)
    n_tracks = n_cues = 0
    for tr in parsed["tracks"]:
        import json as _json
        upsert_track(
            path=tr["path"],
            title=tr["title"],
            artist=tr["artist"],
            genre=tr["genre"],
            bpm=tr["bpm"],
            key_musical=tr["key_musical"],
            key_camelot=tr["key_camelot"],
            duration_seconds=tr["duration_seconds"],
            mix_in_seconds=tr["mix_in_seconds"],
            mix_out_seconds=tr["mix_out_seconds"],
            timeline=_json.dumps(tr["timeline"]) if tr["timeline"] else None,
            cue_points=_json.dumps(tr["cues"]) if tr["cues"] else None,
            has_loop_cue=1 if tr["has_loop_cue"] else 0,
            beatgrid_anchor_seconds=tr["beatgrid_anchor_seconds"],
            beatgrid_bar_seconds=tr["beatgrid_bar_seconds"],
            analysis_source=SOURCE,
            analyzed_at=time.time(),
        )
        tid = get_track_id_by_path(tr["path"])
        if tid and tr["cues"]:
            replace_track_cues(tid, tr["cues"], SOURCE)
            n_cues += len(tr["cues"])
        n_tracks += 1

    # Playlists (after tracks so memberships resolve).
    n_pl = 0
    for pl in parsed["playlists"]:
        pid = upsert_import_playlist(pl["name"], pl["path"], pl["is_folder"], SOURCE)
        for pos, rb_id in enumerate(pl["track_rb_ids"]):
            fs_path = parsed["trackid_to_path"].get(rb_id)
            if not fs_path:
                continue
            tid = get_track_id_by_path(fs_path)
            if tid:
                add_track_to_playlist(pid, tid, pos)
        n_pl += 1

    summary = {"tracks": n_tracks, "cues": n_cues, "playlists": n_pl}
    if verbose:
        print(f"Rekordbox import: {n_tracks} tracks, {n_cues} cues, "
              f"{n_pl} playlists/folders — zero librosa", flush=True)
    return summary


# ── tiny coercion helpers ───────────────────────────────────────────
def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 -m ops.import_rekordbox <collection.xml>")
        sys.exit(1)
    import_collection(sys.argv[1])
