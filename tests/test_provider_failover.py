import pytest
from agents.models.interface import Model

from ares.agent_limits import (
    PartialProviderStreamError,
    _FailoverModel,
    _FailoverPolicy,
    is_transient_provider_error,
)


class _ProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"provider returned HTTP {status_code}")
        self.status_code = status_code


class _FakeModel(Model):
    def __init__(self, *, response=None, error=None, events=None, stream_error=None) -> None:
        self.response = response
        self.error = error
        self.events = events or []
        self.stream_error = stream_error
        self.response_calls = 0
        self.stream_calls = 0

    async def get_response(self, *args, **kwargs):
        self.response_calls += 1
        if self.error is not None:
            raise self.error
        return self.response

    def stream_response(self, *args, **kwargs):
        async def generate():
            self.stream_calls += 1
            for event in self.events:
                yield event
            if self.stream_error is not None:
                raise self.stream_error

        return generate()


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_transient_provider_statuses_are_failover_eligible(status: int) -> None:
    assert is_transient_provider_error(_ProviderError(status))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_auth_configuration_and_input_errors_do_not_fail_over(status: int) -> None:
    assert not is_transient_provider_error(_ProviderError(status))


def test_network_and_timeout_errors_are_failover_eligible() -> None:
    assert is_transient_provider_error(TimeoutError("read timed out"))
    assert is_transient_provider_error(ConnectionError("connection reset"))


@pytest.mark.asyncio
async def test_response_fails_over_and_uses_fallback_during_primary_cooldown() -> None:
    primary = _FakeModel(error=_ProviderError(429))
    fallback = _FakeModel(response="backup response")
    policy = _FailoverPolicy()
    model = _FailoverModel(primary, fallback, policy)

    assert await model.get_response() == "backup response"
    assert primary.response_calls == 1
    assert fallback.response_calls == 1
    assert policy.should_use_fallback()

    assert await model.get_response() == "backup response"
    assert primary.response_calls == 1
    assert fallback.response_calls == 2


@pytest.mark.asyncio
async def test_auth_error_is_returned_without_trying_fallback() -> None:
    primary = _FakeModel(error=_ProviderError(401))
    fallback = _FakeModel(response="must not be used")
    model = _FailoverModel(primary, fallback, _FailoverPolicy())

    with pytest.raises(_ProviderError):
        await model.get_response()

    assert primary.response_calls == 1
    assert fallback.response_calls == 0


@pytest.mark.asyncio
async def test_stream_fails_over_only_before_first_event() -> None:
    primary = _FakeModel(stream_error=_ProviderError(503))
    fallback = _FakeModel(events=["backup token"])
    model = _FailoverModel(primary, fallback, _FailoverPolicy())

    events = [event async for event in model.stream_response()]

    assert events == ["backup token"]
    assert primary.stream_calls == 1
    assert fallback.stream_calls == 1


@pytest.mark.asyncio
async def test_stream_with_partial_output_is_not_replayed_on_fallback() -> None:
    primary = _FakeModel(events=["partial primary token"], stream_error=_ProviderError(503))
    fallback = _FakeModel(events=["must not be replayed"])
    model = _FailoverModel(primary, fallback, _FailoverPolicy())
    stream = model.stream_response()

    assert await anext(stream) == "partial primary token"
    with pytest.raises(PartialProviderStreamError):
        await anext(stream)

    assert fallback.stream_calls == 0
    assert not is_transient_provider_error(PartialProviderStreamError("partial output"))
