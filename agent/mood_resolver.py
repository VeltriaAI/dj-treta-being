"""LLM mood resolver — normalize free-form mood strings into structured profiles.

Today a user types "BollyAffro" / "bolly afro" / "Bolly-Afro" and downstream
SQL/substring checks silently fail to match the library's "bollyafro" tag,
causing wrong-genre playback. This module fixes that at a single choke point:

    profile = resolve_mood("BollyAffro")
    profile.canonical_slug      # "bollyafro"
    profile.bpm_range           # (115, 125)
    profile.energy_range        # (6, 8)
    profile.vibe_keywords       # ["punjabi", "afro", "vocal", "danceable"]
    profile.confidence          # 0.95

Session fires a callback on every session.mood write; the callback runs
resolve_mood() in a worker thread and writes the result back to
session.mood_profile. Planner/DJ/Library read mood_profile instead of the
raw string.

Cached in SQLite mood_profile_cache keyed on (raw_lower, resolver_version)
so repeated resolutions are free. Phase 3.6 will add discogs_primary_genre +
discogs_subgenres once the Discogs genre reference ships.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("dj-treta")

# Bump on any breaking change to prompt, schema, or defaults. Older cached
# profiles with a different version are ignored (forces re-resolve).
# v2: added discogs_primary_genre + discogs_subgenres (Phase 3.6).
RESOLVER_VERSION = "v2"

_DISCOGS_REFERENCE_PATH = (
    Path(__file__).parent / "knowledge" / "reference" / "discogs_genres.json"
)


def _load_discogs_reference() -> dict:
    """Load the static Discogs genre/subgenre reference. Cached once."""
    try:
        return json.loads(_DISCOGS_REFERENCE_PATH.read_text())
    except Exception as exc:
        log.warning(f"Discogs reference load failed: {exc}")
        return {"primary_genres": [], "subgenres": {}}


_DISCOGS_CACHE: Optional[dict] = None


def _discogs_reference() -> dict:
    global _DISCOGS_CACHE
    if _DISCOGS_CACHE is None:
        _DISCOGS_CACHE = _load_discogs_reference()
    return _DISCOGS_CACHE


@dataclass
class MoodProfile:
    """Structured representation of a DJ mood / set intent."""

    raw: str                                      # original user input
    canonical_slug: str                           # "bollyafro", "melodic-techno"
    bpm_range: tuple                              # (low, high) — typical BPM
    energy_range: tuple                           # (low, high) — 1-10 scale
    vibe_keywords: list                           # evocative adjectives
    confidence: float                             # 0.0-1.0
    resolved_at: float = 0.0
    resolver_version: str = RESOLVER_VERSION

    # Phase 3.6 additions (populated when Discogs reference is available).
    discogs_primary_genre: Optional[str] = None
    discogs_subgenres: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # tuples are JSON-serialized as lists; keep as-is.
        d["bpm_range"] = list(self.bpm_range)
        d["energy_range"] = list(self.energy_range)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "MoodProfile":
        return cls(
            raw=data.get("raw", ""),
            canonical_slug=data.get("canonical_slug", ""),
            bpm_range=tuple(data.get("bpm_range", (120, 128))),
            energy_range=tuple(data.get("energy_range", (5, 8))),
            vibe_keywords=list(data.get("vibe_keywords", [])),
            confidence=float(data.get("confidence", 0.0)),
            resolved_at=float(data.get("resolved_at", 0.0)),
            resolver_version=str(data.get("resolver_version", RESOLVER_VERSION)),
            discogs_primary_genre=data.get("discogs_primary_genre"),
            discogs_subgenres=list(data.get("discogs_subgenres", [])),
        )


def _fallback_profile(raw: str) -> MoodProfile:
    """Heuristic when LLM is unreachable.

    Returns a low-confidence profile using the lowercased raw string as slug.
    Downstream code can check confidence < 0.5 and decide whether to trust it.
    """
    slug = raw.strip().lower().replace(" ", "-").replace("_", "-") or "unknown"
    return MoodProfile(
        raw=raw,
        canonical_slug=slug,
        bpm_range=(120, 128),
        energy_range=(5, 8),
        vibe_keywords=[],
        confidence=0.0,
        resolved_at=time.time(),
    )


def resolve_mood(raw: str) -> MoodProfile:
    """Resolve a raw mood string into a MoodProfile.

    Order of operations:
      1. Empty input → low-confidence "unknown" profile (no LLM call, no cache write).
      2. Cache lookup in mood_profile_cache. Hit → return cached.
      3. LLM call. Success → cache + return. Failure → fallback + cache fallback
         (prevents retry storms if LLM is persistently down).
    """
    raw = (raw or "").strip()
    if not raw:
        return _fallback_profile("")

    cached = _get_cached(raw)
    if cached is not None:
        return cached

    profile = _llm_resolve(raw)
    _cache_profile(raw, profile)
    return profile


def _llm_resolve(raw: str) -> MoodProfile:
    """Make one LLM call to resolve the raw mood. On failure, return fallback."""
    ref = _discogs_reference()
    primary_list = ", ".join(ref.get("primary_genres") or [])
    # Flatten subgenres into a single list the LLM can match against.
    all_subs: list = []
    for g, subs in (ref.get("subgenres") or {}).items():
        all_subs.extend(subs)
    subs_list = ", ".join(sorted(set(all_subs))[:80])  # cap prompt size

    prompt = f"""You normalize DJ mood / genre strings into structured profiles.
