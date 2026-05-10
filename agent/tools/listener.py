"""Listener-pulse tools — give Treta a snapshot of how Manish is engaging.

Two read-only tools:

  get_listener_pulse(window_minutes)
    Recent-window view: likes/dislikes/skips in the last N minutes,
    plus mood-request shape directives parsed from the planner queue.
    For "what's the room doing right now".

  get_listener_profile()
    Cross-session view: lifetime per-genre like/dislike/skip totals
    pulled from the listener_profile KV table. For "who is Manish,
    in aggregate, before we even start".

Both are pure reads — no state mutation, no raises on empty data.
Empty profile = all-zeros dict; missing data = empty list / -1 sentinel.

Known gaps (intentional, not bugs):
  - recent_skips: skip-log table doesn't exist yet. Returns []. Will
    populate once heartbeat records skips into a queryable log.
  - message_count_last_window / last_message_age_s: no listener-message
    log standardized yet, so both return -1.
"""

import time

from .. import db


def _session():
    """Import-time-safe accessor for the Session singleton."""
    from ..session_state import get_session
    return get_session()


# Genre tokens we look for inside shape directives targeting the planner.
# Lowercased substring match against payload.text. Order matters only
# for tie-breaks — we count every occurrence.
_KNOWN_GENRE_TOKENS = (
    "bollyafro",
    "melodic-techno",
    "dark-techno",
    "organic-house",
    "peak-time",
    "progressive",
    "psytrance",
    "ambient",
    "minimal",
    "vocal",
    "house",
    "deep",
)


def get_listener_pulse(window_minutes: int = 30) -> dict:
    """Snapshot of listener engagement in the recent window.

    Args:
        window_minutes: How far back to look. Default 30.

    Returns:
        {
          "recent_likes":    [{"track_title", "ts", "genre"}, ...],
          "recent_dislikes": [{"track_title", "ts", "genre"}, ...],
          "recent_skips":    [],   # skip log pending
          "message_count_last_window": int,   # -1 if no log
          "mood_requests_seen": {<genre_token>: int, ...},
          "last_message_age_s": int,          # -1 if no log
        }
    """
    try:
        window_s = max(1, int(window_minutes)) * 60
    except Exception:
        window_s = 30 * 60
    cutoff = time.time() - window_s

    recent_likes: list[dict] = []
    recent_dislikes: list[dict] = []

    try:
        conn = db.get_db()
        try:
            rows = conn.execute(
                """
                SELECT f.track_title, f.feedback, f.created_at AS ts,
                       (SELECT t.genre FROM tracks t
                          WHERE t.title LIKE '%' || f.track_title || '%'
                          LIMIT 1) AS genre
                FROM feedback f
                WHERE f.created_at >= ?
                ORDER BY f.created_at DESC
                """,
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            entry = {
                "track_title": r["track_title"],
                "ts": r["ts"],
                "genre": r["genre"],
            }
            if r["feedback"] == "like":
                recent_likes.append(entry)
            elif r["feedback"] == "dislike":
                recent_dislikes.append(entry)
    except Exception:
        # Defensive: never raise from a snapshot tool.
        pass

    # skip log pending — will populate once heartbeat records skips
    # into a queryable log table.
    recent_skips: list[dict] = []

    # Mood-request parsing from active/satisfied planner shape directives.
    mood_requests_seen: dict[str, int] = {}
    try:
        sess = _session()
        if sess is not None:
            for d in (getattr(sess, "directives", None) or []):
                if d.get("kind") != "shape":
                    continue
                if d.get("target") != "planner":
                    continue
                if d.get("status") not in ("active", "satisfied"):
                    continue
                payload = d.get("payload") or {}
                text = (payload.get("text") or "").lower()
                if not text:
                    continue
                for tok in _KNOWN_GENRE_TOKENS:
                    if tok in text:
                        mood_requests_seen[tok] = mood_requests_seen.get(tok, 0) + 1
    except Exception:
        pass

    # No standardized listener-message log yet — return -1 sentinels.
    # The chat_history schema isn't reliable enough to count against.
    message_count_last_window = -1
    last_message_age_s = -1

    return {
        "recent_likes": recent_likes,
        "recent_dislikes": recent_dislikes,
        "recent_skips": recent_skips,
        "message_count_last_window": message_count_last_window,
        "mood_requests_seen": mood_requests_seen,
        "last_message_age_s": last_message_age_s,
    }


def get_listener_profile() -> dict:
    """Full snapshot of the cross-session listener profile.

    Reads the flat listener_profile KV table, parses the well-known
    `total_<likes|dislikes|skips>_<genre>` keys into a per-genre matrix,
    and sums them into lifetime totals.

    Returns:
        {
          "by_genre": {
            "<genre>": {"likes": int, "dislikes": int, "skips": int}
          },
          "totals":   {"likes": int, "dislikes": int, "skips": int},
          "raw":      <full output of db.get_listener_profile_all()>,
          "last_updated_at": float,   # max updated_at across all rows
        }
    """
    try:
        raw = db.get_listener_profile_all() or {}
    except Exception:
        raw = {}

    by_genre: dict[str, dict[str, int]] = {}
    totals = {"likes": 0, "dislikes": 0, "skips": 0}
    last_updated_at = 0.0

    # Map of total_<bucket>_<genre> key prefix → which bucket it counts.
    prefix_map = {
        "total_likes_": "likes",
        "total_dislikes_": "dislikes",
        "total_skips_": "skips",
    }

    for key, meta in raw.items():
        if not isinstance(meta, dict):
            continue
        # Track most-recent update across the whole table.
        try:
            ua = float(meta.get("updated_at") or 0.0)
            if ua > last_updated_at:
                last_updated_at = ua
        except Exception:
            pass

        # Coerce value to int; skip rows we can't parse.
        try:
            value_int = int(meta.get("value") or 0)
        except Exception:
            continue

        for prefix, bucket in prefix_map.items():
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]
            # The "_all" rollup is a totals shortcut — not a genre.
            if suffix == "all":
                # Don't double-count: we sum from per-genre rows below.
                # The raw rollup is still exposed in `raw` for callers
                # that want it.
                break
            if not suffix:
                break
            genre = suffix
            entry = by_genre.setdefault(
                genre, {"likes": 0, "dislikes": 0, "skips": 0}
            )
            entry[bucket] = entry.get(bucket, 0) + value_int
            totals[bucket] = totals.get(bucket, 0) + value_int
            break

    return {
        "by_genre": by_genre,
        "totals": totals,
        "raw": raw,
        "last_updated_at": last_updated_at,
    }
