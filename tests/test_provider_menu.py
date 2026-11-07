import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ares.telegram_bot import ARESBot


def _callback(data: str) -> dict:
    return {
        "id": "query-1",
        "from": {"id": 42},
        "message": {
            "message_id": 100,
            "chat": {"id": 42, "type": "private"},
        },
        "data": data,
    }


def test_opening_provider_refreshes_catalog_automatically() -> None:
    bot = object.__new__(ARESBot)
    bot.config = SimpleNamespace(admin_id=42)
    bot.state = {"ui_message_id": None, "ui_chat_id": None}
    bot._refresh_provider_catalog = AsyncMock()
    bot._provider_models = AsyncMock()
    bot.answer_callback = AsyncMock()

    asyncio.run(bot._handle_callback(_callback("provider:view:provider-1")))

    bot._refresh_provider_catalog.assert_awaited_once_with("provider-1")
    bot._provider_models.assert_awaited_once_with(42, "provider-1", 0)
    bot.answer_callback.assert_awaited_once()


def test_failed_auto_refresh_keeps_saved_models_visible() -> None:
    bot = object.__new__(ARESBot)
    bot.config = SimpleNamespace(admin_id=42)
    bot.state = {"ui_message_id": None, "ui_chat_id": None}
    bot._refresh_provider_catalog = AsyncMock(side_effect=RuntimeError("provider offline"))
    bot._provider_models = AsyncMock()
    bot.answer_callback = AsyncMock()

    asyncio.run(bot._handle_callback(_callback("provider:view:provider-1")))

    bot._provider_models.assert_awaited_once_with(
        42,
        "provider-1",
        0,
        notice="⚠️ Could not refresh the catalog; showing saved models.",
    )


def test_provider_models_menu_marks_selected_model_and_has_no_router_controls() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {
        "providers": {
            "provider-1": {
                "id": "provider-1",
                "name": "Example",
                "models": ["vendor/model-a", "vendor/model-b"],
            }
        },
        "active_provider_id": "provider-1",
        "active_model": "vendor/model-b",
    }
    bot.render_menu = AsyncMock()

    asyncio.run(bot._provider_models(42, "provider-1"))

    text = bot.render_menu.await_args.args[1]
    keyboard = bot.render_menu.await_args.args[2]
    callbacks = [button["callback_data"] for row in keyboard for button in row]
    labels = [button["text"] for row in keyboard for button in row]
    assert "Selected:" in text
    assert any(label == "model-b ✓" for label in labels)
    assert not any(callback.startswith(("router:", "failover:")) for callback in callbacks)
    assert not any(callback.startswith("provider:refresh:") for callback in callbacks)
    assert "provider:delete:provider-1" in callbacks


def test_models_menu_uses_one_provider_list_without_router_configuration() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {
        "providers": {
            "one": {"id": "one", "name": "Provider One", "models": ["one/model"]},
            "two": {"id": "two", "name": "Provider Two", "models": ["two/model"]},
        },
        "active_provider_id": "one",
        "active_model": "one/model",
        "routing_pool": [{"provider_id": "two", "model": "two/model"}],
    }
    bot.render_menu = AsyncMock()

    asyncio.run(bot._models_menu(42))

    text = bot.render_menu.await_args.args[1]
    keyboard = bot.render_menu.await_args.args[2]
    callbacks = [button["callback_data"] for row in keyboard for button in row]
    labels = [button["text"] for row in keyboard for button in row]
    assert "background AI routing automatically" in text
    assert "provider:view:one" in callbacks
    assert "provider:view:two" in callbacks
    assert not any(callback.startswith(("router:", "failover:")) for callback in callbacks)
    assert all("Router" not in label for label in labels)


def test_home_menu_places_live_control_before_models() -> None:
    bot = object.__new__(ARESBot)

    keyboard = bot.home_keyboard()

    assert keyboard[0][1]["callback_data"] == "run:dashboard"
    assert keyboard[1][0]["callback_data"] == "models:list"


def test_refresh_provider_catalog_updates_saved_model_list() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {
        "providers": {
            "provider-1": {
                "base_url": "https://api.example.test/v1",
                "api_key": "secret-token",
                "models": ["old/model"],
            }
        }
    }
    discover = AsyncMock(return_value=["new/model"])

    from unittest.mock import patch

    with patch("ares.telegram_bot.discover_models", discover):
        count = asyncio.run(bot._refresh_provider_catalog("provider-1"))

    assert count == 1
    assert bot.state["providers"]["provider-1"]["models"] == ["new/model"]
    discover.assert_awaited_once_with("https://api.example.test/v1", "secret-token")
