from ares.telegram_bot import _format_structured_value


def test_structured_finding_value_is_rendered_as_readable_sections() -> None:
    rendered = _format_structured_value(
        {
            "http_exchange": [{"method": "GET", "url": "https://example.test/api"}],
            "retry_after_seconds": 3,
            "verified": True,
        }
    )

    assert "Http exchange:" in rendered
    assert "• Method: GET" in rendered
    assert "Url: https://example.test/api" in rendered
    assert "Retry after seconds: 3" in rendered
    assert "Verified: Yes" in rendered
    assert not rendered.lstrip().startswith("{")
