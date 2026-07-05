"""NS-003 — per-agent model map (config.llm.models) tests.

AC1: no `models` key → resolved model per agent byte-identical to the
     old two-tier {model, being_model} split.
AC2: `models` map set → named agents get their model, unnamed fall back.
AC3: planner keeps its DEDICATED LiteLlm instance with
     response_format={"type": "json_object"}; others have none.
"""

import pytest

from agent.config import Config, LLMConfig, resolve_model_params

# All actual LlmAgent names constructed in create_agents().
ALL_AGENT_NAMES = [
    "mixer", "library", "dj_treta", "producer",
    "planner", "treta", "library_manager",
]


def _llm(**kw) -> LLMConfig:
    base = dict(
        model="openai/flash-loop",
        being_model="openai/pro-being",
        api_base="http://localhost:4000",
        api_key="k1",
    )
    base.update(kw)
    return LLMConfig(**base)


# ---------------------------------------------------------------------------
# AC1 — no models key → identical to today's model/being_model split
# ---------------------------------------------------------------------------

def test_no_models_key_matches_two_tier_split():
    llm = _llm()
    resolved = {n: resolve_model_params(llm, n) for n in ALL_AGENT_NAMES}
    expected = {
        n: (
            "openai/pro-being" if n == "treta" else "openai/flash-loop",
            "k1",
            "http://localhost:4000",
        )
        for n in ALL_AGENT_NAMES
    }
    assert resolved == expected


def test_empty_being_model_falls_back_to_model():
    llm = _llm(being_model="")
    assert resolve_model_params(llm, "treta")[0] == "openai/flash-loop"


# ---------------------------------------------------------------------------
# AC2 — models map: named agents mapped, unnamed fall back
# ---------------------------------------------------------------------------

def test_models_map_named_and_fallback():
    llm = _llm(models={"planner": "openai/planner-x", "dj": "openai/dj-y"})
    assert resolve_model_params(llm, "planner")[0] == "openai/planner-x"
    # alias "dj" in config resolves for actual agent name "dj_treta"
    assert resolve_model_params(llm, "dj_treta")[0] == "openai/dj-y"
    # unnamed agents fall back exactly as before
    assert resolve_model_params(llm, "mixer")[0] == "openai/flash-loop"
    assert resolve_model_params(llm, "library_manager")[0] == "openai/flash-loop"
    assert resolve_model_params(llm, "treta")[0] == "openai/pro-being"


def test_models_map_aliases_being_and_library_peer():
    llm = _llm(models={"being": "openai/being-z", "library_peer": "openai/lib-w"})
    assert resolve_model_params(llm, "treta")[0] == "openai/being-z"
    assert resolve_model_params(llm, "library_manager")[0] == "openai/lib-w"


def test_model_overrides_api_base_and_key():
    llm = _llm(
        models={"being": "openai/being-z"},
        model_overrides={"being": {"api_base": "http://other:9", "api_key": "k2"}},
    )
    assert resolve_model_params(llm, "treta") == (
        "openai/being-z", "k2", "http://other:9",
    )
    # overrides without a models entry still apply endpoint-only
    llm2 = _llm(model_overrides={"planner": {"api_base": "http://p:1"}})
    assert resolve_model_params(llm2, "planner") == (
        "openai/flash-loop", "k1", "http://p:1",
    )
    # missing override fields fall back to the top-level values
    assert resolve_model_params(llm, "mixer") == (
        "openai/flash-loop", "k1", "http://localhost:4000",
    )


def test_load_config_parses_models_map(tmp_path):
    from agent.config import load_config
    p = tmp_path / "config.yaml"
    p.write_text(
        "llm:\n"
        "  model: openai/flash-loop\n"
        "  models:\n"
        "    planner: openai/planner-x\n"
        "  model_overrides:\n"
        "    being:\n"
        "      api_base: http://other:9\n"
    )
    cfg = load_config(p)
    assert cfg.llm.models == {"planner": "openai/planner-x"}
    assert resolve_model_params(cfg.llm, "planner")[0] == "openai/planner-x"
    assert resolve_model_params(cfg.llm, "treta")[2] == "http://other:9"


# ---------------------------------------------------------------------------
# AC1/AC3 — full create_agents wiring
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def default_agents():
    from agent.agents import create_agents
    cfg = Config()
    cfg.llm = _llm()
    return create_agents(cfg)


def test_create_agents_default_mapping(default_agents):
    being, dj, planner, library_peer, producer = default_agents
    assert being.model.model == "openai/pro-being"
    for agent in (dj, planner, library_peer, producer, dj.sub_agents[0]):
        assert agent.model.model == "openai/flash-loop"
    # loops keep SHARING one LiteLlm instance (old behavior preserved)
    assert dj.model is library_peer.model is producer.model
    assert dj.sub_agents[0].model is dj.model  # mixer
    assert being.model is not dj.model


def test_planner_dedicated_response_format(default_agents):
    being, dj, planner, library_peer, producer = default_agents
    # planner: distinct instance, json_object response_format (NS-001)
    assert planner.model is not dj.model
    assert planner.model is not being.model
    assert planner.model._additional_args.get("response_format") == {
        "type": "json_object"
    }
    # nobody else ever sees response_format
    for agent in (being, dj, library_peer, producer, dj.sub_agents[0]):
        assert "response_format" not in agent.model._additional_args


def test_create_agents_with_models_map():
    from agent.agents import create_agents
    cfg = Config()
    cfg.llm = _llm(models={"planner": "openai/planner-x", "dj": "openai/dj-y"})
    being, dj, planner, library_peer, producer = create_agents(cfg)
    assert planner.model.model == "openai/planner-x"
    assert planner.model._additional_args.get("response_format") == {
        "type": "json_object"
    }
    assert dj.model.model == "openai/dj-y"
    assert being.model.model == "openai/pro-being"
    assert library_peer.model.model == "openai/flash-loop"
    assert producer.model.model == "openai/flash-loop"
    # dj now has its own instance; other loops still share
    assert dj.model is not producer.model
    assert library_peer.model is producer.model
