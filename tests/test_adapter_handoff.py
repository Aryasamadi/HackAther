from __future__ import annotations

import asyncio

import pytest

from ares.strix_adapter import StrixAdapter


class _HandoffCoordinator:
    def __init__(
        self,
        statuses: dict[str, str],
        parents: dict[str, str | None],
        scan_task: asyncio.Future[None],
    ) -> None:
        self.scan_task = scan_task
        self.statuses = dict(statuses)
        self.parent_of = dict(parents)
        self.errors = {"failed": "provider rejected request"}
        self._lock = asyncio.Lock()
        self.snapshot_written = False

    async def graph_snapshot(self):
        return (
            dict(self.parent_of),
            dict(self.statuses),
            {agent_id: agent_id for agent_id in self.statuses},
            dict(self.errors),
        )

    async def cancel_descendants_graceful(self, root_id: str):
        assert root_id == "root"
        async with self._lock:
            for agent_id in self.statuses:
                self.statuses[agent_id] = "stopped"
        if not self.scan_task.done():
            self.scan_task.set_result(None)
        return list(self.statuses)

    def _set_status_locked(self, agent_id: str, status: str, *, error=None) -> None:
        self.statuses[agent_id] = status
        if error is not None:
            self.errors[agent_id] = error
        elif status == "running":
            self.errors.pop(agent_id, None)

    async def _maybe_snapshot(self) -> None:
        self.snapshot_written = True


@pytest.mark.asyncio
async def test_runner_handoff_restores_active_agents_for_native_resume() -> None:
    loop = asyncio.get_running_loop()
    scan_task: asyncio.Future[None] = loop.create_future()
    coordinator = _HandoffCoordinator(
        {
            "root": "running",
            "child-active": "running",
            "child-waiting": "waiting",
            "child-complete": "completed",
            "child-user-stopped": "stopped",
            "child-failed": "failed",
        },
        {
            "root": None,
            "child-active": "root",
            "child-waiting": "root",
            "child-complete": "root",
            "child-user-stopped": "root",
            "child-failed": "root",
        },
        scan_task,
    )
    adapter = object.__new__(StrixAdapter)
    adapter.coordinator = coordinator
    adapter.scan_task = scan_task

    await adapter.stop_scan(preserve_for_resume=True)

    assert coordinator.statuses == {
        "root": "running",
        "child-active": "running",
        "child-waiting": "running",
        "child-complete": "completed",
        "child-user-stopped": "stopped",
        "child-failed": "failed",
    }
    assert coordinator.errors["failed"] == "provider rejected request"
    assert coordinator.snapshot_written is True


@pytest.mark.asyncio
async def test_manual_stop_does_not_restore_stopped_agents_for_resume() -> None:
    loop = asyncio.get_running_loop()
    scan_task: asyncio.Future[None] = loop.create_future()
    coordinator = _HandoffCoordinator(
        {"root": "running", "child": "running"},
        {"root": None, "child": "root"},
        scan_task,
    )
    adapter = object.__new__(StrixAdapter)
    adapter.coordinator = coordinator
    adapter.scan_task = scan_task

    await adapter.stop_scan()

    assert coordinator.statuses == {"root": "stopped", "child": "stopped"}
    assert coordinator.snapshot_written is False
