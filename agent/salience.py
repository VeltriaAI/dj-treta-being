"""Pure wake-salience scoring for Notebook events — DJ Treta v11 Phase 2.

This module answers ONE question: *is this notebook event worth waking the
Being off-cadence for?* It is a **pure scorer** — a stack of small, named
predicates over a single event dict, plus a threshold check. It owns NO state,
holds NO locks, touches NO session / notebook / mixxx / disk. Feed it an event,
get a 0..1 wake-salience back. That is the whole contract.

Why a separate module (per `docs/V11_BUILD_PLAN.md` Phase 2): the autonomous
wake fires on high-salience appends — crowd-collapse, drop-landed, skip-burst,
human directive, contradiction. Keeping the *decision* of "high-salience" as a
pure function means it is trivially unit-testable, has zero blast radius, and
can be wired (or not) entirely from `main.py` behind `config.evolution.enabled`.

  - `score_event(event)` → 0..1 wake-salience.
  - `is_wake_worthy(event)` → `score_event(event) >= WAKE_THRESHOLD`.

Event schema (from `agent/notebook.py`, append-only JSONL row):
    {seq, ts, author, kind, payload, salience, confidence, event_id, dedup_key?}
  kind ∈ {percept, decision, transition, claim, directive,
          generated_track, reflection}

Scoring shape (deliberately simple, so it stays auditable):
    base   = event["salience"]              # the writer's own floor (default 0.3)
    base  += sum(evidence from wake triggers that match this event)
    score  = clamp(base * event["confidence"], 0, 1)

Evidence is ADDITIVE and capped by the final clamp — a directive that ALSO
looks like a contradiction simply saturates toward 1.0, which is the desired
behaviour (more reasons to wake → wake). Confidence MULTIPLIES last, so a
low-confidence high-evidence event is correctly damped.

CRITICAL — music-never-stops is NOT this module's concern. P1 (silence) and
P2 (emergency-load) priorities sit ABOVE wake and are NEVER routed through any
salience check or veto. This scorer only ranks *off-cadence wake* candidates;
it can never starve audio. The suppressor (`agent/suppressor.py`) applies the
vetoes; this module only measures pull.
"""

from __future__ import annotations

from typing import Any

# ── Frozen contract ─────────────────────────────────────────────────────

#: An event must reach this wake-salience to pull the Being off-cadence.
#: Tuned so a clear human directive or a confident high-signal percept clears
#: it, while routine percepts/decisions (room-sense storm, ordinary loads) do
#: not. Single source of truth; `is_wake_worthy` reads it.
WAKE_THRESHOLD: float = 0.70

# ── Evidence weights (per wake trigger named in the build plan) ──────────
#
# Each is the bump ADDED to the event's own salience when its predicate fires.
# Sized so any single strong trigger, starting from a typical writer salience
# (~0.3–0.6), clears WAKE_THRESHOLD on its own at full confidence — but a bare
# percept with no trigger does not.

# Sized so ANY single named trigger clears WAKE_THRESHOLD even from a bare
# percept floor (0.2) at full confidence — these are the high-signal wake
# triggers the plan names; a room-sense collapse or a listener skip-burst MUST
# pull a wake on its own. Drop-landed is intentionally lighter (a good moment,
# not an emergency) — it clears from a transition's normal 0.5 salience but not
# from a bare 0.2 floor. Multi-trigger events saturate toward 1.0 (more reasons
# to wake → wake), which the final clamp handles.
_W_HUMAN_DIRECTIVE = 0.55   # a human spoke — almost always worth a wake
_W_CONTRADICTION = 0.55     # a claim conflicts with another — resolve it
_W_CROWD_COLLAPSE = 0.55    # the room's energy fell off a cliff
_W_SKIP_BURST = 0.55        # listeners are bailing — the read is wrong
_W_DROP_LANDED = 0.30       # a drop just hit — a moment to ride / react

# Substrings that, in a textual payload, signal each trigger. Matched
# case-insensitively against a flattened lower-cased view of the payload.
_DIRECTIVE_AUTHOR_PREFIXES = ("manish", "user", "human")
_CONTRADICTION_TOKENS = ("contradict", "conflict", "disagree", "but actually",
                         "inconsistent", "mismatch", "doesn't match",
                         "does not match")
_COLLAPSE_TOKENS = ("breakdown", "collapse", "energy fell", "energy dropped",
                    "dead", "emptying", "crowd left", "→ silence",
                    "-> silence", "to silence", "flatline")
_SKIP_TOKENS = ("skip", "skipped", "next track please", "bailed", "left")
_DROP_TOKENS = ("drop", "dropped", "the drop", "drop hit", "drop land",
                "bass drop")
_DROP_TECHNIQUES = ("drop", "double-drop", "double drop", "slam", "cut-in",
                    "hard cut", "impact")


# ── Helpers: flatten the payload to a searchable lower-cased string ──────

def _payload_text(event: dict) -> str:
    """Flatten an event's payload (+author/kind) to one lower-cased string.

    Payloads are arbitrary JSON (dict / list / str / number). We don't care
    about structure for keyword evidence — we just want a haystack to scan.
    `author` and `kind` are folded in so a predicate can key off them too.
    Pure; never raises (a weird payload just yields a partial string).
    """
    parts: list[str] = []
    parts.append(str(event.get("author", "")))
    parts.append(str(event.get("kind", "")))

    payload = event.get("payload")
    try:
        _flatten(payload, parts)
    except Exception:
        # Defensive: a pathological payload (e.g. a self-referential object)
        # must never make a pure scorer raise. Fall back to its repr.
        parts.append(repr(payload))
    return " ".join(parts).lower()