Return STRICT JSON only — no prose, no markdown fences.

Input mood: "{raw}"

Output schema (all fields REQUIRED):
{{
  "canonical_slug": "lowercase-hyphen-separated identifier",
  "bpm_range": [low_int, high_int],
  "energy_range": [low_int, high_int],
  "vibe_keywords": ["3-5 evocative words"],
  "discogs_primary_genre": "one of the primary genres below, or null",
  "discogs_subgenres": ["up to 3 from the subgenres list, or []"],
  "confidence": 0.0-1.0
}}

Available Discogs primary genres: {primary_list}.
Available Discogs subgenres (pick 1-3 most relevant): {subs_list}.

Rules:
- canonical_slug: normalize typos, case, and whitespace. "BollyAffro" → "bollyafro".
  "MELODIC TECHNO" → "melodic-techno". "Deep_Chill" → "deep-chill".
- discogs_primary_genre: MUST be one of the primary genres above, or null if
  genuinely no fit.
- discogs_subgenres: MUST be picked from the subgenre list above. Never invent.
  Order by relevance (most specific first).
- bpm_range: typical range. Afro House ~115-125. Melodic techno ~120-125.
  Psytrance ~138-142. Drum'n'bass ~170-180. Ambient ~60-90.
- energy_range: 1=ambient, 3=lounge, 5=warmup, 7=prime time, 10=festival peak.
- vibe_keywords: terms a DJ would use to describe the feel.
- If raw is gibberish or highly ambiguous, return best-guess with confidence < 0.5.

Examples:
  Input: "BollyAffro"
  → {{"canonical_slug": "bollyafro", "bpm_range": [115, 125], "energy_range": [6, 8],
      "vibe_keywords": ["punjabi", "afro", "vocal", "danceable"],
      "discogs_primary_genre": "Electronic",
      "discogs_subgenres": ["Afro House", "Bollywood House"], "confidence": 0.95}}

  Input: "melodic techno"
  → {{"canonical_slug": "melodic-techno", "bpm_range": [120, 125], "energy_range": [6, 9],
      "vibe_keywords": ["atmospheric", "driving", "emotive", "hypnotic"],
      "discogs_primary_genre": "Electronic",
      "discogs_subgenres": ["Melodic Techno", "Techno"], "confidence": 0.97}}

  Input: "reggaeton peak hour"
  → {{"canonical_slug": "reggaeton", "bpm_range": [90, 100], "energy_range": [7, 9],
      "vibe_keywords": ["latin", "bouncy", "vocal"],
      "discogs_primary_genre": "Reggae",
      "discogs_subgenres": ["Reggaeton"], "confidence": 0.95}}

