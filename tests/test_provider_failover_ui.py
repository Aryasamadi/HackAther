import asyncio
from unittest.mock import AsyncMock

from ares.telegram_bot import ARESBot


def test_failover_menu_lists_only_non_primary_providers() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {
        "active_provider_id": "primary",
        "active_model": "primary/model",
        "providers": {
            "primary": {"id": "primary", "name": "Primary", "models": ["primary/model"]},
            "backup": {"id": "backup", "name": "Backup", "models": ["backup/model"]},
        },
    }
    bot.render_menu = AsyncMock()

    asyncio.run(bot._failover_menu(42))

    text = bot.render_menu.await_args.args[1]
    keyboard = bot.render_menu.await_args.args[2]
    callbacks = [button["callback_data"] for row in keyboard for button in row]
    assert "Primary" in text
    assert "Backup" not in text
    assert "failover:provider:backup" in callbacks
    assert not any(callback == "failover:provider:primary" for callback in callbacks)


def test_fallback_selection_requires_a_distinct_saved_provider_and_model() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {
        "active_provider_id": "primary",
        "fallback_provider_id": "backup",
        "fallback_model": "backup/model",
        "providers": {
            "primary": {"id": "primary", "models": ["primary/model"]},
            "backup": {"id": "backup", "models": ["backup/model"]},
        },
    }

    selection = bot._fallback_selection()
    assert selection is not None
    assert selection[0]["id"] == "backup"
    assert selection[1] == "backup/model"

    bot.state["fallback_provider_id"] = "primary"
    assert bot._fallback_selection() is None

    bot.state["fallback_provider_id"] = "backup"
    bot.state["fallback_model"] = "removed/model"
    assert bot._fallback_selection() is None


def test_provider_removal_clears_backup_selection() -> None:
    from ares.providers import remove_provider

    state = {
        "providers": {"backup": {"id": "backup"}},
        "active_provider_id": "primary",
        "active_model": "primary/model",
        "fallback_provider_id": "backup",
        "fallback_model": "backup/model",
    }

    assert remove_provider(state, "backup")
    assert state["fallback_provider_id"] is None
    assert state["fallback_model"] is None
