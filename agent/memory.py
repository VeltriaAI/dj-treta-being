"""Semantic memory for Treta (the root Being).

Four LanceDB tables, all in a Treta-private namespace separate from
music knowledge. Embeddings via sentence-transformers (MiniLM, 384-dim).

Tables:
  listener_interactions  — {ts, message_text, treta_response, vector, set_id, mood_at_time}
  set_archives           — {set_id, started_at, ended_at, mood_arc, track_count,
                            listener_engagement_score, summary_text, vector}
  journal_entries        — {date, body, vector, themes (json string)}
  treta_thoughts         — {ts, agent_id, decision_text, vector, context (json string)}

Storage path: ~/.beings/dj-treta/memory/lancedb/

Self-contained: no imports from agent.* internals. Best-effort: every
public function wraps in try/except. If lancedb / sentence_transformers
aren't installed, the module loads with `_DB`/`_MODEL` = None and all
ops return safely (False / []) with a one-time warning at module load.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("dj-treta")

# ── Optional heavy deps ──────────────────────────────────────────────
# Wrap imports so a daemon without these libs still boots.

try:
    import lancedb  # type: ignore
    _LANCEDB_AVAILABLE = True
except Exception as _exc:  # pragma: no cover
    lancedb = None  # type: ignore
    _LANCEDB_AVAILABLE = False
    log.warning(f"memory: lancedb unavailable — semantic memory disabled ({_exc})")

try:
    import pyarrow as pa  # type: ignore
    _PYARROW_AVAILABLE = True
except Exception as _exc:  # pragma: no cover
    pa = None  # type: ignore
    _PYARROW_AVAILABLE = False
    log.warning(f"memory: pyarrow unavailable — semantic memory disabled ({_exc})")

try:
    from sentence_transformers import SentenceTransformer  # type: ignore
    _ST_AVAILABLE = True
except Exception as _exc:  # pragma: no cover
    SentenceTransformer = None  # type: ignore
    _ST_AVAILABLE = False
    log.warning(f"memory: sentence_transformers unavailable — semantic memory disabled ({_exc})")


# ── Constants ────────────────────────────────────────────────────────

_MEMORY_DIR = Path.home() / ".beings" / "dj-treta" / "memory" / "lancedb"
_MODEL_NAME = "all-MiniLM-L6-v2"
_VECTOR_DIM = 384

_TABLE_LISTENER = "listener_interactions"
_TABLE_SETS = "set_archives"
_TABLE_JOURNAL = "journal_entries"
_TABLE_THOUGHTS = "treta_thoughts"

# ── Lazy globals ─────────────────────────────────────────────────────

_DB = None
_MODEL = None
_TABLES: dict = {}
_INIT_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()


def _libs_ok() -> bool:
    return _LANCEDB_AVAILABLE and _PYARROW_AVAILABLE and _ST_AVAILABLE


# ── Schemas ──────────────────────────────────────────────────────────

def _schema_for(name: str):
    """Return a pyarrow Schema for the named table.

    The `vector` column is a fixed-size list of float32, length 384.
    """
    if not _PYARROW_AVAILABLE:
        return None
    vec_type = pa.list_(pa.float32(), _VECTOR_DIM)
    if name == _TABLE_LISTENER:
        return pa.schema([
            pa.field("ts", pa.float64()),
            pa.field("message_text", pa.string()),
            pa.field("treta_response", pa.string()),
            pa.field("set_id", pa.string()),
            pa.field("mood_at_time", pa.string()),
            pa.field("vector", vec_type),
        ])
    if name == _TABLE_SETS:
        return pa.schema([
            pa.field("set_id", pa.string()),
            pa.field("started_at", pa.float64()),
            pa.field("ended_at", pa.float64()),
            pa.field("mood_arc", pa.string()),
            pa.field("track_count", pa.int64()),
            pa.field("listener_engagement_score", pa.float64()),
            pa.field("summary_text", pa.string()),
            pa.field("vector", vec_type),
        ])
    if name == _TABLE_JOURNAL:
        return pa.schema([
            pa.field("date", pa.string()),
            pa.field("body", pa.string()),
            pa.field("themes", pa.string()),  # JSON-encoded list[str]
            pa.field("vector", vec_type),
        ])
    if name == _TABLE_THOUGHTS:
        return pa.schema([
            pa.field("ts", pa.float64()),
            pa.field("agent_id", pa.string()),
            pa.field("decision_text", pa.string()),
            pa.field("context", pa.string()),  # JSON-encoded dict
            pa.field("vector", vec_type),
        ])
    return None


# ── Lazy init ────────────────────────────────────────────────────────

def _get_db():
    """Open the Treta-private LanceDB connection. Lazy-init."""
    global _DB
    if not _libs_ok():
        return None
    if _DB is not None:
        return _DB
    with _INIT_LOCK:
        if _DB is not None:
            return _DB
        try:
            _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            _DB = lancedb.connect(str(_MEMORY_DIR))
            log.info(f"memory: lancedb connected at {_MEMORY_DIR}")
        except Exception as exc:
            log.warning(f"memory: lancedb connect failed: {exc}")
            _DB = None
    return _DB


def _get_model():
    """Load sentence-transformers model. Lazy-init. Cached globally."""
    global _MODEL
    if not _ST_AVAILABLE:
        return None
    if _MODEL is not None:
        return _MODEL
    with _INIT_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            t0 = time.time()
            _MODEL = SentenceTransformer(_MODEL_NAME)
            log.info(f"memory: loaded {_MODEL_NAME} in {time.time() - t0:.1f}s")
        except Exception as exc:
            log.warning(f"memory: failed to load {_MODEL_NAME}: {exc}")
            _MODEL = None
    return _MODEL


def _get_table(name: str):
    """Open or create a named table. Lazy. Idempotent."""
    if not _libs_ok():
        return None
    if name in _TABLES and _TABLES[name] is not None:
        return _TABLES[name]
    db = _get_db()
    if db is None:
        return None
    with _INIT_LOCK:
        if name in _TABLES and _TABLES[name] is not None:
            return _TABLES[name]
        try:
            existing = set(db.table_names())
        except Exception as exc:
            log.warning(f"memory: list table_names failed: {exc}")
            return None
        try:
            if name in existing:
                tbl = db.open_table(name)
            else:
                schema = _schema_for(name)
                if schema is None:
                    log.warning(f"memory: no schema for table '{name}'")
                    return None
                # Create with empty arrow table matching the schema.
                empty = pa.Table.from_pylist([], schema=schema)
                tbl = db.create_table(name, data=empty)
                log.info(f"memory: created table '{name}'")
            _TABLES[name] = tbl
            return tbl
        except Exception as exc:
            log.warning(f"memory: open/create table '{name}' failed: {exc}")
            return None


def _embed(text: str) -> Optional[list]:
    """Embed a string. Returns list[float] of length 384, or None on failure."""
    if not text:
        text = ""
    model = _get_model()
    if model is None:
        return None
    try:
        vec = model.encode(text, convert_to_numpy=True, normalize_embeddings=False)
        if isinstance(vec, np.ndarray):
            arr = vec.astype(np.float32).tolist()
        else:
            arr = [float(x) for x in vec]
        if len(arr) != _VECTOR_DIM:
            log.warning(f"memory: embed dim mismatch ({len(arr)} vs {_VECTOR_DIM})")
            return None
        return arr
    except Exception as exc:
        log.warning(f"memory: embed failed: {exc}")
        return None


def _append(name: str, row: dict) -> bool:
    """Append a single row to the named table under the write lock."""
    tbl = _get_table(name)
    if tbl is None:
        return False
    try:
        with _WRITE_LOCK:
            tbl.add([row])
        return True
    except Exception as exc:
        log.warning(f"memory: append to '{name}' failed: {exc}")
        return False


def _results_to_dicts(result) -> list:
    """Coerce a lancedb search result into list[dict]."""
    try:
        if hasattr(result, "to_list"):
            return list(result.to_list())
    except Exception:
        pass
    try:
        if hasattr(result, "to_pandas"):
            return result.to_pandas().to_dict("records")
    except Exception:
        pass
    try:
        return list(result)
    except Exception:
        return []


def _strip_vector(d: dict) -> dict:
    """Drop the heavy vector column from a row before handing back to caller."""
    if not isinstance(d, dict):
        return d
    out = {k: v for k, v in d.items() if k != "vector"}
    return out


# ── Storage helpers ──────────────────────────────────────────────────

def store_interaction(message_text: str, treta_response: str = "",
                      set_id: str = "", mood: str = "") -> bool:
    """Append a listener interaction to memory. Best-effort."""
    try:
        if not _libs_ok():
            return False
        # Embed the user-side message; that's what we'll search by later.
        # If response is rich, blend it lightly into the embed source.
        embed_src = message_text
        if treta_response:
            embed_src = f"{message_text}\n\n[Treta]: {treta_response}"
        vec = _embed(embed_src)
        if vec is None:
            return False
        row = {
            "ts": float(time.time()),
            "message_text": str(message_text or ""),
            "treta_response": str(treta_response or ""),
            "set_id": str(set_id or ""),
            "mood_at_time": str(mood or ""),
            "vector": vec,
        }
        return _append(_TABLE_LISTENER, row)
    except Exception as exc:
        log.warning(f"memory: store_interaction failed: {exc}")
        return False


def store_set_archive(set_id: str, started_at: float, ended_at: float,
                      mood_arc: str, track_count: int,
                      engagement_score: float, summary_text: str) -> bool:
    """Append a completed set archive."""
    try:
        if not _libs_ok():
            return False
        embed_src = f"{mood_arc}\n\n{summary_text}".strip()
        vec = _embed(embed_src or set_id)
        if vec is None:
            return False
        row = {
            "set_id": str(set_id or ""),
            "started_at": float(started_at or 0.0),
            "ended_at": float(ended_at or 0.0),
            "mood_arc": str(mood_arc or ""),
            "track_count": int(track_count or 0),
            "listener_engagement_score": float(engagement_score or 0.0),
            "summary_text": str(summary_text or ""),
            "vector": vec,
        }
        return _append(_TABLE_SETS, row)
    except Exception as exc:
        log.warning(f"memory: store_set_archive failed: {exc}")
        return False


def store_journal_entry(date: str, body: str,
                        themes: Optional[list] = None) -> bool:
    """Append a daily journal entry. `date` is ISO YYYY-MM-DD."""
    try:
        if not _libs_ok():
            return False
        themes_list = list(themes) if themes else []
        try:
            themes_json = json.dumps(themes_list, ensure_ascii=False)
        except Exception:
            themes_json = "[]"
        vec = _embed(body or date)
        if vec is None:
            return False
        row = {
            "date": str(date or ""),
            "body": str(body or ""),
            "themes": themes_json,
            "vector": vec,
        }
        return _append(_TABLE_JOURNAL, row)
    except Exception as exc:
        log.warning(f"memory: store_journal_entry failed: {exc}")
        return False


def store_thought(ts: float, agent_id: str, decision_text: str,
                  context: Optional[dict] = None) -> bool:
    """Append a single Treta thought."""
    try:
        if not _libs_ok():
            return False
        try:
            ctx_json = json.dumps(context or {}, ensure_ascii=False, default=str)
        except Exception:
            ctx_json = "{}"
        vec = _embed(decision_text or agent_id)
        if vec is None:
            return False
        row = {
            "ts": float(ts or time.time()),
            "agent_id": str(agent_id or ""),
            "decision_text": str(decision_text or ""),
            "context": ctx_json,
            "vector": vec,
        }
        return _append(_TABLE_THOUGHTS, row)
    except Exception as exc:
        log.warning(f"memory: store_thought failed: {exc}")
        return False


# ── Recall tools ─────────────────────────────────────────────────────

def _vector_search(table_name: str, query_text: str, k: int,
                   where: str = "") -> list:
    """Run a vector search and return list[dict] (vector column stripped)."""
    if not _libs_ok():
        return []
    if k is None or k <= 0:
        k = 5
    tbl = _get_table(table_name)
    if tbl is None:
        return []
    qvec = _embed(query_text or "")
    if qvec is None:
        return []
    try:
        q = tbl.search(qvec).limit(int(k))
        if where:
            try:
                q = q.where(where)
            except Exception as exc:
                log.warning(f"memory: where clause '{where}' failed: {exc}")
        rows = _results_to_dicts(q)
        return [_strip_vector(r) for r in rows]
    except Exception as exc:
        log.warning(f"memory: search on '{table_name}' failed: {exc}")
        return []


def recall_similar_interaction(query_text: str, k: int = 5) -> list:
    """Up to k past interactions most similar to query_text.

    Returns list of dicts with: ts, message_text, treta_response,
    set_id, mood_at_time, _distance.
    """
    return _vector_search(_TABLE_LISTENER, query_text, k)


def recall_similar_set(query_text: str, k: int = 3) -> list:
    """Up to k past set archives matching query_text."""
    return _vector_search(_TABLE_SETS, query_text, k)


def recall_journal(query_text: str = "",
                   date_range: Optional[tuple] = None,
                   k: int = 5) -> list:
    """Up to k journal entries.

    - If query_text empty, return most recent k (by date desc).
    - If date_range = (start, end), filter by date column (ISO strings).
    """
    try:
        if not _libs_ok():
            return []
        tbl = _get_table(_TABLE_JOURNAL)
        if tbl is None:
            return []
        if k is None or k <= 0:
            k = 5

        where_parts = []
        if date_range and len(date_range) == 2:
            start, end = date_range
            if start:
                where_parts.append(f"date >= '{str(start)}'")
            if end:
                where_parts.append(f"date <= '{str(end)}'")
        where = " AND ".join(where_parts)

        if query_text:
            return _vector_search(_TABLE_JOURNAL, query_text, k, where=where)

        # No query → most recent. LanceDB doesn't have a portable
        # ORDER BY across versions; fetch via to_pandas and sort.
        try:
            q = tbl.search() if hasattr(tbl, "search") else tbl
        except Exception:
            q = tbl
        try:
            df = tbl.to_pandas()
        except Exception as exc:
            log.warning(f"memory: journal to_pandas failed: {exc}")
            return []
        try:
            if where_parts:
                if date_range[0]:
                    df = df[df["date"] >= str(date_range[0])]
                if date_range[1]:
                    df = df[df["date"] <= str(date_range[1])]
            df = df.sort_values("date", ascending=False).head(int(k))
            rows = df.to_dict("records")
            return [_strip_vector(r) for r in rows]
        except Exception as exc:
            log.warning(f"memory: journal recent fetch failed: {exc}")
            return []
    except Exception as exc:
        log.warning(f"memory: recall_journal failed: {exc}")
        return []


def recall_thoughts(query_text: str, k: int = 10,
                    agent_filter: str = "") -> list:
    """Up to k treta_thoughts matching query_text. Optional agent_filter."""
    where = ""
    if agent_filter:
        # Escape single quotes defensively.
        safe = str(agent_filter).replace("'", "''")
        where = f"agent_id = '{safe}'"
    return _vector_search(_TABLE_THOUGHTS, query_text, k, where=where)


# ── Maintenance ──────────────────────────────────────────────────────

def prune_old(days_listener_interactions: int = 90,
              days_treta_thoughts: int = 180) -> dict:
    """Delete entries older than the given cutoffs.

    Returns {table_name: rows_deleted_estimate}. set_archives and
    journal_entries are kept forever.
    """
    out = {_TABLE_LISTENER: 0, _TABLE_THOUGHTS: 0}
    if not _libs_ok():
        return out
    now = time.time()

    targets = [
        (_TABLE_LISTENER, max(1, int(days_listener_interactions))),
        (_TABLE_THOUGHTS, max(1, int(days_treta_thoughts))),
    ]
    for name, days in targets:
        cutoff = now - (days * 86400.0)
        tbl = _get_table(name)
        if tbl is None:
            continue
        try:
            # Count first (best-effort) so we can report a number.
            before = 0
            try:
                before = int(tbl.count_rows())
            except Exception:
                pass
            with _WRITE_LOCK:
                tbl.delete(f"ts < {cutoff}")
            after = before
            try:
                after = int(tbl.count_rows())
            except Exception:
                pass
            out[name] = max(0, before - after)
        except Exception as exc:
            log.warning(f"memory: prune '{name}' failed: {exc}")
    return out
