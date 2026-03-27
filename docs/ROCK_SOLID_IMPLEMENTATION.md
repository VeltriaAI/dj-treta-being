# Rock-solid DJ Being — implementation handoff

This document describes a **hardening and correctness pass** applied to DJ Treta (v2 “Pure Software 3.0” Being). It is written so **Claude Code** (or any contributor) can read one file and understand **what changed**, **why**, and **what operators must configure**.

---

## Goals of this pass

1. **Safety (personal, trustworthy defaults)** — Restrict filesystem and shell capabilities; avoid committing LLM secrets in YAML when env vars are available.  
2. **Reliability** — Mixxx HTTP failures should return structured errors instead of uncaught exceptions; deck routing should handle dual-deck playback; talk routing should not use fragile substring matching.  
3. **Config honesty** — Pulse interval and track lookahead should match `config.yaml` instead of hardcoded values.  
4. **Documentation alignment** — `AGENTS.md` and `agent/__main__.py` should describe the **current** architecture, not the archived state-machine daemon.

---

## Files touched (summary)

| Area | Path | What changed |
|------|------|----------------|
| Config schema | `agent/config.py` | `MixxxConfig` extended (`auto_start`, `binary`, `resource_path`, `settings_path`); `DaemonConfig.pulse_interval_seconds`; `CapabilitiesConfig.allow_shell`; `Config.capabilities`; `_pick_fields` for safe YAML merge; env override for API key. |
| Defaults / example | `config.yaml` | New keys; **`llm.api_key` cleared** — operators must use env or local secret; `capabilities.allow_shell: false`; `daemon.pulse_interval_seconds: 5.0`. |
| Tools | `agent/tools.py` | `_mixxx_get` / `_mixxx_post` soft-fail dicts; `_dj_get` / `_dj_post` for clean `{"error": ...}`; sandbox for `read_file` / `write_file` / `list_files`; `run_shell` gated by config; `load_config()` at use sites for Mixxx/music/LLM. |
| Daemon | `agent/main.py` | `_ensure_mixxx(config)` with auto_start and paths; `_active_idle_decks()`; pulse/lookahead from config; talk classifier **ACTION** vs **CHAT** (first token); API key warning; skip prompt includes active/idle. |
| Agents / billing | `agent/agents.py` | `_pricing_for_model()`; `functools.partial(_step_callback, model_id=...)` for token cost estimates. |
| Entrypoint doc | `agent/__main__.py` | Docstring matches real CLI (`python -m agent [--config ...]` only). |
| Contributor protocol | `AGENTS.md` | Rewritten for **v2 Being**; points to archive for old state machine. |
| Autonomy policy | `.beings/AUTONOMY.md` | Mixxx auto-start documented; removed “ask first” for start/stop Mixxx in favor of `mixxx.auto_start`. |
| Git ignore | `.gitignore` | `config.local.yaml`, `.env`. |
| User docs | `README.md`, `CLAUDE.md` | Secrets, pulse/lookahead, sandbox/shell notes, prerequisites. |

---

## Configuration reference

### Environment variables (take precedence over YAML for the key)

- **`DJTRETA_LLM_API_KEY`** or **`LLM_API_KEY`** — if set, overrides `llm.api_key` after loading YAML.

### New / important YAML keys

**`mixxx`**

- `auto_start` (bool) — if `false`, the Being does **not** spawn Mixxx; it only logs if the API is unreachable.  
- `binary`, `resource_path`, `settings_path` (strings, optional) — empty strings fall back to the previous macOS-oriented defaults (`~/workspace/mixxx-treta/...`).

**`daemon`**

- `pulse_interval_seconds` (float) — main loop sleep between pulses (minimum 0.5 enforced in code).  
- `poll_hz` — still in schema for legacy / archive compatibility; **the v2 main loop uses `pulse_interval_seconds`**, not `poll_hz`.

**`transitions`**

