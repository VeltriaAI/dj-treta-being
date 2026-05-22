"""Pre-flight config validator for `djclaw validate-config`.

Catches the common new-machine setup mistakes BEFORE the daemon starts:
  - Mixxx not running on configured URL
  - LiteLLM proxy not running on configured URL
  - music_dir doesn't exist or is empty
  - DJTRETA_LLM_API_KEY not set
  - djtreta.db schema out of date
  - Vertex/HF env vars missing when their features are enabled

Exit code is the count of failures, so CI / install scripts can branch on
non-zero.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx

from .audio_files import is_audio_file


@dataclass
class Check:
    name: str
    ok: bool
    detail: str  # short failure reason or success summary

    def emoji(self) -> str:
        return "✅" if self.ok else "❌"


def _check_music_dir(cfg) -> Check:
    p = cfg.library.music_path
    if not p.exists():
        return Check(
            "music_dir exists",
            False,
            f"{p} does not exist. Create it with: mkdir -p {p}",
        )
    n_audio = sum(
        1
        for sub in p.iterdir()
        if sub.is_dir() and not sub.name.startswith(".")
        for f in sub.iterdir()
        if is_audio_file(f)
    )
    if n_audio == 0:
        return Check(
            "music_dir has audio",
            False,
            f"{p} has no audio files. DJ Treta needs at least a few "
            f"tracks under {p}/<genre>/*.mp3 to start a set.",
        )
    return Check("music_dir + audio", True, f"{p} ({n_audio} files)")


def _check_mixxx(cfg) -> Check:
    try:
        r = httpx.get(f"{cfg.mixxx.url}/api/status", timeout=2)
        if r.status_code == 200:
            return Check("mixxx reachable", True, f"{cfg.mixxx.url} (HTTP 200)")
        return Check(
            "mixxx reachable",
            False,
            f"{cfg.mixxx.url} returned HTTP {r.status_code}",
        )
    except Exception as exc:
        return Check(
            "mixxx reachable",
            False,
            f"{cfg.mixxx.url} unreachable: {type(exc).__name__}. "
            f"Start mixxx-treta (see docs/) and confirm http-rest-api branch.",
        )


def _check_litellm(cfg) -> Check:
    try:
        r = httpx.get(f"{cfg.llm.api_base}/health", timeout=2)
        if r.status_code in (200, 401):  # 401 = up but unauthenticated, fine
            return Check("litellm reachable", True, f"{cfg.llm.api_base} (HTTP {r.status_code})")
        return Check(
            "litellm reachable",
            False,
            f"{cfg.llm.api_base} returned HTTP {r.status_code}",
        )
    except Exception as exc:
        return Check(
            "litellm reachable",
            False,
            f"{cfg.llm.api_base} unreachable: {type(exc).__name__}. "
            f"Start the LiteLLM proxy (see docs/LITELLM_VERTEX_SETUP.md).",
        )


def _check_llm_api_key(cfg) -> Check:
    key = (
        cfg.llm.api_key
        or os.environ.get("DJTRETA_LLM_API_KEY")
        or os.environ.get("LLM_API_KEY")
    )
    if key:
        return Check("DJTRETA_LLM_API_KEY", True, "set")
    return Check(
        "DJTRETA_LLM_API_KEY",
        False,
        "missing. Add to .env: DJTRETA_LLM_API_KEY=sk-...",
    )


def _check_vertex(cfg) -> Check:
    if not cfg.producer.enabled:
        return Check("vertex (producer)", True, "skipped — producer.enabled=false")
    if not cfg.producer.vertex_project:
        return Check(
            "vertex (producer)",
            False,
            "producer.enabled=true but vertex_project unset. "
            "Set DJTRETA_VERTEX_PROJECT or producer.vertex_project in config.yaml.",
        )
    return Check(
        "vertex (producer)",
        True,
        f"project={cfg.producer.vertex_project} location={cfg.producer.vertex_location}",
    )


def _check_mixxx_binary(cfg) -> Check:
    """When auto_start is on, the agent needs a binary it can launch.

    On end-user installs that's ~/.local/share/djclaw/mixxx/current/...,
    populated by install.sh. On dev installs the user typically has
    their own Mixxx checkout and points cfg.mixxx.binary at it. Either
    way: if auto_start is true and we can't find an executable, the
    agent will silently fail to start Mixxx and the user gets cryptic
    'unreachable' errors downstream.
    """
    if not cfg.mixxx.auto_start:
        return Check("mixxx binary", True, "skipped — auto_start=false")

    candidates = []
    if cfg.mixxx.binary:
        candidates.append(Path(cfg.mixxx.binary).expanduser())
    # Installer-managed paths (mac .app + linux DEB extraction).
    base = Path("~/.local/share/djclaw/mixxx/current").expanduser()
    candidates.extend([
        base / "Mixxx.app" / "Contents" / "MacOS" / "mixxx",
        base / "usr" / "bin" / "mixxx",
    ])

    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return Check("mixxx binary", True, str(c))

    return Check(
        "mixxx binary",
        False,
        "no executable mixxx found. Run "
        "`curl -fsSL https://dj.treta.life/install.sh | sh` to install, "
        "or set mixxx.binary in config.yaml.",
    )


def _check_mixxx_port(cfg) -> Check:
    """Check whether port 7778 is free or already taken by our Mixxx.

    A foreign process on 7778 would block auto_start; we want to flag
    that distinctly from 'mixxx not running yet'.
    """
    import socket
    try:
        host = cfg.mixxx.url.split("://", 1)[-1].split("/", 1)[0]
        host, _, port_s = host.partition(":")
        port = int(port_s) if port_s else 7778
    except Exception:
        return Check("mixxx port", True, "skipped — could not parse mixxx.url")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        result = sock.connect_ex((host, port))
        sock.close()
    except Exception:
        return Check("mixxx port", True, f"skipped — {host}:{port} probe failed")

    if result != 0:
        return Check("mixxx port", True, f"{host}:{port} free")

    # Something IS listening on the port. Determine if it's ours by
    # hitting the Mixxx HTTP API surface.
    try:
        r = httpx.get(f"{cfg.mixxx.url}/api/status", timeout=1)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
            return Check("mixxx port", True, f"{host}:{port} (held by Mixxx-Treta)")
    except Exception:
        pass
    return Check(
        "mixxx port",
        False,
        f"{host}:{port} occupied by a non-Mixxx process. Free it or "
        f"change mixxx.url in config.yaml.",
    )


def _check_db_schema() -> Check:
    db_path = Path(__file__).parent.parent / "djtreta.db"
    if not db_path.exists():
        return Check(
            "djtreta.db schema",
            True,
            "skipped — db will be created on first start",
        )
    try:
        con = sqlite3.connect(str(db_path))
        cols = {row[1] for row in con.execute("PRAGMA table_info(tracks)").fetchall()}
        required = {"path", "canonical_artist", "canonical_song", "canonical_version"}
        missing = required - cols
        if missing:
            return Check(
                "djtreta.db schema",
                False,
                f"tracks table missing columns: {sorted(missing)}. "
                f"Run init_db() or python -c 'from agent.db import init_db; init_db()'.",
            )
        # Check for absolute paths (should be relative post-migration)
        n_abs = con.execute(
            "SELECT COUNT(*) FROM tracks WHERE path LIKE '/%'"
        ).fetchone()[0]
        n_total = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        if n_abs > 0 and n_total > 0:
            return Check(
                "djtreta.db schema",
                False,
                f"{n_abs}/{n_total} rows still have absolute paths. "
                f"Run: python scripts/migrate_paths_to_relative.py",
            )
        return Check(
            "djtreta.db schema",
            True,
            f"{n_total} tracks (all relative paths)",
        )
    except Exception as exc:
        return Check("djtreta.db schema", False, f"{type(exc).__name__}: {exc}")


def run_checks() -> list[Check]:
    """Run all checks and return list. Doesn't raise — caller decides exit code."""
    from .config import load_config
    cfg = load_config()
    return [
        _check_llm_api_key(cfg),
        _check_music_dir(cfg),
        _check_mixxx_binary(cfg),
        _check_mixxx_port(cfg),
        _check_mixxx(cfg),
        _check_litellm(cfg),
        _check_vertex(cfg),
        _check_db_schema(),
    ]


def main() -> int:
    """CLI entry — print results, return failure count."""
    results = run_checks()
    n_fail = sum(1 for c in results if not c.ok)
    print()
    for c in results:
        print(f"  {c.emoji()} {c.name:25s} {c.detail}")
    print()
    if n_fail == 0:
        print("  All checks passed. DJ Treta is ready to run.")
    else:
        print(f"  {n_fail} check(s) failed. Fix the issues above and re-run.")
    print()
    return n_fail


if __name__ == "__main__":
    raise SystemExit(main())
