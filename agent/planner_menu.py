"""NS-009: Planner candidate menu — code pre-filter for the v8 planner path.

The v8 planner dumps the entire analyzed library into the prompt. With 2,000+
rows that's a ~50K-token prompt — minutes of prompt-eval on a local model.
When ``llm.planner_candidate_cap > 0``, :func:`select_planner_candidates`
shrinks the library to the N most relevant rows ("the menu") before prompt
build:

  (a) mood match first — ``row['mood'] == mood slug``, falling back to a
      genre-string contains match;
  (b) then BPM proximity to the currently playing track's BPM (or the mood
      profile's bpm-range midpoint when nothing is playing);
  (c) Camelot-compatibility bonus with the current track's key (same number
      or ±1 on the wheel, A↔B swap) — a compatible key is worth a few BPM
      of distance;
  (d) titles already played this session are excluded.

Pure function of its inputs; deterministic and stable (ties broken by path).
"""

# A harmonically compatible key is worth this many BPM of distance in the
# ranking score. Conservative — proximity still dominates.
_KEY_BONUS_BPM = 6.0

# Rows with no BPM at all rank behind every real BPM distance.
_MISSING_BPM_PENALTY = 1000.0


def _mood_matches(row: dict, mood_slug: str) -> bool:
    """Mood match: exact mood-slug equality, falling back to genre contains."""
    if not mood_slug:
        return True  # no mood context — everything is a match
    row_mood = (row.get("mood") or "").strip().lower()
    if row_mood == mood_slug:
        return True
    genre = (row.get("genre") or "").strip().lower()
    return bool(genre) and mood_slug.replace("-", " ") in genre


def select_planner_candidates(
    library: list[dict],
    *,
    cap: int,
    mood: str = "",
    current_bpm: float | None = None,
    current_key_camelot: str | None = None,
    played_titles: list[str] | None = None,
    mood_profile: dict | None = None,
) -> list[dict]:
    """Return the ≤`cap` best planner candidates from `library`.

    `cap <= 0` (or a library already within the cap) returns the input list
    unchanged — the off switch that preserves today's behavior.
    """
    if cap <= 0 or len(library) <= cap:
        return list(library)

    mood_slug = (mood or "").strip().lower().replace(" ", "-")

    # BPM reference: current track's bpm when known, else mood-profile
    # bpm-range midpoint, else none (bpm proximity becomes a no-op).
    ref_bpm: float | None = None
    if current_bpm:
        try:
            ref_bpm = float(current_bpm)
        except (TypeError, ValueError):
            ref_bpm = None
    if ref_bpm is None and mood_profile:
        rng = mood_profile.get("bpm_range") or []
        if len(rng) == 2:
            try:
                ref_bpm = (float(rng[0]) + float(rng[1])) / 2.0
            except (TypeError, ValueError):
                ref_bpm = None

    compatible_keys: set[str] = set()
    if current_key_camelot:
        from .camelot import get_compatible_keys
        compatible_keys = set(get_compatible_keys(current_key_camelot))

    played = {(t or "").strip().lower() for t in (played_titles or []) if t}

    scored: list[tuple[int, float, str, dict]] = []
    for row in library:
        title = (row.get("title") or "").strip().lower()
        if title and title in played:
            continue

        mood_rank = 0 if _mood_matches(row, mood_slug) else 1

        if ref_bpm is not None:
            bpm = row.get("bpm")
            try:
                bpm_dist = abs(float(bpm) - ref_bpm) if bpm else _MISSING_BPM_PENALTY
            except (TypeError, ValueError):
                bpm_dist = _MISSING_BPM_PENALTY
        else:
            bpm_dist = 0.0

        key = (row.get("key_camelot") or "").strip().upper()
        score = bpm_dist - (_KEY_BONUS_BPM if key and key in compatible_keys else 0.0)

        scored.append((mood_rank, score, row.get("path") or "", row))

    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return [row for _, _, _, row in scored[:cap]]
