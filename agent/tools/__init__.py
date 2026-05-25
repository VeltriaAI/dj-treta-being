"""DJ Treta tools -- plain functions for Google ADK v5.0.

These are ALL the capabilities the Being has:
- DJ controls (Mixxx API)
- Music discovery (YouTube search + download)
- Library management
- Self-awareness (read own code, config, memory)
- Self-improvement (write code, update config, save learnings)

Re-exports every tool function so existing imports like
``from .tools import get_dj_status`` continue to work.
"""

# Shared helpers (also re-exported for tests / direct use)
from .helpers import (
    _SELF_DIR,
    _music_dir,
    _roots,
    _is_under_allowed_roots,
    _resolve_tool_path,
    _normalize_for_search,
    _mixxx_failed,
    _mixxx_get,
    _mixxx_post,
    _dj_get,
    _dj_post,
)

# Mixxx deck controls
from .mixxx import (
    get_dj_status,
    get_deck_info,
    load_track,
    play_deck,
    pause_deck,
    set_volume,
    set_crossfader,
    set_eq,
    set_filter,
    set_sync,
    get_live_data,
    get_track_info,
    set_rate,
    reset_bpm,
    align_beats,
    nudge_track,
)

# Transition techniques
from .transitions import (
    do_transition,
    do_bass_swap,
    do_filter_sweep,
    do_hard_cut,
    do_echo_out,
    do_riser,
    do_dissolve,
    schedule_transition,
)

# Audio perception
from .perception import (
    hear_music,
    analyze_track,
    preview_track,
)

# Music discovery
from .discovery import (
    search_music,
    download_track,
)

# AI music generation
from .generation import (
    generate_track,
)

# Library management
from .library import (
    list_library_tracks,
    get_set_history,
)

# Self-awareness / meta tools
from .meta import (
    read_file,
    write_file,
    list_files,
    run_shell,
    save_learning,
    recall_learnings,
)

# Directive tools (Being → Agent communication)
from .directives import (
    set_dj_directive,
    set_planner_directive,
    set_mood,
    replace_deck,
    play_specific_track,
    get_directives,
    clear_directives,
    defer_decision,
)

# Evolution tools (self-modification + subagent spawning)
from .evolve import (
    evolve,
    propose_change,
    review_evolution,
)

from .spawn import (
    spawn_agent,
    get_spawn_result,
)

# Evolution: visibility + memory + agency + meta-control
from .visibility import (
    get_subagent_activity,
    tail_thinking_log,
)
from .listener import (
    get_listener_pulse,
    get_listener_profile,
)
from .scheduling import (
    schedule_self,
    cancel_self_schedule,
    list_self_schedule,
)
from .arc import (
    plan_set_arc,
    progress_set_arc,
    clear_set_arc,
)
from .meta_control import (
    pause_subagent,
    resume_subagent,
    force_replan,
    restart_subagent,
    get_subagent_pause_state,
)
# Self-suggestion gating — Treta's gate over her own reflection loop.
from .suggestions import (
    list_self_suggestions,
    honor_self_suggestion,
    discard_self_suggestion,
)
# Sarathi Mode — Treta suggests transitions; Manish executes on the FLX4.
from .sarathi import (
    suggest_transition,
    confirm_suggestion,
    reject_suggestion,
    list_pending_suggestions,
)
# Memory recall tools — wrap the agent.memory module (LanceDB-backed)
# in single-import shims so they can be _wrap()-ed by ADK like other tools.
from ..memory import (
    recall_similar_interaction,
    recall_similar_set,
    recall_journal,
    recall_thoughts,
)
# Ordered recent chat (within today) — JSONL-backed, complements
# the semantic-recall tools above.
from ..chat_persistence import recall_recent_chat

__all__ = [
    # helpers
    "_SELF_DIR", "_music_dir", "_roots", "_is_under_allowed_roots",
    "_resolve_tool_path", "_normalize_for_search",
    "_mixxx_failed", "_mixxx_get", "_mixxx_post", "_dj_get", "_dj_post",
    # mixxx
    "get_dj_status", "get_deck_info", "load_track", "play_deck", "pause_deck",
    "set_volume", "set_crossfader", "set_eq", "set_filter", "set_sync",
    "get_live_data", "get_track_info", "set_rate", "reset_bpm",
    "align_beats", "nudge_track",
    # transitions
    "do_transition", "do_bass_swap", "do_filter_sweep", "do_hard_cut",
    "do_echo_out", "do_riser", "do_dissolve", "schedule_transition",
    # perception
    "hear_music", "analyze_track", "preview_track",
    # discovery
    "search_music", "download_track",
    # generation
    "generate_track",
    # library
    "list_library_tracks", "get_set_history",
    # meta
    "read_file", "write_file", "list_files", "run_shell",
    "save_learning", "recall_learnings",
    # directives
    "set_dj_directive", "set_planner_directive", "set_mood",
    "replace_deck", "play_specific_track",
    "get_directives", "clear_directives", "defer_decision",
    # evolution
    "evolve", "propose_change", "review_evolution",
    "spawn_agent", "get_spawn_result",
    # evolution — visibility / memory / agency / meta-control
    "get_subagent_activity", "tail_thinking_log",
    "get_listener_pulse", "get_listener_profile",
    "schedule_self", "cancel_self_schedule", "list_self_schedule",
    "plan_set_arc", "progress_set_arc", "clear_set_arc",
    "pause_subagent", "resume_subagent", "force_replan",
    "restart_subagent", "get_subagent_pause_state",
    "list_self_suggestions", "honor_self_suggestion",
    "discard_self_suggestion",
    # sarathi mode
    "suggest_transition", "confirm_suggestion", "reject_suggestion",
    "list_pending_suggestions",
    "recall_similar_interaction", "recall_similar_set",
    "recall_journal", "recall_thoughts",
    "recall_recent_chat",
]
