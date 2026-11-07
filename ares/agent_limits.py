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
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 529})
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


class _PoolRoute:
    def __init__(
        self,
        key: str,
        name: str,
        model: Model,
        *,
        weight: int = 1,
        max_concurrency: int = 2,
    ) -> None:
        if not 1 <= weight <= 10:
            raise ValueError("Provider route weight must be between 1 and 10")
        if not 1 <= max_concurrency <= 8:
            raise ValueError("Provider route concurrency must be between 1 and 8")
        self.key = key
        self.name = name
        self.model = model
        self.weight = weight
        self.max_concurrency = max_concurrency
        self.in_flight = 0
        self.failures = 0
        self.cooldown_until = 0.0


class _ProviderPool:
    """Shared weighted least-in-flight scheduler with per-provider circuit breakers."""

    def __init__(self, routes: list[dict[str, Any]]) -> None:
        if not routes:
            raise ValueError("At least one compatible provider route is required")
        self.routes = [
            _PoolRoute(
                str(item["key"]),
                str(item.get("name") or item["key"]),
                item["model"],
                weight=int(item.get("weight", 1)),
                max_concurrency=int(item.get("max_concurrency", 2)),
            )
            for item in routes
        ]
        if len({route.key for route in self.routes}) != len(self.routes):
            raise ValueError("Provider pool contains duplicate provider IDs")
        self._condition = asyncio.Condition()
        self._cursor = 0

    async def acquire(self, excluded: set[str]) -> _PoolRoute:
        async with self._condition:
            while True:
                candidates = [
                    (index, route)
                    for index, route in enumerate(self.routes)
                    if route.key not in excluded
                ]
                if not candidates:
                    raise RuntimeError("All configured provider routes failed for this request")

                now = time.monotonic()
                available = [
                    (index, route)
                    for index, route in candidates
                    if route.cooldown_until <= now
                    and route.in_flight < route.max_concurrency
                ]
                if available:
                    index, route = min(
                        available,
                        key=lambda pair: (
                            pair[1].in_flight / pair[1].weight,
                            (pair[0] - self._cursor) % len(self.routes),
                        ),
                    )
                    route.in_flight += 1
                    self._cursor = (index + 1) % len(self.routes)
                    return route

                cooldowns = [
                    route.cooldown_until - now
                    for _, route in candidates
                    if route.cooldown_until > now
                ]
                timeout = max(0.05, min(cooldowns)) if cooldowns else None
                try:
                    if timeout is None:
                        await self._condition.wait()
                    else:
                        await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                except TimeoutError:
                    pass

    async def release(
        self,
        route: _PoolRoute,
        *,
        success: bool,
        transient_failure: bool = False,
    ) -> None:
        async with self._condition:
            route.in_flight = max(0, route.in_flight - 1)
            if success:
                if route.failures:
                    logger.info("Provider route %s recovered and re-entered the pool", route.name)
                route.failures = 0
                route.cooldown_until = 0.0
            elif transient_failure:
                route.failures += 1
                cooldown = min(60.0 * (2 ** (route.failures - 1)), 300.0)
                route.cooldown_until = time.monotonic() + cooldown
                logger.warning(
                    "Provider route %s entered cooldown for %.0f seconds after a transient error",
                    route.name,
                    cooldown,
                )
            self._condition.notify_all()


