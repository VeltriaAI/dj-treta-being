"""Typed query functions against the knowledge backend.

v8 Phase 3.5: all queries are typed stubs. When backend is unavailable
(today's v8 default), each query returns an empty list / None and updates
KnowledgeHealth so callers can see the degradation explicitly — NO silent
return "".

v9 will implement these against the 18M-track parquet dataset. The
function signatures are stable — Phase 6.5 (producer brief enrichment)
and v9 planner KB integration both code against this contract.

Caller pattern (keeps downstream code clean):

    from .knowledge import queries as kb
    candidates = kb.discover_candidates(mood_profile, bpm_range=(115, 125))
    # candidates is list[KnowledgeTrack]; may be empty; never raises
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .client import KnowledgeClient
from .models import (
    CanonicalRef,
    GapReport,
    GenreInfo,
    KnowledgeTrack,
)

log = logging.getLogger("dj-treta")


def _client() -> KnowledgeClient:
    return KnowledgeClient.instance()


def _enabled() -> bool:
    """Resolve the enabled flag from config without a hard dep on runtime."""
    try:
        from ..config import load_config
        cfg = load_config()
        k = getattr(cfg, "knowledge", None)
        return bool(getattr(k, "enabled", False))
    except Exception:
        return False


def _ensure() -> bool:
    """Make sure the backend is loaded. Returns False if unavailable."""
    client = _client()
    enabled = _enabled()
    try:
        from ..config import load_config
        data_dir = getattr(load_config().knowledge, "data_dir", None)
    except Exception:
        data_dir = None
    return client.ensure_loaded(enabled=enabled, data_dir=data_dir)


# ── Queries ───────────────────────────────────────────────────────────

def discover_candidates(
    mood_profile: Optional[dict] = None,
    bpm_range: Optional[tuple] = None,
    exclude_canonical: Optional[list] = None,
    limit: int = 20,
) -> list:
    """Return KnowledgeTrack[] matching the mood.

    v8: returns [] + records health; v9 implements the parquet scan.
    """
    if not _ensure():
        _client().record_degraded("backend unavailable")
        return []
    # v9 implementation hook.
    _client().record_degraded("discover_candidates not implemented until v9")
    return []


def similar_to(
    seed: CanonicalRef,
    limit: int = 20,
    exclude_canonical: Optional[list] = None,
) -> list:
    """Return KnowledgeTrack[] similar to a seed canonical ref (RAG)."""
    if not _ensure():
        _client().record_degraded("backend unavailable")
        return []
    _client().record_degraded("similar_to not implemented until v9")
    return []


def genre_context(genre_slug: str) -> Optional[GenreInfo]:
    """Return GenreInfo for a genre slug, or None if unknown/disabled."""
    if not _ensure():
        _client().record_degraded("backend unavailable")
        return None
    _client().record_degraded("genre_context not implemented until v9")
    return None


def gap_analysis(
    mood_profile: dict,
    local_canonical_refs: list,
) -> GapReport:
    """Return a GapReport describing library coverage for the mood.

    Library manager (Phase 5) uses this to decide what to download.
    Always returns a GapReport (never None) so callers can see local_count
    + saturation even when the dataset backend is offline.
    """
    local_count = len(local_canonical_refs or [])
    slug = (mood_profile or {}).get("canonical_slug", "")

    if not _ensure():
        _client().record_degraded("backend unavailable")
        # Degenerate report: we only know what's in the local library.
        # saturation=1.0 signals "nothing the library manager can do
        # without the dataset".
        return GapReport(
            mood_slug=slug,
            local_count=local_count,
            dataset_count=0,
            saturation=1.0,
        )

    _client().record_degraded("gap_analysis not implemented until v9")
    return GapReport(
        mood_slug=slug,
        local_count=local_count,
        dataset_count=0,
        saturation=1.0,
    )
