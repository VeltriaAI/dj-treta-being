"""KnowledgeClient — singleton wrapping the 18M-track dataset.

v8 Phase 3.5 leaves this as scaffolding. When `config.knowledge.enabled`
is False (v8 default), the client stays offline and every query routes to
the degraded path (returns empty results + updates health with an explicit
reason — NO silent empty strings).

v9 will:
  - Flip config.knowledge.enabled=true once the dataset is production-ready
  - Wire _load_backend() to construct the MusicKnowledge backing store
  - Implement the query functions in queries.py against self._backend

For now, every caller gets:
  - available=False if disabled (zero LLM/IO cost)
  - A KnowledgeHealth status update it can show the user
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .models import KnowledgeHealth

log = logging.getLogger("dj-treta")


class KnowledgeClient:
    """Thread-safe singleton. Holds dataset handle + health."""

    _instance: Optional["KnowledgeClient"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._backend = None                 # populated in v9
        self._load_failed = False
        self.health = KnowledgeHealth.offline("not yet loaded")

    @classmethod
    def instance(cls) -> "KnowledgeClient":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """For tests."""
        with cls._lock:
            cls._instance = None

    def is_available(self) -> bool:
        return self._backend is not None

    def ensure_loaded(self, enabled: bool, data_dir: Optional[str] = None) -> bool:
        """Lazy-load the backend. Returns True if ready; False otherwise.

        In v8 this is a no-op when enabled=False. When v9 flips the flag
        and the parquet dataset is present, this will construct and cache
        the MusicKnowledge backend.
        """
        if not enabled:
            if self.health.available or self.health.last_error != "disabled":
                self.health = KnowledgeHealth.offline("disabled")
            return False

        if self._backend is not None:
            return True

        if self._load_failed:
            return False

        # v9 implementation hook. When enabled, try to construct backend.
        # Keeping the try/except so the v9 change is one function:
        try:
            # NOTE(v9): replace with real MusicKnowledge construction, e.g.:
            #   from integration.knowledge_query import MusicKnowledge
            #   self._backend = MusicKnowledge(data_dir)
            #   _ = len(self._backend.tracks)  # warm up
            raise NotImplementedError(
                "Knowledge backend loader not implemented until v9 — set knowledge.enabled=false"
            )
        except Exception as exc:
            self._load_failed = True
            self.health = KnowledgeHealth(
                available=False,
                last_error=f"{type(exc).__name__}: {exc}",
                checked_at=time.time(),
            )
            log.warning(f"Knowledge backend load failed — queries will degrade: {exc}")
            return False

    def record_query(self, latency_ms: int) -> None:
        """Called by queries.* to update health metrics."""
        self.health = KnowledgeHealth(
            available=True,
            last_error="",
            last_query_ms=latency_ms,
            checked_at=time.time(),
        )

    def record_degraded(self, reason: str) -> None:
        """Called when a query falls back due to backend issue."""
        self.health = KnowledgeHealth(
            available=False,
            last_error=reason,
            checked_at=time.time(),
        )
        log.warning(f"Knowledge query degraded: {reason}")
