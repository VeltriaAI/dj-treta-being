"""First-run setup wizard for `djclaw setup`.

Asks 4 questions, writes ~/.config/djclaw/{config.yaml,secrets.env,litellm.yaml}.
Idempotent in the sense that re-running with `--reconfigure` overwrites;
without that flag, it refuses to overwrite an existing config.

The wizard is intentionally tiny — no fancy TUI library, just stdin
prompts. Runs cleanly inside `curl … | sh` because it gets attached
to a terminal via the install.sh subshell.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path("~/.config/djclaw").expanduser()
TEMPLATES = Path(__file__).parent / "templates"
DEFAULT_MUSIC_DIR = "~/Music/DJTreta"
DEFAULT_MOOD = "melodic techno"


@dataclass
class Provider:
    """Describes one LLM provider option in the wizard."""

    key: str            # short id, e.g. 'gemini-flash'
    label: str          # what we show the user
    model_name: str     # `llm.model` in config.yaml; `model_name` in litellm.yaml
    litellm_model: str  # actual provider/model passed to litellm
    api_key_env: str    # which env var litellm reads for the key
    extra_env: dict[str, str] | None = None  # provider-specific env (project, location)
    notes: str = ""     # surfaced after the user picks


PROVIDERS: list[Provider] = [
    Provider(
        key="gemini-flash",
        label="Gemini 2.5 Flash — Google AI Studio API key (fastest, easiest)",
        model_name="dj-treta-flash",
        litellm_model="gemini/gemini-2.5-flash",
        api_key_env="GEMINI_API_KEY",
        notes="Get a key at https://aistudio.google.com/apikey (free tier available).",
    ),
    Provider(
        key="gemini-enterprise",
        label="Gemini Enterprise — formerly Vertex AI, GCP project + service account",
        model_name="dj-treta-flash",
        litellm_model="vertex_ai/gemini-2.5-flash",
        api_key_env="",  # uses ADC, not an API key
        extra_env={
            "DJTRETA_VERTEX_PROJECT": "<your-gcp-project-id>",
            "DJTRETA_VERTEX_LOCATION": "us-central1",
            "GOOGLE_APPLICATION_CREDENTIALS": "<path-to-service-account-key.json>",
        },
        notes=(
            "Run `gcloud auth application-default login` OR set "
            "GOOGLE_APPLICATION_CREDENTIALS in secrets.env."
        ),
    ),
    Provider(
        key="anthropic",
        label="Anthropic — Claude / Haiku via Anthropic API key",
        model_name="dj-treta-haiku",
        litellm_model="anthropic/claude-haiku-4-5",
        api_key_env="ANTHROPIC_API_KEY",
        notes="Get a key at https://console.anthropic.com/settings/keys.",
    ),
    Provider(
        key="openai",
        label="OpenAI — GPT-4o-mini via OpenAI API key",
        model_name="dj-treta-openai",
        litellm_model="openai/gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        notes="Get a key at https://platform.openai.com/api-keys.",
    ),
]


# ─── Prompt helpers ───────────────────────────────────────────────────


def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"  {question}{suffix} ").strip()
    return raw or default


def _prompt_choice(question: str, options: list[str], default_index: int = 0) -> int:
    print(f"  {question}")
    for i, opt in enumerate(options, 1):
        marker = "▸" if (i - 1) == default_index else " "
        print(f"    {marker} {i}) {opt}")
    while True:
        raw = input(f"  Pick [{default_index + 1}]: ").strip()
        if not raw:
            return default_index
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(options):
                return n - 1
        print("  (Enter a number from the list.)")


def _prompt_secret(question: str, allow_blank: bool = True) -> str:
    """Prompt for a secret. Echoes for clarity — this runs in install.sh
    where we can't easily disable echo and the user is likely the only
    one watching their terminal anyway. They can also leave it blank
    and edit secrets.env later.
    """
    raw = input(f"  {question} ").strip()
    if not raw and not allow_blank:
        return _prompt_secret(question, allow_blank=False)
    return raw


# ─── Mixxx path discovery ─────────────────────────────────────────────


def _discover_mixxx_paths() -> tuple[str, str, str]:
    """Best-effort: find Mixxx binary + resource path under the
    installer-managed ~/.local/share/djclaw/mixxx/current/.

    Returns (binary, resource_path, settings_path). Empty strings if
    nothing found — agent's auto_start will skip and we'll log it.
    """
    base = Path("~/.local/share/djclaw/mixxx/current").expanduser()
    if not base.exists():
        return "", "", ""

    # macOS: Mixxx.app/Contents/MacOS/mixxx + Mixxx.app/Contents/Resources
    app = next(base.glob("*.app"), None)
    if app:
        return (
            str(app / "Contents" / "MacOS" / "mixxx"),
            str(app / "Contents" / "Resources"),
            str(Path("~/Library/Application Support/Mixxx-Treta").expanduser()),
        )

    # Linux DEB extraction: usr/bin/mixxx + usr/share/mixxx
    bin_path = base / "usr" / "bin" / "mixxx"
    if bin_path.exists():
        return (
            str(bin_path),
            str(base / "usr" / "share" / "mixxx"),
            str(Path("~/.config/mixxx-treta").expanduser()),
        )

    return "", "", ""


# ─── File generation ──────────────────────────────────────────────────


def _render_config_yaml(music_dir: str, default_mood: str, provider: Provider) -> str:
    tpl = (TEMPLATES / "config.yaml.tpl").read_text()
    binary, resource_path, settings_path = _discover_mixxx_paths()
    return tpl.format(
        music_dir=music_dir,
        default_mood=default_mood,
        llm_model=provider.model_name,
        mixxx_binary=binary,
        mixxx_resource_path=resource_path,
        mixxx_settings_path=settings_path,
    )


def _render_secrets_env(provider: Provider, llm_api_key: str) -> str:
    tpl = (TEMPLATES / "secrets.env.tpl").read_text()
    extra = provider.extra_env or {}
    lines = []
    if provider.api_key_env:
        lines.append(f"{provider.api_key_env}={llm_api_key}")
    for k, v in extra.items():
        lines.append(f"# {k}={v}")  # leave commented for the user to fill in
    provider_specific = "\n".join(lines) if lines else "# (no extra env for this provider)"
    return tpl.format(
        provider_label=provider.label,
        llm_api_key=llm_api_key or "<paste-key-here>",
        provider_specific_lines=provider_specific,
    )


def _render_litellm_block(provider: Provider, *, active: bool) -> str:
    """Render one model_list block. If active, no leading `#`; if not,
    every line is commented out so the user can flip later by
    uncommenting + re-starting.
    """
    api_key_line = ""
    if provider.api_key_env:
        api_key_line = f"      api_key: os.environ/{provider.api_key_env}\n"

    extra_lines = ""
    if provider.extra_env:
        for k in provider.extra_env:
            # Vertex/GCP params live on the litellm_params dict directly,
            # not via api_key. Keep simple: show as os.environ refs.
            param_name = k.lower().removeprefix("djtreta_")
            if param_name.startswith("vertex_"):
                extra_lines += f"      {param_name}: os.environ/{k}\n"
            elif k == "GOOGLE_APPLICATION_CREDENTIALS":
                # litellm picks this up from env automatically; no
                # litellm_params entry needed
                pass

    block = (
        f"  - model_name: {provider.model_name}\n"
        f"    litellm_params:\n"
        f"      model: {provider.litellm_model}\n"
        f"{api_key_line}"
        f"{extra_lines}"
    )
    if active:
        return block
    return "\n".join("# " + line if line.strip() else line for line in block.splitlines())


def _render_litellm_yaml(active: Provider) -> str:
    tpl = (TEMPLATES / "litellm.yaml.tpl").read_text()
    others = [p for p in PROVIDERS if p.key != active.key]
    return tpl.format(
        active_block=_render_litellm_block(active, active=True),
        commented_blocks="\n# ───── Alternate providers (uncomment one to switch) ─────\n"
        + "\n".join(_render_litellm_block(p, active=False) for p in others),
    )


# ─── Main entry point ────────────────────────────────────────────────


def run_wizard(reconfigure: bool = False) -> int:
    config_path = CONFIG_DIR / "config.yaml"
    secrets_path = CONFIG_DIR / "secrets.env"
    litellm_path = CONFIG_DIR / "litellm.yaml"

    if config_path.exists() and not reconfigure:
        print(
            f"\n  Config already exists at {config_path}.\n"
            "  Re-run with `djclaw setup --reconfigure` to overwrite, "
            "or edit the file directly.\n"
        )
        return 0

    print(
        "\n"
        "  ╭───────────────────────────────╮\n"
        "  │      DJClaw — first-run       │\n"
        "  ╰───────────────────────────────╯\n"
    )

    music_dir = _prompt("Where should your music library live?", DEFAULT_MUSIC_DIR)
    music_dir_expanded = Path(music_dir).expanduser()
    music_dir_expanded.mkdir(parents=True, exist_ok=True)

    print()
    idx = _prompt_choice(
        "Which LLM provider do you want to use?",
        [p.label for p in PROVIDERS],
    )
    provider = PROVIDERS[idx]
    if provider.notes:
        print(f"\n  ⓘ {provider.notes}")

    print()
    if provider.api_key_env:
        api_key = _prompt_secret(
            f"Paste your {provider.api_key_env} (will save to {secrets_path}, chmod 600). "
            "Leave blank to fill in later:"
        )
    else:
        # Vertex/Gemini Enterprise uses ADC, not a paste-able key
        api_key = ""
        print(
            f"  ⓘ {provider.label} uses Application Default Credentials.\n"
            "    Run `gcloud auth application-default login` after setup.\n"
        )

    print()
    default_mood = _prompt("Default mood for fresh sets?", DEFAULT_MOOD)

    # Render + write everything atomically — chmod 600 on secrets.
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_render_config_yaml(music_dir, default_mood, provider))
    secrets_path.write_text(_render_secrets_env(provider, api_key))
    secrets_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
    litellm_path.write_text(_render_litellm_yaml(provider))

    print(
        f"\n  ✓ wrote {config_path}\n"
        f"  ✓ wrote {secrets_path} (chmod 600)\n"
        f"  ✓ wrote {litellm_path}\n"
        f"  ✓ music dir at {music_dir_expanded}\n"
        f"\n  Done. Next:\n"
        f"      djclaw doctor      # confirm everything is wired up\n"
        f"      djclaw start       # bring up Mixxx + agent\n"
        "\n"
    )
    return 0


def main() -> int:
    """CLI entry — `djclaw setup [--reconfigure]`."""
    reconfigure = "--reconfigure" in sys.argv
    return run_wizard(reconfigure=reconfigure)


if __name__ == "__main__":
    raise SystemExit(main())
