"""NS-002 wire-contract tests for agent/events.py.

The literals below are PRE-CHANGE captures, recorded before the emit sites
were routed through the event types:

- think/call: exact dict shapes hand-built at agent/adk_runner.py
  _process_event (think branch ~304-308, call branch ~333-338) and
  planner_loop._emit_kb (~89-93) as of branch bdl/adopt-lifecycle HEAD.
- billing: no live billing.json existed under the runtime tmp dir at capture
  time, so the literal is constructed from _apply_billing's return shape
  (agent/adk_runner.py:25-55): total_input_tokens, total_output_tokens,
  total_cost_usd, calls, by_agent{name:{input,output,cost,calls}},
  session_start, and conditionally key_spend.

to_wire() must be byte-identical to these dicts (raw and sort_keys
json.dumps) and must return a FRESH dict per call — ws_server stamps "ts"
in place and ring-buffers the dict for replay.
"""

import json

from agent.events import AgentInvoker, BillingEvent, CallEvent, ThinkEvent

# ---- Pre-change captured literals -----------------------------------------

THINK_LITERAL = {
    "agent": "dj",
    "type": "think",
    "text": "The floor is peaking; I'll hold this energy for one more track.",
}

PLANNER_THINK_LITERAL = {
    "agent": "planner",
    "type": "think",
    "text": "[WARN] library scan returned 0 candidates for genre=dub techno",
}

CALL_LITERAL = {
    "agent": "dj",
    "type": "call",
    "tool": "load_track",
    "args": "{'deck': 2, 'path': '/tracks/artist - song.mp3'}",
}

BILLING_LITERAL = {
    "total_input_tokens": 12345,
    "total_output_tokens": 678,
    "total_cost_usd": 0.0421,
    "calls": 7,
    "by_agent": {
        "dj": {"input": 9000, "output": 500, "cost": 0.03, "calls": 5},
        "planner": {"input": 3345, "output": 178, "cost": 0.0121, "calls": 2},
    },
    "session_start": 1751700000.123,
    "key_spend": 1.2345,
}

BILLING_LITERAL_NO_KEY_SPEND = {
    k: v for k, v in BILLING_LITERAL.items() if k != "key_spend"
}


def _assert_wire_identical(wire: dict, literal: dict):
    assert json.dumps(wire) == json.dumps(literal)
    assert json.dumps(wire, sort_keys=True) == json.dumps(literal, sort_keys=True)
    for k, v in literal.items():
        assert type(wire[k]) is type(v), f"type drift on key {k!r}"


# ---- ThinkEvent ------------------------------------------------------------

def test_think_wire_matches_capture():
    ev = ThinkEvent(agent="dj", text=THINK_LITERAL["text"])
    _assert_wire_identical(ev.to_wire(), THINK_LITERAL)


def test_planner_think_wire_matches_capture():
    ev = ThinkEvent(agent="planner", text=PLANNER_THINK_LITERAL["text"])
    _assert_wire_identical(ev.to_wire(), PLANNER_THINK_LITERAL)


def test_think_log_line():
    ev = ThinkEvent(agent="dj", text="hello floor")
    assert ev.log_line() == "[THINK:dj] hello floor"


def test_think_notebook_payload():
    ev = ThinkEvent(agent="dj", text="abc")
    assert ev.notebook_payload() == {"text": "abc"}


def test_think_to_wire_fresh_dict_per_call():
    ev = ThinkEvent(agent="dj", text="x")
    w1, w2 = ev.to_wire(), ev.to_wire()
    assert w1 is not w2
    w1["ts"] = 111  # ws_server stamps in place
    assert "ts" not in w2 and "ts" not in ev.to_wire()


# ---- CallEvent -------------------------------------------------------------

def test_call_wire_matches_capture():
    ev = CallEvent(agent="dj", tool="load_track", args=CALL_LITERAL["args"])
    _assert_wire_identical(ev.to_wire(), CALL_LITERAL)


def test_call_log_line():
    ev = CallEvent(agent="dj", tool="load_track", args="{'deck': 2}")
    assert ev.log_line() == "[CALL:dj] load_track({'deck': 2})"


def test_call_notebook_payload():
    ev = CallEvent(agent="dj", tool="do_transition", args="{}")
    assert ev.notebook_payload() == {"tool": "do_transition", "args": "{}"}


def test_call_to_wire_fresh_dict_per_call():
    ev = CallEvent(agent="dj", tool="t", args="a")
    w1, w2 = ev.to_wire(), ev.to_wire()
    assert w1 is not w2
    w1["ts"] = 111
    assert "ts" not in w2


# ---- BillingEvent ----------------------------------------------------------

def test_billing_wire_matches_capture_with_key_spend():
    ev = BillingEvent(snapshot=BILLING_LITERAL)
    _assert_wire_identical(ev.to_wire(), BILLING_LITERAL)


def test_billing_wire_matches_capture_without_key_spend():
    ev = BillingEvent(snapshot=BILLING_LITERAL_NO_KEY_SPEND)
    wire = ev.to_wire()
    _assert_wire_identical(wire, BILLING_LITERAL_NO_KEY_SPEND)
    assert "key_spend" not in wire


def test_billing_to_wire_fresh_dict_per_call():
    ev = BillingEvent(snapshot=dict(BILLING_LITERAL))
    w1, w2 = ev.to_wire(), ev.to_wire()
    assert w1 is not w2
    w1["ts"] = 111
    assert "ts" not in w2
    assert "ts" not in ev.snapshot  # snapshot itself not mutated


# ---- AgentInvoker protocol -------------------------------------------------

def test_agent_invoker_structural():
    class Fake:
        def __init__(self):
            self.sent = []

        def _ws_broadcast(self, event, data):
            self.sent.append((event, data))

    f = Fake()
    assert isinstance(f, AgentInvoker)
    f._ws_broadcast("thinking", ThinkEvent("dj", "x").to_wire())
    assert f.sent[0][0] == "thinking"

    class NotInvoker:
        pass

    assert not isinstance(NotInvoker(), AgentInvoker)
