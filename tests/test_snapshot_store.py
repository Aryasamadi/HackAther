from __future__ import annotations

import base64
import json

import httpx
import pytest

from ares.crypto import encrypt_snapshot
from ares.snapshot_store import SnapshotStore


@pytest.mark.asyncio
async def test_snapshot_store_saves_and_restores_encrypted_payload(tmp_path) -> None:
    remote_payload: bytes | None = None
    remote_sha = "snapshot-blob-sha"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal remote_payload
        if request.method == "GET" and request.url.path.endswith("/contents/.ares/strix-runs.enc"):
            if remote_payload is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "sha": remote_sha,
                    "encoding": "base64",
                    "content": base64.b64encode(remote_payload).decode("ascii"),
                },
            )
        if request.method == "PUT" and request.url.path.endswith("/contents/.ares/strix-runs.enc"):
            body = json.loads(request.content)
            remote_payload = base64.b64decode(body["content"])
            return httpx.Response(
                201,
                json={"content": {"sha": remote_sha}},
            )
        return httpx.Response(404, json={"message": "Unexpected request"})

    store = SnapshotStore(
        bot_token="test-token",
        admin_id=123,
        repository="owner/repo",
        github_token="test-github-token",
        branch="main",
    )
    await store._client.aclose()
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    source = tmp_path / "strix-runs.enc"
    expected = encrypt_snapshot(
        b"compressed-strix-history",
        bot_token="test-token",
        repository="owner/repo",
        admin_id=123,
    )
    source.write_bytes(expected)
    restored = tmp_path / "restored.enc"

    try:
        await store.save(source)
        assert remote_payload == expected
        assert await store.restore(restored) is True
        assert restored.read_bytes() == expected
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_missing_durable_snapshot_preserves_legacy_artifact(tmp_path) -> None:
    store = SnapshotStore(
        bot_token="test-token",
        admin_id=123,
        repository="owner/repo",
        github_token="test-github-token",
        branch="main",
    )
    await store._client.aclose()
    store._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={"message": "Not Found"})
        )
    )

    legacy = tmp_path / "legacy-artifact.enc"
    legacy.write_bytes(b"legacy-artifact-data")
    try:
        assert await store.restore(legacy) is False
        assert legacy.read_bytes() == b"legacy-artifact-data"
    finally:
        await store.close()
