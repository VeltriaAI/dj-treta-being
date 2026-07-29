"""Pluggable slow-loop brain providers (2026-07-29).

Two-speed brain: the fast loop (mixing reflexes) stays on the LiteLLM/ADK
path — this module adds a SLOW-loop escape hatch that can run a brain role
on an agentic CLI instead:

  - ``claude-cli``  → ``claude -p`` headless (Claude Code)
  - ``codex-cli``   → ``codex exec`` non-interactive (OpenAI Codex)
  - ``litellm``     → explicit "use the existing path" (never handled here)

Config (``config.llm.brains``, optional — absent means NOTHING changes):

    llm:
      brains:
        library_manager:
          provider: claude-cli      # or codex-cli | litellm
          model: sonnet             # optional; provider default when empty
          timeout: 300              # seconds, optional

Design rule (proven the hard way on 07-15/07-28): the CLI brain CURATES,
code EXECUTES. Providers return text/JSON — they never touch decks, files,
or the DB. Callers parse the result (see ``extract_json``) and run the
actual tools deterministically. Every caller MUST fall back to the existing
LiteLLM/ADK path when the provider fails — a missing binary or a timeout
degrades to yesterday's behavior, never to silence.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("dj-treta")

CLI_PROVIDERS = ("claude-cli", "codex-cli")


def brain_for(llm_config, role: str) -> dict | None:
    """Return the brains-map entry for `role` when it names a CLI provider.

    None ⇒ caller uses the existing LiteLLM/ADK path (also for provider
    'litellm', unknown providers, or a missing binary — with a warning, so a
    typo'd config degrades loudly instead of silently).
    """
    brains = getattr(llm_config, "brains", None) or {}
    entry = brains.get(role)
    if not isinstance(entry, dict):
        return None
    provider = (entry.get("provider") or "").strip().lower()
    if provider in ("", "litellm"):
        return None
    if provider not in CLI_PROVIDERS:
        log.warning(f"brains.{role}: unknown provider '{provider}' — using litellm path")
        return None
    binary = "claude" if provider == "claude-cli" else "codex"
    if not shutil.which(binary):
        log.warning(f"brains.{role}: '{binary}' not on PATH — using litellm path")
        return None
    return {**entry, "provider": provider}


def run_cli_brain(brain: dict, prompt: str, *, allow_web: bool = True) -> str:
    """Run one prompt through a CLI brain and return its final text.

    Raises on non-zero exit, timeout, or empty output — callers catch and
    fall back. Providers run without repo write access: claude gets only
    read/web tools; codex runs in its read-only sandbox.
    """
    provider = brain["provider"]
    model = (brain.get("model") or "").strip()
    timeout = int(brain.get("timeout") or 300)

    if provider == "claude-cli":
        cmd = ["claude", "-p", prompt, "--output-format", "text"]
        if model:
            cmd += ["--model", model]
        if allow_web:
            cmd += ["--allowedTools", "WebSearch,WebFetch"]
        else:
            cmd += ["--allowedTools", ""]
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode != 0:
            raise RuntimeError(
                f"claude -p exit {out.returncode}: {out.stderr.strip()[:300]}")
        text = (out.stdout or "").strip()

    elif provider == "codex-cli":
        with tempfile.NamedTemporaryFile(
                mode="r", suffix=".txt", delete=False) as f:
            last_msg = f.name
        try:
            cmd = ["codex", "exec", "--sandbox", "read-only",
                   "--output-last-message", last_msg]
            if model:
                cmd += ["-m", model]
            cmd += [prompt]
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            if out.returncode != 0:
                raise RuntimeError(
                    f"codex exec exit {out.returncode}: {out.stderr.strip()[:300]}")
            text = Path(last_msg).read_text().strip()
            if not text:  # older codex builds print to stdout instead
                text = (out.stdout or "").strip()
        finally:
            Path(last_msg).unlink(missing_ok=True)
    else:  # pragma: no cover — brain_for() filters these
        raise ValueError(f"not a CLI provider: {provider}")

    if not text:
        raise RuntimeError(f"{provider} returned empty output")
    log.info(f"[brain:{provider}] {len(text)} chars in reply")
    return text
