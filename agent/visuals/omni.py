"""E6 — Gemini Omni visual generation prototype.

Calls Gemini's multimodal / video-generation capability via the
existing LiteLLM gateway to produce a short reactive visual concept
(or rendered video clip) from the current audio context.

STATUS: DOCUMENTED STUB + PARTIAL PROTOTYPE
-----------------------------------------
Gemini Omni (model id "gemini-omni-flash" or the video-generation
preview) may not yet be publicly accessible.  This module:

1. Tries the model with graceful degradation — if the API returns a
   404 / model-not-found / 501 it catches the exception, logs a clear
   TODO, and returns a stub response dict so the caller (VisualEngine)
   never crashes.
2. Exposes the same ``generate_visual()`` coroutine regardless —
   integrator flips ``config.visuals.omni.enabled = true`` and
   optionally sets ``config.visuals.omni.model`` to the correct ID
   when access is confirmed.

How to enable
-------------
1. Confirm the model ID with Google — likely one of:
     - "gemini-omni-flash"           (if available through LiteLLM)
     - "gemini-2.5-pro-preview"      (known multimodal; no video gen yet)
     - "imagen-video-2"              (Vertex AI video generation)
     - "veo-2"                       (Vertex/Gemini video gen, 2026)
2. Update config.yaml:
       visuals:
         omni:
           enabled: true
           model: "gemini-omni-flash"   # or correct ID
3. Ensure your LiteLLM gateway (localhost:4000) has a route for
   the chosen model; for Veo/Imagen-Video use Vertex AI pass-through.
4. Flip ``config.visuals.omni.enabled = true`` and restart the daemon.

Prompt contract
---------------
Input:  A VisualPromptContext dataclass built from current track state.
Output: A dict::
    {
        "ok": bool,
        "concept": str,       # Short text description of the visual
        "video_url": str,     # URL/path to a generated clip (if supported)
        "palette": str,       # Palette name used (echoed for convenience)
        "stub": bool,         # True if model was unavailable
        "error": str | None,  # Error string if ok=False
    }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("dj-treta.visuals.omni")

# Try importing google-genai (already a project dependency per pyproject.toml)
try:
    import google.generativeai as genai  # type: ignore
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False
    log.debug("google-generativeai not importable; Omni will stub all calls")


# ---------------------------------------------------------------------------
# Prompt context
# ---------------------------------------------------------------------------

@dataclass
class VisualPromptContext:
    """All the audio/session context needed to build a visual brief."""

    bpm: float = 128.0
    energy: int = 7
    section: str = "drop"          # intro|breakdown|buildup|drop|outro|main
    key: str = "Am"
    genre: str = "melodic-techno"
    palette: str = "Horizon"
    track_title: str = ""
    artist: str = ""
    mood: str = ""
    # Extra freeform context (e.g. listener emoji, directive)
    extra: str = ""


def _build_prompt(ctx: VisualPromptContext) -> str:
    """Construct the text prompt sent to Omni."""
    section_desc = {
        "intro":     "the track is just beginning — quiet, expectant energy",
        "breakdown": "a melodic breakdown — stripped back, emotional, floating",
        "buildup":   "tension building — rising synths, snare rolls, anticipation",
        "drop":      "the drop — maximum energy, full kick, bass hits hard",
        "outro":     "the track is fading — resolution, breath, release",
        "main":      "a sustained groove — steady energy, hypnotic",
    }.get(ctx.section.lower(), "a moment in the mix")

    track_line = ""
    if ctx.track_title:
        artist_part = f" by {ctx.artist}" if ctx.artist else ""
        track_line = f'Track: "{ctx.track_title}"{artist_part}.\n'

    return (
        f"You are a live visual director for an AI DJ named Treta who is "
        f"performing at a club right now.\n\n"
        f"Current audio state:\n"
        f"  Genre: {ctx.genre}\n"
        f"  BPM: {ctx.bpm:.1f}\n"
        f"  Musical key: {ctx.key}\n"
        f"  Energy level: {ctx.energy}/10\n"
        f"  Section: {ctx.section} — {section_desc}\n"
        f"  Visual palette: {ctx.palette}\n"
        f"{track_line}"
        f"{'  Mood: ' + ctx.mood + chr(10) if ctx.mood else ''}"
        f"{'  Note: ' + ctx.extra + chr(10) if ctx.extra else ''}"
        f"\n"
        f"Generate a 5-10 second reactive visual concept that matches this "
        f"exact moment.  Describe the visuals in one vivid paragraph, then "
        f"output a JSON object (on a new line) with these fields:\n"
        f'  {{"concept": "<one-sentence visual concept>", '
        f'"motion": "<slow|pulse|surge|burst|breathe>", '
        f'"dominant_color": "<hex>", '
        f'"bg_color": "<hex>"}}\n'
        f"\n"
        f"Make the visuals reactive to the energy and section.  "
        f"At a drop, visuals should be high-contrast and kinetic.  "
        f"At a breakdown, soft and abstract.  "
        f"Always reflect the palette: {ctx.palette}."
    )


# ---------------------------------------------------------------------------
# Model IDs to try, in order of preference
# ---------------------------------------------------------------------------
_MODEL_CANDIDATES = [
    "gemini-omni-flash",
    "gemini-2.5-flash-preview",   # known reachable; no video gen but has creative output
    "gemini-2.0-flash",
    "gemini-1.5-flash",           # last-resort fallback
]

# Response when the model is unavailable / access not yet granted
_STUB_RESPONSE: dict = {
    "ok": False,
    "concept": "",
    "video_url": "",
    "palette": "",
    "stub": True,
    "error": (
        "Gemini Omni is not yet reachable from this installation.  "
        "To enable: (1) confirm the correct model ID with Google/Vertex, "
        "(2) set config.visuals.omni.model and .enabled=true, "
        "(3) ensure your LiteLLM gateway has a route for that model ID.  "
        "See agent/visuals/omni.py for full setup instructions."
    ),
}


async def generate_visual(
    ctx: VisualPromptContext,
    model: str = "",
    api_key: str = "",
    api_base: str = "",
) -> dict:
    """Generate a visual concept for the given audio context.

    Tries the configured model first, then iterates through fallback
    candidates.  Returns a stub dict if none is reachable.

    Parameters
    ----------
    ctx:      VisualPromptContext built from current track state.
    model:    Model ID to try first (from config.visuals.omni.model).
              Empty string → use _MODEL_CANDIDATES order.
    api_key:  LiteLLM / Gemini API key.
    api_base: LiteLLM gateway base URL (e.g. "http://localhost:4000").

    Returns
    -------
    dict with keys: ok, concept, video_url, palette, stub, error.
    """
    if not _GENAI_AVAILABLE:
        stub = dict(_STUB_RESPONSE)
        stub["error"] = (
            "google-generativeai package not importable. "
            "Run: pip install google-generativeai. " + stub["error"]
        )
        return stub

    prompt = _build_prompt(ctx)
    candidates = ([model] if model else []) + _MODEL_CANDIDATES

    last_error: str = ""
    for model_id in candidates:
        try:
            response = await _call_model(model_id, prompt, api_key, api_base)
            return _parse_response(response, ctx.palette)
        except _ModelUnavailable as exc:
            last_error = str(exc)
            log.debug("Omni: model %s unavailable — %s", model_id, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            log.warning("Omni: unexpected error with model %s — %s", model_id, exc)
            continue

    stub = dict(_STUB_RESPONSE)
    stub["error"] = f"All model candidates exhausted. Last error: {last_error}. {stub['error']}"
    return stub


class _ModelUnavailable(Exception):
    """Raised when a model returns a not-found / not-supported response."""


async def _call_model(
    model_id: str,
    prompt: str,
    api_key: str,
    api_base: str,
) -> Any:
    """Call the Gemini model and return the raw response object."""
    # ---------------------------------------------------------------------------
    # LiteLLM path (preferred — uses the existing gateway that all other
    # parts of the system use):
    # ---------------------------------------------------------------------------
    try:
        import litellm  # type: ignore
        litellm.api_base = api_base or "http://localhost:4000"
        if api_key:
            litellm.api_key = api_key

        response = await litellm.acompletion(
            model=f"openai/{model_id}",   # LiteLLM OpenAI-compatible prefix
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=512,
            timeout=15,
        )
        text = response.choices[0].message.content or ""
        if not text.strip():
            raise _ModelUnavailable(f"empty response from {model_id}")
        return text

    except ImportError:
        pass  # litellm not importable; fall through to direct genai call

    # ---------------------------------------------------------------------------
    # Direct google-genai path (fallback if litellm not wired):
    # ---------------------------------------------------------------------------
    try:
        if api_key:
            genai.configure(api_key=api_key)
        model_obj = genai.GenerativeModel(model_id)
        response = model_obj.generate_content(prompt)
        return response.text
    except Exception as exc:
        exc_str = str(exc).lower()
        if any(kw in exc_str for kw in ("not found", "invalid", "404", "unsupported", "does not exist")):
            raise _ModelUnavailable(str(exc)) from exc
        raise


def _parse_response(text: str, palette: str) -> dict:
    """Parse the model's text output into a structured dict."""
    import json as _json
    import re

    concept = ""
    video_url = ""
    parsed_json: dict = {}

    # Try to find a JSON block in the response
    json_match = re.search(r"\{[^}]+\}", text, re.DOTALL)
    if json_match:
        try:
            parsed_json = _json.loads(json_match.group())
        except _json.JSONDecodeError:
            pass

    concept = parsed_json.get("concept", text[:200].strip())

    return {
        "ok": True,
        "concept": concept,
        "video_url": video_url,
        "palette": parsed_json.get("palette", palette),
        "dominant_color": parsed_json.get("dominant_color", ""),
        "bg_color": parsed_json.get("bg_color", ""),
        "motion": parsed_json.get("motion", "pulse"),
        "full_text": text,
        "stub": False,
        "error": None,
    }
