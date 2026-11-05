from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from strix.config import apply_config_override, load_settings
from strix.core.agents import AgentCoordinator
from strix.core.paths import run_dir_for
from strix.core.runner import run_strix_scan
from strix.interface.scan_setup import (
    build_targets_info,
    prepare_run,
    preflight_model_connection,
    telemetry_start,
)
from strix.interface.tui.backend.controller import TuiController
from strix.interface.tui.live_view import TuiLiveView
from strix.interface.utils import read_workspace_files
from strix.report.runs import list_run_summaries
from strix.report.state import ReportState, set_global_report_state


logger = logging.getLogger(__name__)


class StrixAdapter:
    """Telegram-facing adapter over Strix's interactive Python runtime.

    It deliberately imports and invokes Strix's own Runner, Coordinator,
    Controller, report state and event projection. It does not fork or replace
    the Strix agent implementation.
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

    @property
    def running(self) -> bool:
        return self.scan_task is not None and not self.scan_task.done()

    def _configure_model(self, provider: dict[str, Any], model: str) -> None:
        provider_id = str(provider["litellm_id"])
        model_ref = model if model.startswith(provider_id + "/") else f"{provider_id}/{model}"
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

    @staticmethod
    def _args(
        *,
        targets: list[str],
        instruction: str,
        scan_mode: str,
        max_budget_usd: float | None,
        max_turns: int,
        resume_name: str | None = None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            target=targets,
            target_list=[],
            instruction=instruction or None,
            instruction_file=None,
            workspace_file=[],
            workspace_files=[],
            non_interactive=False,
            fail_on=None,
            scan_mode=scan_mode,
            scope_mode="auto",
            diff_base=None,
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
            user_explicit_instruction=instruction if resume_name else None,
            user_instruction=instruction or None,
            workspace_mount=None,
            workspace_subdir=None,
        )

    async def start(
        self,
        *,
        targets: list[str],
        instruction: str,
        provider: dict[str, Any],
        model: str,
        scan_mode: str = "deep",
        max_budget_usd: float | None = None,
        max_turns: int = 500,
    ) -> str:
        if self.running:
            raise RuntimeError("A Strix scan is already running")
        if not targets or not any(target.strip() for target in targets):
            raise ValueError("At least one target is required")
        if scan_mode not in {"quick", "standard", "deep"}:
            raise ValueError("Unsupported Strix scan mode")
        self._configure_model(provider, model)
        settings = load_settings()
        model_ref = (settings.llm.model or "").strip()
        await preflight_model_connection(model_ref, settings=settings)
        args = self._args(
            targets=targets,
            instruction=instruction,
            scan_mode=scan_mode,
            max_budget_usd=max_budget_usd,
            max_turns=max_turns,
        )
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
        max_budget_usd: float | None = None,
        max_turns: int = 500,
    ) -> str:
        if self.running:
            raise RuntimeError("A Strix scan is already running")
        self._configure_model(provider, model)
        settings = load_settings()
        await preflight_model_connection((settings.llm.model or "").strip(), settings=settings)
        args = self._args(
            targets=[],
            instruction="",
            scan_mode="deep",
            max_budget_usd=max_budget_usd,
            max_turns=max_turns,
            resume_name=run_name,
        )
        from strix.interface.cli_args import load_resume_state

        load_resume_state(args)
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
        self.scan_task = asyncio.create_task(
            run_strix_scan(
                scan_config=self.scan_config,
                scan_id=self.run_name,
                image=str(settings.runtime.image or "ghcr.io/usestrix/strix-sandbox:latest"),
                local_sources=args.local_sources or [],
                extra_files=read_workspace_files(args.workspace_files),
                coordinator=self.coordinator,
                interactive=True,
                max_turns=args.max_turns,
                max_budget_usd=args.max_budget_usd,
                event_sink=self._capture_event,
                mcp_status_sink=self._capture_mcp_status,
            ),
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
        record = self.report_state.run_record if self.report_state is not None else {}
        agents = list(self.live_view.agents.values()) if self.live_view is not None else []
        findings = (
            self.report_state.get_existing_vulnerabilities()
            if self.report_state is not None
            else []
        )
        usage = self.report_state.get_total_llm_usage() if self.report_state is not None else {}
        return {
            "run_name": self.run_name,
            "status": record.get("status", "running" if self.running else "unknown"),
            "agents": agents,
            "findings": findings,
            "usage": usage,
            "error": self.scan_error,
        }

    async def send_message(self, agent_id: str, message: str) -> None:
        if self.controller is None:
            raise RuntimeError("No active Strix controller")
        await self.controller.handle("agent.send_message", {"agent_id": agent_id, "message": message})

    async def stop_agent(self, agent_id: str) -> None:
        if self.controller is None:
            raise RuntimeError("No active Strix controller")
        await self.controller.handle("agent.stop", {"agent_id": agent_id})

    async def pause_budget(self) -> None:
        if self.coordinator is None:
            raise RuntimeError("No active Strix coordinator")
        await self.coordinator.pause_budget()

    async def resume_budget(self, max_budget_usd: float | None = None) -> None:
        if self.coordinator is None:
            raise RuntimeError("No active Strix coordinator")
        await self.coordinator.resume_budget(max_budget_usd=max_budget_usd)

    async def stop_scan(self) -> None:
        if self.coordinator is None or not self.running:
            return
        parents, _statuses, _names, _errors = await self.coordinator.graph_snapshot()
        roots = [agent_id for agent_id, parent in parents.items() if parent is None]
        if roots:
            await self.coordinator.cancel_descendants_graceful(roots[0])
        task = self.scan_task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=20)
            except TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def close(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        if self.running:
            await self.stop_scan()
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

    def report_markdown(self) -> str:
        if not self.run_name:
            return ""
        path = run_dir_for(self.run_name) / "penetration_test_report.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""
