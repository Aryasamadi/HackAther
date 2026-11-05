from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_MAGIC = b"ARES1"
_SNAPSHOT_MAGIC = b"ARESS1"


def _key(bot_token: str, repository: str, admin_id: int) -> bytes:
    salt = f"ARES|v1|{repository}|{admin_id}".encode("utf-8")
    return hmac.new(bot_token.encode("utf-8"), salt, hashlib.sha256).digest()


def encrypt_state(
    value: dict[str, Any], *, bot_token: str, repository: str, admin_id: int
) -> bytes:
    nonce = os.urandom(12)
    aad = f"ARES1|{repository}|{admin_id}".encode("utf-8")
    plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(_key(bot_token, repository, admin_id)).encrypt(nonce, plaintext, aad)
    return _MAGIC + nonce + ciphertext


def decrypt_state(
    payload: bytes, *, bot_token: str, repository: str, admin_id: int
) -> dict[str, Any]:
    if not payload.startswith(_MAGIC) or len(payload) < len(_MAGIC) + 12 + 16:
        raise ValueError("Encrypted ARES state has an unknown or invalid format")
    nonce_start = len(_MAGIC)
    nonce = payload[nonce_start : nonce_start + 12]
    ciphertext = payload[nonce_start + 12 :]
    aad = f"ARES1|{repository}|{admin_id}".encode("utf-8")
    plaintext = AESGCM(_key(bot_token, repository, admin_id)).decrypt(nonce, ciphertext, aad)
    value = json.loads(plaintext.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Decrypted ARES state is not a JSON object")
    return value


def encrypt_snapshot(
    payload: bytes, *, bot_token: str, repository: str, admin_id: int
) -> bytes:
    nonce = os.urandom(12)
    aad = f"ARESS1|{repository}|{admin_id}".encode("utf-8")
    ciphertext = AESGCM(_key(bot_token, repository, admin_id)).encrypt(nonce, payload, aad)
    return _SNAPSHOT_MAGIC + nonce + ciphertext


def decrypt_snapshot(
    payload: bytes, *, bot_token: str, repository: str, admin_id: int
) -> bytes:
    if not payload.startswith(_SNAPSHOT_MAGIC) or len(payload) < len(_SNAPSHOT_MAGIC) + 28:
        raise ValueError("Encrypted Strix snapshot has an unknown or invalid format")
    start = len(_SNAPSHOT_MAGIC)
    nonce = payload[start : start + 12]
    ciphertext = payload[start + 12 :]
    aad = f"ARESS1|{repository}|{admin_id}".encode("utf-8")
    return AESGCM(_key(bot_token, repository, admin_id)).decrypt(nonce, ciphertext, aad)
