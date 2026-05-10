"""SQLite database for DJ Treta — tracks, history, learnings.

DB path resolution order:
  1. ``$DJCLAW_DB_PATH`` — explicit override (tests, alt installs)
  2. Repo-local ``djtreta.db`` if running inside a checkout (dev flow)
  3. ``~/.local/share/djclaw/db/djtreta.db`` — XDG end-user install,
     created by the installer.

Replaces: .analysis/*.txt, learnings.json, session.json playlist caches.
"""

import json
import os
import sqlite3
import time
from pathlib import Path


def _resolve_db_path() -> Path:
    """Return the SQLite path. Resolution order:

      1. ``$DJCLAW_DB_PATH`` env — explicit override
      2. Repo-local ``djtreta.db`` if it exists at the checkout root —
         keeps existing dev workflow stable; never auto-migrates a dev's
         data into XDG behind their back
      3. ``~/.local/share/djclaw/db/djtreta.db`` — installer-managed,
         created on first init_db() if it doesn't already exist
    """
    env = os.environ.get("DJCLAW_DB_PATH")
    if env:
        p = Path(env).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    repo_db = Path(__file__).parent.parent / "djtreta.db"
    if repo_db.exists():
        return repo_db

    xdg = Path("~/.local/share/djclaw/db/djtreta.db").expanduser()
    xdg.parent.mkdir(parents=True, exist_ok=True)
    return xdg


