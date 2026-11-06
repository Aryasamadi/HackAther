from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from agents.models.interface import Model


_active_semaphore: ContextVar[asyncio.Semaphore | None] = ContextVar(
    "ares_llm_request_semaphore",
    default=None,
)
_patch_lock = asyncio.Lock()


class _LimitedModel(Model):
    """Apply one scan-wide concurrency limit to an SDK model's actual requests."""

    def __init__(self, model: Model, semaphore: asyncio.Semaphore) -> None:
        self._model = model
        self._semaphore = semaphore

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    def get_default_model_settings(self) -> Any:
        return self._model.get_default_model_settings()

    def get_retry_advice(self, request: Any) -> Any:
        return self._model.get_retry_advice(request)

    async def _cleanup_on_run_end(self, owner: object) -> None:
        await self._model._cleanup_on_run_end(owner)

    async def close(self) -> None:
        await self._model.close()

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        async with self._semaphore:
            return await self._model.get_response(*args, **kwargs)

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async def _limited_stream() -> AsyncIterator[Any]:
            async with self._semaphore:
                stream = self._model.stream_response(*args, **kwargs)
                if inspect.isawaitable(stream):
                    stream = await stream
                async for event in stream:
                    yield event

        return _limited_stream()


@asynccontextmanager
async def bounded_llm_requests(limit: int) -> AsyncIterator[None]:
    """Limit in-flight Strix LLM requests without blocking Agent orchestration.

    The limit is acquired around the model response/stream itself, not while a
    parent Agent creates a child. That distinction avoids parent/child deadlocks
    when Strix fans out or recursively spawns Agents.
    """
    if isinstance(limit, bool) or not 1 <= limit <= 8:
        raise ValueError("ARES_MAX_CONCURRENT_LLM_REQUESTS must be between 1 and 8")

    async with _patch_lock:
        from strix.config.models import StrixProvider

        original_get_model = StrixProvider.get_model

        def _get_limited_model(provider: Any, model_name: str | None) -> Model:
            model = original_get_model(provider, model_name)
            semaphore = _active_semaphore.get()
            return _LimitedModel(model, semaphore) if semaphore is not None else model

        semaphore = asyncio.Semaphore(limit)
        token = _active_semaphore.set(semaphore)
        StrixProvider.get_model = _get_limited_model  # type: ignore[method-assign]
        try:
            yield
        finally:
            _active_semaphore.reset(token)
            StrixProvider.get_model = original_get_model  # type: ignore[method-assign]
