from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from ares.strix_adapter import StrixAdapter
from ares.telegram_bot import ARESBot
from strix.interface.tui.backend.live_view import TuiLiveView
from strix.tools.mcp import McpConnectionConfig


def test_native_mcp_http_request_is_validated_and_selected() -> None:
    requests = StrixAdapter._build_mcp_requests(
        [
            {
                "name": "github",
                "transport": "http",
                "url": "https://mcp.example.com/mcp",
                "auth": {"kind": "bearer", "token": "test-token"},
                "allowed_tools": ["search_repositories"],
            },
            {
                "name": "docs",
                "transport": "http",
                "url": "https://docs.example.com/mcp",
            },
        ],
        only=["github"],
    )
    assert len(requests) == 1
    assert requests[0].config.name == "github"
    assert requests[0].config.auth.token == "test-token"


def test_native_mcp_stdio_connection_is_supported() -> None:
    config = McpConnectionConfig.model_validate(
        {
            "name": "local-tools",
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "example_mcp_server"],
            "env": {"MODE": "test"},
        }
    )
    assert config.transport == "stdio"
    assert config.command == "python"


def test_mcp_duplicate_names_are_rejected() -> None:
    entry = {"name": "github", "transport": "http", "url": "https://mcp.example.com"}
    with pytest.raises(ValueError, match="Duplicate MCP"):
        StrixAdapter._build_mcp_requests([entry, entry])


def test_mcp_invalid_transport_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        McpConnectionConfig.model_validate(
            {"name": "broken-http", "transport": "http"}
        )


def test_adapter_args_preserve_scope_and_workspace_files(tmp_path: Path) -> None:
    workspace = tmp_path / "context.md"
    workspace.write_text("authorized test context", encoding="utf-8")
    args = StrixAdapter._args(
        targets=["https://example.com"],
        instruction="Focus on authentication",
        scan_mode="standard",
        scope_mode="diff",
        diff_base="origin/main",
        max_budget_usd=2.5,
        max_turns=120,
        workspace_files=[str(workspace)],
    )
    assert args.scan_mode == "standard"
    assert args.scope_mode == "diff"
    assert args.diff_base == "origin/main"
    assert args.max_budget_usd == 2.5
    assert args.max_turns == 120
    assert args.workspace_files == [
        {
            "source_path": str(workspace.resolve()),
            "workspace_path": "/workspace/context.md",
        }
    ]


def test_live_view_event_schema_matches_native_strix() -> None:
    live_view = TuiLiveView()
    event = live_view._append_event(
        "agent-root",
        "chat",
        {"role": "assistant", "content": "hello"},
    )
    assert event["type"] == "chat"
    assert event["agent_id"] == "agent-root"
    assert event["data"]["content"] == "hello"


def test_bot_module_imports_without_starting_network_clients() -> None:
    assert ARESBot is not None


@pytest.mark.asyncio
async def test_provider_wizard_requests_name_then_base_url_then_token() -> None:
    bot = ARESBot.__new__(ARESBot)
    bot.state = {"pending": {"kind": "provider", "step": "name"}}
    bot.render_menu = AsyncMock()

    assert await bot._consume_pending(123, "My Gateway") is True
    assert bot.state["pending"]["step"] == "base_url"

    assert await bot._consume_pending(123, "https://api.example.com/v1") is True
    assert bot.state["pending"]["step"] == "token"
    assert bot.state["pending"]["name"] == "My Gateway"


@pytest.mark.asyncio
async def test_mcp_wizard_collects_http_fields_without_json() -> None:
    bot = ARESBot.__new__(ARESBot)
    bot.state = {
        "pending": {
            "kind": "mcp",
            "step": "http_url",
            "draft": {"name": "github", "transport": "http"},
        },
        "mcp_servers": [],
    }
    bot.render_menu = AsyncMock()

    assert await bot._consume_pending(123, "https://mcp.example.com/mcp") is True
    assert bot.state["pending"]["step"] == "auth"

    bot.state["pending"]["draft"]["auth"] = {
        "kind": "bearer",
        "token": "test-secret-token",
    }
    bot.state["pending"]["step"] = "allowed_tools"
    assert await bot._consume_pending(123, "search_repositories, get_repository") is True
    assert bot.state["pending"]["step"] == "notes"
    assert await bot._consume_pending(123, "-") is True

    saved = bot.state["mcp_servers"]
    assert len(saved) == 1
    config = McpConnectionConfig.model_validate(saved[0])
    assert config.name == "github"
    assert config.url == "https://mcp.example.com/mcp"
    assert config.auth.token == "test-secret-token"
    assert config.allowed_tools == ["search_repositories", "get_repository"]
    assert bot.state["pending"] is None


@pytest.mark.asyncio
async def test_mcp_wizard_collects_stdio_command_args_and_environment() -> None:
    bot = ARESBot.__new__(ARESBot)
    bot.state = {
        "pending": {
            "kind": "mcp",
            "step": "stdio_command",
            "draft": {"name": "local-tools", "transport": "stdio"},
        },
        "mcp_servers": [],
    }
    bot.render_menu = AsyncMock()

    assert await bot._consume_pending(123, "python") is True
    assert bot.state["pending"]["step"] == "stdio_args"
    assert await bot._consume_pending(123, "-m\nexample_mcp_server") is True
    assert bot.state["pending"]["step"] == "stdio_env"
    assert await bot._consume_pending(123, "MODE=test\nREGION=local") is True
    assert bot.state["pending"]["step"] == "allowed_tools"
    assert await bot._consume_pending(123, "-") is True
    assert bot.state["pending"]["step"] == "notes"
    assert await bot._consume_pending(123, "-") is True

    config = McpConnectionConfig.model_validate(bot.state["mcp_servers"][0])
    assert config.command == "python"
    assert config.args == ["-m", "example_mcp_server"]
    assert config.env == {"MODE": "test", "REGION": "local"}


@pytest.mark.asyncio
async def test_render_menu_edits_current_panel_instead_of_sending_another() -> None:
    bot = ARESBot.__new__(ARESBot)
    bot.state = {"ui_message_id": 901, "ui_chat_id": 123}
    bot.telegram = type("Telegram", (), {"call": AsyncMock(return_value={})})()
    bot.send = AsyncMock()

    await bot.render_menu(123, "<b>Updated panel</b>", [])

    bot.telegram.call.assert_awaited_once()
    method, payload = bot.telegram.call.await_args.args
    assert method == "editMessageText"
    assert payload["message_id"] == 901
    bot.send.assert_not_awaited()
