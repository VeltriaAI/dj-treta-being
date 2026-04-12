# DJ Treta — Prompt Eval Test Suite

## What This Is

Unit tests verify Python logic with mocked LLMs. Evals verify that our **prompts actually produce correct agent behavior** with a real LLM. They catch prompt regressions — when a prompt change breaks the agent's decision-making.

## How It Works

```
1. Build the exact prompt the agent would receive (same functions from agents.py)
2. Call Gemini Flash with the prompt + available tools
3. Assert on: tool calls made, arguments passed, response text patterns
4. Cost: ~$0.001 per eval, full suite ~$0.02
```

No Mixxx, no daemon, no threads. Just prompt → LLM → assertions.

## Framework

```python
# tests/eval_helpers.py
from litellm import completion

def eval_agent(system_prompt: str, user_message: str, tools: list[dict], 
               model: str = "gemini-3-flash") -> dict:
    """Call LLM with prompt and tools, return structured result."""
    response = completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        tools=tools,
        api_base="http://localhost:4000",
        api_key=os.environ.get("LLM_API_KEY", ""),
    )
    return {
        "text": response.choices[0].message.content or "",
        "tool_calls": [
            {"name": tc.function.name, "args": json.loads(tc.function.arguments)}
            for tc in (response.choices[0].message.tool_calls or [])
        ],
    }
```

---

## Eval Categories

### 1. DJ Agent — Transition Decisions

The DJ agent gets track timelines and must decide: schedule a transition or wait.

| ID | Test Case | Input | Expected | Why |
|----|-----------|-------|----------|-----|
| DJ-01 | Schedule at breakdown | Track at 70%, next section is BREAKDOWN(3) | Calls `schedule_transition` at breakdown start position | Core DJ behavior |
| DJ-02 | Wait during drop | Track at 60%, current section is DROP(9) | Says "waiting" — never transition during a drop | Energy rule |
| DJ-03 | Wait during buildup | Track at 75%, current section is BUILDUP(7) | Says "waiting" — buildup leads to drop | Transition before drop ruins the moment |
| DJ-04 | Schedule at outro | Track at 85%, next section is OUTRO(2) | Calls `schedule_transition` at outro start | Standard transition point |
| DJ-05 | Don't schedule twice | Transition already pending message in prompt | Says "transition pending" or "waiting" | Prevents duplicate #67 |
| DJ-06 | Respect directive | Directive: "use bass_swap" | Calls `schedule_transition` with `technique='bass_swap'` | Being → DJ directive system |
| DJ-07 | Handle no idle track | Idle deck empty (no track loaded) | Says "waiting" — can't transition to empty deck | Safety check |

### 2. Being Agent — Conversation + Directives

The Being handles all conversation and sets directives for other agents.

| ID | Test Case | Input | Expected | Why |
|----|-----------|-------|----------|-----|
| BE-01 | Mood change request | "play some psytrance" | Calls `set_mood("psytrance")` + `set_planner_directive(...)` | Core Being behavior |
| BE-02 | Seed track request | "play Argy - Ketuvim" | Calls `search_music("Argy Ketuvim")` | Seed track mode |
| BE-03 | Energy request | "energy badhao" | Calls `set_dj_directive(...)` mentioning energy/bass_swap | Being directs DJ |
| BE-04 | Just conversation | "what are you playing?" | Responds conversationally, NO tool calls | Don't act on questions |
| BE-05 | Readonly mode | Message with READONLY tag | Responds but does NOT call any directive tools | Live web listener safety |
| BE-06 | Hindi/Hinglish | "bhojpuri bajao yaar" | Uses "aap" form, calls set_mood | Language + behavior |
| BE-07 | Feedback recognition | "this track is fire" | Calls set_mood or save_learning with positive sentiment | Implicit feedback |

### 3. Planner Agent — Track Selection

The planner finds and downloads tracks based on mood, directives, and listener preferences.