Return JSON for the input above."""

    try:
        from .config import load_config
        from litellm import completion as _completion
        cfg = load_config()
        resp = _completion(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": prompt}],
            api_base=cfg.llm.api_base, api_key=cfg.llm.api_key,
            temperature=0.1, timeout=15,
        )
        from .billing_rates import bill_from_response
        bill_from_response(resp, "dj_treta")
        from .json_extract import extract_json
        data = json.loads(extract_json(resp.choices[0].message.content or ""))
    except Exception as exc:
        log.warning(
            f"Mood resolver LLM failed for {raw!r} "
            f"({type(exc).__name__}): {exc} — using fallback"
        )
        return _fallback_profile(raw)

    # Defensive parsing — never let bad LLM output crash the caller.
    try:
        bpm = data.get("bpm_range") or [120, 128]
        energy = data.get("energy_range") or [5, 8]

        # Validate Discogs fields against reference — never let an LLM
        # invented value leak through.
        ref = _discogs_reference()
        valid_primaries = set(ref.get("primary_genres") or [])
        all_valid_subs = set()
        for subs in (ref.get("subgenres") or {}).values():
            all_valid_subs.update(subs)

        primary = data.get("discogs_primary_genre")
        if primary and primary not in valid_primaries:
            log.debug(f"Mood resolver: primary genre {primary!r} not in reference; dropping")
            primary = None

        subs_raw = data.get("discogs_subgenres") or []
        if not isinstance(subs_raw, list):
            subs_raw = []
        subs = [s for s in subs_raw if isinstance(s, str) and s in all_valid_subs][:3]

        return MoodProfile(
            raw=raw,
            canonical_slug=str(data.get("canonical_slug") or raw.lower()).strip(),
            bpm_range=(int(bpm[0]), int(bpm[1])),
            energy_range=(int(energy[0]), int(energy[1])),
            vibe_keywords=list(data.get("vibe_keywords") or []),
            confidence=float(data.get("confidence") or 0.5),
            resolved_at=time.time(),
            discogs_primary_genre=primary,
            discogs_subgenres=subs,
        )
    except Exception as exc:
        log.warning(f"Mood resolver: bad LLM JSON ({exc}) — using fallback")
        return _fallback_profile(raw)


# ── SQLite cache ──────────────────────────────────────────────────────

def _get_cached(raw: str) -> Optional[MoodProfile]:
    from .db import get_db
    try:
        db = get_db()
        try:
            row = db.execute(
                "SELECT profile_json FROM mood_profile_cache "
                "WHERE raw_mood_lower=? AND resolver_version=?",
                (raw.lower(), RESOLVER_VERSION),
            ).fetchone()
            if row:
                return MoodProfile.from_dict(json.loads(row["profile_json"]))
        finally:
            db.close()
    except Exception as exc:
        # Table might not exist on very old DB; treat as cache miss.
        log.debug(f"Mood cache read miss: {exc}")
    return None


def _cache_profile(raw: str, profile: MoodProfile) -> None:
    from .db import get_db
    try:
        db = get_db()
        try:
            db.execute(
                "INSERT OR REPLACE INTO mood_profile_cache "
                "(raw_mood_lower, profile_json, resolved_at, resolver_version) "
                "VALUES (?, ?, ?, ?)",
                (
                    raw.lower(),
                    json.dumps(profile.to_dict()),
                    profile.resolved_at,
                    profile.resolver_version,
                ),
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        log.warning(f"Mood cache write failed for {raw!r}: {exc}")


def clear_cache() -> None:
    """Wipe the mood_profile_cache (for tests / manual re-resolve)."""
    from .db import get_db
    db = get_db()
    try:
        db.execute("DELETE FROM mood_profile_cache")
        db.commit()
    finally:
        db.close()
