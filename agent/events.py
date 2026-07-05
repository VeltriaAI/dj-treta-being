"""Typed internal event seam (NS-002).

Framework-independent event dataclasses that every consumer (TUI via
ws_broadcast, thinking.log, notebook, billing snapshot) is constructed
through. The on-the-wire JSON contract is byte-stable with the previous
hand-built dicts — see tests/test_events.py for the captured literals.

IMPORTANT: ``to_wire()`` must return a FRESH mutable dict on every call.
ws_server stamps ``"ts"`` into the dict in place and ring-buffers it for
replay; a shared dict would corrupt already-buffered frames.
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class ThinkEvent:
    """Agent free-text thinking. ``text`` is expected pre-truncated by the
    emit site ([:500] for broadcast/log, [:300] for notebook) — this type
    does not truncate."""
    agent: str
    text: str

    def to_wire(self) -> dict:
        return {
            "agent": self.agent,
            "type": "think",
            "text": self.text,
        }

    def log_line(self) -> str:
        return f"[THINK:{self.agent}] {self.text}"

    def notebook_payload(self) -> dict:
        return {"text": self.text}


@dataclass
class CallEvent:
    """Agent tool call. ``args`` is the already-stringified, already-
    truncated ([:200]) argument repr from the emit site."""
    agent: str
    tool: str
    args: str

    def to_wire(self) -> dict:
        return {
            "agent": self.agent,
            "type": "call",
            "tool": self.tool,
            "args": self.args,
        }

    def log_line(self) -> str:
        return f"[CALL:{self.agent}] {self.tool}({self.args})"

    def notebook_payload(self) -> dict:
        return {"tool": self.tool, "args": self.args}


@dataclass
class BillingEvent:
    """Wraps the ``_apply_billing`` snapshot dict verbatim. Fields are NOT
    re-modelled as dataclass attrs — ``key_spend`` is conditionally present
    and the schema is owned by ``_apply_billing``."""
    snapshot: dict

    def to_wire(self) -> dict:
        # Shallow copy: fresh top-level dict per call (ws_server stamps ts).
        return dict(self.snapshot)


@runtime_checkable
class AgentInvoker(Protocol):
    """Structural type for anything that can broadcast events to the TUI."""

    def _ws_broadcast(self, event: str, data: Any) -> None: ...
