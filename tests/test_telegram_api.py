from __future__ import annotations

import json

import httpx
import pytest

from ares.telegram_bot import TelegramAPI


@pytest.mark.asyncio
async def test_telegram_retries_html_entity_rejection_as_plain_text() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: can't parse entities: Unsupported start tag",
                },
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 123}},
        )

    api = TelegramAPI("test-only-token")
    await api.client.aclose()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        result = await api.call(
            "sendMessage",
            {
                "chat_id": 42,
                "text": "<blockquote expandable>hello &amp; goodbye</blockquote>",
                "parse_mode": "HTML",
            },
        )

        assert result == {"message_id": 123}
        assert len(requests) == 2
        assert requests[0]["parse_mode"] == "HTML"
        assert "parse_mode" not in requests[1]
        assert requests[1]["text"] == "hello & goodbye"
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_telegram_api_error_includes_description_without_bot_token() -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            400,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: chat not found",
            },
        )

    secret = "test-only-token"
    api = TelegramAPI(secret)
    await api.client.aclose()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(RuntimeError, match="chat not found") as raised:
            await api.call("sendMessage", {"chat_id": 42, "text": "hello"})

        assert request_count == 1
        assert secret not in str(raised.value)
        assert "api.telegram.org" not in str(raised.value)
    finally:
        await api.close()


def test_plain_message_text_removes_formatting_and_decodes_entities() -> None:
    assert TelegramAPI._plain_message_text(
        "<b>Header</b><br><blockquote expandable>Body &amp; details</blockquote>"
    ) == "Header\nBody & details"
