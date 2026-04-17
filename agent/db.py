"""SQLite database for DJ Treta — tracks, history, learnings.

Single file: djtreta.db in repo root.
Replaces: .analysis/*.txt, learnings.json, session.json playlist caches.
"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "djtreta.db"


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
    """)
    _migrate_tracks_canonical(db)
    db.commit()
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


def upsert_track(path: str, title: str = None, artist: str = None,
                 genre: str = None, **kwargs):
    """Insert or update a track. Extra kwargs stored as columns if they exist."""
    db = get_db()
    try:
        existing = db.execute("SELECT id FROM tracks WHERE path=?", (path,)).fetchone()
        if existing:
            updates = {k: v for k, v in kwargs.items() if v is not None}
            if title:
                updates["title"] = title
            if artist:
                updates["artist"] = artist
            if genre:
                updates["genre"] = genre
            if updates:
                sets = ", ".join(f"{k}=?" for k in updates)
                db.execute(f"UPDATE tracks SET {sets} WHERE path=?",
                           list(updates.values()) + [path])
        else:
            all_cols = {"path": path, "title": title, "artist": artist, "genre": genre}
            all_cols.update({k: v for k, v in kwargs.items() if v is not None})
            cols = [k for k, v in all_cols.items() if v is not None]
            vals = [v for v in all_cols.values() if v is not None]
            placeholders = ",".join("?" * len(cols))
            db.execute(f"INSERT INTO tracks ({','.join(cols)}) VALUES ({placeholders})", vals)
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
    import unicodedata
    db = get_db()
    try:
        # Exact match first
        row = db.execute("SELECT * FROM tracks WHERE path=?", (path,)).fetchone()
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
    """Record like/dislike feedback for a track."""
    db = get_db()
    try:
        db.execute(
            "INSERT INTO feedback (track_title, track_path, feedback, set_id) VALUES (?, ?, ?, ?)",
            (track_title, track_path, feedback, set_id)
        )
        db.commit()
    finally:
        db.close()


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
