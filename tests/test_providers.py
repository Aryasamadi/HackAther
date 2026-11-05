import pytest

from ares.providers import (
    build_model_reference,
    model_endpoint_candidates,
    parse_provider_descriptor,
    validate_base_url,
)


def test_provider_descriptor_with_custom_label() -> None:
    assert parse_provider_descriptor("My OpenRouter | openrouter") == (
        "My OpenRouter",
        "openrouter",
    )


def test_provider_descriptor_defaults_label_to_litellm_id() -> None:
    assert parse_provider_descriptor("anthropic") == ("anthropic", "anthropic")


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
