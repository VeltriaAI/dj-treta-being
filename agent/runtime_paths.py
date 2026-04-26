"""Single source of truth for DJ Treta's runtime IPC files.

Historical state: ~12 modules each hardcoded `Path("/tmp/dj-treta-*.json")`
literals — fine on Linux/Mac, breaks on Windows, breaks on Docker without
a /tmp mount, and prevents running two instances side-by-side.

Now: every runtime file lives under a single `runtime_dir()`, configurable
in priority order:
  1. `DJTRETA_RUNTIME_DIR` environment variable
  2. `daemon.runtime_dir` in config.yaml
  3. `tempfile.gettempdir()` (usually /tmp on Linux/Mac, %TEMP% on Windows)

All files keep their original `dj-treta-` prefix so they're identifiable
in a shared temp dir.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from functools import lru_cache


@lru_cache(maxsize=1)
def runtime_dir() -> Path:
    """Resolve the runtime IPC directory once per process.

    Cached so repeated calls don't re-load config; restart the process
    (or call runtime_dir.cache_clear()) to pick up env/config changes.
    """
    env = os.environ.get("DJTRETA_RUNTIME_DIR")
    if env:
        d = Path(env).expanduser()
    else:
        cfg_dir = ""
        try:
            from .config import load_config
            cfg_dir = load_config().daemon.runtime_dir or ""
        except Exception:
            pass
        d = Path(cfg_dir).expanduser() if cfg_dir else Path(tempfile.gettempdir())

    # Best-effort create — ignore failures so we don't crash before the
    # caller has a chance to log a clear error.
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def runtime_path(name: str) -> Path:
    """Build a path for a runtime IPC file. Always prefix with dj-treta- if not already."""
    if not name.startswith("dj-treta-"):
        name = f"dj-treta-{name}"
    return runtime_dir() / name
