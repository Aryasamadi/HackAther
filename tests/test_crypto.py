import pytest
from cryptography.exceptions import InvalidTag

from ares.crypto import decrypt_state, decrypt_snapshot, encrypt_state, encrypt_snapshot


def test_state_encryption_round_trip() -> None:
    value = {"providers": {"p1": {"api_key": "secret"}}, "active_model": "demo/model"}
    encrypted = encrypt_state(value, bot_token="bot-token", repository="owner/repo", admin_id=42)
    assert b"secret" not in encrypted
    assert decrypt_state(
        encrypted, bot_token="bot-token", repository="owner/repo", admin_id=42
    ) == value


def test_state_is_bound_to_bot_and_repository() -> None:
    encrypted = encrypt_state(
        {"ok": True}, bot_token="bot-token", repository="owner/repo", admin_id=42
    )
    with pytest.raises(InvalidTag):
        decrypt_state(encrypted, bot_token="different-token", repository="owner/repo", admin_id=42)


def test_snapshot_encryption_round_trip() -> None:
    payload = b"sqlite snapshot bytes"
    encrypted = encrypt_snapshot(
        payload, bot_token="bot-token", repository="owner/repo", admin_id=42
    )
    assert payload not in encrypted
    assert decrypt_snapshot(
        encrypted, bot_token="bot-token", repository="owner/repo", admin_id=42
    ) == payload
