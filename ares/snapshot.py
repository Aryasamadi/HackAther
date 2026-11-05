from __future__ import annotations

import io
import tarfile
from pathlib import Path

from ares.crypto import decrypt_snapshot, encrypt_snapshot


def create_snapshot(
    source_dir: Path,
    destination: Path,
    *,
    bot_token: str,
    repository: str,
    admin_id: int,
) -> bool:
    if not source_dir.is_dir():
        return False
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(source_dir, arcname="strix_runs", recursive=True)
    encrypted = encrypt_snapshot(
        buffer.getvalue(),
        bot_token=bot_token,
        repository=repository,
        admin_id=admin_id,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encrypted)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
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
