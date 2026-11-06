import asyncio
import html
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ares.strix_adapter as adapter_module
from ares.strix_adapter import StrixAdapter
from ares.telegram_bot import (
    ARESBot,
    MAX_TELEGRAM_TEXT,
    _split_utf16,
    _utf16_units,
)


def test_split_utf16_keeps_chunks_within_telegram_limit() -> None:
    value = ("a" * 3999) + "😀" + ("b" * 5001)

    chunks = _split_utf16(value)

    assert "".join(chunks) == value
    assert len(chunks) > 1
    assert all(_utf16_units(chunk) <= MAX_TELEGRAM_TEXT for chunk in chunks)


def test_live_message_shows_only_text_in_expandable_quote() -> None:
    source = "Only the assistant's readable text. " + ("🔍" * 6000)
    message = ARESBot._live_message_text("run-123", "running", [source])
    quote = re.search(r"<blockquote expandable>(.*?)</blockquote>", message)

    assert quote is not None
    visible = html.unescape(quote.group(1))
    assert _utf16_units(visible) <= MAX_TELEGRAM_TEXT
    assert "run-123" not in message
    assert "Status:" not in message
    assert "running" not in message
    assert visible.startswith("Only the assistant's readable text. ")


def test_live_transcript_excludes_tool_events_and_user_messages() -> None:
    events = [
        {"type": "chat", "data": {"role": "assistant", "content": "Assistant text"}},
        {"type": "tool", "data": {"tool_name": "browser.search", "args": {"q": "secret"}}},
        {"type": "chat", "data": {"role": "user", "content": "User instruction"}},
        {"type": "chat", "data": {"role": "assistant", "content": "Second assistant message"}},
        {"type": "event", "data": {"content": "Status: running"}},
    ]

    assert ARESBot._assistant_live_text(events) == (
        "Assistant text\n\nSecond assistant message"
    )


def test_budget_control_is_one_state_dependent_toggle() -> None:
    bot = object.__new__(ARESBot)
    snapshot = {"budget_paused": False}
    bot.adapter = SimpleNamespace(snapshot=lambda: dict(snapshot))

    def budget_button() -> dict[str, str]:
        buttons = [button for row in bot._run_keyboard() for button in row]
        return next(button for button in buttons if button["callback_data"] == "run:toggle_budget")

    assert budget_button()["text"] == "⏸ Pause budget"
    snapshot["budget_paused"] = True
    assert budget_button()["text"] == "▶️ Resume budget"

    callbacks = [
        button["callback_data"]
        for row in bot._run_keyboard()
        for button in row
        if "budget" in button["callback_data"]
    ]
    assert callbacks == ["run:toggle_budget"]


def test_agent_detail_displays_escaped_failure_reason() -> None:
    bot = object.__new__(ARESBot)
    bot.adapter = SimpleNamespace(
        snapshot=lambda: {
            "agents": [
                {
                    "id": "agent-123",
                    "name": "Recon Agent",
                    "status": "failed",
                    "parent_id": "root",
                    "error_message": "Provider returned <html>401 Unauthorized</html>",
                }
            ]
        }
    )
    bot.render_menu = AsyncMock()

    asyncio.run(bot._show_agent(42, "agent-123"))

    bot.render_menu.assert_awaited_once()
    chat_id, message_text, _keyboard = bot.render_menu.await_args.args
    assert chat_id == 42
    assert "Failure reason" in message_text
    assert "401 Unauthorized" in message_text
    assert "&lt;html&gt;" in message_text
    assert "<html>" not in message_text


