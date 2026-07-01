"""Robustly pull a JSON block out of LLM output.

Local/open models (Gemma, Qwen, …) frequently prepend a reasoning preamble
("Thinking Process: …") and/or wrap the payload in ```json fences before the
actual JSON. Naive ``json.loads(raw)`` then fails at char 0. Gateway models
(Gemini) usually return clean JSON, so extraction is a no-op there — this keeps
BOTH the local and gateway paths working from one code path.
"""
import json
import re


def _balanced_spans(s: str):
    """Yield (start, end) of every top-level {...} / [...] span in ``s``,
    respecting strings/escapes so braces inside quotes don't miscount."""
    depth = 0
    start = -1
    in_str = False
    esc = False
    opener = ""
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            if depth == 0:
                start = i
                opener = ch
            depth += 1
        elif ch in "}]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    yield (start, i + 1)
                    start = -1


def extract_json(text: str) -> str:
    """Return a parseable JSON substring from ``text`` (unparsed string).

    Handles ```json fences, a reasoning/"Thinking Process:" preamble (whose
    prose may itself contain stray braces), and trailing prose. Strategy:
    prefer a fenced block; otherwise scan for balanced top-level JSON spans and
    return the LAST one that actually parses (models emit the final answer
    last). No-op for already-clean JSON, so gateway + local both work.
    """
    if not text:
        return ""
    s = text.strip()

    # 1) Prefer a fenced block if its contents parse.
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", s, re.DOTALL):
        block = m.group(1).strip()
        try:
            json.loads(block)
            return block
        except Exception:
            pass

    # 2) Last balanced span that parses.
    last_valid = ""
    for a, b in _balanced_spans(s):
        cand = s[a:b]
        try:
            json.loads(cand)
            last_valid = cand
        except Exception:
            continue
    if last_valid:
        return last_valid

    # 3) Nothing parseable — return stripped text so the caller raises clearly.
    return s