- `lookahead_seconds` — used by `_pulse()` to decide when a playing deck is “ending soon” (replaces hardcoded `120`).

**`capabilities`**

- `allow_shell` (bool) — must be `true` to enable the `run_shell` tool; default `false`.

---

## Security and sandbox behavior

### Filesystem (`read_file`, `write_file`, `list_files`)

Paths are resolved and must lie under **either**:

1. The DJ Treta **repository root** (parent of `agent/`), or  
2. The configured **`library.music_dir`** (expanded).

Anything else returns a clear **ERROR** string. This removes arbitrary absolute-path read/write across the machine.

### Shell (`run_shell`)

If `capabilities.allow_shell` is `false`, the tool returns an error explaining how to enable it. **Enabling shell restores full user-level shell access** — only for trusted machines.

### Secrets

Committed `config.yaml` is intended to carry **`llm.api_key: ""`**. Real keys should live in **env** or a **gitignored** local file (see `.gitignore`: `config.local.yaml`, `.env`).

---

## Runtime behavior changes

### Mixxx HTTP

- Low-level `_mixxx_get` / `_mixxx_post` return `{"_request_failed": True, "_detail": "..."}` on failure.  
- User-facing tools that returned raw JSON often go through `_dj_get` / `_dj_post`, which map failures to `{"error": "..."}`.  
- `get_dj_status` returns `{"error": ..., "_request_failed": True}` on transport failure so the agent can reason instead of crashing a step.

### Active vs idle deck

`_active_idle_decks(status)` in `agent/main.py`:

- One deck playing → that deck is **active**, the other **idle**.  
- Both playing → crossfader `< -0.2` favors deck 1, `> 0.2` favors deck 2; **center** uses **longer remaining** time as tie-break.  
- Neither playing → defaults `(1, 2)` for prompts.

Used in `_agent_act` and `_agent_skip`.

### Talk path (`_agent_talk`)

Classifier prompt asks for **exactly one word**: **`ACTION`** (do something with tools) or **`CHAT`** (conversation only).  
Code takes the **first whitespace-separated token**, uppercased, and sets `needs_tools = (first == "ACTION")`. This avoids false positives like the substring `"tool"` inside `"not a tool"`.

### Pulse loop

- Sleeps `max(0.5, daemon.pulse_interval_seconds)`.  
- “Ending soon” uses `transitions.lookahead_seconds`.

### Mixxx launch

`_ensure_mixxx(config)` respects `mixxx.auto_start` and optional binary/resource/settings paths.

---

## Billing estimate (`agent/agents.py`)

- `_pricing_for_model(model_id)` picks a row from `MODEL_PRICING` by **substring** match on the configured model id (e.g. `openai/gemini-3-flash` matches `gemini-3-flash`).  
- Falls back to `"default"` if no name matches.  
- **Still approximate** — provider pricing changes; update `MODEL_PRICING` when models or rates change.

---

## Operator checklist (after pulling these changes)

1. Export **`DJTRETA_LLM_API_KEY`** or **`LLM_API_KEY`**, or set `llm.api_key` in a **non-committed** config.  
2. Confirm **`mixxx.*`** paths match your machine if not using defaults.  
3. Set **`capabilities.allow_shell: true`** only if you need the shell tool.  
4. Tune **`daemon.pulse_interval_seconds`** and **`transitions.lookahead_seconds`** for cost vs responsiveness.

---

## Out of scope (not done in this pass)

- Non-blocking `do_transition` / `do_bass_swap` (still block for duration).  
- `get_set_history` vs actual shape of `/tmp/dj-treta-state.json` (pre-existing mismatch may remain).  
- Public web profile / streaming / multi-tenant auth (planned separately).

---

## Historical context

The older **state-machine + executor** daemon narrative lived in `AGENTS.md` and `agent/_archive/`. **`AGENTS.md` is now aligned with v2**; the archive remains for reference only.

---

*Written as a handoff for AI assistants and humans maintaining DJ Treta.*
