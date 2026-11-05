import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ares.telegram_bot import ARESBot


def test_user_text_input_detaches_previous_menu_before_reply() -> None:
    bot = object.__new__(ARESBot)
    bot.config = SimpleNamespace(admin_id=42)
    bot.state = {
        "ui_message_id": 100,
        "ui_chat_id": 42,
        "pending": {"kind": "scan", "step": "instruction"},
    }
    bot._consume_setting = AsyncMock(return_value=False)

    async def consume_pending(chat_id: int, text: str) -> bool:
        assert bot.state["ui_message_id"] is None
        assert bot.state["ui_chat_id"] == chat_id
        return True

    bot._consume_pending = consume_pending

    message = {
        "from": {"id": 42},
        "chat": {"id": 42, "type": "private"},
        "message_id": 101,
        "text": "Focus on authentication and access control.",
    }
    asyncio.run(bot._handle_message(message))

    bot._consume_setting.assert_awaited_once_with(42, message["text"])
    assert bot.state["ui_message_id"] is None


def test_scan_instruction_prompt_explains_defaults_and_safe_scope() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {"pending": {"kind": "scan", "step": "target"}}
    bot.render_menu = AsyncMock()

    asyncio.run(bot._consume_pending(42, "https://example.test"))

    prompt = bot.render_menu.await_args.args[1]
    assert "Focus on login, access control and input validation" in prompt
    assert "non-destructive tests only" in prompt
    assert "send <code>-</code>" in prompt
    assert "dedicated test account only" in prompt
