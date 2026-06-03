"""agent/visuals — E6 Generative Visual Layer.

Entry point for the VisualEngine. Import and wire up from the Being's
main loop when config.visuals.enabled is True.

Usage (integrator snippet in main.py / heartbeat.py)::

    from agent.visuals import VisualEngine
    from agent.config import load_config

    cfg = load_config()
    if cfg.visuals.enabled:
        vis = VisualEngine(cfg)
        # On each heartbeat tick, after fetching current track features:
        vis.tick(
            bpm=128.0,
            energy=7,
            section="DROP",
            key="Am",
            genre="melodic-techno",
        )

Everything behind ``config.visuals.enabled = false`` by default.
No import side-effects when disabled; engine is never instantiated.
"""

from .engine import VisualEngine
from .palette import get_palette, PALETTE_REGISTRY as PALETTE_MAP
from .osc_emitter import OSCEmitter

__all__ = ["VisualEngine", "OSCEmitter", "get_palette", "PALETTE_MAP"]
