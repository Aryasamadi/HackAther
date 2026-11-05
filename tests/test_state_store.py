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
