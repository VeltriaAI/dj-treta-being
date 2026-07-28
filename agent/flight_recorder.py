"""Flight recorder — 100% LLM transcript (2026-07-15).

Registers a global LiteLLM success/failure callback so EVERY completion the
daemon makes (all ADK agents + direct call sites: planner, mood, canonicalize,
genre-gate, being, dj, mixer, library) is appended to
``<runtime_dir>/llm-transcript.jsonl`` with full messages in, full response
out, tool calls, tokens and latency. This is the audit trail the truncated
thinking.log can't provide: when a cycle goes wrong, read the exact wire
traffic instead of guessing.

Local-first design: the file grows fast (full prompts), which is fine on a
dev box. ``DJTRETA_FLIGHT_RECORDER=0`` disables. Never raises — recording
must not be able to break the music.
"""

from __future__ import annotations

import json
import os
import time

from .runtime_paths import runtime_dir

_ENABLED = os.environ.get("DJTRETA_FLIGHT_RECORDER", "1") != "0"


def _path():
    return runtime_dir() / "llm-transcript.jsonl"


def _write(rec: dict) -> None:
    try:
        with open(_path(), "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def _extract(kwargs, response) -> dict:
    rec = {
        "ts": time.time(),
        "model": kwargs.get("model", ""),
        "messages": kwargs.get("messages", []),
        "tools": [
            (t.get("function", {}) or {}).get("name", "?")
            for t in (kwargs.get("tools") or [])
        ],
    }
    try:
        msg = response.choices[0].message
        rec["response_text"] = msg.content or ""
        rec["tool_calls"] = [
            {"name": tc.function.name, "args": tc.function.arguments}
            for tc in (msg.tool_calls or [])
        ]
        usage = getattr(response, "usage", None)
        if usage:
            rec["tokens"] = {
                "in": getattr(usage, "prompt_tokens", None),
                "out": getattr(usage, "completion_tokens", None),
            }
    except Exception:
        rec["response_text"] = "<unparseable>"
    return rec


def _on_success(kwargs, completion_response, start_time, end_time):
    try:
        rec = _extract(kwargs, completion_response)
        try:
            rec["latency_s"] = round((end_time - start_time).total_seconds(), 2)
        except Exception:
            pass
        _write(rec)
    except Exception:
        pass


def _on_failure(kwargs, completion_response, start_time, end_time):
    try:
        _write({
            "ts": time.time(),
            "model": kwargs.get("model", ""),
            "messages": kwargs.get("messages", []),
            "FAILED": str(completion_response)[:2000],
        })
    except Exception:
        pass


def _make_async_logger():
    """CustomLogger covering the ADK/async litellm path, which never fires the
    sync success/failure callbacks (the 07-15 known gap: recorder saw ~1 line
    from an 8.5h set because every planner/DJ call went through acompletion)."""
    from litellm.integrations.custom_logger import CustomLogger

    class _FlightRecorder(CustomLogger):
        async def async_log_success_event(self, kwargs, response_obj,
                                          start_time, end_time):
            _on_success(kwargs, response_obj, start_time, end_time)

        async def async_log_failure_event(self, kwargs, response_obj,
                                          start_time, end_time):
            _on_failure(kwargs, response_obj, start_time, end_time)

    return _FlightRecorder()


def install() -> bool:
    """Idempotently register the callbacks. Returns True when recording."""
    if not _ENABLED:
        return False
    try:
        import litellm
        if _on_success not in (litellm.success_callback or []):
            litellm.success_callback = (litellm.success_callback or []) + [_on_success]
        if _on_failure not in (litellm.failure_callback or []):
            litellm.failure_callback = (litellm.failure_callback or []) + [_on_failure]
        # Async (ADK) path — the sync callbacks above never see acompletion.
        try:
            if not any(type(cb).__name__ == "_FlightRecorder"
                       for cb in (litellm.callbacks or [])):
                litellm.callbacks = (litellm.callbacks or []) + [_make_async_logger()]
        except Exception:
            pass  # async coverage is best-effort; sync path still records
        return True
    except Exception:
        return False
