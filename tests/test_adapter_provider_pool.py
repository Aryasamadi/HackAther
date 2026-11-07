import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ares.strix_adapter import StrixAdapter


def _provider(provider_id: str, model: str) -> dict[str, object]:
    return {
        "id": provider_id,
        "name": provider_id,
        "litellm_id": provider_id,
        "api_key": f"{provider_id}-secret",
        "base_url": f"https://{provider_id}.example.test/v1",
        "models": [model],
    }


def _adapter(tmp_path) -> StrixAdapter:
    adapter = object.__new__(StrixAdapter)
    adapter._model_ref = "openai/gpt-4o"
    adapter._fallback_provider = None
    adapter._fallback_model = None
    adapter._provider_pool = None
    adapter._config_path = tmp_path / "strix-cli-config.json"
    adapter._config_path.write_text(
        json.dumps({"env": {"STRIX_LLM": "openai/gpt-4o"}}),
        encoding="utf-8",
    )
    return adapter


def test_provider_pool_accepts_ten_additional_routes(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    primary = _provider("openai", "gpt-4o")
    routes = [
        {
            "provider": _provider(f"gateway{i}", f"model-{i}"),
            "model": f"model-{i}",
            "weight": (i % 10) + 1,
            "max_concurrency": (i % 8) + 1,
        }
        for i in range(1, 11)
    ]

    with (
        patch("ares.strix_adapter.apply_config_override") as apply_override,
        patch(
            "ares.strix_adapter.load_settings",
            return_value=SimpleNamespace(
                llm=SimpleNamespace(api_type="chat_completions")
            ),
        ),
    ):
        adapter._configure_provider_pool(primary, "gpt-4o", routes)

    assert len(adapter._provider_pool or []) == 11
    assert adapter._fallback_provider is None
    assert adapter._fallback_model is None
    apply_override.assert_called_once_with(adapter._config_path)


def test_provider_pool_rejects_duplicate_provider_routes(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    primary = _provider("openai", "gpt-4o")
    duplicate = _provider("gateway", "model-x")

    with pytest.raises(ValueError, match="one model per provider"):
        adapter._configure_provider_pool(
            primary,
            "gpt-4o",
            [
                {"provider": duplicate, "model": "model-x"},
                {"provider": duplicate, "model": "model-x"},
            ],
        )


def test_provider_pool_rejects_models_missing_from_catalog(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    primary = _provider("openai", "gpt-4o")
    other = _provider("gateway", "model-a")

    with pytest.raises(ValueError, match="no longer in the catalog"):
        adapter._configure_provider_pool(
            primary,
            "gpt-4o",
            [{"provider": other, "model": "not-in-catalog"}],
        )


def test_provider_pool_rejects_incompatible_tool_schema_mode(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    primary = _provider("openai", "gpt-4o")
    anthropic = _provider("gateway", "anthropic/claude-sonnet")

    with pytest.raises(ValueError, match="incompatible"):
        adapter._configure_provider_pool(
            primary,
            "gpt-4o",
            [{"provider": anthropic, "model": "anthropic/claude-sonnet"}],
        )


def test_provider_pool_rejects_conflicting_api_type_override(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    primary = _provider("openai", "gpt-4o")
    other = _provider("gateway", "model-a")

    with (
        patch("ares.strix_adapter.apply_config_override"),
        patch(
            "ares.strix_adapter.load_settings",
            return_value=SimpleNamespace(
                llm=SimpleNamespace(api_type="responses")
            ),
        ),
        pytest.raises(ValueError, match="environment overrides"),
    ):
        adapter._configure_provider_pool(
            primary,
            "gpt-4o",
            [{"provider": other, "model": "model-a"}],
        )
