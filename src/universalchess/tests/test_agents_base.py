"""Tests for the AI agent base contract (agents/base.py).

Why these tests exist
---------------------
An :class:`Agent` owns the generic, provider-agnostic behavior every agent shares:
config gating (``is_configured``/``resolved_model``), the settings schema that
drives both settings UIs, and the shared models-response parsing. These tests pin
that contract so a regression cannot, for example, treat an agent with no key as
configured (leading to a 401), drop the base-URL field for an agent that needs one
(making it unsavable), or emit an empty model dropdown from a valid response.
"""

import pytest

from universalchess.agents.base import (
    FIELD_MODEL,
    FIELD_MODEL_TEXT,
    FIELD_SECRET,
    FIELD_TEXT,
    Agent,
    AgentConfig,
    AgentError,
)


class _FixedAgent(Agent):
    """Minimal concrete agent with a fixed endpoint (no base URL required)."""

    id = "fixed"
    name = "Fixed"
    default_model = "m-default"
    requires_base_url = False


class _BaseUrlAgent(Agent):
    """Minimal concrete agent that requires a base URL (no fixed endpoint)."""

    id = "byo"
    name = "BringYourOwn"
    requires_base_url = True
    model_field_kind = FIELD_MODEL_TEXT


def test_is_configured_requires_api_key():
    # is_configured gates every network call; an agent with no key must read as not
    # configured so the UI shows the setup hint instead of issuing a request that
    # would 401.
    assert _FixedAgent().is_configured(AgentConfig(api_key="")) is False
    assert _FixedAgent().is_configured(AgentConfig(api_key="k")) is True


def test_is_configured_requires_base_url_when_agent_has_no_fixed_endpoint():
    # An agent that needs a base URL has nowhere to POST without one; a key alone
    # must not read as configured. Regression: treating it as configured would build
    # a request to a bare "/chat/completions".
    assert _BaseUrlAgent().is_configured(AgentConfig(api_key="k")) is False
    assert _BaseUrlAgent().is_configured(AgentConfig(api_key="k", base_url="http://h/v1")) is True


def test_resolved_model_falls_back_to_default_when_unset():
    # An empty model must fall back to the agent default so a user who only set a
    # key still produces a valid request; an explicit model is used verbatim.
    assert _FixedAgent().resolved_model(AgentConfig(api_key="k")) == "m-default"
    assert _FixedAgent().resolved_model(AgentConfig(api_key="k", model="m-x")) == "m-x"


def test_settings_schema_omits_base_url_for_fixed_endpoint_agent():
    # The settings UIs render exactly the fields the schema lists. A fixed-endpoint
    # agent must expose only api key + model (a base-URL field would be dead config
    # that overrides nothing).
    schema = _FixedAgent().settings_schema()
    kinds = {(f.key_base, f.kind) for f in schema}
    assert kinds == {("coach_api_key", FIELD_SECRET), ("coach_model", FIELD_MODEL)}


def test_settings_schema_includes_base_url_and_free_text_model_for_byo_agent():
    # A base-URL agent must expose the base-URL field (or it is unsavable) and, here,
    # a free-text model field (its models cannot be listed). Order matters for the
    # UI: api key, then model, then base URL.
    schema = _BaseUrlAgent().settings_schema()
    assert [(f.key_base, f.kind) for f in schema] == [
        ("coach_api_key", FIELD_SECRET),
        ("coach_model", FIELD_MODEL_TEXT),
        ("coach_base_url", FIELD_TEXT),
    ]


def test_parse_models_response_extracts_ids_and_raises_on_empty():
    # The shared parser pulls ids from {"data":[{"id":...}]} and must raise on a
    # shape carrying none so the caller falls back rather than showing an empty list.
    data = {"data": [{"id": "a"}, {"id": "b"}, {"nope": 1}]}
    assert _FixedAgent().parse_models_response(data) == ["a", "b"]
    with pytest.raises(AgentError):
        _FixedAgent().parse_models_response({"data": []})
    with pytest.raises(AgentError):
        _FixedAgent().parse_models_response({"unexpected": 1})


def test_get_info_exposes_fields_for_the_settings_ui():
    # The web/board build their per-agent cards from get_info(); it must carry the
    # id/name plus the serialized field list so the UI needs no provider hardcoding.
    info = _BaseUrlAgent().get_info()
    assert info["id"] == "byo"
    assert info["requires_base_url"] is True
    field_bases = [f["key_base"] for f in info["fields"]]
    assert field_bases == ["coach_api_key", "coach_model", "coach_base_url"]
