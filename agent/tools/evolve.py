"""Self-modification tools — DJ Treta can improve her own code.

Safety: git worktree isolation, test gate, PR-based review, SOUL.md readonly.
"""

import json
import logging
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger("dj-treta")

_REPO_ROOT = Path(__file__).parent.parent.parent
_CLAUDE_BIN = Path("~/.local/bin/claude").expanduser()

# Files NEVER allowed to be modified
_READONLY_FILES = {".beings/SOUL.md", ".env", ".git/"}

# Directories the Being CAN modify
_ALLOWED_SCOPES = {"agent/", "tests/", "docs/", "templates/", ".beings/MEMORY.md", ".beings/GOALS.md", ".beings/AUTONOMY.md"}


def _validate_scope(scope: str) -> bool:
    """Check if scope is within allowed directories."""
    return any(scope.startswith(s) or scope == s for s in _ALLOWED_SCOPES)


def _slugify(text: str) -> str:
    """Convert text to branch-safe slug."""
    return re.sub(r'[^a-z0-9]+', '-', text.lower().strip())[:40].strip('-')


def evolve(goal: str, scope: str = "agent/", run_tests: bool = True) -> str:
    """Improve your own code. Runs Claude Code in a git worktree, creates a PR.

    Args:
        goal: What to improve. Be specific. Example: "Add docstrings to heartbeat.py"
        scope: Directory to focus on (must be in allowed scope). Default: "agent/"
        run_tests: Whether to run pytest before creating PR (default True).

    Returns:
        PR URL if successful, or error description.
    """
    # Validate scope
    if not _validate_scope(scope):
        return f"ERROR: Scope '{scope}' not allowed. Allowed: {_ALLOWED_SCOPES}"

    ts = int(time.time())
    slug = _slugify(goal)
    branch = f"evolve/{ts}-{slug}"
    worktree_path = Path(f"/tmp/dj-treta-evolve-{ts}")

    try:
        # 1. Create git worktree
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree_path)],
            cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return f"ERROR: Failed to create worktree: {result.stderr[:200]}"

        log.info(f"Evolution: worktree at {worktree_path}, branch {branch}")

        # 2. Run Claude Code CLI in the worktree
        prompt = (
            f"You are improving DJ Treta's code.\n\n"
            f"GOAL: {goal}\n"
            f"SCOPE: Only modify files under '{scope}'\n\n"
            f"RULES:\n"
            f"- NEVER modify these files: {', '.join(_READONLY_FILES)}\n"
            f"- Only modify files under: {', '.join(_ALLOWED_SCOPES)}\n"
            f"- Write clean, tested code\n"
            f"- Follow existing patterns in the codebase\n"
        )

        # Notification command — tells the main Treta session about the result
        notify_cmd = (
            f'echo \'{{"type":"evolution_complete","goal":"{goal[:60]}","branch":"{branch}"}}\' '
            f'> /tmp/dj-treta-evolution-result.json'
        )

        claude_result = subprocess.run(
            [
                str(_CLAUDE_BIN), "--print",
                "--output-format", "json",
                "--dangerously-skip-permissions",
                "--model", "opus",
                "--tools", "Edit,Write,Bash,Read,Glob,Grep",
                "-p", prompt + f"\n\nWhen done, run this to notify: {notify_cmd}",
            ],
            cwd=str(worktree_path),
            capture_output=True, text=True, timeout=600,  # 10 min for Opus
        )

        if claude_result.returncode != 0:
            return f"ERROR: Claude Code failed: {claude_result.stderr[:300]}"

        # 3. Check for readonly file violations
        diff_result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(worktree_path), capture_output=True, text=True,
        )
        changed_files = diff_result.stdout.strip().split("\n") if diff_result.stdout.strip() else []

        # Also check untracked files
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(worktree_path), capture_output=True, text=True,
        )
        if untracked.stdout.strip():
            changed_files.extend(untracked.stdout.strip().split("\n"))

        for f in changed_files:
            for readonly in _READONLY_FILES:
                if f.startswith(readonly) or f == readonly.rstrip('/'):
                    return f"ABORT: Evolution tried to modify readonly file: {f}"

        if not changed_files:
            return "No changes made — nothing to evolve."

        # 4. Run tests if required
        if run_tests:
            test_result = subprocess.run(
                [str(_REPO_ROOT / ".venv" / "bin" / "python"), "-m", "pytest", "tests/", "-x", "--tb=short", "-q"],
                cwd=str(worktree_path), capture_output=True, text=True, timeout=120,
            )
            if test_result.returncode != 0:
                return f"TESTS FAILED — PR not created.\n{test_result.stdout[-500:]}"

        # 5. Commit changes
        subprocess.run(["git", "add", "-A"], cwd=str(worktree_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"evolve: {goal[:70]}\n\nSelf-evolution by DJ Treta.\nScope: {scope}"],
            cwd=str(worktree_path), capture_output=True, text=True,
        )

        # 6. Push branch
        push_result = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=str(worktree_path), capture_output=True, text=True, timeout=60,
        )
        if push_result.returncode != 0:
            return f"ERROR: Push failed: {push_result.stderr[:200]}"

        # 7. Create PR
        pr_result = subprocess.run(
            ["gh", "pr", "create",
             "--title", f"evolve: {goal[:60]}",
             "--body", f"## Self-Evolution\n\n**Goal:** {goal}\n**Scope:** {scope}\n**Files:** {', '.join(changed_files[:10])}\n\n🤖 Auto-generated by DJ Treta's Evolution Protocol",
             "--base", "main",
             "--head", branch,
             "--label", "evolution"],
            cwd=str(worktree_path), capture_output=True, text=True, timeout=30,
        )

        pr_url = pr_result.stdout.strip() if pr_result.returncode == 0 else "PR creation failed"

        # 8. Log to DB
        try:
            from ..db import get_db
            db = get_db()
            db.execute(
                "INSERT INTO evolution_log (goal, scope, status, pr_url, branch_name, cost_usd, triggered_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (goal, scope, "success" if "github.com" in pr_url else "failed", pr_url, branch, 0, "manual")
            )
            db.commit()
            db.close()
        except Exception:
            pass

        log.info(f"Evolution complete: {pr_url}")
        return f"Evolution PR created: {pr_url}\nFiles changed: {', '.join(changed_files[:5])}"

    except subprocess.TimeoutExpired:
        return "ERROR: Evolution timed out (5 min limit)"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    finally:
        # Always clean up worktree
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=str(_REPO_ROOT), capture_output=True, timeout=30,
            )
        except Exception:
            pass


def propose_change(description: str, files: str = "") -> str:
    """Propose a code change without executing it. Logs to evolution_log for review.

    Use when you have an idea but want to think more or get approval first.

    Args:
        description: What you want to change and why.
        files: Comma-separated list of files that would be affected.
    """
    try:
        from ..db import get_db
        db = get_db()
        db.execute(
            "INSERT INTO evolution_log (goal, scope, status, triggered_by) VALUES (?, ?, 'proposed', 'manual')",
            (description, files)
        )
        db.commit()
        db.close()
    except Exception:
        pass

    log.info(f"Evolution proposed: {description[:100]}")
    return f"Proposal logged: {description}"


def review_evolution(pr_number: int) -> str:
    """Check the status of a previously created evolution PR.

    Args:
        pr_number: GitHub PR number to check.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state,title,reviews,statusCheckRollup"],
            cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout[:1000]
        return f"ERROR: {result.stderr[:200]}"
    except Exception as e:
        return f"ERROR: {e}"
