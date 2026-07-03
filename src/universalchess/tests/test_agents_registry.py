"""Tests for the agents framework registry (discovery).

Why these tests exist
---------------------
The agents framework is a plugin system: built-in agents ship in the package and
users add their own Python modules. These tests pin discovery (built-in + user
drop-ins, override-by-id, malformed-file skipping, blank-id skipping) and the
id/list helpers the settings layer builds storage keys from. A regression here
would drop a user agent, crash discovery on one bad file, or change the id set
that per-provider storage keys are derived from.
"""

import textwrap

import pytest

from universalchess.agents import registry


@pytest.fixture(autouse=True)
def _fresh_registry():
    # Each test starts from a clean cache so discovery reflects only what the test
    # sets up; otherwise a cached registry from another test would leak agents.
    registry.refresh()
    yield
    registry.refresh()


def _write_agent(dir_path, filename, *, class_name, agent_id, name):
    module = textwrap.dedent(
        f"""
        from universalchess.agents.base import AgentConfig
        from universalchess.agents.openai_compatible import OpenAICompatibleAgent

        class {class_name}(OpenAICompatibleAgent):
            id = "{agent_id}"
            name = "{name}"
            description = "test agent"
            default_model = "m"

            def chat_base_url(self, config: AgentConfig) -> str:
                return "http://example/v1"
        """
    )
    (dir_path / filename).write_text(module)


def test_builtin_agents_are_discovered():
    # The three shipped agents must always be present; a broken discovery or package
    # layout would drop them and leave the agent selector empty.
    agents = registry.discover_agents(include_user=False)
    assert set(agents) == {"openai", "anthropic", "custom"}


def test_framework_base_classes_are_not_registered_as_agents():
    # The abstract shape bases (Agent/OpenAICompatibleAgent) are imported by the
    # built-in modules; discovery must not register them as selectable agents (they
    # have blank ids and cannot make requests).
    agents = registry.discover_agents(include_user=False)
    assert "" not in agents
    assert all(a.id for a in agents.values())


def test_user_agent_is_discovered_from_directory(tmp_path):
    # A user Python module in the agents folder must be picked up so the framework is
    # actually expandable; failure means user agents never appear.
    _write_agent(tmp_path, "together.py", class_name="Together", agent_id="together", name="Together")
    agents = registry.discover_agents(user_dir=str(tmp_path))
    assert "together" in agents
    assert agents["together"].name == "Together"


def test_user_agent_overrides_builtin_with_same_id(tmp_path):
    # A user agent sharing an id must override the built-in, so users can customize a
    # shipped agent by shadowing it rather than editing the package.
    _write_agent(tmp_path, "openai.py", class_name="MyOpenAI", agent_id="openai", name="Custom OpenAI")
    agents = registry.discover_agents(user_dir=str(tmp_path))
    assert agents["openai"].name == "Custom OpenAI"


def test_malformed_user_module_is_skipped(tmp_path):
    # One bad file must not break discovery; the good agents (built-in + other user
    # files) must still load. Regression: an import error would abort the whole scan
    # and disable the AI features.
    (tmp_path / "broken.py").write_text("import does_not_exist_xyz\n")
    _write_agent(tmp_path, "ok.py", class_name="Ok", agent_id="ok", name="Ok")
    agents = registry.discover_agents(user_dir=str(tmp_path))
    assert "ok" in agents
    assert "openai" in agents  # built-ins unaffected
    assert "broken" not in agents


def test_agent_class_with_blank_id_is_skipped(tmp_path):
    # An agent with no id cannot be selected or persisted; it must be skipped rather
    # than registered under an empty key that would shadow another agent.
    module = textwrap.dedent(
        """
        from universalchess.agents.base import AgentConfig
        from universalchess.agents.openai_compatible import OpenAICompatibleAgent

        class Nameless(OpenAICompatibleAgent):
            name = "Nameless"

            def chat_base_url(self, config: AgentConfig) -> str:
                return "http://example/v1"
        """
    )
    (tmp_path / "nameless.py").write_text(module)
    agents = registry.discover_agents(user_dir=str(tmp_path))
    assert all(aid != "" for aid in agents)


def test_agent_ids_are_sorted_builtins():
    # Storage keys (coach_api_key_<id> ...) are derived from this set; a change to it
    # would orphan saved credentials. With no user agents it is exactly the builtins.
    registry.refresh()
    # Discover only built-ins by pointing the user dir at an empty location via the
    # public helpers: get_registry uses the real user dir, so assert the builtins are
    # a subset and ordering is sorted.
    ids = registry.agent_ids()
    assert {"openai", "anthropic", "custom"}.issubset(set(ids))
    assert ids == sorted(ids)


def test_get_agent_returns_none_for_unknown_or_blank():
    # A disabled ("none") or stale/removed id must resolve to no agent so callers can
    # treat it as "not configured" rather than crashing.
    assert registry.get_agent("") is None
    assert registry.get_agent("does_not_exist") is None
    assert registry.get_agent("openai") is not None


def test_list_agents_sorted_by_name_with_field_info():
    # The settings UIs render cards from list_agents(); it must be sorted by name and
    # carry each agent's field schema so no provider names are hardcoded in the UI.
    infos = registry.discover_agents(include_user=False)
    ordered = registry.list_agents()  # uses the cached full registry
    names = [i["name"] for i in ordered]
    assert names == sorted(names)
    for info in ordered:
        assert "fields" in info and info["fields"]
    # Sanity: the built-in ids are represented.
    assert {"openai", "anthropic", "custom"}.issubset({i["id"] for i in ordered})
