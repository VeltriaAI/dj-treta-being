"""E6 — VisualEngine: orchestrates palette + OSC + Omni.

This is the single integration point.  The Being's main loop (or a
dedicated visual loop) calls ``VisualEngine.tick()`` once per heartbeat
with the current track's audio features; the engine:

1. Resolves the context-driven color palette (palette.py).
2. Emits the full OSC bundle (osc_emitter.py).
3. Optionally fires a Gemini Omni visual generation call (omni.py)
   — rate-limited so it doesn't fire on every heartbeat.

Everything is guarded by ``config.visuals.enabled = false`` (the
default).  When disabled, ``tick()`` is a no-op so no imports need
to be conditional at the call site.

Threading: ``tick()`` is synchronous.  The Omni call is async; the
engine fires it in a background thread via asyncio.run_coroutine_
threadsafe so it doesn't block the heartbeat loop.  Results are
stored in ``last_omni_result`` and can be read by the Being.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .osc_emitter import OSCEmitter
from .palette import get_palette, palette_colors
from .omni import VisualPromptContext, generate_visual

log = logging.getLogger("dj-treta.visuals.engine")


@dataclass
class VisualEngine:
    """Orchestrator for DJ Treta's generative visual layer.

    Instantiate once (with the config object) at startup.
    Call ``tick()`` from the heartbeat or a dedicated 2–5 Hz loop.
    """

    # Config reference — passed at construction, not stored in full to
    # avoid circular refs.  Only the visuals sub-config is kept.
    enabled: bool = False
    osc_host: str = "127.0.0.1"
    osc_port: int = 7000
    omni_enabled: bool = False
    omni_model: str = ""
    omni_interval_seconds: float = 30.0  # minimum gap between Omni calls
    llm_api_key: str = ""
    llm_api_base: str = "http://localhost:4000"

    # Runtime state (not constructor args)
    _osc: Optional[OSCEmitter] = field(init=False, repr=False, default=None)
    _last_omni_call: float = field(init=False, repr=False, default=0.0)
    _omni_thread: Optional[threading.Thread] = field(init=False, repr=False, default=None)
    last_omni_result: dict = field(init=False, repr=False, default_factory=dict)
    last_palette: str = field(init=False, repr=False, default="Midnight")
    last_palette_colors: dict = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        if self.enabled:
            self._osc = OSCEmitter(host=self.osc_host, port=self.osc_port)
            log.info(
                "VisualEngine started (OSC → %s:%d, Omni=%s)",
                self.osc_host, self.osc_port, self.omni_enabled,
            )
        else:
            log.debug("VisualEngine disabled — set config.visuals.enabled=true to activate")

    @classmethod
    def from_config(cls, config: Any) -> "VisualEngine":
        """Construct a VisualEngine from the full Config object.

        Reads ``config.visuals`` (VisualsConfig dataclass defined in
        agent/config.py once integrated).  Falls back to safe defaults
        if the section is absent (pre-integration compatibility).
        """
        vc = getattr(config, "visuals", None)
        if vc is None:
            # visuals block not yet in the config object — disabled by default.
            return cls(enabled=False)

        osc = getattr(vc, "osc", None) or type("_OSC", (), {"host": "127.0.0.1", "port": 7000})()
        omni = getattr(vc, "omni", None) or type("_Omni", (), {"enabled": False, "model": ""})()
        llm = getattr(config, "llm", None)

        return cls(
            enabled=getattr(vc, "enabled", False),
            osc_host=getattr(osc, "host", "127.0.0.1"),
            osc_port=getattr(osc, "port", 7000),
            omni_enabled=getattr(omni, "enabled", False),
            omni_model=getattr(omni, "model", ""),
            omni_interval_seconds=getattr(vc, "omni_interval_seconds", 30.0),
            llm_api_key=getattr(llm, "api_key", "") if llm else "",
            llm_api_base=getattr(llm, "api_base", "http://localhost:4000") if llm else "http://localhost:4000",
        )

    def tick(
        self,
        bpm: float,
        energy: int,
        section: str,
        key: str = "",
        genre: str = "",
        beat_active: bool = False,
        track_title: str = "",
        artist: str = "",
        mood: str = "",
    ) -> dict:
        """Process one visual tick.

        Called from the heartbeat loop on each beat/section event.
        Returns a dict with the palette name and OSC status so the
        Being can log or surface to the listener.

        When ``enabled=False``, returns immediately with a no-op dict.
        """
        if not self.enabled:
            return {"enabled": False}

        # 1. Resolve palette
        palette_name = get_palette(genre=genre, key=key, energy=energy)
        colors = palette_colors(palette_name)
        self.last_palette = palette_name
        self.last_palette_colors = colors

        # 2. Emit OSC
        osc_ok = False
        if self._osc is not None:
            try:
                self._osc.emit(
                    bpm=bpm,
                    energy=energy,
                    section=section,
                    palette=palette_name,
                    key=key,
                    genre=genre,
                    beat_active=beat_active,
                )
                osc_ok = True
            except Exception as exc:  # noqa: BLE001
                log.warning("VisualEngine OSC emit error: %s", exc)

        # 3. Trigger Omni generation (rate-limited, non-blocking)
        omni_triggered = False
        if self.omni_enabled:
            now = time.monotonic()
            if now - self._last_omni_call >= self.omni_interval_seconds:
                self._last_omni_call = now
                ctx = VisualPromptContext(
                    bpm=bpm, energy=energy, section=section,
                    key=key, genre=genre, palette=palette_name,
                    track_title=track_title, artist=artist, mood=mood,
                )
                self._fire_omni_async(ctx)
                omni_triggered = True

        return {
            "enabled": True,
            "palette": palette_name,
            "colors": colors,
            "osc_ok": osc_ok,
            "omni_triggered": omni_triggered,
            "last_omni_result": self.last_omni_result,
        }

    def _fire_omni_async(self, ctx: VisualPromptContext) -> None:
        """Launch a background thread that runs the async Omni call."""
        if self._omni_thread is not None and self._omni_thread.is_alive():
            log.debug("VisualEngine: Omni call already in-flight, skipping")
            return

        def _run():
            try:
                result = asyncio.run(
                    generate_visual(
                        ctx,
                        model=self.omni_model,
                        api_key=self.llm_api_key,
                        api_base=self.llm_api_base,
                    )
                )
                self.last_omni_result = result
                if result.get("ok"):
                    log.info(
                        "VisualEngine Omni concept [%s]: %s",
                        result.get("palette"), result.get("concept", "")[:120],
                    )
                else:
                    log.debug("VisualEngine Omni stub/unavailable: %s", result.get("error", ""))
            except Exception as exc:  # noqa: BLE001
                log.warning("VisualEngine Omni thread error: %s", exc)

        self._omni_thread = threading.Thread(target=_run, daemon=True, name="visuals-omni")
        self._omni_thread.start()

    def close(self) -> None:
        """Release resources."""
        if self._osc is not None:
            self._osc.close()
        log.debug("VisualEngine closed")


# ---------------------------------------------------------------------------
# ADK tool functions — exposed for registration in agents.py (E6 block)
# ---------------------------------------------------------------------------

def get_visual_status() -> dict:
    """Return the current visual engine state (palette, last Omni concept).

    Tool for the Being agent so she can report visual status to the
    listener: "Visuals are in Horizon palette right now — deep blue,
    synced to the drop."
    """
    # The engine instance is created by the daemon; we return its
    # last-known state from the module-level cache set by the daemon.
    engine = _get_cached_engine()
    if engine is None or not engine.enabled:
        return {"enabled": False, "message": "Visual engine is disabled."}
    return {
        "enabled": True,
        "palette": engine.last_palette,
        "colors": engine.last_palette_colors,
        "omni_enabled": engine.omni_enabled,
        "last_omni_concept": engine.last_omni_result.get("concept", ""),
        "last_omni_motion": engine.last_omni_result.get("motion", ""),
        "last_omni_ok": engine.last_omni_result.get("ok", False),
    }


def set_visual_palette(palette_name: str) -> dict:
    """Override the auto-resolved palette name for the current track.

    Useful when the Being wants to manually pick a visual mood:
    "switch visuals to Fractal for this psytrance drop."
    The override applies until the next tick that resolves a new palette.
    """
    engine = _get_cached_engine()
    if engine is None or not engine.enabled:
        return {"ok": False, "message": "Visual engine is disabled."}
    from .palette import PALETTE_REGISTRY
    if palette_name not in PALETTE_REGISTRY:
        names = list(PALETTE_REGISTRY.keys())
        return {"ok": False, "message": f"Unknown palette. Available: {names}"}
    engine.last_palette = palette_name
    engine.last_palette_colors = PALETTE_REGISTRY[palette_name]
    return {"ok": True, "palette": palette_name, "colors": PALETTE_REGISTRY[palette_name]}


# Module-level singleton cache — set by the daemon/main.py at startup.
# This avoids passing the engine through every tool call.
_ENGINE_CACHE: Optional[VisualEngine] = None


def register_engine(engine: VisualEngine) -> None:
    """Called by the daemon after constructing the engine."""
    global _ENGINE_CACHE
    _ENGINE_CACHE = engine


def _get_cached_engine() -> Optional[VisualEngine]:
    return _ENGINE_CACHE
