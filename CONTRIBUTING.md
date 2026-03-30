# Contributing to DJClaw

DJClaw started as a personal experiment and grew into something real. Contributions are welcome.

## Philosophy

**No deterministic DJ logic in tools.** Tools are dumb, the brain is smart. If you want the DJ to behave differently, change the prompt — not the code.

- `schedule_transition` is a tool the agent calls — it doesn't decide when to transition
- `do_transition` executes a crossfade — it doesn't decide which technique to use
- The agent sees reality (track timelines, BPM, key, energy) and makes decisions

## Areas Where Help Is Needed

- **Linux support** — currently macOS only, need to test Mixxx build + audio routing
- **More transition techniques** — creative mixing styles beyond the current 5
- **Better track selection** — improve BPM/key/energy matching heuristics in the planner
- **Beat detection** — real-time beat grid analysis vs relying on Gemini
- **New LLM providers** — test with Claude, GPT-4, local models via LiteLLM
- **Frontend** — improvements to [dj.treta.life](https://dj.treta.life) visualization
- **Documentation** — tutorials, setup guides, architecture explanations

## Development Setup

```bash
git clone https://github.com/VeltriaAI/dj-treta-being.git
cd dj-treta-being
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# You'll also need:
# - Mixxx fork (VeltriaAI/mixxx, branch feature/http-api)
# - A Gemini API key (or any LiteLLM-compatible model)
export DJTRETA_LLM_API_KEY="your-key"

# Run
djclaw start "melodic techno"
djclaw tui
```

## Code Style

- Python 3.10+ type hints
- No deterministic DJ logic in tools
- Agent prompt changes > code changes for behavior
- Keep tools simple — one responsibility each
- Use `_mixxx_get`/`_mixxx_post` for all Mixxx API calls (handles errors)

## Commit Convention

```
feat: new feature
fix: bug fix
evolve: DJ Treta evolution (architecture, prompts, behavior)
docs: documentation
```

## Pull Requests

1. Fork the repo
2. Create a feature branch: `git checkout -b feature/your-thing`
3. Make changes
4. Test with a running set (at least 3 transitions)
5. `gh pr create`

## License

MIT — do whatever you want with it.
