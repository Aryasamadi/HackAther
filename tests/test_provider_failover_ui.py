from ares.telegram_bot import ARESBot


def test_legacy_backup_selection_remains_readable_for_migration() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {
        "active_provider_id": "primary",
        "active_model": "primary/model",
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


def test_provider_removal_clears_legacy_backup_selection() -> None:
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
