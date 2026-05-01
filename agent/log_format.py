"""Unified log line format shared by the daemon (emitter) and TUI (renderer).

Single source of truth for the level/tag/symbol matrix so both ends agree on
colors and prefixes. The emitter writes structured tuples to thinking.log
(`[YYYY-MM-DD HH:MM:SS LEVEL TAG] message`) and broadcasts them via WS; the
TUI parses + renders with the matching color codes.
"""
from __future__ import annotations

import time
from typing import NamedTuple


# ─── Levels ─────────────────────────────────────────────────────────────
# Symbol left-column gives severity at a glance, independent of the tag.
LEVEL_DEBUG = "DEBUG"
LEVEL_INFO = "INFO"
LEVEL_OK = "OK"
LEVEL_WARN = "WARN"
LEVEL_ERROR = "ERROR"
LEVEL_EVENT = "EVENT"  # state changes — track loaded, transition, set started

LEVEL_SYMBOL = {
    LEVEL_DEBUG: "·",
    LEVEL_INFO: "ⓘ",
    LEVEL_OK: "✓",
    LEVEL_WARN: "⚠",
    LEVEL_ERROR: "✗",
    LEVEL_EVENT: "▶",
}

LEVEL_COLOR = {
    LEVEL_DEBUG: "dim",
    LEVEL_INFO: "white",
    LEVEL_OK: "green",
    LEVEL_WARN: "yellow",
    LEVEL_ERROR: "bold red",
    LEVEL_EVENT: "cyan",
}


# ─── Tags (subsystem) ───────────────────────────────────────────────────
TAG_PLAN = "PLAN"   # planner — knowledge surface, candidate merge, Flash output
TAG_LIB = "LIB"     # library_loop — downloads, enrichment, library_need
TAG_DJ = "DJ"       # dj_treta agent — decisions, defer, hear_music
TAG_MIX = "MIX"     # transitions, deck loads, crossfade
TAG_LOAD = "LOAD"   # deck load events
TAG_AUTO = "AUTO"   # auto-transition watchdog
TAG_KB = "KB"       # knowledge / LanceDB / parquet queries
TAG_PROD = "PROD"   # producer (Lyria) generation
TAG_EVO = "EVO"     # evolution / self-modification
TAG_SOS = "SOS"     # emergency_play
TAG_SET = "SET"     # set lifecycle
TAG_SELF = "SELF"   # being reflection / consciousness
TAG_USER = "USER"   # user input / talk
TAG_SYS = "SYS"     # system events — boot, shutdown, config
TAG_ERR = "ERR"     # errors not pinned to a subsystem

TAG_COLOR = {
    TAG_PLAN: "magenta",
    TAG_LIB: "bright_magenta",
    TAG_DJ: "bright_blue",
    TAG_MIX: "cyan",
    TAG_LOAD: "green",
    TAG_AUTO: "yellow",
    TAG_KB: "blue",
    TAG_PROD: "bright_magenta",
    TAG_EVO: "bold yellow",
    TAG_SOS: "bold red",
    TAG_SET: "bold white",
    TAG_SELF: "bright_cyan",
    TAG_USER: "bright_white",
    TAG_SYS: "dim",
    TAG_ERR: "red",
}

# Which tab(s) each tag should mirror to. "all" is implicit for every line.
TAG_TABS = {
    TAG_PLAN: ["planner"],
    TAG_LIB: ["library"],
    TAG_KB: ["library", "planner"],   # knowledge surfacing — visible in both
    TAG_DJ: ["dj"],
    TAG_MIX: ["dj"],
    TAG_LOAD: ["dj"],
    TAG_AUTO: ["dj"],
    TAG_SOS: ["dj"],
    TAG_PROD: ["library"],
    TAG_EVO: ["treta"],
    TAG_SELF: ["treta"],
    TAG_USER: ["treta"],
    TAG_SET: [],
    TAG_SYS: [],
    TAG_ERR: [],
}


class LogEntry(NamedTuple):
    ts: float        # epoch seconds
    level: str       # one of LEVEL_*
    tag: str         # one of TAG_*
    message: str     # body
    detail: str = ""  # optional multi-line detail block (rendered indented)


def format_ts(ts: float) -> str:
    """HH:MM:SS in local time."""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def render_markup(entry: LogEntry) -> str:
    """Return the Rich-markup string for this entry.

    Format:  HH:MM:SS  ◯  TAG   message
             [dim]ts[/dim]  [color]symbol[/color]  [color]TAG[/color]  body
    """
    sym = LEVEL_SYMBOL.get(entry.level, "·")
    lvl_color = LEVEL_COLOR.get(entry.level, "white")
    tag_color = TAG_COLOR.get(entry.tag, "dim")

    ts_part = f"[dim]{format_ts(entry.ts)}[/dim]"
    sym_part = f"[{lvl_color}]{sym}[/{lvl_color}]"
    tag_part = f"[{tag_color}]{entry.tag:<4}[/{tag_color}]"

    line = f"{ts_part}  {sym_part}  {tag_part}  {entry.message}"
    if entry.detail:
        # Indent each detail line to align under the message column.
        indent = " " * (8 + 2 + 1 + 2 + 4 + 2)  # ts(8) + 2 + sym(1) + 2 + tag(4) + 2
        for line_d in entry.detail.splitlines():
            line += f"\n{indent}{line_d}"
    return line


def render_wire_line(entry: LogEntry) -> str:
    """Plain-text line written to thinking.log for replay.

    Wire format:  ISO_TS\tLEVEL\tTAG\tmessage
    Multi-line detail packed with literal `\\n` and re-split at parse.
    """
    msg = entry.message
    if entry.detail:
        msg = msg + "\n" + entry.detail
    msg_escaped = msg.replace("\n", "\\n").replace("\t", "    ")
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(entry.ts))
    return f"{iso}\t{entry.level}\t{entry.tag}\t{msg_escaped}"


def parse_wire_line(line: str) -> LogEntry | None:
    """Parse a wire-format line back into a LogEntry. None on bad input."""
    parts = line.rstrip("\n").split("\t", 3)
    if len(parts) != 4:
        return None
    iso, level, tag, msg = parts
    try:
        ts_struct = time.strptime(iso, "%Y-%m-%dT%H:%M:%S")
        ts = time.mktime(ts_struct)
    except ValueError:
        return None
    msg = msg.replace("\\n", "\n")
    detail = ""
    if "\n" in msg:
        msg, detail = msg.split("\n", 1)
    return LogEntry(ts=ts, level=level, tag=tag, message=msg, detail=detail)
