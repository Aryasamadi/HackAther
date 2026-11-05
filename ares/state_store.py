from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx

from ares.crypto import decrypt_state, encrypt_state


DEFAULT_STATE: dict[str, Any] = {
    "schema_version": 1,
    "update_offset": 0,
    "providers": {},
    "active_provider_id": None,
    "active_model": None,
    "pending": None,
    "settings": {
        "scan_mode": "deep",
        "max_budget_usd": None,
        "max_turns": 500,
        "scope_mode": "auto",
        "diff_base": None,
    },
    "mcp_servers": [],
    "mcp_only": [],
    "mcp_exclude": [],
    "active_run": None,
}


class StateStore:
    """Encrypted state persistence. GitHub Contents API uses the Actions token."""

    def __init__(
        self,
        *,
        bot_token: str,
        admin_id: int,
        repository: str | None,
        github_token: str | None,
        branch: str,
        local_path: str,
    ) -> None:
        self.bot_token = bot_token
        self.admin_id = admin_id
        self.repository = repository
        self.github_token = github_token
        self.branch = branch
        self.local_path = Path(local_path)
        self.remote_sha: str | None = None
        self._client = httpx.AsyncClient(timeout=25)
        self._api_base = "https://api.github.com"

    async def close(self) -> None:
        await self._client.aclose()

    def _encrypt(self, value: dict[str, Any]) -> bytes:
        return encrypt_state(
            value,
            bot_token=self.bot_token,
            repository=self.repository or "local",
            admin_id=self.admin_id,
        )

    def _decrypt(self, payload: bytes) -> dict[str, Any]:
        return decrypt_state(
            payload,
            bot_token=self.bot_token,
            repository=self.repository or "local",
            admin_id=self.admin_id,
        )

    def _headers(self) -> dict[str, str]:
        if not self.github_token:
            return {}
        return {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _contents_url(self) -> str:
        if not self.repository:
            raise RuntimeError("GITHUB_REPOSITORY is unavailable")
        return f"{self._api_base}/repos/{self.repository}/contents/.ares/state.enc"

    async def load(self) -> dict[str, Any]:
        encrypted: bytes | None = None
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        if self.github_token and self.repository:
            response = await self._client.get(
                self._contents_url(),
                headers=self._headers(),
                params={"ref": self.branch},
            )
            if response.status_code == 200:
                remote = response.json()
                self.remote_sha = remote.get("sha")
                encoded = remote.get("content", "")
                if encoded:
                    encrypted = base64.b64decode(encoded)
                    # Some GitHub Contents API responses for binary blobs may
                    # not preserve the base64 representation consistently.
                    # Retry through the raw media type before treating a
                    # persisted encrypted state as corrupt.
                    if not encrypted.startswith(b"ARES1"):
                        raw_headers = self._headers()
                        raw_headers["Accept"] = "application/vnd.github.raw"
                        raw_response = await self._client.get(
                            self._contents_url(),
                            headers=raw_headers,
                            params={"ref": self.branch},
                        )
                        raw_response.raise_for_status()
                        if raw_response.content.startswith(b"ARES1"):
                            encrypted = raw_response.content
            elif response.status_code != 404:
                response.raise_for_status()
        if encrypted is None and self.local_path.is_file():
            encrypted = self.local_path.read_bytes()
        if encrypted is None:
            return json.loads(json.dumps(DEFAULT_STATE))
        loaded = self._decrypt(encrypted)
        state = json.loads(json.dumps(DEFAULT_STATE))
        state.update(loaded)
        return state

    async def save(self, state: dict[str, Any]) -> None:
        encrypted = self._encrypt(state)
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_bytes(encrypted)
        try:
            self.local_path.chmod(0o600)
        except OSError:
            pass
        if not (self.github_token and self.repository):
            return
        encoded = base64.b64encode(encrypted).decode("ascii")
        body: dict[str, Any] = {
            "message": "chore(ares): update encrypted bot state",
            "content": encoded,
            "branch": self.branch,
        }
        if self.remote_sha:
            body["sha"] = self.remote_sha
        response = await self._client.put(self._contents_url(), headers=self._headers(), json=body)
        if response.status_code == 409:
            await self._refresh_remote_sha()
            if self.remote_sha:
                body["sha"] = self.remote_sha
            response = await self._client.put(self._contents_url(), headers=self._headers(), json=body)
        response.raise_for_status()
        self.remote_sha = response.json().get("content", {}).get("sha", self.remote_sha)

    async def _refresh_remote_sha(self) -> None:
        response = await self._client.get(
            self._contents_url(),
            headers=self._headers(),
            params={"ref": self.branch},
        )
        if response.status_code == 200:
            self.remote_sha = response.json().get("sha")
        elif response.status_code == 404:
            self.remote_sha = None
        else:
            response.raise_for_status()
