"""brain.py — kept for backward compatibility.

The DJBrain class is deprecated in v2. Agent creation is now in agents.py.
Talk fast-path is now in main.py.

This file is kept so old imports don't break.
"""

# Re-export from agents.py for compatibility
from .agents import create_dj_agent, _load_system_prompt
