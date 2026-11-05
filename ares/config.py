from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Config:
    telegram_token: str
    admin_id: int
    github_token: str | None
    github_repository: str | None
    github_ref: str
    runtime_minutes: int
    state_path: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        raw_admin = os.environ.get("ADMIN_ID", "").strip()
        if not token:
            raise RuntimeError("Missing required secret: TELEGRAM_BOT_TOKEN")
        if not raw_admin:
            raise RuntimeError("Missing required secret: ADMIN_ID")
        try:
            admin_id = int(raw_admin)
        except ValueError as exc:
            raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID") from exc
        runtime_minutes = int(os.environ.get("ARES_RUNTIME_MINUTES", "340"))
        if not 1 <= runtime_minutes <= 345:
            raise RuntimeError("ARES_RUNTIME_MINUTES must be between 1 and 345")
        repository = os.environ.get("GITHUB_REPOSITORY") or None
        return cls(
            telegram_token=token,
            admin_id=admin_id,
            github_token=os.environ.get("GITHUB_TOKEN") or None,
            github_repository=repository,
            github_ref=os.environ.get("GITHUB_REF_NAME", "main"),
            runtime_minutes=runtime_minutes,
            state_path=os.environ.get("ARES_STATE_PATH", ".ares-state/state.enc"),
        )
