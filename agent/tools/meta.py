"""Self-awareness tools -- read/write files, shell, learnings."""

import subprocess
from pathlib import Path

from .helpers import _SELF_DIR, _resolve_tool_path, load_config


def read_file(file_path: str) -> str:
    """Read a file under the DJ Treta repo or configured music library only.

    Args:
        file_path: Path relative to the repo (e.g. 'config.yaml', '.beings/SOUL.md') or absolute path under those roots.
    """
    cfg = load_config()
    path = _resolve_tool_path(cfg, file_path)
    if path is None:
        return "ERROR: Path not allowed (must be under the DJ Treta repo or library.music_dir)."
    if not path.exists():
        return f"File not found: {path}"
    content = path.read_text()
    if len(content) > 10000:
        return content[:10000] + f"\n\n... (truncated, {len(content)} chars total)"
    return content


def write_file(file_path: str, content: str) -> str:
    """Write a file — restricted to .beings/ directory and agent/ code files.

    You can update your identity files (.beings/SOUL.md, MEMORY.md, GOALS.md, etc.)
    and agent code (agent/*.py). Writing to repo root or random paths is blocked.

    Args:
        file_path: Path relative to the repo (e.g. '.beings/MEMORY.md', 'agent/heartbeat.py').
        content: The full content to write.
    """
    cfg = load_config()
    path = _resolve_tool_path(cfg, file_path)
    if path is None:
        return "ERROR: Path not allowed (must be under the DJ Treta repo or library.music_dir)."

    # Sandbox: only allow .beings/, agent/, tests/, docs/ — not repo root
    repo_root = Path(__file__).parent.parent.parent
    try:
        rel = path.relative_to(repo_root)
        rel_str = str(rel)
        allowed_prefixes = (".beings/", "agent/", "tests/", "docs/", "templates/")
        if not any(rel_str.startswith(p) for p in allowed_prefixes):
            return f"ERROR: Cannot write to '{rel_str}'. Only allowed under: {', '.join(allowed_prefixes)}"
    except ValueError:
        pass  # path not under repo_root — let _resolve_tool_path handle it

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"Written {len(content)} chars to {path}"


def list_files(directory: str = ".") -> list:
    """List files in a directory under the repo or music library.

    Args:
        directory: Path relative to the repo or absolute under allowed roots.
    """
    cfg = load_config()
    path = _resolve_tool_path(cfg, directory)
    if path is None:
        return ["ERROR: Path not allowed (must be under the DJ Treta repo or library.music_dir)."]
    if not path.exists():
        return [f"Directory not found: {path}"]
    if not path.is_dir():
        return [f"Not a directory: {path}"]
    return [str(f.relative_to(path)) for f in sorted(path.iterdir()) if not f.name.startswith('.')]


def run_shell(command: str) -> str:
    """Run a shell command (disabled unless capabilities.allow_shell is true in config).

    Args:
        command: Shell command to execute.
    """
    if not load_config().capabilities.allow_shell:
        return (
            "ERROR: Shell is disabled. Set capabilities.allow_shell: true in config.yaml "
            "(trusted machines only)."
        )
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=30, cwd=str(_SELF_DIR),
        )
        output = result.stdout + result.stderr
        if len(output) > 5000:
            output = output[:5000] + "\n... (truncated)"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out (30s limit)"


def save_learning(topic: str, content: str) -> str:
    """Save something you learned -- a mixing technique that worked, a track combination,
    a preference, anything worth remembering for future sets.

    Args:
        topic: Short topic name (e.g., 'transition-timing', 'track-pairing', 'eq-technique').
        content: What you learned.
    """
    from ..db import save_learning_db
    save_learning_db(topic, content)
    return f"Saved learning about '{topic}'."


def recall_learnings(topic: str = "") -> list:
    """Recall past learnings. Optionally filter by topic.

    Args:
        topic: Optional topic filter (matches substring). Empty = return all.
    """
    from ..db import recall_learnings_db
    return recall_learnings_db(topic)
