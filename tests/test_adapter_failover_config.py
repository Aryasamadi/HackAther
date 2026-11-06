import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ares.strix_adapter import StrixAdapter


def _provider(provider_id: str, *, models: list[str]) -> dict[str, object]:
    return {
        "id": provider_id,
        "litellm_id": provider_id,
        "api_key": f"{provider_id}-secret",
        "base_url": f"https://{provider_id}.example.test/v1",
        "models": models,
    }


def _adapter(tmp_path) -> StrixAdapter:
    adapter = object.__new__(StrixAdapter)
    adapter._model_ref = "openai/gpt-4o"
    adapter._config_path = tmp_path / "strix-cli-config.json"
    adapter._config_path.write_text(
        json.dumps({"env": {"STRIX_LLM": "openai/gpt-4o"}}),
        encoding="utf-8",
    )
    return adapter


def test_configured_failover_forces_primary_chat_completions(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    primary = _provider("primary", models=["gpt-4o"])
    backup = _provider("backup", models=["meta-llama/llama-3.3"])

    with (
        patch("ares.strix_adapter.apply_config_override") as apply_override,
        patch(
            "ares.strix_adapter.load_settings",
            return_value=SimpleNamespace(
                llm=SimpleNamespace(api_type="chat_completions")
            ),
        ),
    ):
        adapter._configure_failover(primary, backup, "meta-llama/llama-3.3")

    saved = json.loads(adapter._config_path.read_text(encoding="utf-8"))
    assert saved["env"]["STRIX_API_TYPE"] == "chat_completions"
    assert adapter._fallback_provider == backup
    assert adapter._fallback_model == "meta-llama/llama-3.3"
    apply_override.assert_called_once_with(adapter._config_path)


def test_configured_failover_rejects_incompatible_tool_schema_modes(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    primary = _provider("primary", models=["gpt-4o"])
    backup = _provider("backup", models=["anthropic/claude-sonnet"])

    with pytest.raises(ValueError, match="compatible Strix tool-schema modes"):
        adapter._configure_failover(primary, backup, "anthropic/claude-sonnet")


def test_configured_failover_rejects_conflicting_environment_api_type(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    primary = _provider("primary", models=["gpt-4o"])
    backup = _provider("backup", models=["meta-llama/llama-3.3"])

    with (
        patch("ares.strix_adapter.apply_config_override"),
        patch(
            "ares.strix_adapter.load_settings",
            return_value=SimpleNamespace(llm=SimpleNamespace(api_type="responses")),
        ),
        pytest.raises(ValueError, match="process environment forces"),
    ):
        adapter._configure_failover(primary, backup, "meta-llama/llama-3.3")