DB_PATH = _resolve_db_path()


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS tracks (
        id INTEGER PRIMARY KEY,
        path TEXT UNIQUE NOT NULL,
        title TEXT,
        artist TEXT,
        genre TEXT,
        bpm REAL,
        key_musical TEXT,
        key_camelot TEXT,
        energy_peak INTEGER,
        duration_seconds REAL,
        mood TEXT,
        mix_in_seconds REAL,
        mix_out_seconds REAL,
        timeline TEXT,
        analysis_text TEXT,
        similar TEXT,
        verdict TEXT,
        analyzed_at REAL,
        created_at REAL DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS sets (
        id TEXT PRIMARY KEY,
        set_number INTEGER,
        title TEXT,
        started_at REAL,
        ended_at REAL,
        mood TEXT,
        genre TEXT,
        target_duration_minutes INTEGER,
        actual_duration_minutes REAL,
        track_count INTEGER DEFAULT 0,
        peak_energy REAL DEFAULT 0,
        energy_arc TEXT,
        status TEXT DEFAULT 'live',
        recording_path TEXT,
        synced_at REAL
    );

    CREATE TABLE IF NOT EXISTS set_history (
        id INTEGER PRIMARY KEY,
        set_id TEXT REFERENCES sets(id),
        track_id INTEGER REFERENCES tracks(id),
        title TEXT,
        played_at REAL,
        deck INTEGER,
        transition_type TEXT
    );

    CREATE TABLE IF NOT EXISTS learnings (
        id INTEGER PRIMARY KEY,
        topic TEXT,
        content TEXT,
        created_at REAL DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS track_pairs (
        id INTEGER PRIMARY KEY,
        track1_id INTEGER REFERENCES tracks(id),
        track2_id INTEGER REFERENCES tracks(id),
        quality INTEGER,
        notes TEXT,
        created_at REAL DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY,
        track_title TEXT NOT NULL,
        track_path TEXT,
        feedback TEXT NOT NULL,  -- 'like' or 'dislike'
        set_id TEXT,
        created_at REAL DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS evolution_log (
        id INTEGER PRIMARY KEY,
        goal TEXT NOT NULL,
        scope TEXT,
        status TEXT DEFAULT 'pending',
        pr_url TEXT,
        pr_number INTEGER,
        branch_name TEXT,
        cost_usd REAL,
        error TEXT,
        triggered_by TEXT,
        created_at REAL DEFAULT (strftime('%s','now')),
        completed_at REAL
    );

    CREATE TABLE IF NOT EXISTS evolution_patterns (
        id INTEGER PRIMARY KEY,
        pattern_type TEXT NOT NULL,
        description TEXT,
        confidence REAL,
        occurrences INTEGER DEFAULT 1,
        suggested_action TEXT,
        resolved INTEGER DEFAULT 0,
        first_seen REAL DEFAULT (strftime('%s','now')),
        last_seen REAL DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS track_aliases (
        id INTEGER PRIMARY KEY,
        track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
        source_url TEXT UNIQUE,
        original_title TEXT,
        original_uploader TEXT,
        added_at REAL DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS mood_profile_cache (
        raw_mood_lower TEXT NOT NULL,
        profile_json TEXT NOT NULL,
        resolved_at REAL NOT NULL,
        resolver_version TEXT NOT NULL,
        PRIMARY KEY (raw_mood_lower, resolver_version)
    );

    CREATE TABLE IF NOT EXISTS llm_calls (
        id INTEGER PRIMARY KEY,
        ts REAL,
        agent TEXT,
        instruction_preview TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_usd REAL,
        latency_ms INTEGER,
        tool_calls_json TEXT,
        error TEXT
    );

    CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY,
        ts REAL,
        agent TEXT,
        decision_type TEXT,
        picked_rank INTEGER,
        reason TEXT,
        context_preview TEXT
    );

    CREATE TABLE IF NOT EXISTS agent_health (
        agent TEXT PRIMARY KEY,
        last_tick REAL,
        last_error TEXT,
        consecutive_errors INTEGER DEFAULT 0,
        thread_alive INTEGER DEFAULT 1
    );

    -- Listener profile (cross-session). Flat KV that survives daemon
    -- restarts. Updated incrementally by like/dislike/skip handlers.
    -- Treta reads this at session-start to know who Manish is *before*
    -- they start chatting. See evolution plan Tier 1.5.
    CREATE TABLE IF NOT EXISTS listener_profile (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at REAL DEFAULT (strftime('%s','now')),
        calibration_count INTEGER DEFAULT 0
    );
    """)
    _migrate_tracks_canonical(db)
    db.commit()
    db.close()


# ── Listener profile helpers ───────────────────────────────────────


def get_listener_profile_kv(key: str, default: str = "") -> str:
    """Read a single listener-profile value."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT value FROM listener_profile WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        db.close()


def set_listener_profile_kv(key: str, value: str) -> None:
    """Set a listener-profile value. Bumps calibration_count + updated_at."""
    import time as _t
    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO listener_profile (key, value, updated_at, calibration_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                calibration_count = listener_profile.calibration_count + 1
            """,
            (key, str(value), _t.time()),
        )
        db.commit()
    finally:
        db.close()


def increment_listener_profile_kv(key: str, delta: int = 1) -> int:
    """Atomically increment an integer-valued profile key. Returns new value."""
    import time as _t
    db = get_db()
    try:
        row = db.execute(
            "SELECT value FROM listener_profile WHERE key = ?", (key,)
        ).fetchone()
        cur = int(row["value"]) if (row and row["value"]) else 0
        new = cur + delta
        db.execute(
            """
            INSERT INTO listener_profile (key, value, updated_at, calibration_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                calibration_count = listener_profile.calibration_count + 1
            """,
            (key, str(new), _t.time()),
        )
        db.commit()
        return new
    finally:
        db.close()


def get_listener_profile_all() -> dict:
    """Snapshot every listener-profile key → value, with metadata."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT key, value, updated_at, calibration_count "
            "FROM listener_profile ORDER BY key"
        ).fetchall()
        return {
            r["key"]: {
                "value": r["value"],
                "updated_at": r["updated_at"],
                "calibration_count": r["calibration_count"],
            }
            for r in rows
        }
    finally:
        db.close()


def _migrate_tracks_canonical(db):
    """Add canonical identity columns to tracks table (idempotent)."""
    cols = {row["name"] for row in db.execute("PRAGMA table_info(tracks)").fetchall()}
    additions = [
        ("source_url", "TEXT"),
        ("original_title", "TEXT"),
        ("canonical_artist", "TEXT"),
        ("canonical_song", "TEXT"),
        ("canonical_version", "TEXT"),
        ("remixer", "TEXT"),
        ("canonical_confidence", "REAL"),
    ]
    for name, sqltype in additions:
        if name not in cols:
            db.execute(f"ALTER TABLE tracks ADD COLUMN {name} {sqltype}")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_source_url "
        "ON tracks(source_url) WHERE source_url IS NOT NULL"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_canonical "
        "ON tracks(canonical_artist, canonical_song, canonical_version, remixer)"
    )
    # Partial UNIQUE: only enforced for rows whose canonical fields are populated.
    # Legacy rows with NULL canonicals stay valid; new canonicalized rows can't
    # collide with each other. Prevents the ghost-row accumulation that
    # happened during cross-machine library scans.
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_canonical_unique "
        "ON tracks(canonical_artist, canonical_song, canonical_version, remixer) "
        "WHERE canonical_artist IS NOT NULL AND canonical_song IS NOT NULL"
    )


def _normalize_track_path(path: str) -> str:
    """Convert absolute path under library.music_dir to its relative form.

    This keeps tracks.path portable across machines (Mac dev / Linux VM).
    Paths outside music_dir, or paths with cross-machine prefixes the
    migration script knows about, are left as-is — the migration script
    handles those one-shot.
    """
    if not path or not path.startswith("/"):
        return path
    try:
        from .config import load_config
        music_dir = load_config().library.music_path.resolve()
    except Exception:
        return path
    md_str = str(music_dir) + "/"
    if path.startswith(md_str):
        return path[len(md_str):]
    return path


def upsert_track(path: str, title: str = None, artist: str = None,
                 genre: str = None, **kwargs):
    """Insert or update a track. Extra kwargs stored as columns if they exist.

    Atomic via SQLite's ON CONFLICT(path) DO UPDATE — was previously a
    SELECT-then-INSERT/UPDATE which raced under scan_library's tight loop
    and crashed the daemon at boot with `UNIQUE constraint failed: tracks.path`.
    """
    path = _normalize_track_path(path)

    # Build the row's values from explicit args + kwargs (skipping None).
    all_cols = {"path": path, "title": title, "artist": artist, "genre": genre}
    all_cols.update({k: v for k, v in kwargs.items() if v is not None})
    insert_pairs = [(k, v) for k, v in all_cols.items() if v is not None]
    insert_cols = [k for k, _ in insert_pairs]
    insert_vals = [v for _, v in insert_pairs]
    placeholders = ",".join("?" * len(insert_cols))

    # Update set: everything except `path` (the conflict key).
    update_cols = [c for c in insert_cols if c != "path"]
    update_set = ", ".join(f"{c}=excluded.{c}" for c in update_cols)

    sql = (
        f"INSERT INTO tracks ({','.join(insert_cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET {update_set}"
    ) if update_cols else (
        f"INSERT INTO tracks ({','.join(insert_cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO NOTHING"
    )

    db = get_db()
    try:
        db.execute(sql, insert_vals)
        db.commit()
    finally:
        db.close()


def find_track_by_source_url(url: str) -> dict | None:
    """Return track dict if URL already in tracks.source_url or track_aliases. None otherwise."""
    if not url:
        return None
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM tracks WHERE source_url = ?", (url,)
        ).fetchone()
        if row:
            return dict(row)
        row = db.execute(
            "SELECT t.* FROM tracks t "
            "JOIN track_aliases a ON a.track_id = t.id "
            "WHERE a.source_url = ?",
            (url,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def find_track_by_canonical(artist: str, song: str, version: str | None,
                            remixer: str | None) -> dict | None:
    """Return track dict matching the canonical 4-tuple. None otherwise.

    Matches NULLs correctly — "Morgen (no version)" != "Morgen (Original Mix)".
    """
    if not artist or not song:
        return None
    db = get_db()
    try:
        query = (
            "SELECT * FROM tracks "
            "WHERE LOWER(canonical_artist) = LOWER(?) "
            "  AND LOWER(canonical_song) = LOWER(?) "
            "  AND (canonical_version IS ? OR LOWER(canonical_version) = LOWER(?)) "
            "  AND (remixer IS ? OR LOWER(remixer) = LOWER(?)) "
            "LIMIT 1"
        )
        row = db.execute(query, (artist, song, version, version, remixer, remixer)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def add_track_alias(track_id: int, source_url: str, original_title: str = "",
                    original_uploader: str = ""):
    """Record that source_url points to an existing track. Idempotent on URL."""
    if not source_url or not track_id:
        return
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO track_aliases "
            "(track_id, source_url, original_title, original_uploader) "
            "VALUES (?, ?, ?, ?)",
            (track_id, source_url, original_title, original_uploader),
        )
        db.commit()
    finally:
        db.close()


def find_compatible_tracks(bpm: float, key_camelot: str, energy: int,
                           played_titles: list, limit: int = 5) -> list[dict]:
    """Find tracks compatible with current: BPM ±10, compatible key, energy ±3."""
    from .camelot import get_compatible_keys
    compatible_keys = get_compatible_keys(key_camelot) if key_camelot else []

    db = get_db()
    try:
        query = """
            SELECT * FROM tracks
            WHERE bpm BETWEEN ? AND ?
              AND energy_peak BETWEEN ? AND ?
              AND analyzed_at IS NOT NULL
        """
        params: list = [bpm - 10, bpm + 10, max(1, energy - 3), min(10, energy + 3)]

        if compatible_keys:
            placeholders = ",".join("?" * len(compatible_keys))
            query += f" AND key_camelot IN ({placeholders})"
            params.extend(compatible_keys)

        if played_titles:
            # Substring match — Mixxx titles may differ from DB titles
            # e.g. DB: "Afterlife - Anyma - Sentient", Mixxx: "Anyma - Sentient"
            for pt in played_titles:
                query += " AND title NOT LIKE ?"
                params.append(f"%{pt}%")

        query += " ORDER BY RANDOM() LIMIT ?"
        params.append(limit)

        return [dict(row) for row in db.execute(query, params).fetchall()]
    finally:
        db.close()


def get_track_by_path(path: str) -> dict | None:
    """Look up track by path. Robust to absolute/relative + cross-machine paths.

    DB stores paths relative to library.music_dir post-migration. Callers may
    pass absolute paths from Mixxx (different machine) or relative paths from
    session.playlist — both forms resolve here.
    """
    import unicodedata
    db = get_db()
    try:
        # Exact match first
        row = db.execute("SELECT * FROM tracks WHERE path=?", (path,)).fetchone()
        if row:
            return dict(row)

        # Try the relativized form (handles caller passing absolute path
        # when DB has relative-to-music_dir).
        relative = _normalize_track_path(path)
        if relative != path:
            row = db.execute("SELECT * FROM tracks WHERE path=?", (relative,)).fetchone()
            if row:
                return dict(row)

        # Normalize unicode and try again (Mixxx vs Python encoding differences)
        normalized = unicodedata.normalize("NFC", path)
        row = db.execute("SELECT * FROM tracks WHERE path=?", (normalized,)).fetchone()
        if row:
            return dict(row)

        # Fallback: match by filename (last component)
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        rows = db.execute("SELECT * FROM tracks WHERE path LIKE ?", (f"%{filename}",)).fetchall()
        if len(rows) == 1:
            return dict(rows[0])

        return None
    finally:
        db.close()


def get_unanalyzed_tracks(limit: int = 5) -> list[dict]:
    db = get_db()
    try:
        return [dict(r) for r in db.execute(
            "SELECT * FROM tracks WHERE analyzed_at IS NULL LIMIT ?", (limit,)
        ).fetchall()]
    finally:
        db.close()


def get_all_analyzed_tracks() -> list[dict]:
    db = get_db()
    try:
        return [dict(r) for r in db.execute(
            "SELECT title, path, bpm, key_musical, key_camelot, energy_peak, "
            "duration_seconds, mix_in_seconds, mix_out_seconds, mood "
            "FROM tracks WHERE analyzed_at IS NOT NULL"
        ).fetchall()]
    finally:
        db.close()


def get_library_with_metadata(include_unanalyzed: bool = False) -> list[dict]:
    """Return every library track with its full canonical + analysis metadata.

    Used by the v8 planner: LLM sees the entire library in one shot and picks.
    No SQL pre-filter — the LLM decides which tracks fit mood/BPM/energy.

    When include_unanalyzed=False (default), skips tracks with no analyzed_at
    since we can't give the planner their BPM/key/energy.
    """
    db = get_db()
    try:
        query = (
            "SELECT path, title, artist, genre, bpm, key_musical, key_camelot, "
            "energy_peak, duration_seconds, mood, canonical_artist, canonical_song, "
            "canonical_version, remixer "
            "FROM tracks"
        )
        if not include_unanalyzed:
            query += " WHERE analyzed_at IS NOT NULL"
        return [dict(r) for r in db.execute(query).fetchall()]
    finally:
        db.close()


def save_learning_db(topic: str, content: str):
    db = get_db()
    try:
        db.execute("INSERT INTO learnings (topic, content) VALUES (?, ?)", (topic, content))
        db.commit()
    finally:
        db.close()


def recall_learnings_db(topic: str = "") -> list[dict]:
    db = get_db()
    try:
        if topic:
            rows = db.execute(
                "SELECT * FROM learnings WHERE topic LIKE ? OR content LIKE ? "
                "ORDER BY created_at DESC LIMIT 20",
                (f"%{topic}%", f"%{topic}%")
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM learnings ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def scan_library(music_path: Path):
    """Scan music directory and add any tracks not already in DB."""
    if not music_path.exists():
        return
    for genre_dir in sorted(music_path.iterdir()):
        if not genre_dir.is_dir() or genre_dir.name.startswith('.'):
            continue
        for f in sorted(genre_dir.iterdir()):
            if f.suffix.lower() in ('.mp3', '.wav', '.flac', '.ogg', '.m4a'):
                upsert_track(path=str(f), title=f.stem, genre=genre_dir.name)


# ── Sets ──────────────────────────────────────────────────────────────

def get_next_set_number() -> int:
    db = get_db()
    try:
        row = db.execute("SELECT MAX(set_number) FROM sets").fetchone()
        return (row[0] or 0) + 1
    finally:
        db.close()


def insert_set(set_data: dict):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO sets (id, set_number, title, started_at, mood, genre, "
            "target_duration_minutes, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (set_data["id"], set_data.get("set_number", 1),
             set_data.get("title", ""), set_data["started_at"],
             set_data.get("mood"), set_data.get("genre"),
             set_data.get("target_duration"), "live")
        )
        db.commit()
    finally:
        db.close()


def update_set(set_data: dict):
    db = get_db()
    try:
        actual = None
        if set_data.get("ended_at") and set_data.get("started_at"):
            actual = (set_data["ended_at"] - set_data["started_at"]) / 60
        db.execute(
            "UPDATE sets SET ended_at=?, status=?, actual_duration_minutes=?, "
            "track_count=?, peak_energy=?, energy_arc=?, recording_path=? WHERE id=?",
            (set_data.get("ended_at"), set_data.get("status", "finished"),
             actual, set_data.get("track_count", 0),
             set_data.get("peak_energy", 0),
             json.dumps(set_data.get("energy_arc", [])),
             set_data.get("recording_path"), set_data["id"])
        )
        db.commit()
    finally:
        db.close()


def add_track_to_set(set_id: str, title: str, deck: int, transition_type: str = ""):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO set_history (set_id, title, played_at, deck, transition_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (set_id, title, time.time(), deck, transition_type)
        )
        db.commit()
    finally:
        db.close()


def get_current_set() -> dict | None:
    db = get_db()
    try:
        row = db.execute("SELECT * FROM sets WHERE status='live' ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def get_set_tracks(set_id: str) -> list[dict]:
    db = get_db()
    try:
        return [dict(r) for r in db.execute(
            "SELECT * FROM set_history WHERE set_id=? ORDER BY played_at", (set_id,)
        ).fetchall()]
    finally:
        db.close()


def add_feedback(track_title: str, feedback: str, track_path: str = "", set_id: str = ""):
    """Record like/dislike feedback for a track + bump listener profile."""
    db = get_db()
    try:
        db.execute(
            "INSERT INTO feedback (track_title, track_path, feedback, set_id) VALUES (?, ?, ?, ?)",
            (track_title, track_path, feedback, set_id)
        )
        # Update listener profile counters. Look up the track's genre to
        # build per-genre like/dislike running totals — this is what
        # Treta reads via get_listener_profile() to know "Manish skips
        # 73% of vocal house at peak hours".
        try:
            row = db.execute(
                "SELECT genre FROM tracks WHERE title LIKE '%' || ? || '%' LIMIT 1",
                (track_title,)
            ).fetchone()
            genre = (row["genre"] if row and row["genre"] else "unknown").lower()
        except Exception:
            genre = "unknown"
        db.commit()
    finally:
        db.close()
    # Profile updates use their own helpers (own connections).
    if feedback == "like":
        increment_listener_profile_kv(f"total_likes_{genre}", 1)
        increment_listener_profile_kv("total_likes_all", 1)
    elif feedback == "dislike":
        increment_listener_profile_kv(f"total_dislikes_{genre}", 1)
        increment_listener_profile_kv("total_dislikes_all", 1)


def record_skip(track_title: str, reason: str = "", set_id: str = ""):
    """Record a skip event in the listener profile counters.

    Skips are a softer signal than dislikes — they say "not now",
    not "never". Tracked separately so Treta can distinguish between
    "Manish actively skipped this" vs "this just didn't fit the
    moment". Genre is best-effort lookup against tracks table.
    """
    db = get_db()
    try:
        row = db.execute(
            "SELECT genre FROM tracks WHERE title LIKE '%' || ? || '%' LIMIT 1",
            (track_title,)
        ).fetchone()
        genre = (row["genre"] if row and row["genre"] else "unknown").lower()
    except Exception:
        genre = "unknown"
    finally:
        db.close()
    increment_listener_profile_kv(f"total_skips_{genre}", 1)
    increment_listener_profile_kv("total_skips_all", 1)


def get_liked_tracks(limit: int = 20) -> list[dict]:
    """Get recently liked tracks with their metadata."""
    db = get_db()
    try:
        return [dict(r) for r in db.execute("""
            SELECT f.track_title, f.feedback, t.bpm, t.key_camelot, t.energy_peak, t.genre, t.mood
            FROM feedback f
            LEFT JOIN tracks t ON t.title LIKE '%' || f.track_title || '%'
            WHERE f.feedback = 'like'
            ORDER BY f.created_at DESC LIMIT ?
        """, (limit,)).fetchall()]
    finally:
        db.close()


def get_disliked_tracks(limit: int = 20) -> list[str]:
    """Get recently disliked track titles."""
    db = get_db()
    try:
        return [r["track_title"] for r in db.execute(
            "SELECT track_title FROM feedback WHERE feedback='dislike' ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()]
    finally:
        db.close()
