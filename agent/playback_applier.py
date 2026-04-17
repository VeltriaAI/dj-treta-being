"""Thin Mixxx load helper — no decisions, just applies a pick to the idle deck.

Phase 3 of v8 splits track SELECTION (planner writes session.playlist) from
track LOADING (this module posts to Mixxx /api/load). Phase 4 moves the
selection step from heartbeat into the DJ agent itself; this module stays as
the last-mile executor either way.

All functions are Mixxx-only — they do not read mood, playlist, or SQL.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger("dj-treta")


def load_on_deck(mixxx_url: str, deck: int, track_path: str, timeout: float = 5.0) -> bool:
    """Post a single track to a Mixxx deck. Returns True on success.

    Does not wait for the track to analyze / cue up — Mixxx handles that
    asynchronously. Callers can poll /api/deck/N/track_info to confirm.
    """
    try:
        resp = httpx.post(
            f"{mixxx_url}/api/load",
            json={"deck": deck, "track": track_path},
            timeout=timeout,
        )
        result = resp.json() if resp.status_code == 200 else {}
        if result.get("ok"):
            log.info(f"Loaded deck {deck}: {Path(track_path).stem[:60]}")
            return True
        log.warning(f"Load deck {deck} returned not-ok: {result}")
        return False
    except Exception as exc:
        log.warning(f"Load deck {deck} failed: {exc}")
        return False


def get_deck_paths(mixxx_url: str) -> dict:
    """Return {deck_number: file_path} for both decks. Missing → empty string."""
    out = {1: "", 2: ""}
    for dk in (1, 2):
        try:
            resp = httpx.get(f"{mixxx_url}/api/deck/{dk}/track_info", timeout=2)
            info = resp.json() if resp.status_code == 200 else {}
            out[dk] = info.get("file_path", "") or ""
        except Exception:
            pass
    return out


def refresh_duration(mixxx_url: str, deck: int, track_path: str) -> None:
    """After a load, read Mixxx's duration back and upsert to DB.

    Librosa analysis sometimes misses duration on unusual files; Mixxx always
    knows once it has parsed the file. Call this shortly after `load_on_deck`.
    """
    try:
        time.sleep(1.0)  # give Mixxx a moment to parse
        from .main import _get_status
        st = _get_status(mixxx_url)
        if not st:
            return
        duration = float(st.get(f"deck{deck}", {}).get("duration", 0) or 0)
        if duration > 0:
            from .db import upsert_track
            upsert_track(path=track_path, duration_seconds=duration)
    except Exception as exc:
        log.debug(f"refresh_duration skipped: {exc}")
