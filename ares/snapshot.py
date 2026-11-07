from __future__ import annotations

import io
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

from ares.crypto import decrypt_snapshot, encrypt_snapshot


def _stage_consistent_run_tree(source_dir: Path, staging_root: Path) -> Path:
    """Copy run artifacts and use SQLite's backup API for live agent sessions.

    Copying agents.db as an ordinary file while Strix is writing it can leave a
    snapshot with a mismatched database/WAL pair. The backup API produces a
    transactionally consistent SQLite database; the live WAL/SHM files are not
    copied into the archive.
    """
    sqlite_sidecars = {"agents.db", "agents.db-wal", "agents.db-shm"}

    def _ignore_sqlite_sidecars(_directory: str, names: list[str]) -> list[str]:
        return [name for name in names if name in sqlite_sidecars]

    staged_runs = staging_root / "strix_runs"
    shutil.copytree(
        source_dir,
        staged_runs,
        ignore=_ignore_sqlite_sidecars,
    )

    for source_db in source_dir.rglob("agents.db"):
        if not source_db.is_file():
            continue
        relative = source_db.relative_to(source_dir)
        staged_db = staged_runs / relative
        staged_db.parent.mkdir(parents=True, exist_ok=True)
        try:
            with (
                closing(sqlite3.connect(str(source_db), timeout=30)) as live,
                closing(sqlite3.connect(str(staged_db), timeout=30)) as backup,
            ):
                live.backup(backup, pages=256, sleep=0.1)
                check = backup.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError(
                        f"SQLite integrity check failed for {relative}: {check!r}"
                    )
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Could not create a consistent snapshot of Strix session database {relative}"
            ) from exc

    return staged_runs


def create_snapshot(
    source_dir: Path,
    destination: Path,
    *,
    bot_token: str,
    repository: str,
    admin_id: int,
) -> bool:
    source_dir.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory(
        dir=destination.parent,
        prefix=".ares-snapshot-stage-",
    ) as staging:
        staged_runs = _stage_consistent_run_tree(source_dir, Path(staging))
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            archive.add(staged_runs, arcname="strix_runs", recursive=True)
    encrypted = encrypt_snapshot(
        buffer.getvalue(),
        bot_token=bot_token,
        repository=repository,
        admin_id=admin_id,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(encrypted)
            temporary_path = Path(temporary_file.name)
        try:
            temporary_path.chmod(0o600)
        except OSError:
            pass
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return True


def restore_snapshot(
    source: Path,
    destination_dir: Path,
    *,
    bot_token: str,
    repository: str,
    admin_id: int,
) -> bool:
    if not source.is_file():
        return False
    payload = decrypt_snapshot(
        source.read_bytes(),
        bot_token=bot_token,
        repository=repository,
        admin_id=admin_id,
    )
    destination_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("Snapshot contains an unsafe archive entry")
        archive.extractall(destination_dir, filter="data")
    return True
