"""Tests for per-model cost accounting (agent.billing_rates + the ADK cost wrap).

Grounds the 2026-06-03 fix that replaced a flat $0.10/$0.40-per-M rate with the
gateway's authoritative per-model pricing.
"""

from types import SimpleNamespace

import pytest

from agent import billing_rates as br


@pytest.fixture(autouse=True)
def _isolate_rates():
    """Snapshot/restore module globals so tests don't leak state."""
    saved_rates, saved_config = dict(br._rates), br._config
    yield
    br._rates, br._config = saved_rates, saved_config


def _fake_config(api_base="http://127.0.0.1:1/unreachable", api_key="sk-test"):
    return SimpleNamespace(llm=SimpleNamespace(
        api_base=api_base, api_key=api_key,
        model="openai/gemini-flash", being_model="openai/gemini-pro"))


class TestRates:

    def test_static_fallback_when_unreachable(self):
        # Bad api_base → init falls back to the static map, doesn't raise.
        br.init(_fake_config())
        assert br._rates == br._STATIC_RATES
        # flash input rate, 1M tokens = $1.50
        assert abs(br.cost_for("gemini-flash", 1_000_000, 0) - 1.5) < 1e-9

    def test_cost_for_per_model(self):
        br._rates = dict(br._STATIC_RATES)
        # flash: 1M in @1.5 + 1M out @9 = 10.5
        assert abs(br.cost_for("gemini-flash", 1_000_000, 1_000_000) - 10.5) < 1e-9
        # pro: 1M in @2 + 1M out @12 = 14.0
        assert abs(br.cost_for("gemini-pro", 1_000_000, 1_000_000) - 14.0) < 1e-9

    def test_unknown_alias_returns_zero_not_flash(self):
        br._rates = dict(br._STATIC_RATES)
        # The old bug silently applied a default rate; now unknown → 0.
        assert br.cost_for("does-not-exist", 1_000_000, 1_000_000) == 0.0

    def test_alias_for_agent_from_config(self):
        br._config = _fake_config().__class__(  # reuse namespace shape
            llm=SimpleNamespace(model="openai/gemini-flash", being_model="openai/gemini-pro"))
        assert br.alias_for_agent("treta") == "gemini-pro"
        for a in ("dj_treta", "planner", "library_manager", "producer", "mixer", "consciousness"):
            assert br.alias_for_agent(a) == "gemini-flash"

    def test_rates_parsed_from_model_info(self, monkeypatch):
        payload = {"data": [
            {"model_name": "gemini-flash", "model_info": {
                "input_cost_per_token": 1.5e-6, "output_cost_per_token": 9e-6}},
            {"model_name": "gemini-pro", "model_info": {
                "input_cost_per_token": 2e-6, "output_cost_per_token": 1.2e-5}},
        ]}

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return payload

        import httpx
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
        br.init(_fake_config(api_base="https://gw.example"))
        assert br._rates["gemini-flash"] == (1.5e-6, 9e-6)
        assert br._rates["gemini-pro"] == (2e-6, 1.2e-5)


class TestResponseCostExtraction:

    def test_response_cost_of(self):
        resp = SimpleNamespace(_hidden_params={"response_cost": 0.007186})
        assert br.response_cost_of(resp) == 0.007186

    def test_response_cost_of_absent(self):
        assert br.response_cost_of(SimpleNamespace(_hidden_params={})) is None
        assert br.response_cost_of(SimpleNamespace()) is None


class TestAdkCostWrap:

    def test_convert_with_cost_stashes_response_cost(self):
        # The agents.py wrap must copy _hidden_params['response_cost'] onto the
        # LlmResponse.custom_metadata so _process_event can bill it.
        import agent.agents as agents
        from google.adk.models.llm_response import LlmResponse

        base = LlmResponse(custom_metadata={"existing": 1})
        # Stub the original converter to return our base response.
        orig = agents._orig_convert
        try:
            agents._orig_convert = lambda response, *a, **k: base
            fake_litellm_resp = SimpleNamespace(_hidden_params={
                "response_cost": 0.00301,
                "additional_headers": {
                    "llm_provider-x-litellm-model-group": "gemini-pro",
                    "llm_provider-x-litellm-key-spend": "1.95",
                },
            })
            out = agents._convert_with_cost(fake_litellm_resp)
            assert out.custom_metadata["response_cost"] == 0.00301
            assert out.custom_metadata["model_group"] == "gemini-pro"
            assert out.custom_metadata["key_spend"] == 1.95
            assert out.custom_metadata["existing"] == 1  # preserved
        finally:
            agents._orig_convert = orig

    def test_convert_with_cost_no_cost_is_safe(self):
        import agent.agents as agents
        from google.adk.models.llm_response import LlmResponse
        base = LlmResponse()
        orig = agents._orig_convert
        try:
            agents._orig_convert = lambda response, *a, **k: base
            out = agents._convert_with_cost(SimpleNamespace(_hidden_params={}))
            assert (out.custom_metadata or {}).get("response_cost") is None
        finally:
            agents._orig_convert = orig
