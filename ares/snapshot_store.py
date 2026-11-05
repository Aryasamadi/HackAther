from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from ares.crypto import decrypt_snapshot


logger = logging.getLogger(__name__)
_RELEASE_TAG = "ares-state-snapshot"
_ASSET_PREFIX = "strix-runs-"
_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024


class SnapshotStore:
    """Durable encrypted Strix snapshots stored as a replaceable GitHub Release asset."""

    def __init__(
        self,
        *,
        bot_token: str,
        admin_id: int,
        repository: str,
        github_token: str,
        branch: str,
    ) -> None:
        if not repository or not github_token:
            raise ValueError("SnapshotStore requires a repository and GitHub token")
        self.bot_token = bot_token
        self.admin_id = admin_id
        self.repository = repository
        self.github_token = github_token
        self.branch = branch
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(180, connect=20),
            follow_redirects=True,
        )
        self._lock = asyncio.Lock()
        self._api_base = "https://api.github.com"

    async def close(self) -> None:
        await self._client.aclose()

    def _url(self, suffix: str) -> str:
        return f"{self._api_base}/repos/{self.repository}/{suffix.lstrip('/')}"

    def _headers(self, *, binary: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": (
                "application/octet-stream"
                if binary
                else "application/vnd.github+json"
            ),
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _validate_payload(self, payload: bytes) -> None:
        try:
            decrypt_snapshot(
                payload,
                bot_token=self.bot_token,
                repository=self.repository,
                admin_id=self.admin_id,
            )
        except Exception as exc:
            raise ValueError(
                "The Strix snapshot failed authenticated decryption; "
                "the previous durable release asset was left untouched."
            ) from exc

    async def _get_release(self) -> dict[str, object] | None:
        response = await self._client.get(
            self._url(f"releases/tags/{quote(_RELEASE_TAG, safe='')}"),
            headers=self._headers(),
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        release = response.json()
        if not isinstance(release, dict):
            raise ValueError("GitHub returned an invalid ARES snapshot release")
        return release

    async def _ensure_release(self) -> dict[str, object]:
        release = await self._get_release()
        if release is not None:
            return release

        response = await self._client.post(
            self._url("releases"),
            headers=self._headers(),
            json={
                "tag_name": _RELEASE_TAG,
                "target_commitish": self.branch,
                "name": "ARES encrypted runtime state",
                "body": (
                    "Automatically managed encrypted Strix history snapshots. "
                    "Do not edit or delete the latest asset while ARES is running."
                ),
                "draft": False,
                "prerelease": True,
            },
        )
        if response.status_code == 422:
            # A concurrent startup may have created the fixed release tag.
            release = await self._get_release()
            if release is not None:
                return release
        response.raise_for_status()
        created = response.json()
        if not isinstance(created, dict):
            raise ValueError("GitHub did not return the created ARES snapshot release")
        return created

    @staticmethod
    def _snapshot_assets(release: dict[str, object]) -> list[dict[str, object]]:
        assets = release.get("assets")
        if not isinstance(assets, list):
            return []
        return [
            asset
            for asset in assets
            if isinstance(asset, dict)
            and str(asset.get("name") or "").startswith(_ASSET_PREFIX)
            and asset.get("state", "uploaded") == "uploaded"
        ]

    async def _read_remote(self) -> bytes | None:
        release = await self._get_release()
        if release is None:
            return None
        assets = self._snapshot_assets(release)
        if not assets:
            return None
        latest = max(
            assets,
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("name") or ""),
            ),
        )
        asset_id = latest.get("id")
        if not isinstance(asset_id, int):
            raise ValueError("The latest ARES snapshot release asset has no valid ID")
        response = await self._client.get(
            self._url(f"releases/assets/{asset_id}"),
            headers=self._headers(binary=True),
        )
        response.raise_for_status()
        payload = response.content
        self._validate_payload(payload)
        return payload

    async def restore(self, destination: Path) -> bool:
        """Restore the latest complete release asset; preserve any fallback if absent."""
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

    async def save(self, source: Path) -> None:
        """Upload a new authenticated snapshot before deleting the previous asset."""
        payload = source.read_bytes()
        if len(payload) > _MAX_SNAPSHOT_BYTES:
            raise ValueError(
                "Encrypted Strix snapshot exceeds the safe release-asset size limit; "
                "the previous durable snapshot was left untouched."
            )
        self._validate_payload(payload)

        async with self._lock:
            release = await self._ensure_release()
            release_id = release.get("id")
            upload_template = release.get("upload_url")
            if not isinstance(release_id, int) or not isinstance(upload_template, str):
                raise ValueError("ARES snapshot release is missing its ID or upload URL")

            upload_url = upload_template.split("{", 1)[0]
            asset_name = f"{_ASSET_PREFIX}{time.time_ns()}.enc"
            upload_response = await self._client.post(
                upload_url,
                params={"name": asset_name},
                headers={
                    **self._headers(),
                    "Content-Type": "application/octet-stream",
                },
                content=payload,
            )
            upload_response.raise_for_status()
            uploaded_asset = upload_response.json()
            uploaded_id = uploaded_asset.get("id") if isinstance(uploaded_asset, dict) else None
            if not isinstance(uploaded_id, int):
                raise RuntimeError("GitHub did not confirm the uploaded ARES snapshot asset")

            # Delete old snapshots only after the new one is confirmed uploaded.
            for old_asset in self._snapshot_assets(release):
                old_id = old_asset.get("id")
                if not isinstance(old_id, int) or old_id == uploaded_id:
                    continue
                try:
                    delete_response = await self._client.delete(
                        self._url(f"releases/assets/{old_id}"),
                        headers=self._headers(),
                    )
                    if delete_response.status_code not in {204, 404}:
                        delete_response.raise_for_status()
                except Exception:
                    logger.warning(
                        "New ARES snapshot is stored, but an older release asset could not be removed"
                    )
            logger.info("Durable encrypted Strix release asset committed")
