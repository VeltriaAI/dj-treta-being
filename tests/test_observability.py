"""Tests for agent.observability — structured telemetry sinks."""

import time

import pytest

from agent.observability import (
    record_llm_call,
    record_decision,
    tick,
    spend_today,
)


class TestRecordLLMCall:

    @pytest.fixture(autouse=True)
    def _billing_rates(self):
        """Configure per-model rates so the fallback path (no model_cost) resolves
        to gateway flash/pro rates instead of $0/unknown-alias."""
        from types import SimpleNamespace
        from agent import billing_rates as br
        br._rates = dict(br._STATIC_RATES)
        br._config = SimpleNamespace(
            llm=SimpleNamespace(model="openai/gemini-flash", being_model="openai/gemini-pro"))
        yield

    def test_insert_creates_row(self, test_db):
        # Authoritative gateway cost is passed through verbatim.
        cost = record_llm_call(
            agent="planner",
            instruction="plan something deep",
            input_tokens=1000,
            output_tokens=200,
            latency_ms=1500,
            tool_calls=[{"name": "list_library_tracks", "args": "{}"}],
            model_cost=0.30409,
        )
        assert cost == 0.30409

        from agent.db import get_db
        db = get_db()
        try:
            row = db.execute("SELECT * FROM llm_calls WHERE agent=?", ("planner",)).fetchone()
            assert row is not None
            assert row["input_tokens"] == 1000
            assert row["output_tokens"] == 200
            assert row["latency_ms"] == 1500
            assert abs(row["cost_usd"] - 0.30409) < 1e-9
        finally:
            db.close()

    def test_cost_computation_uses_per_model_rate(self, test_db):
        # No model_cost → falls back to the per-model rate map. dj_treta is a
        # subagent → flash ($1.5/M in, $9/M out): 1M+1M = $10.50 (NOT the old
        # flat ~$1.25 — that flat rate was the bug).
        cost = record_llm_call(
            agent="dj_treta",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        assert abs(cost - 10.5) < 1e-6

    def test_authoritative_cost_overrides_ratemap(self, test_db):
        cost = record_llm_call(agent="dj_treta", input_tokens=1_000_000,
                               output_tokens=1_000_000, model_cost=0.42)
        assert cost == 0.42

    def test_zero_tokens_still_records(self, test_db):
        cost = record_llm_call(agent="dj_treta", input_tokens=0, output_tokens=0)
        assert cost == 0.0


class TestRecordDecision:

    def test_dj_pick_row(self, test_db):
        record_decision(
            agent="dj_treta",
            decision_type="dj_pick_track",
            picked_rank=2,
            reason="rank 1 already on deck",
            context="mood=bollyafro",
        )
        from agent.db import get_db
        db = get_db()
        try:
            row = db.execute("SELECT * FROM decisions WHERE agent=?", ("dj_treta",)).fetchone()
            assert row is not None
            assert row["picked_rank"] == 2
            assert "rank 1" in row["reason"]
        finally:
            db.close()

    def test_null_rank_ok(self, test_db):
        record_decision(
            agent="planner",
            decision_type="replan_triggered",
            reason="mood changed",
        )
        from agent.db import get_db
        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM decisions WHERE decision_type=?",
                ("replan_triggered",),
            ).fetchone()
            assert row is not None
            assert row["picked_rank"] is None
        finally:
            db.close()


class TestAgentHealth:

    def test_tick_upsert(self, test_db):
        tick("planner")
        tick("planner")
        tick("planner")
        from agent.db import get_db
        db = get_db()
        try:
            rows = db.execute("SELECT * FROM agent_health WHERE agent=?", ("planner",)).fetchall()
            assert len(rows) == 1  # upserted, not duplicated
            assert rows[0]["consecutive_errors"] == 0
        finally:
            db.close()

    def test_tick_with_error_increments(self, test_db):
        tick("library")                      # clean tick
        tick("library", error="timeout")     # error 1
        tick("library", error="timeout")     # error 2
        from agent.db import get_db
        db = get_db()
        try:
            row = db.execute("SELECT * FROM agent_health WHERE agent=?", ("library",)).fetchone()
            assert row["consecutive_errors"] == 2
            assert "timeout" in row["last_error"]
        finally:
            db.close()

    def test_clean_tick_resets_errors(self, test_db):
        tick("producer", error="oops")
        tick("producer")  # clean
        from agent.db import get_db
        db = get_db()
        try:
            row = db.execute("SELECT * FROM agent_health WHERE agent=?", ("producer",)).fetchone()
            assert row["consecutive_errors"] == 0
        finally:
            db.close()


class TestSpendToday:

    def test_aggregates_by_agent(self, test_db):
        record_llm_call(agent="planner", input_tokens=100_000, output_tokens=20_000)
        record_llm_call(agent="planner", input_tokens=50_000, output_tokens=10_000)
        record_llm_call(agent="being", input_tokens=200_000, output_tokens=5_000)

        planner_spend = spend_today("planner")
        being_spend = spend_today("being")
        total = spend_today()

        assert planner_spend > 0
        assert being_spend > 0
        assert total > planner_spend
        assert total > being_spend

    def test_zero_when_no_rows(self, test_db):
        assert spend_today("no_such_agent") == 0.0
