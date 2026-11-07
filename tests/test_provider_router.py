import asyncio

import pytest
from agents.models.interface import Model

from ares.agent_limits import PartialProviderStreamError, _ProviderPoolModel


class _ProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _FakeModel(Model):
    def __init__(
        self,
        name: str,
        *,
        error=None,
        events=None,
        stream_error=None,
        wait_after_event: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.error = error
        self.events = events or []
        self.stream_error = stream_error
        self.wait_after_event = wait_after_event
        self.calls = 0
        self.active = 0
        self.peak = 0

    async def get_response(self, *args, **kwargs):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            if self.error:
                raise self.error
            return self.name
        finally:
            self.active -= 1

    def stream_response(self, *args, **kwargs):
        async def generate():
            self.calls += 1
            for event in self.events:
                yield event
                if self.wait_after_event is not None:
                    await self.wait_after_event.wait()
            if self.stream_error:
                raise self.stream_error
        return generate()


def _route(key: str, model: Model, *, weight: int = 1, cap: int = 2) -> dict:
    return {
        "key": key,
        "name": key,
        "model": model,
        "weight": weight,
        "max_concurrency": cap,
    }


@pytest.mark.asyncio
async def test_pool_spreads_parallel_requests_across_healthy_providers() -> None:
    first = _FakeModel("first")
    second = _FakeModel("second")
    pool = _ProviderPoolModel([_route("one", first), _route("two", second)])

    results = await asyncio.gather(*(pool.get_response() for _ in range(2)))

    assert set(results) == {"first", "second"}
    assert first.calls == 1
    assert second.calls == 1


@pytest.mark.asyncio
async def test_pool_fails_over_and_cools_down_only_the_failing_provider() -> None:
    primary = _FakeModel("primary", error=_ProviderError(429))
    secondary = _FakeModel("secondary")
    pool = _ProviderPoolModel([_route("primary", primary), _route("secondary", secondary)])

    assert await pool.get_response() == "secondary"
    assert primary.calls == 1
    assert secondary.calls == 1
    assert await pool.get_response() == "secondary"
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_pool_stream_fails_over_before_first_event() -> None:
    primary = _FakeModel("primary", stream_error=_ProviderError(503))
    secondary = _FakeModel("secondary", events=["fallback"])
    pool = _ProviderPoolModel([_route("primary", primary), _route("secondary", secondary)])

    assert [event async for event in pool.stream_response()] == ["fallback"]
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_pool_never_replays_a_stream_after_partial_output() -> None:
    primary = _FakeModel(
        "primary",
        events=["partial"],
        stream_error=_ProviderError(503),
    )
    secondary = _FakeModel("secondary", events=["must-not-replay"])
    pool = _ProviderPoolModel([_route("primary", primary), _route("secondary", secondary)])
    stream = pool.stream_response()

    assert await anext(stream) == "partial"
    with pytest.raises(PartialProviderStreamError):
        await anext(stream)

    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_pool_releases_provider_slot_when_stream_is_closed() -> None:
    gate = asyncio.Event()
    provider = _FakeModel("provider", events=["first"], wait_after_event=gate)
    pool = _ProviderPoolModel([_route("provider", provider, cap=1)])
    stream = pool.stream_response()

    assert await anext(stream) == "first"
    await stream.aclose()

    assert await asyncio.wait_for(pool.get_response(), timeout=0.2) == "provider"


@pytest.mark.asyncio
async def test_pool_honours_per_provider_parallel_request_cap() -> None:
    first = _FakeModel("first")
    second = _FakeModel("second")
    pool = _ProviderPoolModel(
        [_route("one", first, cap=1), _route("two", second, cap=1)]
    )

    await asyncio.gather(*(pool.get_response() for _ in range(4)))

    assert first.peak <= 1
    assert second.peak <= 1
