from __future__ import annotations

import base64
import json

import httpx
import pytest

from ares.crypto import encrypt_snapshot
from ares.snapshot_store import SnapshotStore


@pytest.mark.asyncio
async def test_snapshot_store_rotates_and_restores_encrypted_release_asset(tmp_path) -> None:
    release: dict[str, object] | None = None
    remote_payload: bytes | None = None
    next_asset_id = 100

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal release, remote_payload, next_asset_id

        if request.method == "GET" and request.url.path.endswith(
            "/releases/tags/ares-state-snapshot"
        ):
            if release is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=release)

        if request.method == "POST" and request.url.path.endswith("/releases"):
            release = {
                "id": 77,
                "upload_url": (
                    "https://uploads.github.com/repos/owner/repo/releases/77/assets"
                    "{?name,label}"
                ),
                "assets": [],
            }
            return httpx.Response(201, json=release)

        if request.method == "POST" and request.url.host == "uploads.github.com":
            assert release is not None
            remote_payload = request.content
            next_asset_id += 1
            asset = {
                "id": next_asset_id,
                "name": request.url.params["name"],
                "state": "uploaded",
                "created_at": f"2026-11-06T00:00:{next_asset_id % 60:02d}Z",
            }
            assets = release["assets"]
            assert isinstance(assets, list)
            assets.append(asset)
            return httpx.Response(201, json=asset)

        if request.method == "GET" and "/releases/assets/" in request.url.path:
            if remote_payload is None:
                return httpx.Response(404)
            return httpx.Response(
                200,
                content=remote_payload,
                headers={"Content-Type": "application/octet-stream"},
            )

        if request.method == "DELETE" and "/releases/assets/" in request.url.path:
            assert release is not None
            asset_id = int(request.url.path.rsplit("/", 1)[-1])
            assets = release["assets"]
            assert isinstance(assets, list)
            release["assets"] = [asset for asset in assets if asset["id"] != asset_id]
            return httpx.Response(204)

        return httpx.Response(404, json={"message": "Unexpected request"})

    store = SnapshotStore(
        bot_token="test-token",
        admin_id=123,
        repository="owner/repo",
        github_token="test-github-token",
        branch="main",
    )
    await store._client.aclose()
    store._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )

    source = tmp_path / "strix-runs.enc"
    restored = tmp_path / "restored.enc"
    first = encrypt_snapshot(
        b"first-strix-history",
        bot_token="test-token",
        repository="owner/repo",
        admin_id=123,
    )
    second = encrypt_snapshot(
        b"second-strix-history",
        bot_token="test-token",
        repository="owner/repo",
        admin_id=123,
    )

    try:
        source.write_bytes(first)
        await store.save(source)
        source.write_bytes(second)
        await store.save(source)

        assert remote_payload == second
        assert release is not None
        assets = release["assets"]
        assert isinstance(assets, list)
        assert len(assets) == 1

        assert await store.restore(restored) is True
        assert restored.read_bytes() == second
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_missing_release_preserves_legacy_artifact(tmp_path) -> None:
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
