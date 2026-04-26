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
    "pro": "openai/gemini-3.1-pro",
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
