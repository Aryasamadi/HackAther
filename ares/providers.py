from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx


_PROVIDER_ID = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


def parse_provider_descriptor(raw: str) -> tuple[str, str]:
    """Parse a display name, defaulting to the OpenAI-compatible LiteLLM route."""
    value = raw.strip()
    if not value:
        raise ValueError("Provider name is required")
    if "|" in value:
        display_name, provider_id = (part.strip() for part in value.split("|", 1))
    else:
        display_name, provider_id = value, "openai"
    if not display_name or not _PROVIDER_ID.fullmatch(provider_id):
        raise ValueError(
            "Use a provider name and LiteLLM ID, for example: OpenRouter Personal | openrouter"
        )
    return display_name[:64], provider_id.lower()


def validate_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL must not contain credentials, query parameters or fragments")
    return value


def model_endpoint_candidates(base_url: str) -> list[str]:
    base = validate_base_url(base_url)
    if base.endswith("/v1"):
        return [f"{base}/models", f"{base[:-3]}/models"]
    return [f"{base}/v1/models", f"{base}/models"]


def build_model_reference(provider_id: str, model_id: str) -> str:
    provider = provider_id.lower().strip()
    model = model_id.strip()
    if not _PROVIDER_ID.fullmatch(provider) or not model or model.startswith("/"):
        raise ValueError("Invalid provider or model identifier")
    prefix = f"{provider}/"
    return model if model.startswith(prefix) else f"{prefix}{model}"


def model_button_label(provider: dict[str, object], model: object) -> str:
    """Show the model ID without the provider routing prefix used internally."""
    label = str(model).strip()
    prefixes = (
        str(provider.get("litellm_id", "")).strip(),
        str(provider.get("name", "")).strip(),
    )
    for prefix in prefixes:
        marker = f"{prefix}/" if prefix else ""
        if marker and label.casefold().startswith(marker.casefold()):
            candidate = label[len(marker):]
            if candidate:
                return candidate
    return label


def remove_provider(state: dict[str, object], provider_id: str) -> bool:
    """Remove one provider and clear selection only when it was the active one."""
    providers = state.get("providers")
    if not isinstance(providers, dict) or provider_id not in providers:
        return False
    del providers[provider_id]
    if state.get("active_provider_id") == provider_id:
        state["active_provider_id"] = None
        state["active_model"] = None
    return True


async def discover_models(base_url: str, api_token: str) -> list[str]:
    if not api_token.strip():
        raise ValueError("Provider token is empty")
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for endpoint in model_endpoint_candidates(base_url):
            try:
                response = await client.get(
                    endpoint,
                    headers={"Authorization": f"Bearer {api_token}", "Accept": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
                entries = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(entries, list):
                    raise ValueError("The models endpoint did not return an OpenAI-compatible data list")
                models = sorted(
                    {
                        str(item["id"]).strip()
                        for item in entries
                        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
                    },
                    key=str.casefold,
                )
                if not models:
                    raise ValueError("The provider returned an empty model list")
                return models
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                errors.append(f"{endpoint} ({status or type(exc).__name__})")
    raise RuntimeError(
        "Could not discover models. This provider must expose an OpenAI-compatible "
        "GET /models or GET /v1/models endpoint. Checked: " + "; ".join(errors)
    )


def new_provider(
    raw_provider: str, base_url: str, api_token: str, models: list[str]
) -> dict[str, object]:
    display_name, provider_id = parse_provider_descriptor(raw_provider)
    return {
        "id": uuid.uuid4().hex[:12],
        "name": display_name,
        "litellm_id": provider_id,
        "base_url": validate_base_url(base_url),
        "api_key": api_token,
        "models": models,
        "created_at": datetime.now(UTC).isoformat(),
    }
