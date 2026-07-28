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
    # gateway.infrax.ai serves gemini-flash (3.5-flash) — her ACTUAL cloud brain.
    # (The old "gemini-3-flash" alias lived on the retired localhost:4000 proxy.)
    "flash": "openai/gemini-flash",
    "flash-gw": "openai/gemini-flash",
    "pro": "openai/gemini-pro",
    # Local model via Ollama. Run with:
    #   EVAL_MODEL=gemma4 LITELLM_API_BASE=http://localhost:11434 pytest ...
    "gemma4": "ollama_chat/gemma4:e2b-it-qat",
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
    # Ollama-hosted models (e.g. gemma4 QAT) default to slow "thinking" mode;
    # think=False cuts latency ~3x. API_BASE must point at ollama for these
    # (set LITELLM_API_BASE=http://localhost:11434 at run time). flash/pro
    # paths are unchanged (still hit the LiteLLM proxy on :4000).
    extra_kwargs = {}
    if "ollama" in model:
        extra_kwargs["think"] = False
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
        **extra_kwargs,
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


def text_contains(result: dict, *keywords: str) -> bool:
    """Check that the text response contains all given keywords (case-insensitive)."""
    text = result["text"].lower()
    return all(k.lower() in text for k in keywords)


# Aliases / convenience wrappers expected by tests/eval_transition_scenarios.py.
# Originally lived in main but didn't make it through the v9 merge cleanly —
# adding here so the eval suite collects.

def eval_agent_nonempty(*args, **kwargs):
    """Run eval_agent and assert the result has either a tool call or non-empty text."""
    result = eval_agent(*args, **kwargs)
    assert (result.get("tool_calls") or (result.get("text") or "").strip()), (
        f"Agent returned empty response: {result!r}"
    )
    return result


def assert_technique_acceptable(picked, expected=None, alternatives=None, rejected=None) -> None:
    """Assert the DJ's chosen transition technique is acceptable for a scenario.

    picked:       the technique string the DJ actually scheduled.
    expected:     the ideal Technique (enum) — or None if a wait was expected.
    alternatives: also-acceptable Techniques (enum list).
    rejected:     Techniques that must NOT be used.

    Rejected is always enforced; the acceptable set (expected + alternatives)
    is enforced only when defined.
    """
    def _v(t):
        return (t.value if hasattr(t, "value") else str(t)).lower()

    picked_l = _v(picked)
    rejected_l = {_v(t) for t in (rejected or [])}
    assert picked_l not in rejected_l, (
        f"Technique {picked!r} is explicitly rejected for this scenario {sorted(rejected_l)}"
    )
    acceptable = set()
    if expected is not None:
        acceptable.add(_v(expected))
    acceptable |= {_v(t) for t in (alternatives or [])}
    if acceptable:
        assert picked_l in acceptable, (
            f"Technique {picked!r} not in acceptable set {sorted(acceptable)}"
        )


def assert_in_range(value, low, high, label: str = "value") -> None:
    """Assert ``low <= value <= high`` with a clear failure message."""
    assert low <= value <= high, f"{label}={value} outside range [{low}, {high}]"