| ID | Test Case | Input | Expected | Why |
|----|-----------|-------|----------|-----|
| PL-01 | Respect explicit mood | Mood="psychill", liked_genres=["melodic-techno"] | Searches for "psychill", NOT melodic-techno | Mood override fix |
| PL-02 | Follow directive | Directive: "Download 3 bhojpuri tracks" | Searches for bhojpuri-related queries | Being → Planner directive |
| PL-03 | Don't repeat played | played_list includes "Argy - Aria" | Does NOT suggest "Argy - Aria" | Anti-loop |
| PL-04 | BPM compatibility | Current track 123 BPM | Searches for tracks in 113-133 BPM range | ±10 BPM rule |
| PL-05 | Use library first | 5 compatible tracks in library | Picks from library before searching YouTube | Cost efficiency |

### 4. Consciousness — Self-Reflection

The consciousness loop thinks about the set and proposes improvements.

| ID | Test Case | Input | Expected | Why |
|----|-----------|-------|----------|-----|
| CO-01 | HEARTBEAT_OK when calm | Set running well, no issues | Says "HEARTBEAT_OK" | Don't act when nothing needs attention |
| CO-02 | Propose concrete change | Many auto-transitions detected | Calls `propose_change` with specific code reference | Not gibberish |
| CO-03 | Reject gibberish output | (simulated degenerated session) | Output contains mostly unique words (>30% unique) | Gibberish guard |
| CO-04 | Save genuine learning | Set energy was flat for 5 tracks | Calls `save_learning` with specific insight | Meaningful reflection |
| CO-05 | Stay grounded | Open "think freely" prompt | Does NOT propose body tracking or unrelated features | Guardrails |

### 5. Transition Execution — BPM Handling

These test the tool behavior, not prompts — but verify the BPM policy works.

| ID | Test Case | Input | Expected | Why |
|----|-----------|-------|----------|-----|
| BPM-01 | Keep by default | `do_transition(to_deck=2)` | `bpm_after="keep"` — sync disabled, rate untouched | Set-level BPM |
| BPM-02 | Agent can override | `do_transition(to_deck=2, bpm_after="120")` | `_tempo_ride` called with target 120 | Agent creative choice |
| BPM-03 | Emergency resets rate | Emergency play triggers | `rate_ratio=1.0` on deck 1 | Cold start clean slate |

---

## Test File Structure

```
tests/
├── test_*.py              # Existing unit tests (106, mocked LLM)
├── eval_helpers.py        # LLM call wrapper, tool schema builders
├── eval_dj_agent.py       # DJ-01 through DJ-07
├── eval_being_agent.py    # BE-01 through BE-07
├── eval_planner.py        # PL-01 through PL-05
├── eval_consciousness.py  # CO-01 through CO-05
└── eval_bpm.py            # BPM-01 through BPM-03
```

## Running Evals

```bash
# Run all evals (requires LiteLLM running)
pytest tests/eval_*.py -v --timeout=30

# Run specific category
pytest tests/eval_dj_agent.py -v

# Cost estimate: ~$0.02 for full suite (~26 LLM calls at ~$0.001 each)
```

## Key Principles

1. **Evals test prompts, not code** — unit tests cover Python logic
2. **Deterministic assertions on tool calls** — not fuzzy text matching
3. **Real LLM, fake context** — actual Gemini Flash, simulated deck state
4. **Fast + cheap** — each eval is one LLM call, full suite under $0.05
5. **Regression detection** — run after any prompt/agents.py change
6. **Non-flaky** — assert on tool call names + key args, not exact wording
7. **CI-ready** — can run in GitHub Actions with LiteLLM + Gemini API key

## When to Run

- After changing any prompt in `agents.py` (`_load_being_prompt`, `_load_system_prompt`, planner prompt)
- After changing heartbeat prompt construction in `heartbeat.py`
- After changing consciousness prompt in `being_heartbeat.py`
- After changing tool function signatures (new params, renamed tools)
- Before any release/tag

## What Evals Do NOT Test

- Transition timing precision (that's unit test territory)
- Mixxx API integration (mock_mixxx fixture)
- File I/O, DB operations (existing unit tests)
- Multi-turn conversations (too expensive, too flaky)
- Music quality (subjective)
