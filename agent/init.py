"""DJClaw init — interactive setup wizard for new DJ Beings."""

import os
import re
import sys
from pathlib import Path


TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
BEINGS_DIR = Path(__file__).parent.parent / ".beings"

LLM_PROVIDERS = {
    "1": {
        "name": "Google Gemini (via LiteLLM)",
        "model": "openai/gemini-3-flash",
        "api_base": "http://localhost:4000",
    },
    "2": {
        "name": "OpenAI",
        "model": "gpt-4o-mini",
        "api_base": "https://api.openai.com/v1",
    },
    "3": {
        "name": "Anthropic Claude",
        "model": "claude-sonnet-4-20250514",
        "api_base": "https://api.anthropic.com",
    },
    "4": {
        "name": "Local (Ollama)",
        "model": "ollama/llama3",
        "api_base": "http://localhost:11434",
    },
}


def _slugify(name: str) -> str:
    """Convert 'DJ Rajesh' to 'DJRajesh'."""
    return re.sub(r"[^a-zA-Z0-9]", "", name)


def _cli_name(name: str) -> str:
    """Convert 'DJ Rajesh' to 'djrajesh' (CLI command name)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _build_taste_description(genres: str, vibe: str) -> str:
    """Build a taste description from user input."""
    lines = [
        f"I am drawn to {vibe}.\n",
        "Genres I gravitate toward (but I'm not limited to):",
    ]
    for genre in genres.split(","):
        genre = genre.strip()
        if genre:
            lines.append(f"- {genre}")
    lines.append("\nI discover new artists by searching, listening, and following the music — not a fixed playlist.")
    lines.append("Every set should have at least one track I've never played before.")
    return "\n".join(lines)


def _render_template(template_name: str, context: dict) -> str:
    """Simple string template rendering using str.format_map."""
    template_path = TEMPLATES_DIR / template_name
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    content = template_path.read_text()
    return content.format_map(context)


def run_init():
    """Interactive setup wizard."""
    print("\n  DJClaw — Create Your DJ Being\n")

    # DJ name
    dj_name = input("  What should I call your DJ? [DJ Treta]: ").strip()
    if not dj_name:
        dj_name = "DJ Treta"

    # Music taste
    genres = input(f"  What kind of music does {dj_name} love?\n  (comma separated, e.g. melodic techno, psytrance, bhojpuri): ").strip()
    if not genres:
        genres = "melodic techno, progressive house, deep house"

    vibe = input(f"  What's the vibe?\n  (e.g. deep and emotional, party energy, chill ambient): ").strip()
    if not vibe:
        vibe = "music where intelligence and emotion aren't opposites"

    # User info
    user_name = input("  Your name? [Listener]: ").strip()
    if not user_name:
        user_name = "Listener"

    music_prefs = input(f"  Any specific preferences for {user_name}? [surprise me]: ").strip()
    if not music_prefs:
        music_prefs = "Surprise me with good music. I trust the DJ."

    # LLM provider
    print("\n  LLM provider:")
    for key, provider in LLM_PROVIDERS.items():
        print(f"    {key}. {provider['name']}")
    llm_choice = input("  Choose [1]: ").strip()
    if llm_choice not in LLM_PROVIDERS:
        llm_choice = "1"
    provider = LLM_PROVIDERS[llm_choice]

    # Default mood
    default_mood = input(f"  Default mood for sets? [deep]: ").strip()
    if not default_mood:
        default_mood = "deep"

    # Build context
    dj_slug = _slugify(dj_name)
    taste_desc = _build_taste_description(genres, vibe)

    template_context = {
        "dj_name": dj_name,
        "dj_name_slug": dj_slug,
        "taste_description": taste_desc,
        "user_name": user_name,
        "music_preferences": music_prefs,
        "communication_style": "Be brief, warm, direct.",
        "llm_model": provider["model"],
        "llm_api_base": provider["api_base"],
        "default_mood": default_mood,
    }

    # Generate files
    BEINGS_DIR.mkdir(parents=True, exist_ok=True)

    soul_content = _render_template("SOUL.md", template_context)
    (BEINGS_DIR / "SOUL.md").write_text(soul_content)
    print(f"\n  Created .beings/SOUL.md")

    user_content = _render_template("USER.md", template_context)
    (BEINGS_DIR / "USER.md").write_text(user_content)
    print(f"  Created .beings/USER.md")

    config_content = _render_template("config.yaml", template_context)
    config_path = Path(__file__).parent.parent / "config.yaml"
    config_path.write_text(config_content)
    print(f"  Created config.yaml")

    # Create empty MEMORY.md and GOALS.md if they don't exist
    memory = BEINGS_DIR / "MEMORY.md"
    if not memory.exists():
        memory.write_text(f"# MEMORY.md — {dj_name}\n\n*No memories yet. They'll come after the first set.*\n")
        print(f"  Created .beings/MEMORY.md")

    goals = BEINGS_DIR / "GOALS.md"
    if not goals.exists():
        goals.write_text(f"# GOALS.md — {dj_name}\n\n- Play my first set\n- Discover my sound\n- Learn from every track\n")
        print(f"  Created .beings/GOALS.md")

    # Create music directory
    music_dir = Path(f"~/Music/{dj_slug}").expanduser()
    music_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Music directory: {music_dir}")

    # Create Being CLI alias (symlink to djclaw)
    cli_cmd = _cli_name(dj_name)
    _create_cli_alias(cli_cmd)

    print(f"\n  {dj_name} is ready!")
    print(f"  Run: {cli_cmd} start")
    if llm_choice == "1":
        print(f"  (Make sure LiteLLM is running on localhost:4000)")
    print(f"  Set your API key: export DJTRETA_LLM_API_KEY='your-key'\n")


def _create_cli_alias(cli_cmd: str):
    """Create a CLI symlink for the Being: djrajesh → djclaw."""
    # Find where djclaw lives
    djclaw_bin = Path(sys.executable).parent / "djclaw"
    if not djclaw_bin.exists():
        # Fallback: search PATH
        import shutil
        found = shutil.which("djclaw")
        if found:
            djclaw_bin = Path(found)
        else:
            print(f"  [warn] Could not find djclaw binary — skipping CLI alias")
            return

    alias_bin = djclaw_bin.parent / cli_cmd
    if alias_bin.exists():
        if alias_bin.resolve() == djclaw_bin.resolve():
            print(f"  CLI alias: {cli_cmd} (already exists)")
            return
        # Don't overwrite something that isn't ours
        print(f"  [warn] {alias_bin} already exists — skipping CLI alias")
        return

    try:
        os.symlink(str(djclaw_bin), str(alias_bin))
        print(f"  CLI alias: {cli_cmd} → djclaw")
    except OSError as e:
        print(f"  [warn] Could not create CLI alias: {e}")
