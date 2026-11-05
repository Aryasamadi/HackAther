from ares.strix_adapter import StrixAdapter


def test_adapter_imports_the_pinned_strix_runtime() -> None:
    adapter = StrixAdapter()
    assert adapter.running is False
    assert adapter.coordinator is None
