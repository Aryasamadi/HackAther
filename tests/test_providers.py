import pytest

from ares.providers import (
    build_model_reference,
    model_button_label,
    model_endpoint_candidates,
    parse_provider_descriptor,
    remove_provider,
    validate_base_url,
)


def test_provider_descriptor_with_custom_label() -> None:
    assert parse_provider_descriptor("My OpenRouter | openrouter") == (
        "My OpenRouter",
        "openrouter",
    )


def test_provider_name_alone_uses_default_route() -> None:
    assert parse_provider_descriptor("My AI Provider") == ("My AI Provider", "openai")


def test_provider_descriptor_rejects_invalid_litellm_id() -> None:
    with pytest.raises(ValueError):
        parse_provider_descriptor("Gateway | invalid/provider")


def test_base_url_validation() -> None:
    assert validate_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    with pytest.raises(ValueError):
        validate_base_url("ftp://api.example.com")
    with pytest.raises(ValueError):
        validate_base_url("https://user:pass@example.com/v1")


def test_model_endpoint_candidates() -> None:
    assert model_endpoint_candidates("https://api.example.com/v1") == [
        "https://api.example.com/v1/models",
        "https://api.example.com/models",
    ]
    assert model_endpoint_candidates("https://api.example.com") == [
        "https://api.example.com/v1/models",
        "https://api.example.com/models",
    ]


def test_litellm_model_reference() -> None:
    assert build_model_reference("openrouter", "vendor/model-x") == "openrouter/vendor/model-x"
    assert build_model_reference("openrouter", "openrouter/vendor/model-x") == "openrouter/vendor/model-x"


def test_model_button_label_hides_only_the_provider_route_prefix() -> None:
    provider = {"name": "OpenRouter", "litellm_id": "openrouter"}
    assert model_button_label(provider, "openrouter/anthropic/claude-sonnet") == (
        "anthropic/claude-sonnet"
    )
    assert model_button_label(provider, "anthropic/claude-sonnet") == "anthropic/claude-sonnet"


def test_remove_active_provider_clears_selection_and_preserves_other_providers() -> None:
    state = {
        "providers": {"one": {"api_key": "secret"}, "two": {"api_key": "other"}},
        "active_provider_id": "one",
        "active_model": "model-a",
    }
    assert remove_provider(state, "one") is True
    assert state["providers"] == {"two": {"api_key": "other"}}
    assert state["active_provider_id"] is None
    assert state["active_model"] is None


def test_remove_inactive_provider_preserves_active_selection() -> None:
    state = {
        "providers": {"one": {}, "two": {}},
        "active_provider_id": "one",
        "active_model": "model-a",
    }
    assert remove_provider(state, "two") is True
    assert state["active_provider_id"] == "one"
    assert state["active_model"] == "model-a"
    assert remove_provider(state, "missing") is False



def test_remove_provider_also_removes_its_shared_router_route() -> None:
    state = {
        "providers": {"one": {"api_key": "secret"}, "two": {"api_key": "other"}},
        "active_provider_id": "two",
        "active_model": "model-b",
        "routing_pool": [
            {"provider_id": "one", "model": "model-a", "weight": 2},
            {"provider_id": "two", "model": "model-b", "weight": 1},
        ],
    }
    assert remove_provider(state, "one") is True
    assert state["routing_pool"] == [
        {"provider_id": "two", "model": "model-b", "weight": 1}
    ]