def _flatten(obj: Any, out: list[str]) -> None:
    """Recursively append the leaf strings/scalars of `obj` to `out`."""
    if obj is None:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            _flatten(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            _flatten(item, out)
    else:
        out.append(str(obj))


def _has_any(text: str, tokens) -> bool:
    """True if any token substring is present in `text` (already lower-cased)."""
    return any(tok in text for tok in tokens)


def _payload_get(event: dict, key: str, default: Any = None) -> Any:
    """Read `key` from a dict payload, else `default`. Pure, never raises."""
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload.get(key, default)
    return default


# ── Wake-trigger predicates (one per high-signal trigger in the plan) ─────

def _is_human_directive(event: dict, text: str) -> bool:
    """A human told her to do something.

    Fires when the event is an explicit directive kind, OR the author is a
    human (manish / user / human ...). Authored-by-human is the strongest
    real-world signal — Manish saying anything mid-set should pierce.
    """
    if event.get("kind") == "directive":
        return True
    author = str(event.get("author", "")).lower()
    return author.startswith(_DIRECTIVE_AUTHOR_PREFIXES)


def _is_contradiction(event: dict, text: str) -> bool:
    """A claim conflicts with another claim / the current read.

    Either an explicit flag on a dict payload (`conflict`/`contradicts`), or a
    `claim`-kind event whose text says it disagrees with something.
    """
    if _payload_get(event, "conflict") or _payload_get(event, "contradicts"):
        return True
    if event.get("kind") == "claim" and _has_any(text, _CONTRADICTION_TOKENS):
        return True
    # A non-claim event can still narrate a contradiction in its payload text.
    return _has_any(text, _CONTRADICTION_TOKENS)


def _is_crowd_collapse(event: dict, text: str) -> bool:
    """Room energy fell off a cliff (breakdown → silence, crowd emptying).

    Reads room-sense structure when present (a hard energy fall / breakdown
    flag), and falls back to collapse keywords in the payload text.
    """
    # Structured room-sense: a steep negative energy delta, or a breakdown
    # flag, both signal collapse. Tolerant of either flat or nested shape.
    energy_delta = _payload_get(event, "energy_delta")
    if isinstance(energy_delta, (int, float)) and energy_delta <= -0.35:
        return True
    if _payload_get(event, "breakdown") and _payload_get(event, "energy", 1.0) is not None:
        # breakdown flag + a low/falling energy reading
        energy = _payload_get(event, "energy", 1.0)
        if isinstance(energy, (int, float)) and energy <= 0.25:
            return True
    return _has_any(text, _COLLAPSE_TOKENS)


def _is_skip_burst(event: dict, text: str) -> bool:
    """Multiple skips in quick succession — listeners rejecting the read.

    Either a numeric skip count >= 2 on the payload, or a listener-authored
    event whose text mentions skipping. One skip is noise; a burst is signal.
    """
    skip_count = _payload_get(event, "skip_count")
    if isinstance(skip_count, (int, float)) and skip_count >= 2:
        return True
    skips = _payload_get(event, "skips")
    if isinstance(skips, (list, tuple)) and len(skips) >= 2:
        return True
    author = str(event.get("author", "")).lower()
    if author.startswith("listener") and _has_any(text, _SKIP_TOKENS):
        return True
    return False


def _is_drop_landed(event: dict, text: str) -> bool:
    """A transition just landed a drop — a live moment to ride / react to.

    Fires on a `transition`-kind event whose technique / payload implies a
    drop. Lower weight than the failure triggers: a landed drop is a good
    moment to engage, not an emergency.
    """
    if event.get("kind") != "transition":
        return False
    technique = str(_payload_get(event, "technique", "")).lower()
    if _has_any(technique, _DROP_TECHNIQUES):
        return True
    return _has_any(text, _DROP_TOKENS)


# ── Public scorer ────────────────────────────────────────────────────────

def score_event(event: dict) -> float:
    """Return a 0..1 wake-salience for one notebook event.

    Starts from the event's own `salience` (default 0.3 — matches Notebook's
    neutral floor), ADDS evidence for each matched wake trigger, MULTIPLIES by
    the writer's `confidence` (default 1.0), and clamps to [0, 1].

    Pure: no I/O, no state, never raises. A non-dict / empty event scores the
    neutral floor.
    """
    if not isinstance(event, dict):
        return 0.3

    # Base = the writer's own salience floor (Notebook defaults this to 0.3
    # for unknown kinds, 0.2 for percepts, 0.6 for directives, etc.).
    base = event.get("salience", 0.3)
    try:
        base = float(base)
    except (TypeError, ValueError):
        base = 0.3

    text = _payload_text(event)

    # Additive evidence from each independent wake trigger. Order is irrelevant
    # (pure addition); the final clamp saturates a multi-trigger event toward 1.
    evidence = 0.0
    if _is_human_directive(event, text):
        evidence += _W_HUMAN_DIRECTIVE
    if _is_contradiction(event, text):
        evidence += _W_CONTRADICTION
    if _is_crowd_collapse(event, text):
        evidence += _W_CROWD_COLLAPSE
    if _is_skip_burst(event, text):
        evidence += _W_SKIP_BURST
    if _is_drop_landed(event, text):
        evidence += _W_DROP_LANDED

    # Confidence damps the whole signal: a low-confidence high-evidence event
    # should NOT wake. Default 1.0 (full trust) when the writer omits it.
    confidence = event.get("confidence", 1.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 1.0

    score = (base + evidence) * confidence
    return _clamp01(score)


def is_wake_worthy(event: dict) -> bool:
    """True iff `event` clears the wake threshold (off-cadence wake candidate)."""
    return score_event(event) >= WAKE_THRESHOLD


def _clamp01(x: float) -> float:
    """Clamp `x` into [0.0, 1.0]."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x
