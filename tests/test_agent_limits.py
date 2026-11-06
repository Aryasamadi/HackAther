import asyncio

import pytest
from agents.models.interface import Model
from strix.config.models import StrixProvider

from ares.agent_limits import bounded_llm_requests


class _FakeModel(Model):
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def get_response(self, *args, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return "ok"
        finally:
            self.active -= 1

    def stream_response(self, *args, **kwargs):
        async def generate():
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.01)
                yield "token"
            finally:
                self.active -= 1

        return generate()


@pytest.mark.asyncio
async def test_llm_request_limit_bounds_regular_and_streaming_calls(monkeypatch) -> None:
    model = _FakeModel()
    monkeypatch.setattr(StrixProvider, "get_model", lambda self, _name: model)
    provider = object.__new__(StrixProvider)

    async with bounded_llm_requests(2):
        limited = [provider.get_model("test/model") for _ in range(6)]
        await asyncio.gather(*(item.get_response() for item in limited))
        assert model.peak == 2

        model.peak = 0

        async def consume(item) -> None:
            async for _ in item.stream_response():
                pass

        await asyncio.gather(*(consume(item) for item in limited))
        assert model.peak == 2

    assert provider.get_model("test/model") is model


@pytest.mark.parametrize("limit", [0, -1, 9, True])
def test_llm_request_limit_rejects_unsafe_values(limit: int) -> None:
    async def run() -> None:
        async with bounded_llm_requests(limit):
            pass

    with pytest.raises(ValueError):
        asyncio.run(run())
