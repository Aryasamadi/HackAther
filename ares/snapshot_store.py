from __future__ import annotations

import asyncio
import base64
import binascii
import tempfile
from pathlib import Path

import httpx


_SNAPSHOT_MAGIC = b"ARESS1"
_MAX_SNAPSHOT_BYTES = 90 * 1024 * 1024


class SnapshotStore:
    """Durable encrypted Strix snapshots stored as a GitHub Contents file."""

    def __init__(
        self,
        *,
        bot_token: str,
        admin_id: int,
        repository: str,
        github_token: str,
        branch: str,
        remote_path: str = ".ares/strix-runs.enc",
    ) -> None:
        if not repository or not github_token:
            raise ValueError("SnapshotStore requires a repository and GitHub token")
        self.bot_token = bot_token
        self.admin_id = admin_id
        self.repository = repository
        self.github_token = github_token
        self.branch = branch
        self.remote_path = remote_path
        self.remote_sha: str | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20))
        self._lock = asyncio.Lock()
        self._api_base = "https://api.github.com"

    async def close(self) -> None:
        await self._client.aclose()

    def _url(self) -> str:
        return (
            f"{self._api_base}/repos/{self.repository}/contents/"
            f"{self.remote_path}"
        )

    def _headers(self, *, raw: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": (
                "application/vnd.github.raw"
                if raw
                else "application/vnd.github+json"
            ),
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _decode_base64(value: str) -> bytes:
        try:
            return base64.b64decode("".join(value.split()), validate=True)
        except (ValueError, binascii.Error):
            return b""

    async def _read_remote(self) -> bytes | None:
        response = await self._client.get(
            self._url(),
            headers=self._headers(),
            params={"ref": self.branch},
        )
        if response.status_code == 404:
            self.remote_sha = None
            return None
        response.raise_for_status()
        remote = response.json()
        self.remote_sha = remote.get("sha")
        payload = self._decode_base64(str(remote.get("content") or ""))

        if not payload.startswith(_SNAPSHOT_MAGIC) and self.remote_sha:
            blob_url = (
                f"{self._api_base}/repos/{self.repository}/git/blobs/"
                f"{self.remote_sha}"
            )
            blob_response = await self._client.get(
                blob_url, headers=self._headers()
            )
            blob_response.raise_for_status()
            blob = blob_response.json()
            if blob.get("encoding") == "base64":
                payload = self._decode_base64(str(blob.get("content") or ""))

        if not payload.startswith(_SNAPSHOT_MAGIC):
            raw_response = await self._client.get(
                self._url(),
                headers=self._headers(raw=True),
                params={"ref": self.branch},
            )
            raw_response.raise_for_status()
            payload = raw_response.content

        if not payload.startswith(_SNAPSHOT_MAGIC):
            raise ValueError(
                "The durable Strix snapshot exists but has an invalid encrypted format; "
                "the existing remote file was not overwritten."
            )
        return payload

    async def restore(self, destination: Path) -> bool:
        """Restore the durable snapshot; leave a legacy artifact untouched if absent."""
        async with self._lock:
            payload = await self._read_remote()
            if payload is None:
                return False
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
                    temporary_file.write(payload)
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

    async def _refresh_remote_sha(self) -> None:
        response = await self._client.get(
            self._url(),
            headers=self._headers(),
            params={"ref": self.branch},
        )
        if response.status_code == 404:
            self.remote_sha = None
            return
        response.raise_for_status()
        self.remote_sha = response.json().get("sha")

    async def save(self, source: Path) -> None:
        """Persist a complete authenticated-encryption snapshot atomically."""
        payload = source.read_bytes()
        if not payload.startswith(_SNAPSHOT_MAGIC):
            raise ValueError("Refusing to persist a file that is not an encrypted Strix snapshot")
        if len(payload) > _MAX_SNAPSHOT_BYTES:
            raise ValueError(
                "Encrypted Strix snapshot exceeds the safe GitHub Contents API limit; "
                "the previous durable snapshot was left untouched."
            )

        encoded = base64.b64encode(payload).decode("ascii")
        async with self._lock:
            for attempt in range(3):
                if self.remote_sha is None:
                    await self._refresh_remote_sha()
                body: dict[str, str] = {
                    "message": "chore(ares): checkpoint encrypted Strix history",
                    "content": encoded,
                    "branch": self.branch,
                }
                if self.remote_sha:
                    body["sha"] = self.remote_sha
                response = await self._client.put(
                    self._url(),
                    headers=self._headers(),
                    json=body,
                )
                if response.status_code == 409:
                    await self._refresh_remote_sha()
                    continue
                response.raise_for_status()
                content = response.json().get("content") or {}
                self.remote_sha = content.get("sha") or self.remote_sha
                return
            raise RuntimeError(
                "Could not checkpoint Strix history after three GitHub content conflicts"
            )
