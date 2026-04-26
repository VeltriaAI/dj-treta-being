"""KnowledgeClient — singleton backed by polars (metadata) + LanceDB (vectors).

v9 implementation. The 354-MB dj_treta_library v3 parquet lives at
`~/Music/DJTreta/knowledge/dj_treta_library.parquet` (local cache) and the
text-embedding-005 vector index lives at
`~/Music/DJTreta/knowledge/lancedb/tracks.lance`.

Design:
  - Polars LazyFrame for metadata scans (~50ms filtered query against 3.5M rows).
  - LanceDB for ANN similarity (~10ms k=20 query).
  - Either can be available independently. Client reports degraded state per
    backend on health — planner falls back to metadata-only when vectors
    aren't ready yet.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from .models import KnowledgeHealth

log = logging.getLogger("dj-treta")

_DEFAULT_DIR = Path.home() / "Music" / "DJTreta" / "knowledge"
_PARQUET_NAME = "dj_treta_library.parquet"
_LANCEDB_DIR = "lancedb"
_LANCEDB_TABLE = "tracks"


class KnowledgeClient:
    """Thread-safe singleton. Holds polars LazyFrame + LanceDB table handles."""

    _instance: Optional["KnowledgeClient"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._lf = None                    # polars LazyFrame for metadata
        self._vec_tbl = None               # LanceDB Table for vectors
        self._data_dir: Optional[Path] = None
        self._metadata_failed = False
        self._vectors_failed = False
        self.health = KnowledgeHealth.offline("not yet loaded")

    @classmethod
    def instance(cls) -> "KnowledgeClient":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None

    # ── Availability ──────────────────────────────────────────────────

    def is_available(self) -> bool:
        return self._lf is not None

    def has_vectors(self) -> bool:
        """True only when the vector table exists AND has rows.

        An empty table (handle valid, count=0) means embeddings are still
        being ingested — similarity queries would silently return []. Caller
        should treat this as not-ready and degrade cleanly.
        """
        if self._vec_tbl is None:
            return False
        try:
            return self._vec_tbl.count_rows() > 0
        except Exception:
            return False

    def ensure_loaded(self, enabled: bool, data_dir: Optional[str] = None) -> bool:
        """Lazy-load the polars metadata + LanceDB vectors.

        Returns True if metadata is available (vectors are optional — queries
        that don't need them still succeed). False only when disabled or
        metadata parquet is missing/corrupt.
        """
        if not enabled:
            if self.health.available or self.health.last_error != "disabled":
                self.health = KnowledgeHealth.offline("disabled")
            return False

        if self._lf is not None:
            return True

        if self._metadata_failed:
            return False

        self._data_dir = Path(data_dir).expanduser() if data_dir else _DEFAULT_DIR

        parquet_path = self._data_dir / _PARQUET_NAME
        if not parquet_path.exists():
            self._metadata_failed = True
            self.health = KnowledgeHealth(
                available=False,
                last_error=f"parquet not found at {parquet_path}",
                checked_at=time.time(),
            )
            log.warning(
                "Knowledge parquet missing — run scripts/knowledge/01_download_parquet.py"
            )
            return False

        try:
            import polars as pl
            self._lf = pl.scan_parquet(parquet_path)
            row_count = self._lf.select(pl.len()).collect().item()
            log.info(
                f"Knowledge parquet loaded: {row_count:,} rows from {parquet_path.name}"
            )
        except Exception as exc:
            self._metadata_failed = True
            self.health = KnowledgeHealth(
                available=False,
                last_error=f"parquet scan failed: {type(exc).__name__}: {exc}",
                checked_at=time.time(),
            )
            log.warning(f"Knowledge parquet scan failed: {exc}")
            return False

        # Vectors are optional — try to load, but metadata-only is fine.
        self._try_load_vectors()

        self.health = KnowledgeHealth(
            available=True,
            last_error="" if self._vec_tbl else "vectors not yet built",
            checked_at=time.time(),
        )
        return True

    def _try_load_vectors(self) -> None:
        if self._vectors_failed or self._vec_tbl is not None:
            return
        lance_dir = self._data_dir / _LANCEDB_DIR
        try:
            import lancedb
            need_build = (
                not lance_dir.exists()
                or _LANCEDB_TABLE not in lancedb.connect(str(lance_dir)).table_names()
            )
        except Exception as exc:
            self._vectors_failed = True
            log.warning(
                f"Knowledge LanceDB probe failed — similarity disabled: {exc}"
            )
            return

        if need_build:
            built = self._try_build_vectors_from_parquet()
            if not built:
                log.info(
                    f"Knowledge vectors not built yet at {lance_dir} — "
                    "metadata-only queries will work"
                )
                return

        try:
            import lancedb
            db = lancedb.connect(str(lance_dir))
            names = db.table_names()
            if _LANCEDB_TABLE not in names:
                log.info(
                    f"Knowledge LanceDB found but table '{_LANCEDB_TABLE}' missing; "
                    f"available tables: {names}"
                )
                return
            self._vec_tbl = db.open_table(_LANCEDB_TABLE)
            n = self._vec_tbl.count_rows()
            log.info(f"Knowledge vectors loaded: {n:,} rows in LanceDB")
        except Exception as exc:
            self._vectors_failed = True
            log.warning(
                f"Knowledge LanceDB load failed — similarity disabled: {exc}"
            )

    def _try_build_vectors_from_parquet(self) -> bool:
        """If the parquet has a `vector` column and LanceDB has no `tracks`
        table, build it once.

        Returns True if a fresh table was created. Returns False if no vector
        column is present (legacy v3 parquet) or build failed (logged).
        """
        try:
            import polars as pl
        except Exception:
            return False
        parquet_path = self._data_dir / _PARQUET_NAME
        try:
            schema = pl.scan_parquet(parquet_path).collect_schema()
        except Exception as exc:
            log.warning(f"Knowledge parquet schema probe failed: {exc}")
            return False
        if "vector" not in schema.names():
            return False

        log.info(
            "Knowledge parquet has 'vector' column — "
            "building LanceDB index (one-time, ~5 min)..."
        )
        try:
            import lancedb
            lance_dir = self._data_dir / _LANCEDB_DIR
            lance_dir.mkdir(parents=True, exist_ok=True)
            db = lancedb.connect(str(lance_dir))
            df = (
                pl.scan_parquet(parquet_path)
                .select(["mbid", "vector"])
                .collect()
            )
            tbl = db.create_table(
                _LANCEDB_TABLE, data=df.to_arrow(), mode="create"
            )
            n = tbl.count_rows()
            log.info(f"Knowledge LanceDB created: {n:,} rows; building IVF-PQ index...")
            tbl.create_index(
                metric="cosine",
                num_partitions=512,
                num_sub_vectors=48,
                vector_column_name="vector",
            )
            log.info("Knowledge LanceDB IVF-PQ index built")
            return True
        except Exception as exc:
            self._vectors_failed = True
            log.warning(
                f"Knowledge LanceDB build from parquet failed: {exc}"
            )
            return False

    # ── Accessors (for queries.py) ────────────────────────────────────

    @property
    def lf(self):
        return self._lf

    @property
    def vec_tbl(self):
        return self._vec_tbl

    # ── Health recording ──────────────────────────────────────────────

    def record_query(self, latency_ms: int) -> None:
        self.health = KnowledgeHealth(
            available=True,
            last_error="",
            last_query_ms=latency_ms,
            checked_at=time.time(),
        )

    def record_degraded(self, reason: str) -> None:
        self.health = KnowledgeHealth(
            available=False,
            last_error=reason,
            checked_at=time.time(),
        )
        log.warning(f"Knowledge query degraded: {reason}")
