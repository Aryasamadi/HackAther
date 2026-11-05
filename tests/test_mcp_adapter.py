from pathlib import Path

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
