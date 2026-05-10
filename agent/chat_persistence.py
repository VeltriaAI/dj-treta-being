"""Treta chat persistence — JSONL dump per day, replay on boot.

Why this exists: Treta's ADK session is in-memory (InMemorySessionService).
On daemon restart she'd wake with no recollection of the chat she just had.
This module writes every turn to a daily JSONL file and, on the FIRST
Treta invocation after a fresh boot, replays the last K turns as a
context block prepended to her user_message. After that first turn,
the ADK session accumulates new turns normally.

Three layers of durability for chat memory:
  1. Layer 1 — ADK session (in-memory, dies with daemon)
  2. Layer 2 — LanceDB listener_interactions (semantic recall, durable)
  3. Layer 3 — JSONL daily file (this module, ordered + auditable + replayable)

JSONL location: ~/.beings/dj-treta/sessions/treta-YYYY-MM-DD.jsonl
Line schema (one JSON object per line):
  {
    ts: float,
    turn_id: str,            # uuid-ish; appends share a turn_id between user+assistant if needed
    role: "user" | "assistant",
    content: str,
    set_id: str,
    mood: str,
    tool_calls: list[dict] | None,  # ADK tool call summary if available
  }

Reset semantics: `djtreta reset` truncates today's JSONL. Older JSONLs
are kept on disk (audit trail).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("dj-treta")

SESSIONS_DIR = Path.home() / ".beings" / "dj-treta" / "sessions"


def _today_jsonl_path() -> Path:
    """Resolve today's JSONL path, ensuring the parent dir exists."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    return SESSIONS_DIR / f"treta-{date_str}.jsonl"


def append_chat_turn(
    message: str,
    response: str,
    set_id: str = "",
    mood: str = "",
    tool_calls: Optional[list[dict]] = None,
) -> bool:
    """Append one full chat turn (user msg + Treta response) to today's JSONL.

    Writes two lines: one role='user', one role='assistant'. Both share
    a `turn_id` so readers can pair them. Best-effort — never raises.
    """
    try:
        path = _today_jsonl_path()
        ts = time.time()
        turn_id = uuid.uuid4().hex[:12]
        user_line = {
            "ts": ts,
            "turn_id": turn_id,
            "role": "user",
            "content": message or "",
            "set_id": set_id or "",
            "mood": mood or "",
            "tool_calls": None,
        }
        assistant_line = {
            "ts": ts + 0.001,   # ensure stable ordering when timestamps tie
            "turn_id": turn_id,
            "role": "assistant",
            "content": response or "",
            "set_id": set_id or "",
            "mood": mood or "",
            "tool_calls": tool_calls or None,
        }
        with path.open("a") as f:
            f.write(json.dumps(user_line, default=str) + "\n")
            f.write(json.dumps(assistant_line, default=str) + "\n")
        return True
    except Exception as exc:
        log.warning(f"[chat-jsonl] append failed (non-fatal): {exc}")
        return False


def load_recent_turns(n: int = 20, max_age_hours: float = 24.0) -> list[dict]:
    """Read the last N turns from today's JSONL.

    A "turn" here is one JSONL line (so N=20 = up to 10 user/assistant pairs).
    `max_age_hours` caps freshness — if today's JSONL has nothing fresher,
    returns []. Returns oldest→newest.

    Best-effort — returns [] on any error.
    """
    try:
        path = _today_jsonl_path()
        if not path.exists():
            return []
        cutoff = time.time() - (max_age_hours * 3600)
        # Tail-read: for small files, just read all. JSONL daily file
        # rarely exceeds a few hundred KB.
        with path.open() as f:
            lines = f.readlines()
        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if (entry.get("ts") or 0) < cutoff:
                continue
            entries.append(entry)
        return entries[-n:]
    except Exception as exc:
        log.warning(f"[chat-jsonl] load failed (non-fatal): {exc}")
        return []


def format_replay_block(turns: list[dict], max_chars: int = 4000) -> str:
    """Format a list of turns as a prepend-able context block for Treta's prompt.

    Output shape:
      ── RECENT CONVERSATION (replayed from today's session JSONL) ──
      [HH:MM] user: <message>
      [HH:MM] treta: <response>
      ...
      ── END RECENT CONVERSATION ──

    Caps total length at `max_chars` (oldest dropped first if over).
    Returns "" when turns is empty.
    """
    if not turns:
        return ""
    rendered_lines = []
    total_chars = 0
    # Walk oldest→newest, building lines; cap at max_chars from the *newest* side.
    pieces = []
    for entry in turns:
        ts = entry.get("ts") or 0
        role = entry.get("role", "?")
        content = (entry.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        hhmm = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "??:??"
        speaker = "treta" if role == "assistant" else "user"
        line = f"[{hhmm}] {speaker}: {content}"
        pieces.append(line)
    # Trim from the front (oldest) if over budget.
    while pieces and sum(len(p) + 1 for p in pieces) > max_chars:
        pieces.pop(0)
    if not pieces:
        return ""
    header = "── RECENT CONVERSATION (replayed from today's session JSONL) ──"
    footer = "── END RECENT CONVERSATION ──"
    return "\n".join([header, *pieces, footer, ""])


def truncate_today_jsonl() -> int:
    """Truncate today's JSONL — called by `djtreta reset`. Returns bytes removed."""
    try:
        path = _today_jsonl_path()
        if not path.exists():
            return 0
        size = path.stat().st_size
        path.write_text("")
        log.info(f"[chat-jsonl] truncated today's JSONL ({size} bytes)")
        return size
    except Exception as exc:
        log.warning(f"[chat-jsonl] truncate failed: {exc}")
        return 0


def recall_recent_chat(n: int = 20) -> list[dict]:
    """Treta-facing tool — read the last N turns of her own recent chat.

    Useful when she wants to ground her response in earlier conversation
    that may have happened before the daemon was restarted, or when ADK's
    in-memory session has rolled off the context window. Complements
    recall_similar_interaction() — that's semantic recall across all
    time; this one is strictly ordered recent (within today).

    Args:
      n: how many turns to return (max 100, clamped).

    Returns:
      Oldest-first list of {ts, turn_id, role, content, mood}. Empty
      list if no chat today or on any error.
    """
    n = max(1, min(100, int(n)))
    raw = load_recent_turns(n=n, max_age_hours=24.0)
    # Slim payload — drop set_id + tool_calls + tunnel-internal fields
    return [
        {
            "ts": r.get("ts"),
            "turn_id": r.get("turn_id"),
            "role": r.get("role"),
            "content": r.get("content"),
            "mood": r.get("mood", ""),
        }
        for r in raw
    ]


def list_session_files() -> list[Path]:
    """All session JSONL files on disk, newest first."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(
        SESSIONS_DIR.glob("treta-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
