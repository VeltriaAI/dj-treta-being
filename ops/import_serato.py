"""Import a Serato library (crates + per-file cue/beatgrid metadata).

E3 (Library Ingestion & Analysis Coverage), Serato variant. Best-effort.

FORMAT ASSUMPTIONS (documented because Serato's format is undocumented and
reverse-engineered by the community — `serato-tags`, `pyserato`, Holzhaus's
notes):

1. **Crates** live in `_Serato_/Subcrates/*.crate`. Each is a binary blob of
   tagged chunks. The `ptrk` tag holds a track file path (UTF-16-BE). We parse
   the crate filename (with `%%` → folder separators) as the crate's logical
   path and extract the `ptrk` track paths for membership. This is stable.

2. **Cues / beatgrid / loops** live in the audio FILE itself, in vendor tags:
   - MP3: ID3 GEOB frames `Serato Markers2` (cues+loops, base64) and
     `Serato BeatGrid`.
   - MP4/FLAC: equivalent atoms/Vorbis comments.
   Decoding `Serato Markers2` fully is involved; we use `mutagen` (already a
   dependency) to read the GEOB frame and decode the well-understood CUE / LOOP
   / COLOR entries. If `mutagen` or the tag is absent, we still import the track
   with crate membership and fall back to librosa via ops/backfill_analysis.py.

3. **Colors**: Serato cue colors are stored as 3 RGB bytes per CUE entry.
   We surface them as #RRGGBB.

`Serato Markers2` entry layout (community-reverse-engineered):
   The GEOB payload (after a 2-byte version header) is base64. Decoded, it is a
   sequence of NUL-terminated `name` strings each followed by a 4-byte
   big-endian length and that many bytes of entry data. Entry names of
   interest: 'CUE' and 'LOOP'. CUE data: 1 byte field, 1 byte index, 4-byte BE
   position-ms, 1 byte field, 3 bytes RGB color, 2 bytes field, NUL-terminated
   name. LOOP data: index, 4-byte BE start-ms, 4-byte BE end-ms, ... , name.

If any of the above doesn't decode cleanly, we degrade gracefully (skip that
track's cues, keep the track + crate membership). NEVER raises on a bad file.

Run:
    python3 -m ops.import_serato ~/Music/_Serato_
    # or via:
    python3 -m ops.ingest_library ~/Music/_Serato_
"""
import base64
import struct
import sys
import time
from pathlib import Path

from agent.audio_analysis import map_section_to_block, block_color
from agent.db import (
    upsert_track,
    replace_track_cues,
    get_track_id_by_path,
    upsert_import_playlist,
    add_track_to_playlist,
)

SOURCE = "serato"


def _decode_crate(crate_file: Path) -> list[str]:
    """Extract track file paths (ptrk tags) from a .crate binary blob.

    Crate format: repeating [4-byte tag][4-byte BE length][payload]. The 'otrk'
    tag wraps a track; inside it 'ptrk' holds the path as UTF-16-BE.
    """
    paths = []
    try:
        data = crate_file.read_bytes()
    except OSError:
        return paths
    i = 0
    n = len(data)
    while i + 8 <= n:
        tag = data[i:i + 4]
        (length,) = struct.unpack(">I", data[i + 4:i + 8])
        payload = data[i + 8:i + 8 + length]
        if tag == b"ptrk":
            try:
                p = payload.decode("utf-16-be").rstrip("\x00")
                if p:
                    # Serato stores paths relative to the volume root; make
                    # absolute by prefixing "/" if it isn't already.
                    paths.append(p if p.startswith("/") else "/" + p)
            except UnicodeDecodeError:
                pass
        i += 8 + length
    return paths


def _crate_logical_path(crate_file: Path) -> str:
    """`Sets%%Warmup.crate` → `Sets/Warmup`."""
    return crate_file.stem.replace("%%", "/")


def _read_serato_markers(audio_path: str, bpm: float, duration: float) -> list[dict]:
    """Best-effort decode of the `Serato Markers2` GEOB frame via mutagen.

    Returns our cue dicts. On ANY failure returns [] (caller falls back to
    librosa). Never raises.
    """
    try:
        from mutagen.id3 import ID3
    except Exception:
        return []
    try:
        tags = ID3(audio_path)
    except Exception:
        return []

    geob = None
    for key in tags.keys():
        if key.startswith("GEOB") and "Serato Markers2" in key:
            geob = tags[key]
            break
    if geob is None:
        return []

    try:
        raw = bytes(geob.data)
        # Strip the 2-byte version header, then base64-decode the rest.
        b64 = raw[2:].replace(b"\n", b"")
        decoded = base64.b64decode(b64 + b"=" * (-len(b64) % 4))
    except Exception:
        return []

    return _parse_markers2(decoded, bpm, duration)


