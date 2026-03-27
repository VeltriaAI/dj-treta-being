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

    CREATE TABLE IF NOT EXISTS set_history (
        id INTEGER PRIMARY KEY,
        track_id INTEGER REFERENCES tracks(id),
        title TEXT,
        played_at REAL,
        deck INTEGER,
        session_id TEXT
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
    """)
    db.commit()
    db.close()


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
            placeholders = ",".join("?" * len(played_titles))
            query += f" AND title NOT IN ({placeholders})"
            params.extend(played_titles)

        query += " ORDER BY RANDOM() LIMIT ?"
        params.append(limit)

        return [dict(row) for row in db.execute(query, params).fetchall()]
    finally:
        db.close()


def get_track_by_path(path: str) -> dict | None:
    db = get_db()
    try:
        row = db.execute("SELECT * FROM tracks WHERE path=?", (path,)).fetchone()
        return dict(row) if row else None
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
