"""brain.py — kept for backward compatibility.

The DJBrain class is deprecated in v2. Agent creation is now in agents.py.
Talk fast-path is now in main.py.

This file is kept so old imports don't break.
"""

# Re-export from agents.py for compatibility. `create_agents` returns a
# tuple (being, dj, planner, library, producer); legacy callers expected
# `create_dj_agent` returning just the DJ root agent.
from .agents import create_agents, _load_system_prompt


def create_dj_agent(*args, **kwargs):
    """Legacy shim: returns the DJ root agent from the v8 agent graph."""
    return create_agents(*args, **kwargs)[1]
