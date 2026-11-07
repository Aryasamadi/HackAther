from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ares.config import Config
from ares.telegram_bot import ARESBot


def _bot_for_recovery(*, status: str, resumable: bool = True) -> ARESBot:
    bot = object.__new__(ARESBot)
    bot.config = SimpleNamespace(admin_id=42)
    bot.state = {
        "active_run": "scan-123",
        "scan_control": {
            "run_name": "scan-123",
            "intent": "running",
            "status": "running",
            "started_at": 100,
        },
    }
    bot.adapter = SimpleNamespace(
        history=lambda: [
            {"run_name": "scan-123", "status": status, "resumable": resumable}
        ]
    )
    bot._save_state = AsyncMock()
    bot.send = AsyncMock()
    bot._resume_scan = AsyncMock()
    return bot


@pytest.mark.parametrize("intent", ["running", "handoff"])
def test_running_or_handoff_intent_is_eligible_for_recovery(intent: str) -> None:
    assert ARESBot._should_auto_resume(
        {"run_name": "scan-123", "intent": intent}
    )


@pytest.mark.parametrize("intent", ["manual_stop", "completed", "recovery_required", None])
def test_manual_stop_and_terminal_intents_are_never_auto_resumed(intent: str | None) -> None:
    assert not ARESBot._should_auto_resume(
        {"run_name": "scan-123", "intent": intent}
    )


@pytest.mark.asyncio
async def test_completed_strix_run_is_not_started_again() -> None:
    bot = _bot_for_recovery(status="completed")

    await bot.recover_interrupted_scan()

    assert bot.state["scan_control"]["intent"] == "completed"
    assert bot.state["scan_control"]["status"] == "already_completed"
    bot._resume_scan.assert_not_awaited()
    bot._save_state.assert_awaited_once()
    bot.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_strix_run_requires_inspection_instead_of_auto_retry() -> None:
    bot = _bot_for_recovery(status="failed")

    await bot.recover_interrupted_scan()

    assert bot.state["scan_control"]["intent"] == "recovery_required"
    assert bot.state["scan_control"]["status"] == "previous_run_failed"
    bot._resume_scan.assert_not_awaited()
    bot._save_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_stopped_resumable_run_is_automatically_resumed() -> None:
    bot = _bot_for_recovery(status="stopped")

    await bot.recover_interrupted_scan()

    bot._resume_scan.assert_awaited_once_with(42, "scan-123")


@pytest.mark.asyncio
async def test_unavailable_history_is_marked_recovery_required() -> None:
    bot = _bot_for_recovery(status="stopped", resumable=False)

    await bot.recover_interrupted_scan()

    assert bot.state["scan_control"]["intent"] == "recovery_required"
    assert bot.state["scan_control"]["status"] == "not_resumable"
    bot._resume_scan.assert_not_awaited()


def test_scan_control_updates_keep_durable_checkpoint_metadata() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {
        "scan_control": {
            "run_name": "scan-123",
            "intent": "running",
            "status": "running",
            "started_at": 100,
            "last_checkpoint_at": 200,
            "checkpoint_sequence": 7,
        }
    }

    bot._set_scan_control("scan-123", "handoff", "stopping_for_runner_handoff")

    assert bot.state["scan_control"]["last_checkpoint_at"] == 200
    assert bot.state["scan_control"]["checkpoint_sequence"] == 7
    assert bot.state["scan_control"]["started_at"] == 100


@pytest.mark.asyncio
async def test_runner_handoff_checkpoints_before_and_after_graceful_stop() -> None:
    order: list[str] = []

    class FakeAdapter:
        running = True
        run_name = "scan-123"

        def snapshot(self) -> dict:
            return {"status": "running"}

        async def close(self, *, preserve_for_resume: bool = False) -> None:
            assert preserve_for_resume is True
            order.append("adapter.close")
            self.running = False

    class FakeTelegram:
        async def close(self) -> None:
            order.append("telegram.close")

    bot = object.__new__(ARESBot)
    bot.state = {
        "active_run": "scan-123",
        "scan_control": {
            "run_name": "scan-123",
            "intent": "running",
            "status": "running",
            "started_at": 100,
        },
    }
    bot.adapter = FakeAdapter()
    bot.telegram = FakeTelegram()
    bot.checkpoint_task = None
    bot.startup_task = None
    bot.monitor_task = None

    save_count = 0

    async def save_state() -> None:
        nonlocal save_count
        save_count += 1
        order.append(f"save.{save_count}")

    checkpoint_count = 0

    async def checkpoint() -> bool:
        nonlocal checkpoint_count
        checkpoint_count += 1
        order.append(f"checkpoint.{checkpoint_count}")
        return True

    bot._save_state = save_state
    bot._checkpoint_snapshot = checkpoint

    await bot.close()

    assert order == [
        "save.1",
        "checkpoint.1",
        "save.2",
        "adapter.close",
        "checkpoint.2",
        "save.3",
        "telegram.close",
    ]
    assert bot.state["scan_control"]["intent"] == "handoff"
    assert bot.state["scan_control"]["status"] == "handoff_stopped"


@pytest.mark.asyncio
async def test_close_never_relabels_manual_stop_as_handoff() -> None:
    class FakeAdapter:
        running = True
        run_name = "scan-123"

        def snapshot(self) -> dict:
            return {"status": "running"}

        async def close(self, *, preserve_for_resume: bool = False) -> None:
            assert preserve_for_resume is False
            self.running = False

    bot = object.__new__(ARESBot)
    bot.state = {
        "active_run": "scan-123",
        "scan_control": {
            "run_name": "scan-123",
            "intent": "manual_stop",
            "status": "stopping",
            "started_at": 100,
        },
    }
    bot.adapter = FakeAdapter()
    bot.telegram = SimpleNamespace(close=AsyncMock())
    bot.checkpoint_task = None
    bot.startup_task = None
    bot.monitor_task = None
    bot._save_state = AsyncMock()
    bot._checkpoint_snapshot = AsyncMock(return_value=True)

    await bot.close()

    assert bot.state["scan_control"]["intent"] == "manual_stop"
    bot._checkpoint_snapshot.assert_awaited_once()
    assert bot._save_state.await_count == 2


def test_runtime_workflow_queues_new_workers_and_has_a_safe_shutdown_window() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/ares-runtime.yml").read_text(
        encoding="utf-8"
    )

    assert "cancel-in-progress: false" in workflow
    assert 'ARES_RUNTIME_MINUTES: "340"' in workflow
    assert 'ARES_CHECKPOINT_INTERVAL_SECONDS: "60"' in workflow
    assert "timeout-minutes: 360" in workflow



def test_runtime_worker_window_is_within_configured_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ADMIN_ID", "42")
    monkeypatch.setenv("ARES_RUNTIME_MINUTES", "340")

    config = Config.from_env()

    assert config.runtime_minutes == 340
    assert config.runtime_minutes < 360
