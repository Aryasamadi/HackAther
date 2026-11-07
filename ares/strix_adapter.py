from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from ares.agent_limits import bounded_llm_requests
from strix.config import apply_config_override, load_settings
from strix.core.agents import AgentCoordinator
from strix.core.paths import run_dir_for
from strix.core.runner import run_strix_scan
from strix.interface.scan_setup import (
    build_targets_info,
    prepare_run,
    preflight_model_connection,
    preflight_request,
    telemetry_start,
)
from strix.interface.tui.backend.controller import TuiController
from strix.interface.tui.backend.live_view import TuiLiveView
from strix.interface.utils import read_workspace_files, resolve_workspace_files
from strix.report.runs import list_run_summaries
from strix.report.state import ReportState, set_global_report_state
from strix.tools.mcp import McpConnectionConfig, McpConnectionRequest


logger = logging.getLogger(__name__)


class StrixAdapter:
    """Telegram adapter over the official Strix interactive runtime.

    ARES does not clone or reimplement Strix agents. It invokes Strix's runner,
    coordinator, Go-TUI controller, live projection, MCP contracts and report state.
    """

    def __init__(self) -> None:
        self.coordinator: AgentCoordinator | None = None
        self.live_view: TuiLiveView | None = None
        self.controller: TuiController | None = None
        self.report_state: ReportState | None = None
        self.scan_task: asyncio.Task[None] | None = None
        self.scan_error: str | None = None
        self.run_name: str | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self._config_path: Path | None = None
        self._mcp_requests: list[McpConnectionRequest] = []
        self._workspace_files: list[dict[str, Any]] = []
        self._scan_mode = "deep"
        self._scope_mode = "auto"
        self._diff_base: str | None = None
        self._model_ref: str | None = None
        self._fallback_provider: dict[str, Any] | None = None
        self._fallback_model: str | None = None
        self._provider_pool: list[dict[str, Any]] | None = None
        raw_request_limit = os.environ.get("ARES_MAX_CONCURRENT_LLM_REQUESTS", "2").strip()
        try:
            self.max_concurrent_llm_requests = int(raw_request_limit)
        except ValueError as exc:
            raise RuntimeError("ARES_MAX_CONCURRENT_LLM_REQUESTS must be an integer from 1 to 8") from exc
        if not 1 <= self.max_concurrent_llm_requests <= 8:
            raise RuntimeError("ARES_MAX_CONCURRENT_LLM_REQUESTS must be between 1 and 8")

    @property
    def running(self) -> bool:
        return self.scan_task is not None and not self.scan_task.done()

    def _configure_model(self, provider: dict[str, Any], model: str) -> None:
        provider_id = str(provider["litellm_id"])
        model_ref = model if model.startswith(provider_id + "/") else f"{provider_id}/{model}"
        self._model_ref = model_ref
        self._config_path = Path(".ares-runtime") / "strix-cli-config.json"
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {
            "env": {
                "STRIX_LLM": model_ref,
                "LLM_API_KEY": str(provider["api_key"]),
                "LLM_API_BASE": str(provider["base_url"]),
            }
        }
        self._config_path.write_text(json.dumps(config), encoding="utf-8")
        try:
            self._config_path.chmod(0o600)
        except OSError:
            pass
        apply_config_override(self._config_path)

    def _configure_failover(
        self,
        primary_provider: dict[str, Any],
        fallback_provider: dict[str, Any] | None,
        fallback_model: str | None,
    ) -> None:
        if (fallback_provider is None) != (fallback_model is None):
            raise ValueError("Fallback provider and model must be configured together")
        self._fallback_provider = None
        self._fallback_model = None
        self._provider_pool = None
        if fallback_provider is None or fallback_model is None:
            return
        if fallback_provider.get("id") == primary_provider.get("id"):
            raise ValueError("Fallback provider must be different from the primary provider")
        models = fallback_provider.get("models", [])
        if not isinstance(models, list) or fallback_model not in models:
            raise ValueError("Configured fallback model is no longer in the provider catalog")
        for key in ("id", "litellm_id", "api_key", "base_url"):
            if not str(fallback_provider.get(key, "")).strip():
                raise ValueError(f"Fallback provider is missing required field: {key}")

        from strix.config.models import supports_strict_tool_schemas

        route_id = str(fallback_provider["litellm_id"])
        fallback_ref = (
            fallback_model
            if fallback_model.startswith(route_id + "/")
            else f"{route_id}/{fallback_model}"
        )
        if supports_strict_tool_schemas(self._model_ref or "") != supports_strict_tool_schemas(
            fallback_ref
        ):
            raise ValueError(
                "Primary and backup models must use compatible Strix tool-schema modes. "
                "Choose two models that both support strict schemas, or two that both "
                "require non-strict schemas."
            )

        # The credential-bound secondary StrixProvider uses Chat Completions.
        # Force the primary route to the same API shape while failover is enabled.
        if self._config_path is None:
            raise RuntimeError("Primary model configuration was not initialized")
        config = json.loads(self._config_path.read_text(encoding="utf-8"))
        env = config.setdefault("env", {})
        if not isinstance(env, dict):
            raise ValueError("Primary Strix configuration has an invalid env block")
        env["STRIX_API_TYPE"] = "chat_completions"
        self._config_path.write_text(json.dumps(config), encoding="utf-8")
        try:
            self._config_path.chmod(0o600)
        except OSError:
            pass
        apply_config_override(self._config_path)
        effective_settings = load_settings()
        if effective_settings.llm.api_type not in (None, "chat_completions"):
            raise ValueError(
                "The process environment forces a non-Chat-Completions STRIX_API_TYPE. "
                "Remove that override before enabling this backup route."
            )

        self._fallback_provider = dict(fallback_provider)
        self._fallback_model = fallback_model
        self._provider_pool = None

    def _configure_provider_pool(
        self,
        primary_provider: dict[str, Any],
        primary_model: str,
        additional_routes: list[dict[str, Any]],
    ) -> None:
        """Validate and prepare the primary plus every enabled shared-pool route."""
        from ares.providers import build_model_reference
        from strix.config.models import supports_strict_tool_schemas

        if self._model_ref is None:
            raise RuntimeError("Primary model configuration was not initialized")
        routes: list[dict[str, Any]] = []
        seen_provider_ids: set[str] = set()
        primary_schema_mode = supports_strict_tool_schemas(self._model_ref)

        def add_route(
            provider: dict[str, Any],
            model: str,
            *,
            weight: int = 1,
            max_concurrency: int = 2,
        ) -> None:
            if not isinstance(provider, dict):
                raise ValueError("Provider pool contains an invalid provider record")
            required = ("id", "litellm_id", "api_key", "base_url")
            if any(not str(provider.get(key, "")).strip() for key in required):
                raise ValueError("A provider pool route is missing credentials or routing metadata")
            provider_id = str(provider["id"])
            if provider_id in seen_provider_ids:
                raise ValueError("The AI Router accepts one model per provider at a time")
            models = provider.get("models", [])
            if not isinstance(models, list) or model not in models:
                raise ValueError(
                    f"Model {model!r} is not in the saved catalog for "
                    f"{provider.get('name', provider_id)}. Refresh its model list."
                )
            if isinstance(weight, bool) or not 1 <= int(weight) <= 10:
                raise ValueError("Provider weight must be between 1 and 10")
            if isinstance(max_concurrency, bool) or not 1 <= int(max_concurrency) <= 8:
                raise ValueError("Provider concurrency must be between 1 and 8")
            model_ref = build_model_reference(str(provider["litellm_id"]), model)
            if supports_strict_tool_schemas(model_ref) != primary_schema_mode:
                raise ValueError(
                    f"{provider.get('name', provider_id)} / {model} has an incompatible "
                    "Strix tool-schema mode. Choose a model with the same strict-schema "
                    "compatibility as the primary model."
                )
            seen_provider_ids.add(provider_id)
            routes.append(
                {
                    "provider": dict(provider),
                    "model": model,
                    "weight": int(weight),
                    "max_concurrency": int(max_concurrency),
                }
            )

        add_route(primary_provider, primary_model)
        for item in additional_routes:
            if not isinstance(item, dict):
                raise ValueError("Provider pool entries must be objects")
            provider = item.get("provider")
            model = str(item.get("model") or "").strip()
            if not model:
                raise ValueError("A provider pool route has no selected model")
            add_route(
                provider,
                model,
                weight=item.get("weight", 1),
                max_concurrency=item.get("max_concurrency", 2),
            )

        self._provider_pool = routes
        self._fallback_provider = None
        self._fallback_model = None

        if len(routes) > 1:
            if self._config_path is None:
                raise RuntimeError("Primary model configuration was not initialized")
            config = json.loads(self._config_path.read_text(encoding="utf-8"))
            env = config.setdefault("env", {})
            if not isinstance(env, dict):
                raise ValueError("Primary Strix configuration has an invalid env block")
            env["STRIX_API_TYPE"] = "chat_completions"
            self._config_path.write_text(json.dumps(config), encoding="utf-8")
            try:
                self._config_path.chmod(0o600)
            except OSError:
                pass
            apply_config_override(self._config_path)
            effective_settings = load_settings()
            if effective_settings.llm.api_type != "chat_completions":
                raise ValueError(
                    "The process environment overrides STRIX_API_TYPE. The shared "
                    "provider pool requires the OpenAI-compatible Chat Completions API."
                )

    async def _preflight_provider_pool(self) -> None:
        """Probe every configured route and retain only endpoints that answer."""
        if not self._provider_pool:
            raise RuntimeError("The shared AI Router has no configured provider routes")

        from ares.providers import build_model_reference
        from strix.config.models import StrixProvider, configure_sdk_model_defaults

        settings = load_settings()
        configure_sdk_model_defaults(settings)
        semaphore = asyncio.Semaphore(min(3, len(self._provider_pool)))

        async def probe(route: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
            provider = route["provider"]
            model_ref = build_model_reference(
                str(provider["litellm_id"]), str(route["model"])
            )
            model = None
            try:
                model = StrixProvider(
                    api_key=str(provider["api_key"]),
                    base_url=str(provider["base_url"]),
                ).get_model(model_ref)
                async with semaphore:
                    await preflight_request(
                        model,
                        model_name=model_ref,
                        extra_headers=settings.llm.extra_headers,
                        timeout=settings.llm.preflight_timeout,
                        api_base_setting=f"Base URL for {provider.get('name', provider['id'])}",
                    )
                return route, None
            except Exception as exc:
                logger.warning(
                    "AI Router route %s failed startup preflight (%s)",
                    provider.get("name", provider["id"]),
                    type(exc).__name__,
                )
                return route, f"{provider.get('name', provider['id'])}: {type(exc).__name__}"
            finally:
                if model is not None:
                    try:
                        await model.close()
                    except Exception:
                        logger.debug("Could not close temporary provider preflight model")

        results = await asyncio.gather(*(probe(route) for route in self._provider_pool))
        healthy = [route for route, error in results if error is None]
        failures = [error for _, error in results if error is not None]
        if not healthy:
            raise RuntimeError(
                "No provider in the shared AI Router passed its startup connection check. "
                + "; ".join(str(item) for item in failures)
            )
        if failures:
            logger.warning(
                "AI Router temporarily excluded %d unhealthy provider route(s): %s",
                len(failures),
                "; ".join(str(item) for item in failures),
            )
        self._provider_pool = healthy

    @staticmethod
    def _build_mcp_requests(
        configs: list[dict[str, Any]] | None,
        *,
        only: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[McpConnectionRequest]:
        validated: list[McpConnectionConfig] = []
        seen: set[str] = set()
        for raw in configs or []:
            config = McpConnectionConfig.model_validate(raw)
            if config.name in seen:
                raise ValueError(f"Duplicate MCP connection name: {config.name}")
            seen.add(config.name)
            validated.append(config)
        only_set = {item.strip() for item in (only or []) if item.strip()}
        exclude_set = {item.strip() for item in (exclude or []) if item.strip()}
        selected = [
            item for item in validated
            if (not only_set or item.name in only_set) and item.name not in exclude_set
        ]
        unknown = (only_set | exclude_set) - seen
        if unknown:
            raise ValueError("Unknown MCP connection name(s): " + ", ".join(sorted(unknown)))
        return [McpConnectionRequest(config=item) for item in selected]

    @staticmethod
    def _args(
        *,
        targets: list[str],
        instruction: str,
        scan_mode: str,
        scope_mode: str,
        diff_base: str | None,
        max_budget_usd: float | None,
        max_turns: int,
        workspace_files: list[str] | None = None,
        resume_name: str | None = None,
    ) -> argparse.Namespace:
        args = argparse.Namespace(
            target=targets,
            target_list=[],
            instruction=instruction or None,
            instruction_file=None,
            workspace_file=workspace_files or [],
            workspace_files=[],
            non_interactive=False,
            fail_on=None,
            scan_mode=scan_mode,
            scope_mode=scope_mode,
            diff_base=diff_base,
            config=None,
            mcp_config=None,
            mcp_server=None,
            mcp_exclude=None,
            max_budget_usd=max_budget_usd,
            max_turns=max_turns,
            resume=resume_name,
            targets_info=[],
            local_sources=[],
            diff_scope={"active": False},
            run_name=resume_name,
            needs_setup=False,
            resume_picker=False,
            user_explicit_instruction=None,
            user_instruction=instruction or None,
            workspace_mount=None,
            workspace_subdir=None,
        )
        args.workspace_files = resolve_workspace_files(args.workspace_file)
        args.user_explicit_instruction = instruction or None if resume_name else None
        return args

    async def start(
        self,
        *,
        targets: list[str],
        instruction: str,
        provider: dict[str, Any],
        model: str,
        fallback_provider: dict[str, Any] | None = None,
        fallback_model: str | None = None,
        provider_pool: list[dict[str, Any]] | None = None,
        scan_mode: str = "deep",
        scope_mode: str = "auto",
        diff_base: str | None = None,
        max_budget_usd: float | None = None,
        max_turns: int = 500,
        workspace_files: list[str] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        mcp_only: list[str] | None = None,
        mcp_exclude: list[str] | None = None,
    ) -> str:
        if self.running:
            raise RuntimeError("A Strix scan is already running")
        if not targets or not any(target.strip() for target in targets):
            raise ValueError("At least one target is required")
        if scan_mode not in {"quick", "standard", "deep"}:
            raise ValueError("Unsupported Strix scan mode")
        if scope_mode not in {"auto", "diff", "full"}:
            raise ValueError("Unsupported Strix scope mode")
        self._configure_model(provider, model)
        if provider_pool is None:
            self._configure_failover(provider, fallback_provider, fallback_model)
        else:
            self._configure_provider_pool(provider, model, provider_pool)
        settings = load_settings()
        if self._provider_pool is None:
            await preflight_model_connection((settings.llm.model or "").strip(), settings=settings)
        else:
            await self._preflight_provider_pool()
        args = self._args(
            targets=targets,
            instruction=instruction,
            scan_mode=scan_mode,
            scope_mode=scope_mode,
            diff_base=diff_base,
            max_budget_usd=max_budget_usd,
            max_turns=max_turns,
            workspace_files=workspace_files,
        )
        self._mcp_requests = self._build_mcp_requests(mcp_servers, only=mcp_only, exclude=mcp_exclude)
        self._workspace_files = args.workspace_files
        self._scan_mode = scan_mode
        self._scope_mode = scope_mode
        self._diff_base = diff_base
        build_targets_info(args)
        prepare_run(args)
        telemetry_start(args)
        await self._start_runtime(args)
        assert self.run_name is not None
        return self.run_name

    async def resume(
        self,
        *,
        run_name: str,
        provider: dict[str, Any],
        model: str,
        fallback_provider: dict[str, Any] | None = None,
        fallback_model: str | None = None,
        provider_pool: list[dict[str, Any]] | None = None,
        max_budget_usd: float | None = None,
        max_turns: int = 500,
        mcp_servers: list[dict[str, Any]] | None = None,
        mcp_only: list[str] | None = None,
        mcp_exclude: list[str] | None = None,
    ) -> str:
        if self.running:
            raise RuntimeError("A Strix scan is already running")
        self._configure_model(provider, model)
        if provider_pool is None:
            self._configure_failover(provider, fallback_provider, fallback_model)
        else:
            self._configure_provider_pool(provider, model, provider_pool)
        settings = load_settings()
        if self._provider_pool is None:
            await preflight_model_connection((settings.llm.model or "").strip(), settings=settings)
        else:
            await self._preflight_provider_pool()
        args = self._args(
            targets=[],
            instruction="",
            scan_mode="deep",
            scope_mode="auto",
            diff_base=None,
            max_budget_usd=max_budget_usd,
            max_turns=max_turns,
            resume_name=run_name,
        )
        from strix.interface.cli_args import load_resume_state

        load_resume_state(args)
        self._mcp_requests = self._build_mcp_requests(mcp_servers, only=mcp_only, exclude=mcp_exclude)
        self._workspace_files = args.workspace_files
        self._scan_mode = args.scan_mode
        self._scope_mode = args.scope_mode
        self._diff_base = args.diff_base
        prepare_run(args)
        await self._start_runtime(args)
        return run_name

    async def _start_runtime(self, args: argparse.Namespace) -> None:
        settings = load_settings()
        self.run_name = str(args.run_name)
        self.coordinator = AgentCoordinator()
        self.live_view = TuiLiveView()
        self.report_state = ReportState(self.run_name)
        self.report_state.hydrate_from_run_dir()
        self.scan_config = {
            "scan_id": self.run_name,
            "targets": args.targets_info,
            "user_instructions": args.instruction or "",
            "run_name": self.run_name,
            "diff_scope": args.diff_scope,
            "scan_mode": args.scan_mode,
            "non_interactive": False,
            "local_sources": args.local_sources or [],
            "workspace_files": args.workspace_files or [],
            "scope_mode": args.scope_mode,
            "diff_base": args.diff_base,
            "resume_instruction": args.user_explicit_instruction or "",
            "workspace_mount": args.workspace_mount or "",
            "workspace_subdir": args.workspace_subdir or "",
        }
        self.report_state.set_scan_config(self.scan_config)
        self.report_state.save_run_data()
        set_global_report_state(self.report_state)
        self.live_view.hydrate_from_run_dir(run_dir_for(self.run_name))
        self.controller = TuiController(
            args,
            live_view=self.live_view,
            coordinator=self.coordinator,
        )
        self.controller.set_runtime(
            report_state=self.report_state,
            scan_loop=asyncio.get_running_loop(),
        )
        self.report_state.vulnerability_found_callback = lambda _item: self.controller.notify_changed()
        self.report_state.vulnerability_updated_callback = lambda _item: self.controller.notify_changed()
        self.report_state.vulnerability_deleted_callback = lambda _item: self.controller.notify_changed()
        self.scan_error = None
        async def _run_bounded_scan() -> None:
            logger.info(
                "Applying scan-wide LLM request limit: %d",
                self.max_concurrent_llm_requests,
            )
            async with bounded_llm_requests(
                self.max_concurrent_llm_requests,
                primary_model_ref=self._model_ref,
                fallback_provider=self._fallback_provider,
                fallback_model=self._fallback_model,
                provider_pool=self._provider_pool,
            ):
                await run_strix_scan(
                    scan_config=self.scan_config,
                    scan_id=self.run_name,
                    image=str(settings.runtime.image or "strix-sandbox:latest"),
                    local_sources=args.local_sources or [],
                    extra_files=read_workspace_files(args.workspace_files),
                    coordinator=self.coordinator,
                    interactive=True,
                    max_turns=args.max_turns,
                    max_budget_usd=args.max_budget_usd,
                    event_sink=self._capture_event,
                    mcp_connection_requests=self._mcp_requests,
                    mcp_status_sink=self._capture_mcp_status,
                )

        self.scan_task = asyncio.create_task(
            _run_bounded_scan(),
            name=f"strix:{self.run_name}",
        )
        self._sync_task = asyncio.create_task(self._sync_agents(), name="ares-agent-sync")
        self.scan_task.add_done_callback(self._scan_finished)

    def _capture_event(self, agent_id: str, event: Any) -> None:
        if self.live_view is not None:
            self.live_view.ingest_sdk_event(agent_id, event)
        if self.controller is not None:
            self.controller.notify_changed()

    def _capture_mcp_status(self, roster: list[dict[str, Any]]) -> None:
        if self.controller is not None:
            self.controller.set_mcp_connections(roster)

    async def _sync_agents(self) -> None:
        while self.running and self.coordinator is not None and self.live_view is not None:
            try:
                parent_of, statuses, names, errors = await self.coordinator.graph_snapshot()
                for agent_id, status in statuses.items():
                    self.live_view.upsert_agent(
                        agent_id,
                        name=names.get(agent_id, agent_id),
                        parent_id=parent_of.get(agent_id),
                        status=str(status),
                        error_message=errors.get(agent_id),
                    )
                self.live_view.flush_user_instruction()
                if self.controller is not None:
                    self.controller.notify_changed()
            except Exception:
                logger.exception("Could not synchronize Strix agent state")
            await asyncio.sleep(0.5)

    def _scan_finished(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            self.scan_error = "Scan was interrupted"
        else:
            error = task.exception()
            if error is not None:
                logger.error("Strix scan failed: %s", error)
                self.scan_error = str(error)
        if self._config_path is not None:
            try:
                self._config_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary Strix configuration")
        if self.controller is not None:
            self.controller.notify_changed()

    def snapshot(self) -> dict[str, Any]:
        controller_state = self.controller.snapshot() if self.controller is not None else {}
        agents = self.controller.collection("agents") if self.controller is not None else []
        events = self.controller.collection("events") if self.controller is not None else []
        findings = (
            self.report_state.get_existing_vulnerabilities()
            if self.report_state is not None
            else []
        )
        usage = self.report_state.get_total_llm_usage() if self.report_state is not None else {}
        return {
            **controller_state,
            "run_name": self.run_name,
            "status": controller_state.get("scan_state", "running" if self.running else "unknown"),
            "agents": agents,
            "events": events,
            "findings": findings,
            "usage": usage,
            "error": self.scan_error or controller_state.get("error"),
            "mcp_connections": controller_state.get("connections", []),
            "budget_paused": bool(
                getattr(
                    self.coordinator,
                    "operator_paused",
                    getattr(self.coordinator, "budget_paused", False),
                )
            ) if self.coordinator is not None else False,
        }

    def events_for_agent(self, agent_id: str) -> list[dict[str, Any]]:
        if self.live_view is None:
            return []
        return self.live_view.events_for_agent(agent_id)

    async def send_message(self, agent_id: str, message: str) -> None:
        if self.controller is None:
            raise RuntimeError("No active Strix controller")
        await self.controller.handle("agent.send_message", {"agent_id": agent_id, "message": message})

    async def stop_agent(self, agent_id: str) -> None:
        if self.controller is None:
            raise RuntimeError("No active Strix controller")
        await self.controller.handle("agent.stop", {"agent_id": agent_id})

    async def open_viewer(self) -> dict[str, Any]:
        if self.controller is None:
            raise RuntimeError("No active Strix controller")
        return await self.controller.handle("viewer.open", {})

    async def pause_budget(self) -> None:
        if self.coordinator is None:
            raise RuntimeError("No active Strix coordinator")
        await self.coordinator.pause_budget()

    async def resume_budget(self, max_budget_usd: float | None = None) -> None:
        if self.coordinator is None:
            raise RuntimeError("No active Strix coordinator")
        await self.coordinator.resume_budget(max_budget_usd=max_budget_usd)

    async def stop_scan(self, *, preserve_for_resume: bool = False) -> None:
        if self.coordinator is None or not self.running:
            return
        parents, statuses, _names, errors = await self.coordinator.graph_snapshot()
        original_statuses = dict(statuses)
        original_errors = dict(errors)
        roots = [agent_id for agent_id, parent in parents.items() if parent is None]
        if roots:
            await self.coordinator.cancel_descendants_graceful(roots[0])
        else:
            self.coordinator.mark_shutting_down()
        task = self.scan_task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=20)
            except TimeoutError:
                logger.warning(
                    "Strix did not finish graceful shutdown within 20 seconds; "
                    "cancelling the scan task so its final snapshot can run"
                )
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if preserve_for_resume and self.coordinator is not None:
            # Strix's graceful stop marks every agent stopped. That is correct
            # for a user STOP, but a planned Runner handoff must make agents that
            # were active/waiting eligible for native automatic respawn. Preserve
            # terminal/user-stopped states unchanged.
            async with self.coordinator._lock:
                for agent_id, previous_status in original_statuses.items():
                    if agent_id not in self.coordinator.statuses:
                        continue
                    restored_status = (
                        "running" if str(previous_status) in {"running", "waiting"}
                        else previous_status
                    )
                    self.coordinator._set_status_locked(
                        agent_id,
                        restored_status,
                        error=(
                            None
                            if restored_status == "running"
                            else original_errors.get(agent_id)
                        ),
                    )
            await self.coordinator._maybe_snapshot()
            logger.info(
                "Prepared %d previously active/waiting Strix agent(s) for automatic Resume",
                sum(
                    1 for status in original_statuses.values()
                    if str(status) in {"running", "waiting"}
                ),
            )

    async def close(self, *, preserve_for_resume: bool = False) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        if self.running:
            await self.stop_scan(preserve_for_resume=preserve_for_resume)
        if self.controller is not None:
            self.controller.close_viewer()

    def history(self) -> list[dict[str, Any]]:
        return [
            {
                "run_name": item.run_name,
                "target": item.target,
                "started_at": item.started_at,
                "status": item.status,
                "findings": item.findings,
                "resumable": item.resumable,
            }
            for item in list_run_summaries()
        ]

    def delete_history_item(self, run_name: str) -> None:
        """Delete exactly one completed Strix run directory, never shared state."""
        if not run_name or Path(run_name).name != run_name or run_name in {".", ".."}:
            raise ValueError("Invalid run name")
        if self.running and run_name == self.run_name:
            raise ValueError("The active scan cannot be deleted")

        known_runs = {item["run_name"] for item in self.history()}
        if run_name not in known_runs:
            raise ValueError("Run not found")

        runs_root = run_dir_for("_ares_path_check_").parent.resolve()
        target = run_dir_for(run_name).resolve()
        if target.parent != runs_root or target == runs_root:
            raise ValueError("Run path is outside the Strix history directory")
        if not target.is_dir() or not (target / "run.json").is_file():
            raise ValueError("Run not found")

        shutil.rmtree(target)

    def report_markdown(self, run_name: str | None = None) -> str:
        name = run_name or self.run_name
        if not name:
            return ""
        path = run_dir_for(name) / "penetration_test_report.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""
