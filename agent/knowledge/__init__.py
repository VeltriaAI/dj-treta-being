"""Knowledge package — typed contracts for the electronic music dataset.

v8 Phase 3.5: scaffolding only. config.knowledge.enabled defaults False.
All queries return empty results + update KnowledgeHealth so callers
degrade gracefully (no silent empty strings, no hardcoded genre dicts).

v9 will:
  - Flip enabled=true
  - Populate queries.* against the 18M-track parquet dataset
  - Enable planner KB integration (Phase 3.7 deferred from v8)

Public API:
    from agent.knowledge import (
        CanonicalRef, KnowledgeTrack, GenreInfo, GapReport,
        MergedCandidate, KnowledgeHealth,
        KnowledgeClient,
        discover_candidates, similar_to, genre_context, gap_analysis,
        merge_candidate, local_row_to_canonical,
    )
"""

from .models import (
    CanonicalRef,
    GapReport,
    GenreInfo,
    KnowledgeHealth,
    KnowledgeTrack,
    MergedCandidate,
)
from .client import KnowledgeClient
from .queries import (
    discover_candidates,
    similar_to,
    genre_context,
    gap_analysis,
)
from .merge import merge_candidate, local_row_to_canonical

__all__ = [
    "CanonicalRef",
    "GapReport",
    "GenreInfo",
    "KnowledgeHealth",
    "KnowledgeTrack",
    "MergedCandidate",
    "KnowledgeClient",
    "discover_candidates",
    "similar_to",
    "genre_context",
    "gap_analysis",
    "merge_candidate",
    "local_row_to_canonical",
]
