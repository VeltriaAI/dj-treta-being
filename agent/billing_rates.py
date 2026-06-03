"""Per-model LLM cost — grounded in the gateway's own numbers.

DJ Treta's cost used to be a flat $0.10/M-in + $0.40/M-out for EVERY agent
(adk_runner._update_billing), which under-reported the real gateway bill ~21x
because the Being runs on a pro model and the planner on a flash model at very
different rates.

This module is the single source of truth for cost. Priority of truth:

  1. The gateway's authoritative per-call ``response_cost`` (LiteLLM computes it
     and returns it on every response — see agents.py, which rides it onto the
     ADK event, and ``bill_from_response`` for direct litellm calls). Used
     verbatim — model-correct and already includes reasoning tokens.
  2. A per-model rate map fetched once from the gateway's ``/model/info`` at
     startup (keyed by the SAME alias the agent used, e.g. ``gemini-flash``),
     used only when (1) isn't available. Verified to reproduce ``response_cost``
     to the cent.
  3. A static map (last-known gateway rates) if ``/model/info`` is unreachable.

Unknown aliases are billed 0 with a WARNING — never silently charged flash
rates (the old bug's failure mode).
"""

from __future__ import annotations

import logging

log = logging.getLogger("dj-treta")

# Last-known gateway-published rates (USD per token), used only if /model/info
# is unreachable at startup. Verified live 2026-06-03 against gateway.infrax.ai.
_STATIC_RATES: dict[str, tuple[float, float]] = {
    "gemini-flash": (1.5e-6, 9e-6),     # gemini-3.5-flash
    "gemini-pro":   (2e-6,   1.2e-5),   # gemini-3.1-pro-preview
    "nano-banana":  (0.0,    0.0),      # image — not token-billed
}

# Populated by init(); maps alias -> (input_cost_per_token, output_cost_per_token).
_rates: dict[str, tuple[float, float]] = {}

# Stashed at init() so alias resolution + fallbacks work without threading config
# through every call site.
_config = None


def init(config) -> None:
    """Fetch the gateway's per-model rates once at startup and cache them.

    Falls back to the static map on any failure. Idempotent.
    """
    global _rates, _config
    _config = config
    api_base = (getattr(config.llm, "api_base", "") or "").rstrip("/")
    api_key = getattr(config.llm, "api_key", "") or ""
    if not api_base:
        _rates = dict(_STATIC_RATES)
        log.warning("billing rates: no api_base — using static map")
        return
    try:
        import httpx
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(f"{api_base}/model/info", headers=headers, timeout=5.0)
        r.raise_for_status()
        out: dict[str, tuple[float, float]] = {}
        for m in (r.json().get("data") or []):
            name = m.get("model_name")
            info = m.get("model_info") or {}
            ci = info.get("input_cost_per_token")
            co = info.get("output_cost_per_token")
            if name and ci is not None and co is not None:
                out[name] = (float(ci), float(co))
        _rates = out or dict(_STATIC_RATES)
        log.info("billing rates loaded from gateway: %s", dict(_rates))
    except Exception as e:
        _rates = dict(_STATIC_RATES)
        log.warning("billing rates: /model/info unreachable (%s) — using static map", e)


def _strip_prefix(model_str: str) -> str:
    """`openai/gemini-flash` / `vertex_ai/gemini-3.5-flash` -> bare alias."""
    return (model_str or "").split("/", 1)[-1]


def alias_for_agent(agent_name: str) -> str:
    """Resolve which model alias an ADK agent used, from config.

    The Being ("treta") runs on ``being_model``; every other agent (dj_treta,
    planner, library_manager, producer, mixer, consciousness) runs on ``model``.
    """
    if _config is None:
        return ""
    llm = _config.llm
    model = llm.being_model if agent_name == "treta" else llm.model
    return _strip_prefix(model or llm.model)


def cost_for(alias: str, inp: int, out: int) -> float:
    """USD for `inp` input + `out` output tokens at the gateway's rate for `alias`.

    Returns 0.0 (with a WARNING) for an unknown alias — never silently applies a
    default rate.
    """
    rates = _rates or _STATIC_RATES
    rate = rates.get(alias)
    if rate is None:
        log.warning("billing: unknown model alias %r — cost left 0 (not defaulting to flash)", alias)
        return 0.0
    ci, co = rate
    return inp * ci + out * co


def response_cost_of(resp) -> float | None:
    """Extract the gateway's authoritative per-call USD from a raw litellm
    response (``_hidden_params['response_cost']``). None if absent."""
    try:
        hp = getattr(resp, "_hidden_params", None) or {}
        rc = hp.get("response_cost")
        return float(rc) if rc is not None else None
    except Exception:
        return None


def bill_from_response(resp, agent_name: str) -> None:
    """Bill a DIRECT ``litellm.completion(...)`` response (call sites that don't
    flow through the ADK event loop) into the shared billing accumulator, using
    the gateway's authoritative ``response_cost`` when present."""
    try:
        cost = response_cost_of(resp)
        u = getattr(resp, "usage", None)
        inp = int(getattr(u, "prompt_tokens", 0) or 0)
        out = int(getattr(u, "completion_tokens", 0) or 0)
        if inp == 0 and out == 0 and cost is None:
            return
        from .adk_runner import bill_external
        bill_external(agent_name, inp, out, cost)
    except Exception:
        pass
