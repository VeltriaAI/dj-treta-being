"""LLM call wrapper for DJ Treta eval tests.

Each eval is ONE LLM call: system prompt + user message + tools -> assert on response.
Uses LiteLLM proxy for model access.

Supports multi-model testing: set EVAL_MODEL env var or use --eval-model pytest flag.
Supports retry for flaky tests: eval_agent_retry() runs N trials, passes if K succeed.
"""

import json
import os
import time
from typing import Optional

from litellm import completion


# ── Config ───────────────────────────────────────────────────────────────

MODELS = {
    "flash": "openai/gemini-3-flash",
    "flash3": "openai/gemini-3-flash-preview",
    "pro": "openai/gemini-3.1-pro",
    "flash25": "openai/gemini-3-flash-25",
    "gemma4": "openai/gemma-4",
}

DEFAULT_MODEL = MODELS.get(
    os.environ.get("EVAL_MODEL", "flash"),
    "openai/gemini-3-flash",
)
API_BASE = os.environ.get("LITELLM_API_BASE", "http://localhost:4000")
API_KEY = os.environ.get(
    "LITELLM_API_KEY",
    os.environ.get("DJTRETA_LLM_API_KEY", "sk-test"),
)


# ── Core ─────────────────────────────────────────────────────────────────

def eval_agent(
    system_prompt: str,
    user_message: str,
    tools: list[dict],
    model: str = DEFAULT_MODEL,
) -> dict:
    """Call LLM with prompt and tools, return structured result."""
    t0 = time.time()
    response = completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        tools=tools if tools else None,
        temperature=0,
        api_base=API_BASE,
        api_key=API_KEY,
    )
    elapsed = time.time() - t0
    msg = response.choices[0].message
    tool_calls = []
    for tc in (msg.tool_calls or []):
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            args = {}
        tool_calls.append({"name": tc.function.name, "args": args})

    return {
        "text": msg.content or "",
        "tool_calls": tool_calls,
        "elapsed_s": round(elapsed, 2),
        "model": model,
    }


def eval_agent_nonempty(
    system_prompt: str,
    user_message: str,
    tools: list,
    trials: int = 5,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Wrapper around eval_agent that retries when Gemini Flash returns an
    empty response (silent drop).

    Flash has a measurable ~60% empty-response rate on certain niche
    prompts (6-step Camelot clash, atmospheric progressive house, dramatic
    genre switch). When it does respond, the answer is usually correct.
    This wrapper returns the first non-empty result within `trials`
    attempts. If all trials drop, returns the last result so the test's
    downstream asserts can produce an informative failure.

    Prob of green with 5 trials + 60% empty rate: 1 - 0.6^5 = 92.2%.
    """
    last = None
    for _ in range(max(1, trials)):
        result = eval_agent(system_prompt, user_message, tools, model)
        last = result
        has_text = bool((result.get("text") or "").strip())
        has_calls = bool(result.get("tool_calls"))
        if has_text or has_calls:
            return result
    return last


def eval_agent_retry(
    system_prompt: str,
    user_message: str,
    tools: list[dict],
    check_fn,
    trials: int = 3,
    required_passes: int = 2,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Run eval multiple times, pass if check_fn succeeds on enough trials.

    Args:
        check_fn: callable(result) -> bool. Returns True if the result passes.
        trials: number of attempts.
        required_passes: minimum passes needed.

    Returns the last result. Raises AssertionError if not enough passes.
    """
    passes = 0
    last_result = None
    for i in range(trials):
        result = eval_agent(system_prompt, user_message, tools, model)
        last_result = result
        try:
            if check_fn(result):
                passes += 1
        except (AssertionError, Exception):
            pass
        if passes >= required_passes:
            return last_result

    assert passes >= required_passes, (
        f"Flaky: passed {passes}/{trials} trials (need {required_passes}). "
        f"Last result: tools={[tc['name'] for tc in last_result['tool_calls']]}, "
        f"text={last_result['text'][:200]}"
    )
    return last_result


# ── Assertion Helpers ────────────────────────────────────────────────────

def has_tool_call(result: dict, name: str) -> bool:
    """Check if result contains a tool call with the given name."""
    return any(tc["name"] == name for tc in result["tool_calls"])


def get_tool_args(result: dict, name: str) -> Optional[dict]:
    """Get arguments for the first tool call matching the given name."""
    for tc in result["tool_calls"]:
        if tc["name"] == name:
            return tc["args"]
    return None


def has_no_tool_calls(result: dict) -> bool:
    """Check that the result has zero tool calls."""
    return len(result["tool_calls"]) == 0


# ── Scenario-driven helpers (transition eval framework) ────────────────


def assert_technique_acceptable(
    picked: str,
    expected: str | None,
    alternatives: list[str],
    rejected: list[str],
) -> None:
    """Pass if `picked` matches expected or an allowed alternative; fail
    if it's in the rejected set. Unknown techniques fail silently as a
    soft warning (catch bad output schemas)."""
    assert picked not in (rejected or []), (
        f"DJ picked rejected technique {picked!r}; "
        f"expected {expected!r} or alternatives {alternatives!r}"
    )
    valid = {expected, *(alternatives or [])} - {None}
    if valid:
        assert picked in valid, (
            f"DJ picked {picked!r}, not in accepted set "
            f"{sorted(v for v in valid if v)}"
        )


def assert_phrase_aligned(
    at_position: float,
    bpm: float,
    section_start: float,
    phrase_beats: int = 32,
    tolerance_beats: float = 1.0,
) -> None:
    """Assert that `at_position - section_start` is a multiple of the
    phrase length within ±tolerance_beats beats. A standard techno phrase
    is 32 beats (8 bars × 4)."""
    phrase_s = (60.0 / bpm) * phrase_beats
    offset = at_position - section_start
    if offset < 0:
        return  # position is before the section we asked about — let other asserts catch
    remainder = offset % phrase_s
    tol_s = (60.0 / bpm) * tolerance_beats
    # remainder close to 0 OR close to full phrase (just before next boundary)
    aligned = remainder <= tol_s or (phrase_s - remainder) <= tol_s
    assert aligned, (
        f"at_position {at_position:.1f}s is not phrase-aligned "
        f"(offset {offset:.1f}s from section start {section_start:.1f}s; "
        f"phrase {phrase_s:.2f}s; remainder {remainder:.2f}s > tolerance {tol_s:.2f}s)"
    )


def assert_in_range(value: float, lo: float, hi: float, label: str) -> None:
    """Inclusive range check with a readable assertion message."""
    assert lo <= value <= hi, (
        f"{label} {value} outside expected range [{lo}, {hi}]"
    )


def text_contains(result: dict, *keywords: str) -> bool:
    """Check that the text response contains all given keywords (case-insensitive)."""
    text = result["text"].lower()
    return all(k.lower() in text for k in keywords)