def _parse_markers2(decoded: bytes, bpm: float, duration: float) -> list[dict]:
    """Parse decoded Serato Markers2 entries. Defensive: bail on malformed
    data rather than raising."""
    cues = []
    i = 1  # skip leading version byte
    n = len(decoded)
    beat_seconds = (60.0 / bpm) if bpm else 0.0
    raw_cues = []
    try:
        while i < n:
            end_name = decoded.index(b"\x00", i)
            name = decoded[i:end_name].decode("latin-1")
            i = end_name + 1
            if i + 4 > n:
                break
            (length,) = struct.unpack(">I", decoded[i:i + 4])
            i += 4
            entry = decoded[i:i + length]
            i += length
            if name == "CUE" and len(entry) >= 13:
                idx = entry[1]
                (pos_ms,) = struct.unpack(">I", entry[2:6])
                color = f"#{entry[7]:02X}{entry[8]:02X}{entry[9]:02X}"
                raw_cues.append({
                    "cue_index": idx,
                    "start_seconds": pos_ms / 1000.0,
                    "kind": "cue", "is_loop": False,
                    "loop_length_beats": None, "color": color, "name": "",
                })
            elif name == "LOOP" and len(entry) >= 13:
                idx = entry[1]
                (start_ms,) = struct.unpack(">I", entry[2:6])
                (end_ms,) = struct.unpack(">I", entry[6:10])
                loop_len = round((end_ms - start_ms) / 1000.0 / beat_seconds, 2) \
                    if beat_seconds else None
                raw_cues.append({
                    "cue_index": idx,
                    "start_seconds": start_ms / 1000.0,
                    "kind": "loop", "is_loop": True,
                    "loop_length_beats": loop_len, "color": None, "name": "",
                })
    except Exception:
        # Partial parse is fine; return whatever we got.
        pass

    return _assign_sections(raw_cues, duration)


def _assign_sections(cues: list[dict], duration: float) -> list[dict]:
    """Mirror the Rekordbox section mapping: first→mix_in, last/outro→mix_out,
    loop→LOOP, rest→timeline."""
    if not cues:
        return cues
    ordered = sorted(cues, key=lambda c: c["start_seconds"])
    n = len(ordered)
    for idx, c in enumerate(ordered):
        if c["is_loop"]:
            c["section"], block = "mix_loop", "LOOP"
        elif idx == 0:
            c["section"], block = "mix_in", "START"
        elif idx == n - 1 or (duration and c["start_seconds"] > duration * 0.85):
            c["section"], block = "mix_out", "BREAK"
        else:
            c["section"], block = "timeline", "DROP"
        if not c.get("color"):
            c["color"] = block_color(block)
    return ordered


def _build_timeline(cues: list[dict], duration: float) -> list[dict]:
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
            "block": block, "color": block_color(block), "energy": 5,
        })
    return timeline


def import_serato_library(serato_dir: str, verbose: bool = True) -> dict:
    """Import crates + per-file cues from a `_Serato_` directory."""
    import json as _json
    root = Path(serato_dir).expanduser()
    subcrates = root / "Subcrates"
    if not subcrates.exists():
        # Allow passing the parent (~/Music) — look for _Serato_/Subcrates.
        alt = root / "_Serato_" / "Subcrates"
        subcrates = alt if alt.exists() else subcrates

    n_tracks = n_cues = n_pl = 0
    seen_paths: set[str] = set()

    if subcrates.exists():
        for crate_file in sorted(subcrates.glob("*.crate")):
            logical = _crate_logical_path(crate_file)
            pid = upsert_import_playlist(crate_file.stem.replace("%%", " / "),
                                         logical, False, SOURCE)
            n_pl += 1
            for pos, track_path in enumerate(_decode_crate(crate_file)):
                if track_path not in seen_paths:
                    _import_track_file(track_path)
                    seen_paths.add(track_path)
                    n_tracks += 1
                tid = get_track_id_by_path(track_path)
                if tid:
                    add_track_to_playlist(pid, tid, pos)

    # Recount cues from what we wrote (the per-file import did the work).
    summary = {"tracks": n_tracks, "playlists": n_pl}
    if verbose:
        print(f"Serato import: {n_tracks} tracks, {n_pl} crates "
              f"(cues best-effort from file tags)", flush=True)
    return summary


def _import_track_file(track_path: str) -> None:
    """Upsert a single track from its Serato file tags. Cues best-effort."""
    import json as _json
    p = Path(track_path)
    title = p.stem
    # Pull bpm/duration from mutagen if available (else None — backfill later).
    bpm = duration = None
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(track_path)
        if mf is not None and mf.info is not None:
            duration = round(getattr(mf.info, "length", 0) or 0, 1) or None
    except Exception:
        pass

    cues = _read_serato_markers(track_path, bpm or 0.0, duration or 0.0)
    timeline = _build_timeline(cues, duration or 0.0)
    upsert_track(
        path=track_path,
        title=title,
        duration_seconds=duration,
        cue_points=_json.dumps(cues) if cues else None,
        timeline=_json.dumps(timeline) if timeline else None,
        has_loop_cue=1 if any(c["is_loop"] for c in cues) else 0,
        analysis_source=SOURCE if cues else None,
        # analyzed_at left NULL when no cues → ops/backfill_analysis.py picks
        # it up for a librosa pass. With cues, mark analyzed.
        analyzed_at=time.time() if cues else None,
    )
    tid = get_track_id_by_path(track_path)
    if tid and cues:
        replace_track_cues(tid, cues, SOURCE)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 -m ops.import_serato <_Serato_ dir | Music dir>")
        sys.exit(1)
    import_serato_library(sys.argv[1])
