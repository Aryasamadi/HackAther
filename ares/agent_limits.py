from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agents.models.interface import Model
import httpx
from openai import APIConnectionError, APITimeoutError

logger = logging.getLogger(__name__)

_patch_lock = asyncio.Lock()
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_PRIMARY_COOLDOWN_SECONDS = 60.0


class PartialProviderStreamError(RuntimeError):
    """A provider stream failed after emitting output and must not replay the turn."""


def is_transient_provider_error(exc: BaseException) -> bool:
    """Only capacity, timeout, network and transient server errors may fail over."""
    if isinstance(
        exc,
        (
            APIConnectionError,
            APITimeoutError,
            TimeoutError,
            ConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ),
    ):
        return True

    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return isinstance(status, int) and status in _RETRYABLE_STATUS_CODES


class _FailoverPolicy:
    """Shared primary-provider circuit state for all Agents in one scan."""

    def __init__(self) -> None:
        self.primary_unavailable_until = 0.0

    def should_use_fallback(self) -> bool:
        return time.monotonic() < self.primary_unavailable_until

    def mark_primary_unavailable(self, exc: BaseException) -> None:
        self.primary_unavailable_until = max(
            self.primary_unavailable_until,
            time.monotonic() + _PRIMARY_COOLDOWN_SECONDS,
        )
        logger.warning(
            "Primary LLM provider temporarily unavailable; switching to configured "
            "fallback for %.0f seconds (%s)",
            _PRIMARY_COOLDOWN_SECONDS,
            type(exc).__name__,
        )

    def mark_primary_healthy(self) -> None:
        if self.primary_unavailable_until:
            logger.info("Primary LLM provider recovered; primary route is active again")
        self.primary_unavailable_until = 0.0


class _FailoverModel(Model):
    """Retry a model request through a configured secondary provider when safe."""

    def __init__(self, primary: Model, fallback: Model, policy: _FailoverPolicy) -> None:
        self._primary = primary
        self._fallback = fallback
        self._policy = policy

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)

    def get_default_model_settings(self) -> Any:
        return self._primary.get_default_model_settings()

    def get_retry_advice(self, request: Any) -> Any:
        return self._primary.get_retry_advice(request)

    async def _cleanup_on_run_end(self, owner: object) -> None:
        await self._primary._cleanup_on_run_end(owner)
        await self._fallback._cleanup_on_run_end(owner)

    async def close(self) -> None:
        await self._primary.close()
        await self._fallback.close()

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        if self._policy.should_use_fallback():
            logger.info("Using configured fallback LLM provider during primary cooldown")
            return await self._fallback.get_response(*args, **kwargs)

        try:
            response = await self._primary.get_response(*args, **kwargs)
        except Exception as exc:
            if not is_transient_provider_error(exc):
                raise
            self._policy.mark_primary_unavailable(exc)
            logger.warning("Retrying the failed LLM request with the configured fallback")
            return await self._fallback.get_response(*args, **kwargs)

        self._policy.mark_primary_healthy()
        return response

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async def _stream() -> AsyncIterator[Any]:
            if self._policy.should_use_fallback():
                logger.info("Streaming through configured fallback during primary cooldown")
                async for event in self._fallback.stream_response(*args, **kwargs):
                    yield event
                return

            emitted = False
            try:
                stream = self._primary.stream_response(*args, **kwargs)
                if inspect.isawaitable(stream):
                    stream = await stream
                async for event in stream:
                    emitted = True
                    yield event
            except Exception as exc:
                # Once a stream has emitted anything, the Strix turn retry path
                # must not replay it: visible text or tool calls may already exist.
                if emitted:
                    raise PartialProviderStreamError(
                        "Primary provider stream failed after partial output; "
                        "the turn was not replayed to avoid duplicate content."
                    ) from exc
                if not is_transient_provider_error(exc):
                    raise
                self._policy.mark_primary_unavailable(exc)
                logger.warning("Primary stream failed before output; switching to fallback")
                async for event in self._fallback.stream_response(*args, **kwargs):
                    yield event
            else:
                self._policy.mark_primary_healthy()

        return _stream()


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
async def bounded_llm_requests(
    limit: int,
    *,
    primary_model_ref: str | None = None,
    fallback_provider: dict[str, Any] | None = None,
    fallback_model: str | None = None,
) -> AsyncIterator[None]:
    """Limit real LLM calls and optionally fail over to one explicitly selected model.

    Failover is limited to the configured primary model. It only catches transient
    capacity/network/server errors; authentication, invalid-model, context and
    policy failures are returned unchanged. Streaming can fail over only before
    its first event is emitted.
    """
    if isinstance(limit, bool) or not 1 <= limit <= 8:
        raise ValueError("ARES_MAX_CONCURRENT_LLM_REQUESTS must be between 1 and 8")
    if (fallback_provider is None) != (fallback_model is None):
        raise ValueError("Fallback provider and fallback model must be configured together")
    if fallback_provider is not None:
        required = ("id", "litellm_id", "api_key", "base_url")
        if any(not str(fallback_provider.get(key, "")).strip() for key in required):
            raise ValueError("Fallback provider configuration is incomplete")
        if not str(fallback_model).strip():
            raise ValueError("Fallback model is empty")

    async with _patch_lock:
        from strix.config.models import StrixProvider

        original_get_model = StrixProvider.get_model
        semaphore = asyncio.Semaphore(limit)
        policy = _FailoverPolicy() if fallback_provider is not None else None

        def _get_limited_model(provider: Any, model_name: str | None) -> Model:
            model = original_get_model(provider, model_name)
            resolved_name = model_name
            if resolved_name is None:
                from strix.config import load_settings

                resolved_name = load_settings().llm.model
            if (
                policy is not None
                and fallback_provider is not None
                and fallback_model is not None
                and primary_model_ref
                and resolved_name == primary_model_ref
            ):
                route_id = str(fallback_provider["litellm_id"])
                fallback_ref = (
                    fallback_model
                    if fallback_model.startswith(route_id + "/")
                    else f"{route_id}/{fallback_model}"
                )
                secondary_provider = StrixProvider(
                    api_key=str(fallback_provider["api_key"]),
                    base_url=str(fallback_provider["base_url"]),
                )
                secondary_model = original_get_model(secondary_provider, fallback_ref)
                model = _FailoverModel(model, secondary_model, policy)

            return _LimitedModel(model, semaphore)

        StrixProvider.get_model = _get_limited_model  # type: ignore[method-assign]
        try:
            yield
        finally:
            StrixProvider.get_model = original_get_model  # type: ignore[method-assign]
