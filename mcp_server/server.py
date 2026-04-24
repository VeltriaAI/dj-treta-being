"""DJ Treta MCP SSE server — FastMCP bound to 127.0.0.1:8765.

Run with: `python -m mcp_server.server`

Environment:
    DJTRETA_MCP_TOKEN  — required. Bearer token for clients.
    DJTRETA_MCP_HOST   — optional. Default 127.0.0.1.
    DJTRETA_MCP_PORT   — optional. Default 8765.

Nginx at mcp.dj.treta.life fronts this with TLS + Authorization passthrough.
"""
from __future__ import annotations

import logging
import os
import sys

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .auth import BearerAuthMiddleware
from . import tools as dj_tools


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("dj-treta-mcp")


def build_mcp() -> FastMCP:
    """Construct the FastMCP instance and register all tools."""
    # DNS rebinding protection: because FastMCP auto-binds localhost-only,
    # it defaults to rejecting Host headers that aren't 127.0.0.1/localhost.
    # Nginx forwards the original Host (mcp.dj.treta.life), so we must
    # allow it — plus local listeners for direct health checks.
    allowed_hosts_env = os.environ.get(
        "DJTRETA_MCP_ALLOWED_HOSTS",
        "127.0.0.1:*,localhost:*,[::1]:*,mcp.dj.treta.life,mcp.dj.treta.life:*",
    )
    allowed_origins_env = os.environ.get(
        "DJTRETA_MCP_ALLOWED_ORIGINS",
        "http://127.0.0.1:*,http://localhost:*,http://[::1]:*,"
        "https://mcp.dj.treta.life,http://mcp.dj.treta.life",
    )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h.strip() for h in allowed_hosts_env.split(",") if h.strip()],
        allowed_origins=[o.strip() for o in allowed_origins_env.split(",") if o.strip()],
    )

    mcp = FastMCP("dj-treta", instructions=(
        "DJ Treta is an autonomous AI DJ streaming live at https://dj.treta.life. "
        "These tools let you see what she's playing, change her mood, skip tracks, "
        "request specific tracks, and (in future) co-DJ alongside her. "
        "All write actions are asynchronous: the agent picks up commands on its "
        "next heartbeat (~2s). Read tools are live against /tmp state + session.json."
    ), transport_security=security)

    # Read-only
    mcp.add_tool(
        dj_tools.dj_status,
        name="dj_status",
        description=(
            "Compact now-playing snapshot: current track (title/bpm/key/position), "
            "next track, mood + profile, set info (elapsed/remaining/peak_energy), "
            "playlist depth, last command result. Safe read-only."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_playlist,
        name="dj_playlist",
        description=(
            "Ranked upcoming candidates with downloaded flags. Use this to see "
            "what the planner queued and whether the library agent has fetched it."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_session_state,
        name="dj_session_state",
        description=(
            "Full session.json snapshot — mood, directives, playlist, history. "
            "Verbose; prefer dj_status for quick checks."
        ),
    )

    # Write — routed via command file (/tmp/dj-treta-command.json)
    mcp.add_tool(
        dj_tools.dj_talk,
        name="dj_talk",
        description=(
            "Send DJ Treta a conversational intent. She responds via her Being "
            "agent. Examples: 'play something darker', 'bring it up a notch', "
            "'what's your favourite track tonight'. Waits up to 30s for a reply."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_set_mood,
        name="dj_set_mood",
        description=(
            "Change DJ Treta's current mood. Accepts natural language like "
            "'darker techno' or 'afro house' — her mood resolver canonicalises it. "
            "Triggers a planner replan."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_skip,
        name="dj_skip",
        description=(
            "Skip current track. style='fast' (hard crossfade ~2s) or "
            "style='smooth' (graceful ~10s). DJ agent handles the actual transition."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_request_track,
        name="dj_request_track",
        description=(
            "Request DJ Treta to fetch and queue a specific track by artist "
            "and title. Library agent (K5) downloads it; planner considers "
            "it on next replan."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_feedback,
        name="dj_feedback",
        description=(
            "Mark the current track as 'like' or 'dislike'. Planner uses "
            "the feedback history to bias future recommendations."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_set_sources,
        name="dj_set_sources",
        description=(
            "Toggle a music source on or off. source: 'youtube' (alias 'yt') "
            "or 'originals'. enabled: True/False."
        ),
    )

    # Direct mixer / deck — posted straight to Mixxx for low-latency feel.
    mcp.add_tool(
        dj_tools.dj_deck_play,
        name="dj_deck_play",
        description="Resume or start playback on a deck. deck_num: 1 or 2.",
    )
    mcp.add_tool(
        dj_tools.dj_deck_pause,
        name="dj_deck_pause",
        description=(
            "Pause a deck. Note: pausing the live-stream deck will cause "
            "dead air — prefer dj_skip for set-time interruptions."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_set_volume,
        name="dj_set_volume",
        description="Set deck volume. value: 0.0 (silent) to 1.0 (unity).",
    )
    mcp.add_tool(
        dj_tools.dj_set_crossfader,
        name="dj_set_crossfader",
        description=(
            "Set crossfader position. 0.0 = full Deck 1, 0.5 = center, "
            "1.0 = full Deck 2."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_set_eq,
        name="dj_set_eq",
        description=(
            "Set an EQ band on a deck. band: hi / mid / lo. "
            "value: 0.0 (cut) .. 1.0 (unity) .. 4.0 (boost)."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_set_filter,
        name="dj_set_filter",
        description=(
            "Set the quick-effect filter on a deck. "
            "0.0 = full high-pass, 0.5 = neutral, 1.0 = full low-pass."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_load_track,
        name="dj_load_track",
        description=(
            "Load an absolute filesystem path onto a deck directly via Mixxx. "
            "Use dj_search_library to find a path first. Does NOT claim deck "
            "ownership — DJ Treta may auto-load over it on the next tick."
        ),
    )

    # Library search (read-only, SQLite)
    mcp.add_tool(
        dj_tools.dj_search_library,
        name="dj_search_library",
        description=(
            "Fuzzy-search the local library DB by title or artist. Returns "
            "BPM / key / energy / absolute path for each hit."
        ),
    )

    # Co-being deck hooks (Phase 7 live — DJ agent honours reservations)
    mcp.add_tool(
        dj_tools.dj_take_deck,
        name="dj_take_deck",
        description=(
            "Reserve a deck for external control by a named Being. "
            "Phase 6: recorded but DJ agent does not yet honour reservations. "
            "Phase 7 will make DJ skip reserved decks in auto-load decisions."
        ),
    )
    mcp.add_tool(
        dj_tools.dj_release_deck,
        name="dj_release_deck",
        description="Release a previously-taken deck back to DJ Treta.",
    )
    mcp.add_tool(
        dj_tools.dj_load_on_deck,
        name="dj_load_on_deck",
        description=(
            "Co-being: load a specific track onto a reserved deck. Requires "
            "a prior dj_take_deck by the same being_id. Posts directly to "
            "Mixxx /api/load. Phase 6 warning: DJ agent may still auto-load "
            "over this until Phase 7 reservation honouring ships."
        ),
    )

    return mcp


def build_app() -> Starlette:
    """Wrap FastMCP's SSE app with bearer auth + /health route."""
    mcp = build_mcp()

    # FastMCP's SSE transport exposes /sse and /messages/ on its own router.
    sse_app = mcp.sse_app()

    async def health(request):
        return JSONResponse({
            "ok": True,
            "service": "dj-treta-mcp",
            "tools": [t.name for t in await mcp.list_tools()],
        })

    app = Starlette(
        debug=False,
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/", app=sse_app),
        ],
    )

    # Auth AFTER routes are mounted (middleware wraps the whole app).
    app.add_middleware(BearerAuthMiddleware)
    return app


def main() -> int:
    if not os.environ.get("DJTRETA_MCP_TOKEN"):
        print("FATAL: DJTRETA_MCP_TOKEN env var not set", file=sys.stderr)
        return 1

    host = os.environ.get("DJTRETA_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("DJTRETA_MCP_PORT", "8765"))

    app = build_app()
    log.info("DJ Treta MCP SSE server starting on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
