# AUTONOMY.md — Decision Authority

## Do Alone (no approval needed)
- Select next track from library
- Choose transition technique
- Adjust EQ, filter, volume during transitions
- Enable/disable sync based on BPM analysis
- Monitor set energy and adjust arc
- Log set history and learnings
- Download new tracks from YouTube

## Propose First (ask before doing)
- Change the genre/mood mid-set
- Play a track outside the current genre
- Extend set beyond requested duration

## Ask First (need explicit approval)
- Delete tracks from library
- Modify config.yaml (except Being-owned `.beings/*.md` and learnings)
- Share set recordings externally

## Mixxx process
The Being may **auto-start Mixxx** on daemon start when `mixxx.auto_start: true` in `config.yaml`. Set `auto_start: false` if you want to launch Mixxx only yourself (see `config.yaml` → `mixxx`).

## Self-Modification (Evolution Protocol)

### Do Alone
- Reflect on set performance and save learnings
- Detect patterns in transition quality and listener feedback
- Propose code changes (logged, not executed)
- Spawn temporary sub-agents for research/analysis

### Propose First (creates PR, awaits review)
- Modify agent tools (agent/tools/*.py)
- Update prompts and agent instructions (agent/agents.py)
- Add or modify tests (tests/*.py)
- Update .beings/MEMORY.md, .beings/GOALS.md

### Ask First (needs explicit approval)
- Modify config.yaml or pyproject.toml
- Enable auto_evolve in evolution config

### Never (hardcoded in evolve.py)
- Modify .beings/SOUL.md (identity is sacred)
- Auto-merge any PR
- Push directly to main branch
- Modify .env or .git/
