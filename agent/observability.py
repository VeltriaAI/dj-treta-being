"""Observability — structured per-LLM-call trace + per-agent health.

v8 Phase 8: every LLM invocation writes an llm_calls row. DJ picks (rank
N from planner playlist) write decisions rows. Long-lived threads tick
agent_health so the TUI can show which peers are alive.

Zero-impact when DB is unreachable — all functions fail-silent (log
warning, return None). Never allowed to break a live DJ set.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

log = logging.getLogger("dj-treta")

# Approx cost per token (Gemini Flash 3). v8 uses these for the billing
# counter; v9 will swap for a per-model table.
_COST_PER_INPUT_TOKEN = 0.00000025  # $0.25 per 1M
_COST_PER_OUTPUT_TOKEN = 0.00000100  # $1.00 per 1M


def record_llm_call(
    agent: str,
    instruction: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    tool_calls: Optional[list] = None,
    error: str = "",
) -> float:
    """Insert an llm_calls row. Returns the computed cost_usd."""
    cost = (input_tokens * _COST_PER_INPUT_TOKEN
            + output_tokens * _COST_PER_OUTPUT_TOKEN)
    try:
        from .db import get_db
        db = get_db()
        try:
            db.execute(
                "INSERT INTO llm_calls "
                "(ts, agent, instruction_preview, input_tokens, output_tokens, "
                " cost_usd, latency_ms, tool_calls_json, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    agent,
                    (instruction or "")[:200],
                    int(input_tokens),
                    int(output_tokens),
                    float(cost),
                    int(latency_ms),
                    json.dumps(tool_calls or []),
                    error or None,
                ),
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        log.debug(f"Observability: llm_call record failed: {exc}")
    return cost


def record_decision(
    agent: str,
    decision_type: str,
    picked_rank: Optional[int] = None,
    reason: str = "",
    context: str = "",
) -> None:
    """Insert a decisions row (e.g. DJ picked rank 2 from planner playlist)."""
    try:
        from .db import get_db
        db = get_db()
        try:
            db.execute(
                "INSERT INTO decisions "
                "(ts, agent, decision_type, picked_rank, reason, context_preview) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    agent,
                    decision_type,
                    int(picked_rank) if picked_rank is not None else None,
                    (reason or "")[:500],
                    (context or "")[:500],
                ),
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        log.debug(f"Observability: decision record failed: {exc}")


def tick(agent: str, error: str = "") -> None:
    """Update agent_health.last_tick. Call from every long-lived thread."""
    try:
        from .db import get_db
        db = get_db()
        try:
            now = time.time()
            if error:
                db.execute(
                    "INSERT INTO agent_health "
                    "(agent, last_tick, last_error, consecutive_errors, thread_alive) "
                    "VALUES (?, ?, ?, 1, 1) "
                    "ON CONFLICT(agent) DO UPDATE SET "
                    " last_tick=excluded.last_tick, "
                    " last_error=excluded.last_error, "
                    " consecutive_errors=consecutive_errors+1",
                    (agent, now, error[:500]),
                )
            else:
                db.execute(
                    "INSERT INTO agent_health "
                    "(agent, last_tick, last_error, consecutive_errors, thread_alive) "
                    "VALUES (?, ?, '', 0, 1) "
                    "ON CONFLICT(agent) DO UPDATE SET "
                    " last_tick=excluded.last_tick, "
                    " last_error='', "
                    " consecutive_errors=0",
                    (agent, now),
                )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        log.debug(f"Observability: tick record failed: {exc}")


def spend_today(agent: Optional[str] = None) -> float:
    """Sum cost_usd for today. Filter by agent if given."""
    try:
        from .db import get_db
        db = get_db()
        try:
            day_start = time.time() - (time.time() % 86400)
            if agent:
                row = db.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) FROM llm_calls "
                    "WHERE ts >= ? AND agent = ?",
                    (day_start, agent),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) FROM llm_calls "
                    "WHERE ts >= ?",
                    (day_start,),
                ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            db.close()
    except Exception:
        return 0.0
