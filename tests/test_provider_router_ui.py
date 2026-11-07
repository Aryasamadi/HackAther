import asyncio
from unittest.mock import AsyncMock

from ares.telegram_bot import ARESBot


def _provider(provider_id: str, model: str) -> dict:
    return {
        "id": provider_id,
        "name": provider_id.title(),
        "litellm_id": provider_id,
        "base_url": f"https://{provider_id}.example.test/v1",
        "api_key": "secret",
        "models": [model],
    }


def test_routing_pool_payload_reads_multiple_routes_and_legacy_backup() -> None:
    bot = object.__new__(ARESBot)
    providers = {
        key: _provider(key, f"{key}/model")
        for key in ("primary", "backup", "third", "fourth")
    }
    bot.state = {
        "providers": providers,
        "active_provider_id": "primary",
        "active_model": "primary/model",
        "routing_pool": [
            {
                "provider_id": "third",
                "model": "third/model",
                "weight": 3,
                "max_concurrency": 4,
            },
            {"provider_id": "fourth", "model": "stale/model"},
            {"provider_id": "missing", "model": "missing/model"},
            {"provider_id": "primary", "model": "primary/model"},
        ],
        "fallback_provider_id": "backup",
        "fallback_model": "backup/model",
    }

    routes = bot._routing_pool_payload()

    assert [item["provider"]["id"] for item in routes] == ["third", "backup"]
    assert routes[0]["weight"] == 3
    assert routes[0]["max_concurrency"] == 4
    assert routes[1]["weight"] == 1


def test_router_menu_shows_configuration_controls_and_explicit_remove_label() -> None:
    bot = object.__new__(ARESBot)
    primary = _provider("primary", "primary/model")
    backup = _provider("backup", "backup/model")
    bot.state = {
        "providers": {"primary": primary, "backup": backup},
        "active_provider_id": "primary",
        "active_model": "primary/model",
        "routing_pool": [
            {
                "provider_id": "backup",
                "model": "backup/model",
                "weight": 2,
                "max_concurrency": 3,
            }
        ],
    }
    bot.render_menu = AsyncMock()

    asyncio.run(bot._router_menu(42))

    text = bot.render_menu.await_args.args[1]
    keyboard = bot.render_menu.await_args.args[2]
    callbacks = [button["callback_data"] for row in keyboard for button in row]
    labels = [button["text"] for row in keyboard for button in row]
    assert "weighted least-in-flight" in text
    assert "router:weight:backup:3" in callbacks
    assert "router:concurrency:backup:4" in callbacks
    assert "router:remove:backup" in callbacks
    assert any(label.startswith("🗑 Remove:") for label in labels)


def test_routing_pool_payload_ignores_invalid_weight() -> None:
    bot = object.__new__(ARESBot)
    backup = _provider("backup", "backup/model")
    bot.state = {
        "providers": {"backup": backup},
        "active_provider_id": "primary",
        "routing_pool": [
            {"provider_id": "backup", "model": "backup/model", "weight": 11},
        ],
    }

    assert bot._routing_pool_payload() == []
