from pathlib import Path
import sqlite3

from ares.snapshot import create_snapshot, restore_snapshot


def test_snapshot_round_trip_preserves_every_run_and_report(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    for name in ("run-one", "run-two"):
        run_dir = runs / name
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            '{"run_name": "' + name + '", "status": "completed"}',
            encoding="utf-8",
        )
        (run_dir / "penetration_test_report.md").write_text(
            f"# Private report for {name}",
            encoding="utf-8",
        )
        (run_dir / "vulnerabilities.json").write_text("[]", encoding="utf-8")

    encrypted = tmp_path / "ares-state" / "strix-runs.enc"
    assert create_snapshot(
        runs,
        encrypted,
        bot_token="test-token",
        repository="owner/repo",
        admin_id=123,
    )
    assert b"Private report" not in encrypted.read_bytes()

    restored_root = tmp_path / "restored"
    assert restore_snapshot(
        encrypted,
        restored_root,
        bot_token="test-token",
        repository="owner/repo",
        admin_id=123,
    )
    for name in ("run-one", "run-two"):
        run_dir = restored_root / "strix_runs" / name
        assert (run_dir / "run.json").is_file()
        assert (run_dir / "penetration_test_report.md").read_text(
            encoding="utf-8"
        ) == f"# Private report for {name}"


def test_snapshot_is_created_for_empty_history_on_first_boot(tmp_path: Path) -> None:
    encrypted = tmp_path / "ares-state" / "strix-runs.enc"
    assert create_snapshot(
        tmp_path / "not-created-yet" / "strix_runs",
        encrypted,
        bot_token="test-token",
        repository="owner/repo",
        admin_id=123,
    )

    restored_root = tmp_path / "restored"
    assert restore_snapshot(
        encrypted,
        restored_root,
        bot_token="test-token",
        repository="owner/repo",
        admin_id=123,
    )
    assert (restored_root / "strix_runs").is_dir()



def test_snapshot_uses_a_consistent_sqlite_backup_for_live_agent_sessions(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "strix_runs"
    state_dir = runs / "run-live" / ".state"
    state_dir.mkdir(parents=True)
    database = state_dir / "agents.db"
    live = sqlite3.connect(database)
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("CREATE TABLE messages (content TEXT NOT NULL)")
        live.execute("INSERT INTO messages(content) VALUES ('durable checkpoint')")
        live.commit()

        encrypted = tmp_path / "ares-state" / "strix-runs.enc"
        assert create_snapshot(
            runs,
            encrypted,
            bot_token="test-token",
            repository="owner/repo",
            admin_id=123,
        )

        # This record belongs to the live process after the checkpoint. It must
        # not appear in the already-created snapshot.
        live.execute("INSERT INTO messages(content) VALUES ('after checkpoint')")
        live.commit()
    finally:
        live.close()

    restored_root = tmp_path / "restored"
    assert restore_snapshot(
        encrypted,
        restored_root,
        bot_token="test-token",
        repository="owner/repo",
        admin_id=123,
    )

    restored_db = restored_root / "strix_runs" / "run-live" / ".state" / "agents.db"
    with sqlite3.connect(restored_db) as restored:
        rows = restored.execute("SELECT content FROM messages ORDER BY rowid").fetchall()

    assert rows == [("durable checkpoint",)]
