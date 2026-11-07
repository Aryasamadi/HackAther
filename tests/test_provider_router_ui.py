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


def _provider(provider_id: str, model: str) -> dict:
    return {
        "id": provider_id,
        "name": provider_id.title(),
        "litellm_id": provider_id,
        "base_url": f"https://{provider_id}.example.test/v1",
        "api_key": "secret",
        "models": [model, f"{provider_id}/model-2"],
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


def test_selecting_another_provider_model_automatically_adds_a_route() -> None:
    bot = object.__new__(ARESBot)
    bot.config = SimpleNamespace(admin_id=42)
    bot.state = {
        "ui_message_id": None,
        "ui_chat_id": None,
        "providers": {
            "primary": _provider("primary", "primary/model"),
            "backup": _provider("backup", "backup/model"),
        },
        "active_provider_id": "primary",
        "active_model": "primary/model",
    }
    bot.answer_callback = AsyncMock()
    bot.render_menu = AsyncMock()

    asyncio.run(bot._handle_callback(_callback("model:select:backup:1")))

    assert bot.state["active_provider_id"] == "primary"
    assert bot.state["active_model"] == "primary/model"
    assert bot.state["routing_pool"] == [
        {
            "provider_id": "backup",
            "model": "backup/model-2",
            "weight": 1,
            "max_concurrency": 2,
        }
    ]
    assert "added to background AI routing" in bot.render_menu.await_args.args[1]


def test_changing_a_routed_provider_model_preserves_route_limits() -> None:
    bot = object.__new__(ARESBot)
    bot.config = SimpleNamespace(admin_id=42)
    bot.state = {
        "ui_message_id": None,
        "ui_chat_id": None,
        "providers": {
            "primary": _provider("primary", "primary/model"),
            "backup": _provider("backup", "backup/model"),
        },
        "active_provider_id": "primary",
        "active_model": "primary/model",
        "routing_pool": [
            {
                "provider_id": "backup",
                "model": "backup/model",
                "weight": 4,
                "max_concurrency": 5,
            }
        ],
    }
    bot.answer_callback = AsyncMock()
    bot.render_menu = AsyncMock()

    asyncio.run(bot._handle_callback(_callback("model:select:backup:1")))

    assert bot.state["routing_pool"] == [
        {
            "provider_id": "backup",
            "model": "backup/model-2",
            "weight": 4,
            "max_concurrency": 5,
        }
    ]


def test_first_selected_model_becomes_default_and_is_removed_from_routes() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {
        "providers": {
            "backup": _provider("backup", "backup/model"),
        },
        "active_provider_id": None,
        "active_model": None,
        "routing_pool": [
            {
                "provider_id": "backup",
                "model": "backup/model",
                "weight": 3,
                "max_concurrency": 2,
            }
        ],
    }

    notice = bot._remember_provider_model("backup", "backup/model-2")

    assert bot.state["active_provider_id"] == "backup"
    assert bot.state["active_model"] == "backup/model-2"
    assert bot.state["routing_pool"] == []
    assert notice == "Default model selected."


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