def test_findings_are_sent_as_standalone_message_with_detail_buttons() -> None:
    bot = object.__new__(ARESBot)
    bot.adapter = SimpleNamespace(
        snapshot=lambda: {
            "findings": [
                {
                    "title": "Example issue",
                    "severity": "high",
                    "target": "example.test",
                    "endpoint": "/login",
                }
            ]
        }
    )
    bot.send_long = AsyncMock()

    shown = asyncio.run(bot._show_findings(42))

    assert shown is True
    bot.send_long.assert_awaited_once()
    chat_id, message_text, keyboard = bot.send_long.await_args.args
    assert chat_id == 42
    assert "Strix findings" in message_text
    assert "Example issue" in message_text
    assert keyboard[0][0]["callback_data"] == "finding:view:0"


def test_findings_empty_returns_false_for_callback_alert() -> None:
    bot = object.__new__(ARESBot)
    bot.adapter = SimpleNamespace(snapshot=lambda: {"findings": []})

    assert asyncio.run(bot._show_findings(42)) is False


def test_delete_history_item_removes_only_selected_run(tmp_path, monkeypatch) -> None:
    runs_root = tmp_path / "strix_runs"
    target = runs_root / "run-one"
    other = runs_root / "run-two"
    target.mkdir(parents=True)
    other.mkdir(parents=True)
    (target / "run.json").write_text("{}", encoding="utf-8")
    (other / "run.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(adapter_module, "run_dir_for", lambda name: runs_root / name)
    adapter = object.__new__(StrixAdapter)
    adapter.scan_task = None
    adapter.run_name = None
    adapter.history = lambda: [{"run_name": "run-one"}, {"run_name": "run-two"}]

    adapter.delete_history_item("run-one")

    assert not target.exists()
    assert other.is_dir()


def test_delete_history_item_refuses_active_run(tmp_path, monkeypatch) -> None:
    runs_root = tmp_path / "strix_runs"
    target = runs_root / "active-run"
    target.mkdir(parents=True)
    (target / "run.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(adapter_module, "run_dir_for", lambda name: runs_root / name)

    adapter = object.__new__(StrixAdapter)
    adapter.scan_task = SimpleNamespace(done=lambda: False)
    adapter.run_name = "active-run"
    adapter.history = lambda: [{"run_name": "active-run"}]

    with pytest.raises(ValueError, match="active scan cannot be deleted"):
        adapter.delete_history_item("active-run")


def test_delete_history_item_rejects_path_traversal() -> None:
    adapter = object.__new__(StrixAdapter)
    adapter.scan_task = None
    adapter.run_name = None

    with pytest.raises(ValueError, match="Invalid run name"):
        adapter.delete_history_item("../other-run")

@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("running", True),
        ("handoff", True),
        ("manual_stop", False),
        ("completed", False),
        ("recovery_required", False),
    ],
)
def test_auto_resume_policy_distinguishes_crash_from_manual_stop(
    intent: str, expected: bool
) -> None:
    control = {"run_name": "scan-123", "intent": intent}
    assert ARESBot._should_auto_resume(control) is expected


def test_auto_resume_policy_requires_a_known_run_name() -> None:
    assert ARESBot._should_auto_resume({"intent": "running", "run_name": ""}) is False
    assert ARESBot._should_auto_resume(None) is False

def test_missing_legacy_history_marks_previous_run_recovery_required() -> None:
    state = {
        "active_run": "old-strix-run",
        "scan_control": {"run_name": "old-strix-run", "intent": "running"},
    }

    missing_run = ARESBot._mark_unavailable_history(state)

    assert missing_run == "old-strix-run"
    assert state["scan_control"]["intent"] == "recovery_required"
    assert (
        state["scan_control"]["status"]
        == "history_unavailable_before_durable_checkpoints"
    )
    assert ARESBot._should_auto_resume(state["scan_control"]) is False


@pytest.mark.parametrize("intent", ["manual_stop", "completed"])
def test_missing_legacy_history_preserves_explicit_terminal_intent(
    intent: str,
) -> None:
    control = {"run_name": "old-strix-run", "intent": intent}
    state = {"active_run": "old-strix-run", "scan_control": control}

    assert ARESBot._mark_unavailable_history(state) is None
    assert state["scan_control"] is control
