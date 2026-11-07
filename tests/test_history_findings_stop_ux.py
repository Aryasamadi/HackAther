import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ares.strix_adapter as adapter_module
from ares.strix_adapter import StrixAdapter
from ares.telegram_bot import ARESBot


def _saved_runs(tmp_path: Path, names: list[str], monkeypatch) -> Path:
    root = tmp_path / "strix_runs"
    root.mkdir()
    for name in names:
        run_dir = root / name
        run_dir.mkdir()
        (run_dir / "run.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(adapter_module, "run_dir_for", lambda name: root / name)
    return root


def _adapter(history: list[dict], *, active: str | None = None) -> StrixAdapter:
    adapter = object.__new__(StrixAdapter)
    adapter.scan_task = SimpleNamespace(done=lambda: active is None)
    adapter.run_name = active
    adapter.history = lambda: list(history)
    adapter.report_state = None
    return adapter


def test_delete_all_history_preserves_active_run(tmp_path, monkeypatch) -> None:
    root = _saved_runs(tmp_path, ["old-one", "old-two", "active-run"], monkeypatch)
    history = [{"run_name": name} for name in ["old-one", "old-two", "active-run"]]
    adapter = _adapter(history, active="active-run")

    result = adapter.delete_all_history_items()

    assert result == {"deleted": 2, "skipped_active": 1}
    assert not (root / "old-one").exists()
    assert not (root / "old-two").exists()
    assert (root / "active-run").is_dir()


def test_delete_all_history_does_not_touch_ares_state_or_outside_files(
    tmp_path, monkeypatch
) -> None:
    root = _saved_runs(tmp_path, ["old-one"], monkeypatch)
    state_file = tmp_path / ".ares" / "state.enc"
    state_file.parent.mkdir()
    state_file.write_bytes(b"encrypted state")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    adapter = _adapter([{"run_name": "old-one"}])

    adapter.delete_all_history_items()

    assert not (root / "old-one").exists()
    assert state_file.read_bytes() == b"encrypted state"
    assert outside.read_text(encoding="utf-8") == "keep"


def test_findings_for_saved_run_reads_strix_index(tmp_path, monkeypatch) -> None:
    root = _saved_runs(tmp_path, ["saved-run"], monkeypatch)
    findings = [{"id": "vuln-0001", "title": "Stored issue"}]
    (root / "saved-run" / "vulnerabilities.json").write_text(
        json.dumps(findings), encoding="utf-8"
    )
    adapter = _adapter([{"run_name": "saved-run"}])

    assert adapter.findings_for_run("saved-run") == findings


def test_delete_finding_uses_native_strix_report_state(tmp_path, monkeypatch) -> None:
    _saved_runs(tmp_path, ["saved-run"], monkeypatch)
    calls: list[tuple[str, str]] = []

    class FakeReportState:
        def __init__(self, run_name: str) -> None:
            assert run_name == "saved-run"

        def hydrate_from_run_dir(self) -> None:
            calls.append(("hydrate", "saved-run"))

        def delete_vulnerability_report(self, report_id: str, **kwargs):
            calls.append((report_id, kwargs["delete_reason"]))
            return {"id": report_id, "title": "Stored issue"}

    monkeypatch.setattr(adapter_module, "ReportState", FakeReportState)
    adapter = _adapter([{"run_name": "saved-run"}])

    result = adapter.delete_finding_item("saved-run", "vuln-0001")

    assert result["id"] == "vuln-0001"
    assert calls[0] == ("hydrate", "saved-run")
    assert calls[1][0] == "vuln-0001"
    assert "ARES administrator" in calls[1][1]


def test_delete_finding_refuses_active_scan(tmp_path, monkeypatch) -> None:
    _saved_runs(tmp_path, ["active-run"], monkeypatch)
    adapter = _adapter([{"run_name": "active-run"}], active="active-run")

    with pytest.raises(ValueError, match="while their scan is active"):
        adapter.delete_finding_item("active-run", "vuln-0001")


def test_delete_all_findings_reports_partial_failures() -> None:
    adapter = object.__new__(StrixAdapter)
    adapter.scan_task = None
    adapter.run_name = None
    adapter.findings_for_run = lambda _run: [
        {"id": "vuln-0001"},
        {"id": "vuln-0002"},
        {"id": "vuln-0003"},
    ]

    def delete_one(_run: str, finding_id: str):
        if finding_id == "vuln-0002":
            raise OSError("index is read-only")
        return {"id": finding_id}

    adapter.delete_finding_item = delete_one

    assert adapter.delete_all_findings("saved-run") == {
        "deleted": 2,
        "failed": 1,
    }


def test_findings_list_uses_stable_ids_and_only_offers_delete_for_saved_run() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {}
    bot.adapter = SimpleNamespace(
        run_name="newer-run",
        running=False,
        findings_for_run=lambda _run: [
            {"id": "vuln-0007", "title": "Stored issue", "severity": "high"}
        ],
    )
    bot.send_long = AsyncMock()

    assert asyncio.run(bot._show_findings(42, "saved-run")) is True

    assert bot.state["ui_finding_ids"] == ["vuln-0007"]
    keyboard = bot.send_long.await_args.args[2]
    callbacks = [button["callback_data"] for row in keyboard for button in row]
    assert "finding:view:0" in callbacks
    assert "finding:delete_all" in callbacks
    assert "history:view:saved-run" in callbacks


def test_findings_list_hides_deletion_for_active_scan() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {}
    bot.adapter = SimpleNamespace(
        run_name="active-run",
        running=True,
        findings_for_run=lambda _run: [
            {"id": "vuln-0001", "title": "Live issue", "severity": "medium"}
        ],
    )
    bot.send_long = AsyncMock()

    asyncio.run(bot._show_findings(42, "active-run"))

    keyboard = bot.send_long.await_args.args[2]
    callbacks = [button["callback_data"] for row in keyboard for button in row]
    assert not any("delete" in callback for callback in callbacks)


def test_finding_detail_routes_back_to_its_historical_run() -> None:
    bot = object.__new__(ARESBot)
    bot.state = {
        "ui_findings_run_name": "saved-run",
        "ui_finding_ids": ["vuln-0003"],
    }
    bot.adapter = SimpleNamespace(
        run_name="newer-run",
        running=False,
        findings_for_run=lambda _run: [
            {"id": "vuln-0003", "title": "Historical issue", "severity": "low"}
        ],
    )
    bot.send_long = AsyncMock()

    asyncio.run(bot._show_finding(42, 0))

    keyboard = bot.send_long.await_args.args[2]
    callbacks = [button["callback_data"] for row in keyboard for button in row]
    assert "history:findings:saved-run" in callbacks
    assert "finding:delete:0" in callbacks


@pytest.mark.asyncio
async def test_stop_requires_second_confirmation_before_cancelling() -> None:
    bot = object.__new__(ARESBot)
    bot.config = SimpleNamespace(admin_id=42)
    bot.state = {}
    bot.adapter = SimpleNamespace(running=True, run_name="active-run")
    bot.render_menu = AsyncMock()
    bot.answer_callback = AsyncMock()
    bot._stop_scan_safely = AsyncMock(return_value=[])
    bot.show_home = AsyncMock()

    callback = {
        "id": "query-1",
        "from": {"id": 42},
        "message": {"chat": {"id": 42, "type": "private"}, "message_id": 10},
        "data": "run:stop",
    }
    await bot._handle_callback(callback)

    bot._stop_scan_safely.assert_not_awaited()
    assert bot.state["pending_stop_run_name"] == "active-run"
    prompt = bot.render_menu.await_args.args[1]
    keyboard = bot.render_menu.await_args.args[2]
    assert "Confirm STOP" in prompt
    assert keyboard[0][0]["callback_data"] == "run:stop:confirm"


@pytest.mark.asyncio
async def test_stop_cancel_keeps_scan_running() -> None:
    bot = object.__new__(ARESBot)
    bot.config = SimpleNamespace(admin_id=42)
    bot.state = {"pending_stop_run_name": "active-run"}
    bot.adapter = SimpleNamespace(
        running=True,
        run_name="active-run",
        snapshot=lambda: {"budget_paused": False},
    )
    bot.render_menu = AsyncMock()
    bot.answer_callback = AsyncMock()
    bot._stop_scan_safely = AsyncMock()

    callback = {
        "id": "query-2",
        "from": {"id": 42},
        "message": {"chat": {"id": 42, "type": "private"}, "message_id": 11},
        "data": "run:stop:cancel",
    }
    await bot._handle_callback(callback)

    bot._stop_scan_safely.assert_not_awaited()
    assert "pending_stop_run_name" not in bot.state
    assert "still running" in bot.render_menu.await_args.args[1]


@pytest.mark.asyncio
async def test_stop_confirmation_cancels_only_the_matching_active_run() -> None:
    bot = object.__new__(ARESBot)
    bot.config = SimpleNamespace(admin_id=42)
    bot.state = {"pending_stop_run_name": "active-run"}
    bot.adapter = SimpleNamespace(running=True, run_name="active-run")
    bot.render_menu = AsyncMock()
    bot.answer_callback = AsyncMock()
    bot._stop_scan_safely = AsyncMock(return_value=[])
    bot.show_home = AsyncMock()

    callback = {
        "id": "query-3",
        "from": {"id": 42},
        "message": {"chat": {"id": 42, "type": "private"}, "message_id": 12},
        "data": "run:stop:confirm",
    }
    await bot._handle_callback(callback)

    bot._stop_scan_safely.assert_awaited_once_with("active-run")
    bot.show_home.assert_awaited_once()
    assert "pending_stop_run_name" not in bot.state


@pytest.mark.asyncio
async def test_expired_stop_confirmation_does_not_stop_replacement_run() -> None:
    bot = object.__new__(ARESBot)
    bot.config = SimpleNamespace(admin_id=42)
    bot.state = {"pending_stop_run_name": "old-run"}
    bot.adapter = SimpleNamespace(running=True, run_name="replacement-run")
    bot.render_menu = AsyncMock()
    bot.answer_callback = AsyncMock()
    bot._stop_scan_safely = AsyncMock()

    callback = {
        "id": "query-4",
        "from": {"id": 42},
        "message": {"chat": {"id": 42, "type": "private"}, "message_id": 13},
        "data": "run:stop:confirm",
    }
    await bot._handle_callback(callback)

    bot._stop_scan_safely.assert_not_awaited()
    bot.answer_callback.assert_awaited_once()
    assert bot.answer_callback.await_args.kwargs["show_alert"] is True
