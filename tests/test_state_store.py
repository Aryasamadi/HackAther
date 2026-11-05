import pytest

from ares.state_store import StateStore


@pytest.mark.asyncio
async def test_local_encrypted_state_persistence(tmp_path) -> None:
    path = tmp_path / "state.enc"
    first = StateStore(
        bot_token="test-token",
        admin_id=123,
        repository=None,
        github_token=None,
        branch="main",
        local_path=str(path),
    )
    state = await first.load()
    state["providers"] = {"one": {"name": "private provider", "api_key": "top-secret"}}
    await first.save(state)
    await first.close()

    second = StateStore(
        bot_token="test-token",
        admin_id=123,
        repository=None,
        github_token=None,
        branch="main",
        local_path=str(path),
    )
    loaded = await second.load()
    assert loaded["providers"]["one"]["api_key"] == "top-secret"
    assert b"top-secret" not in path.read_bytes()
    await second.close()


@pytest.mark.asyncio
async def test_github_binary_state_falls_back_to_git_blob(tmp_path) -> None:
    from base64 import b64encode

    import httpx

    from ares.crypto import encrypt_state

    bot_token = "test-token"
    repository = "owner/repo"
    admin_id = 123
    expected = {"providers": {"one": {"api_key": "preserved-secret"}}}
    encrypted = encrypt_state(
        expected,
        bot_token=bot_token,
        repository=repository,
        admin_id=admin_id,
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path.endswith("/contents/.ares/state.enc"):
            return httpx.Response(
                200,
                json={"sha": "state-blob-sha", "encoding": "none", "content": "not base64!"},
            )
        if request.url.path.endswith("/git/blobs/state-blob-sha"):
            return httpx.Response(
                200,
                json={
                    "sha": "state-blob-sha",
                    "encoding": "base64",
                    "content": b64encode(encrypted).decode("ascii"),
                },
            )
        return httpx.Response(404)

    store = StateStore(
        bot_token=bot_token,
        admin_id=admin_id,
        repository=repository,
        github_token="test-github-token",
        branch="main",
        local_path=str(tmp_path / "state.enc"),
    )
    await store._client.aclose()
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        loaded = await store.load()
        assert loaded["providers"]["one"]["api_key"] == "preserved-secret"
        assert store.remote_sha == "state-blob-sha"
        assert any(path.endswith("/git/blobs/state-blob-sha") for path in requested)
    finally:
        await store.close()