class _ProviderPoolModel(Model):
    """Route each Strix request to a healthy provider; never replay a partial stream."""

    def __init__(self, routes: list[dict[str, Any]]) -> None:
        self._pool = _ProviderPool(routes)
        self._routes = self._pool.routes

    def get_default_model_settings(self) -> Any:
        return self._routes[0].model.get_default_model_settings()

    def get_retry_advice(self, request: Any) -> Any:
        return self._routes[0].model.get_retry_advice(request)

    async def _cleanup_on_run_end(self, owner: object) -> None:
        for route in self._routes:
            await route.model._cleanup_on_run_end(owner)

    async def close(self) -> None:
        for route in self._routes:
            await route.model.close()

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        excluded: set[str] = set()
        last_error: BaseException | None = None
        for _ in self._routes:
            route = await self._pool.acquire(excluded)
            try:
                response = await route.model.get_response(*args, **kwargs)
            except asyncio.CancelledError:
                await asyncio.shield(self._pool.release(route, success=False))
                raise
            except Exception as exc:
                transient = is_transient_provider_error(exc)
                await self._pool.release(
                    route, success=False, transient_failure=transient
                )
                if not transient:
                    raise
                excluded.add(route.key)
                last_error = exc
            else:
                await self._pool.release(route, success=True)
                return response
        if last_error is not None:
            raise last_error
        raise RuntimeError("No provider route could serve this request")

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async def _stream() -> AsyncIterator[Any]:
            excluded: set[str] = set()
            last_error: BaseException | None = None
            for _ in self._routes:
                route = await self._pool.acquire(excluded)
                emitted = False
                released = False
                try:
                    stream = route.model.stream_response(*args, **kwargs)
                    if inspect.isawaitable(stream):
                        stream = await stream
                    async for event in stream:
                        emitted = True
                        yield event
                except asyncio.CancelledError:
                    await asyncio.shield(self._pool.release(route, success=False))
                    released = True
                    raise
                except Exception as exc:
                    transient = is_transient_provider_error(exc)
                    await self._pool.release(
                        route, success=False, transient_failure=transient
                    )
                    released = True
                    if emitted:
                        raise PartialProviderStreamError(
                            "Provider stream failed after partial output; the turn was not replayed."
                        ) from exc
                    if not transient:
                        raise
                    excluded.add(route.key)
                    last_error = exc
                else:
                    await self._pool.release(route, success=True)
                    released = True
                    return
                finally:
                    # Also release the route when the consumer closes the stream.
                    if not released:
                        await asyncio.shield(self._pool.release(route, success=False))
            if last_error is not None:
                raise last_error
            raise RuntimeError("No provider route could serve this stream")

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
    provider_pool: list[dict[str, Any]] | None = None,
) -> AsyncIterator[None]:
    """Limit scan-wide LLM pressure and optionally route through a shared provider pool.

    Pool candidates are credential-bound OpenAI-compatible Chat Completions routes.
    The scheduler uses weighted least-in-flight selection, per-provider concurrency,
    and transient-error circuit breakers. A stream is never replayed after output.
    Legacy single-backup arguments remain supported for encrypted-state migration.
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

        pool_model: _ProviderPoolModel | None = None
        if provider_pool is not None:
            from strix.config.models import StrixProvider

            route_models: list[dict[str, Any]] = []
            for item in provider_pool:
                route_provider = item.get("provider")
                route_model_name = str(item.get("model") or "").strip()
                if not isinstance(route_provider, dict) or not route_model_name:
                    raise ValueError("Provider pool entries require a provider and model")
                required = ("id", "litellm_id", "api_key", "base_url")
                if any(not str(route_provider.get(key, "")).strip() for key in required):
                    raise ValueError("A provider pool entry is missing credentials or routing metadata")
                route_id = str(route_provider["litellm_id"])
                route_ref = (
                    route_model_name
                    if route_model_name.startswith(route_id + "/")
                    else f"{route_id}/{route_model_name}"
                )
                credentialed_provider = StrixProvider(
                    api_key=str(route_provider["api_key"]),
                    base_url=str(route_provider["base_url"]),
                )
                route_models.append(
                    {
                        "key": str(route_provider["id"]),
                        "name": str(route_provider.get("name") or route_provider["id"]),
                        "model": original_get_model(credentialed_provider, route_ref),
                        "weight": int(item.get("weight", 1)),
                        "max_concurrency": int(item.get("max_concurrency", 2)),
                    }
                )
            pool_model = _ProviderPoolModel(route_models)

        def _get_limited_model(provider: Any, model_name: str | None) -> Model:
            resolved_name = model_name
            if resolved_name is None:
                from strix.config import load_settings

                resolved_name = load_settings().llm.model
            if pool_model is not None and primary_model_ref and resolved_name == primary_model_ref:
                model = pool_model
            else:
                model = original_get_model(provider, model_name)
            if (
                pool_model is None
                and policy is not None
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
