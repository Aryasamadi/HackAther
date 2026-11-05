from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ares.config import Config
from ares.providers import discover_models, model_button_label, new_provider, remove_provider
from ares.snapshot import restore_snapshot
from ares.state_store import StateStore
from ares.strix_adapter import StrixAdapter
from strix.tools.mcp import McpConnectionConfig


logger = logging.getLogger(__name__)


def _h(value: object) -> str:
    return html.escape(str(value), quote=False)


def _button(text: str, callback_data: str) -> dict[str, str]:
    return {"text": text, "callback_data": callback_data}


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(35, connect=15))

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        response = await self.client.post(f"{self.base}/{method}", json=payload or {})
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {result.get('description', 'unknown error')}")
        return result.get("result")

    async def download_document(self, file_id: str, destination: Path) -> Path:
        metadata = await self.call("getFile", {"file_id": file_id})
        file_path = metadata.get("file_path") if isinstance(metadata, dict) else None
        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError("Telegram did not return a valid document path")
        file_url = f"{self.base.replace('/bot', '/file/bot')}/{file_path}"
        response = await self.client.get(file_url)
        response.raise_for_status()
        if len(response.content) > 20 * 1024 * 1024:
            raise ValueError("This Telegram Bot API download is larger than 20 MiB")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
        return destination

    async def close(self) -> None:
        await self.client.aclose()


class ARESBot:
    def __init__(self, config: Config, store: StateStore, state: dict[str, Any]) -> None:
        self.config = config
        self.store = store
        self.state = state
        self.telegram = TelegramAPI(config.telegram_token)
        self.adapter = StrixAdapter()
        self.monitor_task: asyncio.Task[None] | None = None
        self.chat_id: int | None = None

    async def close(self) -> None:
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        await self.adapter.close()
        await self.telegram.close()

    async def send(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
        *,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = await self.telegram.call("sendMessage", payload)
        return result if isinstance(result, dict) else {}

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            await self.telegram.call("editMessageText", payload)
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                logger.debug("Dashboard edit skipped: %s", type(exc).__name__)

    async def render_menu(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Edit the current Telegram control panel instead of flooding the chat."""
        message_id = self.state.get("ui_message_id")
        previous_chat = self.state.get("ui_chat_id")
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard or []},
        }
        if isinstance(message_id, int) and previous_chat == chat_id:
            payload["message_id"] = message_id
            try:
                await self.telegram.call("editMessageText", payload)
                return
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    return
                logger.debug("Could not edit ARES menu; sending replacement: %s", type(exc).__name__)
        sent = await self.send(chat_id, text, keyboard)
        if isinstance(sent.get("message_id"), int):
            self.state["ui_message_id"] = int(sent["message_id"])
            self.state["ui_chat_id"] = chat_id

    async def answer_callback(
        self, query_id: str, text: str | None = None, *, show_alert: bool = False
    ) -> None:
        payload: dict[str, Any] = {"callback_query_id": query_id}
        if text:
            payload["text"] = text[:190]
        if show_alert:
            payload["show_alert"] = True
        try:
            await self.telegram.call("answerCallbackQuery", payload)
        except Exception as exc:
            logger.debug("Could not answer callback query: %s", type(exc).__name__)

    async def send_long(self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None:
        chunks = [text[i : i + 3800] for i in range(0, len(text), 3800)] or ["(empty)"]
        for index, chunk in enumerate(chunks):
            await self.send(chat_id, chunk, keyboard if index == len(chunks) - 1 else None)

    def home_keyboard(self) -> list[list[dict[str, str]]]:
        return [
            [_button("🚀 New scan", "scan:new"), _button("📡 Live control", "run:dashboard")],
            [_button("🧠 Models", "models:list"), _button("📚 Scan history", "runs:list")],
            [_button("⚙️ Settings", "settings:show"), _button("🔌 MCP servers", "mcp:list")],
            [_button("ℹ️ Engine info", "engine:info")],
        ]

    async def show_home(self, chat_id: int) -> None:
        provider = self._active_provider()
        model = self.state.get("active_model") or "Not selected"
        provider_label = provider.get("name") if provider else "Not configured"
        await self.render_menu(
            chat_id,
            "<b>ARES Control Center</b>\n"
            "Official Strix runtime · Telegram control panel\n\n"
            f"<b>Provider:</b> {_h(provider_label)}\n"
            f"<b>Model:</b> <code>{_h(model)}</code>\n"
            "<b>Engine:</b> Strix 1.7.0",
            self.home_keyboard(),
        )

    def _active_provider(self) -> dict[str, Any] | None:
        provider_id = self.state.get("active_provider_id")
        providers = self.state.get("providers", {})
        provider = providers.get(provider_id) if isinstance(providers, dict) else None
        return provider if isinstance(provider, dict) else None

    async def _show_mcp_menu(self, chat_id: int) -> None:
        servers = self.state.get("mcp_servers", [])
        rows: list[list[dict[str, str]]] = []
        if isinstance(servers, list):
            for index, item in enumerate(servers):
                if isinstance(item, dict):
                    label = f"{item.get('name', 'unnamed')} · {item.get('transport', 'http')}"
                    rows.append([_button(label[:60], f"mcp:view:{index}")])
        rows.append([_button("➕ Add MCP connection", "mcp:add")])
        rows.append([_button("⬅️ Main menu", "menu:home")])
        statuses = self.adapter.snapshot().get("mcp_connections", [])
        status_text = ", ".join(
            f"{item.get('name')}: {'offline' if item.get('dead') else 'connected'}"
            for item in statuses
        ) or "No active scan connections"
        await self.render_menu(
            chat_id,
            "<b>MCP manager</b>\n"
            f"Saved connections: <b>{len(servers) if isinstance(servers, list) else 0}</b>\n"
            f"Current run: {_h(status_text)}\n\n"
            "Choose a connection to inspect, or add one step by step.",
            rows,
        )

    async def _begin_mcp_add(self, chat_id: int) -> None:
        self.state["pending"] = {"kind": "mcp", "step": "name", "draft": {}}
        await self.render_menu(
            chat_id,
            "<b>Add MCP · 1/5</b>\n"
            "What name should this connection have?\n"
            "<i>Example: github, browser, filesystem</i>",
            [[_button("Cancel", "mcp:cancel")]],
        )

    async def _show_mcp_detail(self, chat_id: int, index: int) -> None:
        servers = self.state.get("mcp_servers", [])
        if not isinstance(servers, list) or not 0 <= index < len(servers):
            await self.render_menu(chat_id, "MCP connection not found.", [[_button("⬅️ MCP list", "mcp:list")]])
            return
        item = servers[index]
        safe = dict(item)
        auth = safe.get("auth")
        if isinstance(auth, dict):
            safe["auth"] = {"kind": auth.get("kind"), "token": "••••••••"}
        if isinstance(safe.get("env"), dict):
            safe["env"] = {key: "••••••••" for key in safe["env"]}
        serialized = json.dumps(safe, ensure_ascii=False, indent=2)
        await self.render_menu(
            chat_id,
            f"<b>{_h(item.get('name', 'MCP server'))}</b>\n<pre>{_h(serialized[:2800])}</pre>",
            [[_button("🗑 Remove connection", f"mcp:delete:{index}")],
             [_button("⬅️ MCP list", "mcp:list")]],
        )

    async def _models_menu(self, chat_id: int) -> None:
        providers = self.state.get("providers", {})
        rows: list[list[dict[str, str]]] = []
        if isinstance(providers, dict):
            for provider_id, item in providers.items():
                name = str(item.get("name", provider_id))
                count = len(item.get("models", [])) if isinstance(item.get("models"), list) else 0
                active = " ✓" if provider_id == self.state.get("active_provider_id") else ""
                rows.append([_button(f"{name}{active} · {count} models"[:60], f"provider:view:{provider_id}")])
        rows.append([_button("➕ Add model provider", "provider:add")])
        rows.append([_button("⬅️ Main menu", "menu:home")])
        await self.render_menu(
            chat_id,
            "<b>Models</b>\n"
            "Select a provider to browse its discovered models.\n"
            "Add a provider using its name, Base URL and token.",
            rows,
        )

    async def _refresh_provider_catalog(self, provider_id: str) -> int:
        providers = self.state.get("providers", {})
        provider = providers.get(provider_id) if isinstance(providers, dict) else None
        if not isinstance(provider, dict):
            raise ValueError("Provider was not found")
        models = await discover_models(str(provider["base_url"]), str(provider["api_key"]))
        provider["models"] = models
        return len(models)

    async def _provider_models(
        self,
        chat_id: int,
        provider_id: str,
        page: int = 0,
        *,
        notice: str | None = None,
    ) -> None:
        provider = self.state.get("providers", {}).get(provider_id)
        if not isinstance(provider, dict):
            await self.render_menu(chat_id, "Provider not found.", [[_button("⬅️ Models", "models:list")]])
            return
        models = provider.get("models", [])
        if not isinstance(models, list):
            models = []
        page_size = 10
        page_count = max(1, (len(models) + page_size - 1) // page_size)
        page = max(0, min(page, page_count - 1))
        start = page * page_size
        visible = models[start : start + page_size]
        rows: list[list[dict[str, str]]] = []
        for index in range(0, len(visible), 2):
            row = []
            for local_index, model in enumerate(visible[index : index + 2], start=index):
                model_index = start + local_index
                label = model_button_label(provider, model)
                row.append(_button(label, f"model:select:{provider_id}:{model_index}"))
            rows.append(row)
        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append(_button("⬅️ Previous", f"model:page:{provider_id}:{page - 1}"))
        if page + 1 < page_count:
            navigation.append(_button("Next ➡️", f"model:page:{provider_id}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([_button("🗑 Remove provider", f"provider:delete:{provider_id}")])
        rows.append([_button("⬅️ Providers", "models:list"), _button("Main menu", "menu:home")])
        active = self.state.get("active_model")
        model_range = f"{start + 1}–{start + len(visible)} of {len(models)}" if models else "No models discovered"
        menu_text = (
            f"<b>{_h(provider.get('name', provider_id))}</b>\n"
            f"Models: <b>{model_range}</b> · Page {page + 1}/{page_count}\n"
            f"Active model: <code>{_h(active or 'none')}</code>\n\n"
            "Choose a model. The catalog refreshes automatically whenever you open this provider."
        )
        if notice:
            menu_text = f"{_h(notice)}\n\n" + menu_text
        await self.render_menu(chat_id, menu_text, rows)

    async def _begin_provider(self, chat_id: int) -> None:
        self.state["pending"] = {"kind": "provider", "step": "name"}
        await self.render_menu(
            chat_id,
            "<b>Add model provider · 1/3</b>\n"
            "Enter a display name for this provider.\n"
            "<i>Example: OpenRouter, Groq, My AI Gateway</i>\n\n"
            "The provider must expose an OpenAI-compatible models endpoint.",
            [[_button("Cancel", "provider:cancel")]],
        )

    async def _consume_pending(self, chat_id: int, text: str) -> bool:
        pending = self.state.get("pending")
        if not isinstance(pending, dict):
            return False
        kind, step, value = pending.get("kind"), pending.get("step"), text.strip()
        if value.lower() in {"/cancel", "cancel"}:
            self.state["pending"] = None
            await self.show_home(chat_id)
            return True

        if kind == "provider":
            if step == "name":
                if not value or len(value) > 64:
                    await self.render_menu(chat_id, "⚠️ Provider name must be between 1 and 64 characters.\nEnter the name.", [[_button("Cancel", "provider:cancel")]])
                    return True
                providers = self.state.get("providers", {})
                if isinstance(providers, dict) and any(
                    isinstance(item, dict) and str(item.get("name", "")).casefold() == value.casefold()
                    for item in providers.values()
                ):
                    await self.render_menu(
                        chat_id,
                        f"⚠️ Provider name <b>{_h(value)}</b> already exists. Choose another name.",
                        [[_button("Cancel", "provider:cancel")]],
                    )
                    return True
                pending["name"] = value
                pending["step"] = "base_url"
                await self.render_menu(
                    chat_id,
                    f"<b>{_h(value)}</b> · Add model provider · 2/3\n"
                    "Send the API Base URL.\n<i>Example: https://api.example.com/v1</i>",
                    [[_button("⬅️ Back", "provider:back:name"), _button("Cancel", "provider:cancel")]],
                )
                return True
            if step == "base_url":
                from ares.providers import validate_base_url
                try:
                    pending["base_url"] = validate_base_url(value)
                except ValueError as exc:
                    await self.render_menu(chat_id, f"⚠️ {_h(str(exc))}\nSend the API Base URL again.", [[_button("Cancel", "provider:cancel")]])
                    return True
                pending["step"] = "token"
                await self.render_menu(
                    chat_id,
                    f"<b>{_h(pending['name'])}</b> · Add model provider · 3/3\n"
                    f"Base URL: <code>{_h(pending['base_url'])}</code>\n"
                    "Send the API token. It will be stored encrypted.",
                    [[_button("⬅️ Edit Base URL", "provider:back:base_url"), _button("Cancel", "provider:cancel")]],
                )
                return True
            if step == "token":
                try:
                    await self.render_menu(chat_id, f"Connecting to <b>{_h(pending['name'])}</b> and discovering models…", [])
                    models = await discover_models(str(pending["base_url"]), value)
                    provider = new_provider(str(pending["name"]), str(pending["base_url"]), value, models)
                    provider_id = str(provider["id"])
                    self.state.setdefault("providers", {})[provider_id] = provider
                    self.state["active_provider_id"] = provider_id
                    self.state["pending"] = None
                    await self.render_menu(
                        chat_id,
                        f"✅ <b>Provider added</b>\nName: <b>{_h(provider['name'])}</b>\nModels discovered: <b>{len(models)}</b>",
                        [[_button("Browse models", f"provider:view:{provider_id}")],
                         [_button("Models list", "models:list")]],
                    )
                except Exception as exc:
                    logger.warning("Provider discovery failed: %s", type(exc).__name__)
                    pending["step"] = "token"
                    await self.render_menu(
                        chat_id,
                        "⚠️ <b>Provider could not be added.</b>\n"
                        f"<code>{_h(str(exc)[:500])}</code>\n\n"
                        "Check the token and Base URL, then send the token again.",
                        [[_button("⬅️ Edit Base URL", "provider:back:base_url")],
                         [_button("Cancel", "provider:cancel")]],
                    )
                return True

        if kind == "mcp":
            draft = pending.setdefault("draft", {})
            if step == "name":
                if not value or len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                    await self.render_menu(chat_id, "⚠️ Name must use 1–64 letters, numbers, dots, underscores or hyphens.", [[_button("Cancel", "mcp:cancel")]])
                    return True
                current = self.state.get("mcp_servers", [])
                if any(isinstance(item, dict) and item.get("name") == value for item in current):
                    await self.render_menu(chat_id, f"⚠️ MCP connection <b>{_h(value)}</b> already exists. Choose another name.", [[_button("Cancel", "mcp:cancel")]])
                    return True
                draft["name"] = value
                pending["step"] = "transport"
                await self.render_menu(
                    chat_id,
                    f"<b>{_h(value)}</b> · Add MCP · 2/5\nChoose how Strix should connect.",
                    [[_button("HTTP / Streamable HTTP", "mcp:transport:http")],
                     [_button("Local STDIO process", "mcp:transport:stdio")],
                     [_button("Cancel", "mcp:cancel")]],
                )
                return True
            if step == "http_url":
                from ares.providers import validate_base_url
                try:
                    draft["url"] = validate_base_url(value)
                except ValueError as exc:
                    await self.render_menu(chat_id, f"⚠️ {_h(str(exc))}\nSend the MCP endpoint URL again.", [[_button("Cancel", "mcp:cancel")]])
                    return True
                pending["step"] = "auth"
                await self.render_menu(
                    chat_id,
                    "<b>MCP authentication</b>\nDoes this server require a Bearer token?",
                    [[_button("No authentication", "mcp:auth:none")],
                     [_button("Bearer token", "mcp:auth:bearer")],
                     [_button("Cancel", "mcp:cancel")]],
                )
                return True
            if step == "bearer_token":
                if not value:
                    await self.render_menu(chat_id, "⚠️ Token cannot be empty. Send the Bearer token.", [[_button("Cancel", "mcp:cancel")]])
                    return True
                draft["auth"] = {"kind": "bearer", "token": value}
                pending["step"] = "allowed_tools"
                await self.render_menu(chat_id, "<b>MCP tool allowlist · optional</b>\nEnter tool names separated by commas, or send <code>-</code> to allow all tools.", [[_button("Allow all tools", "mcp:tools:all"), _button("Cancel", "mcp:cancel")]])
                return True
            if step == "stdio_command":
                if not value:
                    await self.render_menu(chat_id, "⚠️ Command cannot be empty. Enter the executable name or path.", [[_button("Cancel", "mcp:cancel")]])
                    return True
                draft["command"] = value
                pending["step"] = "stdio_args"
                await self.render_menu(chat_id, "<b>STDIO arguments · optional</b>\nEnter one argument per line, or send <code>-</code> to skip.", [[_button("Skip arguments", "mcp:args:skip"), _button("Cancel", "mcp:cancel")]])
                return True
            if step == "stdio_args":
                draft["args"] = [] if value == "-" else [line for line in text.splitlines() if line.strip()]
                pending["step"] = "stdio_env"
                await self.render_menu(chat_id, "<b>STDIO environment · optional</b>\nEnter one KEY=VALUE pair per line, or send <code>-</code> to skip.", [[_button("Skip environment", "mcp:env:skip"), _button("Cancel", "mcp:cancel")]])
                return True
            if step == "stdio_env":
                env: dict[str, str] = {}
                if value != "-":
                    for line in text.splitlines():
                        if "=" not in line:
                            await self.render_menu(chat_id, f"⚠️ Invalid environment entry: <code>{_h(line[:100])}</code>\nUse KEY=VALUE, one per line.", [[_button("Cancel", "mcp:cancel")]])
                            return True
                        key, item_value = line.split("=", 1)
                        key = key.strip()
                        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                            await self.render_menu(chat_id, f"⚠️ Invalid environment variable: <code>{_h(key[:100])}</code>", [[_button("Cancel", "mcp:cancel")]])
                            return True
                        env[key] = item_value
                if env:
                    draft["env"] = env
                pending["step"] = "allowed_tools"
                await self.render_menu(chat_id, "<b>MCP tool allowlist · optional</b>\nEnter tool names separated by commas, or send <code>-</code> to allow all tools.", [[_button("Allow all tools", "mcp:tools:all"), _button("Cancel", "mcp:cancel")]])
                return True
            if step == "allowed_tools":
                if value != "-":
                    tools = [item.strip() for item in value.split(",") if item.strip()]
                    if not tools or any(len(item) > 128 or any(ord(char) < 32 for char in item) for item in tools):
                        await self.render_menu(
                            chat_id,
                            "⚠️ Tool names must be comma-separated identifiers. Send <code>-</code> to allow all tools.",
                            [[_button("Allow all tools", "mcp:tools:all"), _button("Cancel", "mcp:cancel")]],
                        )
                        return True
                    draft["allowed_tools"] = tools
                pending["step"] = "notes"
                await self.render_menu(
                    chat_id,
                    "<b>MCP purpose · optional</b>\nDescribe what this connection is for, or send <code>-</code> to skip.",
                    [[_button("Skip notes", "mcp:notes:skip"), _button("Cancel", "mcp:cancel")]],
                )
                return True
            if step == "notes":
                if value != "-":
                    draft["notes"] = value
                try:
                    config = McpConnectionConfig.model_validate(draft)
                    current = self.state.setdefault("mcp_servers", [])
                    if not isinstance(current, list):
                        raise ValueError("Stored MCP configuration has an invalid format")
                    current.append(config.model_dump(mode="json", exclude_none=True))
                    self.state["pending"] = None
                    await self.render_menu(
                        chat_id,
                        f"✅ <b>MCP connection added</b>\nName: <b>{_h(config.name)}</b>\nTransport: <b>{_h(config.transport)}</b>",
                        [[_button("Open MCP manager", "mcp:list")]],
                    )
                except Exception as exc:
                    logger.warning("MCP connection validation failed: %s", type(exc).__name__)
                    self.state["pending"] = None
                    await self.render_menu(
                        chat_id,
                        f"⚠️ <b>Connection not saved.</b>\n<code>{_h(str(exc)[:500])}</code>",
                        [[_button("Restart MCP setup", "mcp:add")], [_button("MCP list", "mcp:list")]],
                    )
                return True

        if kind == "scan":
            if step == "target":
                targets = [line.strip() for line in value.splitlines() if line.strip()]
                if not targets:
                    await self.render_menu(chat_id, "⚠️ Send at least one target.", [])
                    return True
                pending["targets"] = targets
                pending["step"] = "instruction"
                await self.render_menu(
                    chat_id,
                    "<b>Scan instructions</b>\nSend optional focus instructions, credentials or test context. Send <code>-</code> to use engine defaults.",
                    [[_button("Cancel", "scan:cancel")]],
                )
                return True
            if step == "instruction":
                pending["instruction"] = "" if value == "-" else value
                pending["workspace_files"] = []
                pending["step"] = "attachments"
                await self.render_menu(
                    chat_id,
                    "<b>Optional workspace files</b>\nUpload API specs, source files, notes or wordlists. You can send multiple files, then press Continue.",
                    [[_button("Continue without more files ➡️", "scan:files_done")], [_button("Cancel", "scan:cancel")]],
                )
                return True
            if step == "attachments":
                await self.render_menu(
                    chat_id,
                    f"📎 Files attached: <b>{len(pending.get('workspace_files', []))}</b>\nUpload another document or press Continue.",
                    [[_button("Continue ➡️", "scan:files_done"), _button("Cancel", "scan:cancel")]],
                )
                return True

        if kind == "steer":
            self.state["pending"] = None
            try:
                await self.adapter.send_message(str(pending["agent_id"]), value)
                await self.render_menu(chat_id, "✅ Message delivered to the selected Strix agent.", self._run_keyboard())
            except Exception as exc:
                await self.render_menu(chat_id, f"⚠️ Could not deliver message: <code>{_h(str(exc)[:500])}</code>", self._run_keyboard())
            return True

        if kind == "resume":
            self.state["pending"] = None
            await self._resume_scan(chat_id, value)
            return True

        return False

    async def _launch_scan(
        self,
        chat_id: int,
        targets: list[str],
        instruction: str,
        workspace_files: list[str] | None = None,
    ) -> None:
        provider = self._active_provider()
        model = self.state.get("active_model")
        if provider is None or not isinstance(model, str) or not model:
            await self.render_menu(chat_id, "Select a Provider and model first.", self.home_keyboard())
            return
        try:
            run_name = await self.adapter.start(
                targets=targets,
                instruction=instruction,
                provider=provider,
                model=model,
                scan_mode=str(self.state.get("settings", {}).get("scan_mode", "deep")),
                scope_mode=str(self.state.get("settings", {}).get("scope_mode", "auto")),
                diff_base=self.state.get("settings", {}).get("diff_base"),
                max_budget_usd=self.state.get("settings", {}).get("max_budget_usd"),
                max_turns=int(self.state.get("settings", {}).get("max_turns", 500)),
                workspace_files=workspace_files or [],
                mcp_servers=self.state.get("mcp_servers", []),
                mcp_only=self.state.get("mcp_only", []),
                mcp_exclude=self.state.get("mcp_exclude", []),
            )
            self.state["active_run"] = run_name
            await self.render_menu(
                chat_id,
                f"🚀 <b>Strix scan started</b>\nRun: <code>{_h(run_name)}</code>\n"
                f"Targets: <b>{len(targets)}</b>\nMode: <b>{_h(self.state['settings']['scan_mode'])}</b>",
                self._run_keyboard(),
            )
            message_id = self.state.get("ui_message_id")
            if isinstance(message_id, int):
                self._start_monitor(chat_id, message_id)
        except Exception as exc:
            logger.exception("Could not start Strix scan")
            await self.render_menu(
                chat_id,
                f"⚠️ Scan could not start:\n<code>{_h(str(exc)[:900])}</code>",
                self.home_keyboard(),
            )

    async def _resume_scan(self, chat_id: int, run_name: str) -> None:
        provider = self._active_provider()
        model = self.state.get("active_model")
        if provider is None or not isinstance(model, str) or not model:
            await self.render_menu(chat_id, "Select a Provider and model first.", self.home_keyboard())
            return
        try:
            name = await self.adapter.resume(
                run_name=run_name,
                provider=provider,
                model=model,
                max_budget_usd=self.state.get("settings", {}).get("max_budget_usd"),
                max_turns=int(self.state.get("settings", {}).get("max_turns", 500)),
                mcp_servers=self.state.get("mcp_servers", []),
                mcp_only=self.state.get("mcp_only", []),
                mcp_exclude=self.state.get("mcp_exclude", []),
            )
            self.state["active_run"] = name
            await self.render_menu(
                chat_id,
                f"▶️ Resumed Strix run <code>{_h(name)}</code>.",
                self._run_keyboard(),
            )
            message_id = self.state.get("ui_message_id")
            if isinstance(message_id, int):
                self._start_monitor(chat_id, message_id)
        except Exception as exc:
            await self.render_menu(
                chat_id,
                f"⚠️ Resume failed:\n<code>{_h(str(exc)[:900])}</code>",
                self.home_keyboard(),
            )

    def _run_keyboard(self) -> list[list[dict[str, str]]]:
        return [
            [_button("🤖 Agents", "run:agents"), _button("🔎 Findings", "run:findings")],
            [_button("🧾 Events", "run:events"), _button("🔌 MCP status", "mcp:status")],
            [_button("⏸ Pause budget", "run:pause"), _button("▶️ Resume budget", "run:resume_budget")],
            [_button("🛑 Stop scan", "run:stop"), _button("📄 Report", "run:report")],
            [_button("🌐 Strix Viewer", "run:viewer")],
            [_button("⬅️ Main menu", "menu:home")],
        ]

    def _start_monitor(self, chat_id: int, message_id: int) -> None:
        if self.monitor_task is not None:
            self.monitor_task.cancel()
        self.monitor_task = asyncio.create_task(self._monitor_scan(chat_id, message_id))

    @staticmethod
    def _has_active_agent(snapshot: dict[str, Any]) -> bool:
        agents = snapshot.get("agents", [])
        if not isinstance(agents, list):
            return False
        active_statuses = {
            "active", "running", "working", "thinking", "executing",
            "streaming", "in_progress", "tool_running", "starting", "initializing",
        }
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            status = str(agent.get("status", "")).casefold().rsplit(".", 1)[-1]
            if status in active_statuses:
                return True
        return False

    async def _monitor_scan(self, chat_id: int, message_id: int) -> None:
        last_status = ""
        next_typing_at = 0.0
        while self.adapter.running:
            snapshot = self.adapter.snapshot()
            now = time.monotonic()
            if self._has_active_agent(snapshot) and now >= next_typing_at:
                try:
                    # Telegram's typing action expires after a few seconds, so
                    # refresh it while at least one Strix agent is actively working.
                    await self.telegram.call(
                        "sendChatAction", {"chat_id": chat_id, "action": "typing"}
                    )
                except Exception as exc:
                    logger.debug("Could not refresh Telegram typing indicator: %s", type(exc).__name__)
                next_typing_at = now + 4.0

            agents = snapshot.get("agents", [])
            findings = snapshot.get("findings", [])
            usage = snapshot.get("usage", {})
            total_cost = usage.get("total_cost", usage.get("estimated_cost", "unknown")) if isinstance(usage, dict) else "unknown"
            text = (
                f"<b>Strix · {_h(snapshot.get('run_name'))}</b>\n"
                f"Status: <b>{_h(snapshot.get('status'))}</b>\n"
                f"Agents: <b>{len(agents)}</b> · Findings: <b>{len(findings)}</b>\n"
                f"LLM usage: <code>{_h(total_cost)}</code>"
            )
            if text != last_status:
                await self.edit(chat_id, message_id, text, self._run_keyboard())
                last_status = text
            await asyncio.sleep(4)
        snapshot = self.adapter.snapshot()
        final_text = (
            f"<b>Strix run finished · {_h(snapshot.get('run_name'))}</b>\n"
            f"Status: <b>{_h(snapshot.get('status'))}</b>\n"
            f"Agents: <b>{len(snapshot.get('agents', []))}</b> · "
            f"Findings: <b>{len(snapshot.get('findings', []))}</b>"
        )
        if snapshot.get("error"):
            final_text += f"\nError: <code>{_h(snapshot['error'])}</code>"
        await self.edit(chat_id, message_id, final_text, self._run_keyboard())

    async def _show_agents(self, chat_id: int) -> None:
        agents = self.adapter.snapshot().get("agents", [])
        if not agents:
            await self.render_menu(chat_id, "No agent activity is available yet.", self._run_keyboard())
            return
        rows = []
        for agent in agents[:40]:
            agent_id = str(agent.get("id", ""))
            name = str(agent.get("name", agent_id))
            status = str(agent.get("status", "unknown"))
            rows.append([_button(f"{name[:34]} · {status}"[:60], f"agent:view:{agent_id}")])
        rows.append([_button("⬅️ Run controls", "run:dashboard")])
        await self.render_menu(chat_id, "<b>Strix agents</b>\nChoose an agent to inspect or control it.", rows)

    async def _show_agent(self, chat_id: int, agent_id: str) -> None:
        agent = next(
            (item for item in self.adapter.snapshot().get("agents", []) if str(item.get("id")) == agent_id),
            None,
        )
        if not agent:
            await self.render_menu(chat_id, "Agent not found.", [[_button("⬅️ Agents", "run:agents")]])
            return
        text = (
            f"<b>{_h(agent.get('name', agent_id))}</b>\n"
            f"ID: <code>{_h(agent_id)}</code>\n"
            f"Status: <b>{_h(agent.get('status'))}</b>\n"
            f"Parent: <code>{_h(agent.get('parent_id') or 'root')}</code>"
        )
        await self.render_menu(
            chat_id,
            text,
            [[_button("✉️ Send instruction", f"agent:message:{agent_id}"),
              _button("⏹ Stop agent", f"agent:stop:{agent_id}")],
             [_button("🧾 Agent events", f"agent:events:{agent_id}")],
             [_button("⬅️ Agents", "run:agents")]],
        )

    async def _show_findings(self, chat_id: int) -> None:
        findings = self.adapter.snapshot().get("findings", [])
        if not findings:
            await self.render_menu(chat_id, "No findings have been recorded yet.", self._run_keyboard())
            return
        rows: list[list[dict[str, str]]] = []
        lines = ["<b>Strix findings</b>", f"Total: <b>{len(findings)}</b>"]
        for index, item in enumerate(findings[:20]):
            title = str(item.get("title", "Untitled finding"))
            severity = str(item.get("severity", "unknown")).upper()
            rows.append([_button(f"{severity} · {title}"[:60], f"finding:view:{index}")])
        rows.append([_button("⬅️ Run controls", "run:dashboard")])
        await self.render_menu(chat_id, "\n".join(lines), rows)

    async def _show_events(self, chat_id: int) -> None:
        events = self.adapter.snapshot().get("events", [])
        if not events:
            await self.render_menu(chat_id, "No Strix events have been recorded yet.", self._run_keyboard())
            return
        lines = ["<b>Recent Strix events</b>"]
        for item in events[-12:]:
            agent = str(item.get("agent_id", "agent"))
            payload = item.get("data", {})
            if not isinstance(payload, dict):
                payload = {}
            content = payload.get("content") or payload.get("tool_name") or payload.get("output") or ""
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            lines.append(
                f"• <code>{_h(agent[:12])}</code> · <b>{_h(item.get('type', 'event'))}</b>\n"
                f"{_h(str(content)[:160])}"
            )
        await self.render_menu(chat_id, "\n\n".join(lines), self._run_keyboard())

    async def _show_agent_events(self, chat_id: int, agent_id: str) -> None:
        events = self.adapter.events_for_agent(agent_id)
        if not events:
            await self.render_menu(chat_id, "No transcript/events are available for this agent.", [[_button("⬅️ Agent", f"agent:view:{agent_id}")]])
            return
        lines = [f"<b>Agent events · {_h(agent_id)}</b>"]
        for item in events[-8:]:
            payload = item.get("data", {})
            if not isinstance(payload, dict):
                payload = {}
            content = payload.get("content") or payload.get("tool_name") or payload.get("output") or ""
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            lines.append(f"<b>{_h(item.get('type', 'event'))}</b>\n{_h(str(content)[:250])}")
        await self.render_menu(
            chat_id,
            "\n\n".join(lines),
            [[_button("⬅️ Agent", f"agent:view:{agent_id}")]],
        )

    async def _show_finding(self, chat_id: int, index: int) -> None:
        findings = self.adapter.snapshot().get("findings", [])
        if not 0 <= index < len(findings):
            await self.render_menu(chat_id, "Finding not found.", [[_button("⬅️ Findings", "run:findings")]])
            return
        finding = findings[index]
        fields = [
            ("Severity", "severity"), ("Confidence", "confidence"), ("Target", "target"),
            ("Endpoint", "endpoint"), ("Description", "description"), ("Impact", "impact"),
            ("Technical analysis", "technical_analysis"), ("Evidence", "evidence"),
            ("Remediation", "remediation_steps"),
        ]
        chunks = [f"<b>{_h(finding.get('title', 'Untitled finding'))}</b>"]
        for label, key in fields:
            value = finding.get(key)
            if value:
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, indent=2)
                chunks.append(f"<b>{label}</b>\n{_h(str(value)[:260])}")
        await self.render_menu(
            chat_id,
            "\n\n".join(chunks),
            [[_button("⬅️ Findings", "run:findings")]],
        )

    async def _open_viewer(self, chat_id: int) -> None:
        try:
            result = await self.adapter.open_viewer()
            url = result.get("url")
            if url:
                await self.render_menu(
                    chat_id,
                    "Strix Viewer started on the GitHub Runner. Its URL is private to that worker "
                    "and cannot be opened directly from Telegram without a secure tunnel.\n"
                    f"Runner-local URL: <code>{_h(url)}</code>",
                    self._run_keyboard(),
                )
            else:
                await self.render_menu(
                    chat_id,
                    f"Viewer status: <b>{_h(result.get('status', 'unavailable'))}</b>\n"
                    f"{_h(result.get('error', 'No viewer URL returned'))}",
                    self._run_keyboard(),
                )
        except Exception as exc:
            await self.render_menu(
                chat_id,
                f"⚠️ Viewer could not start: <code>{_h(str(exc)[:500])}</code>",
                self._run_keyboard(),
            )

    async def _show_history(self, chat_id: int) -> None:
        rows = []
        for item in self.adapter.history()[:15]:
            name = str(item["run_name"])
            label = f"{item['status']} · {item['findings']} findings · {item['target']}"
            rows.append([_button(label[:60], f"history:view:{name}")])
        rows.append([_button("⬅️ Main menu", "menu:home")])
        await self.render_menu(chat_id, "<b>Strix scan history</b>\nSelect a run to inspect or resume.", rows)

    async def _show_run_history_item(self, chat_id: int, run_name: str) -> None:
        item = next((x for x in self.adapter.history() if x["run_name"] == run_name), None)
        if not item:
            await self.render_menu(chat_id, "Run not found.", [[_button("⬅️ History", "runs:list")]])
            return
        buttons = []
        if item.get("resumable"):
            buttons.append(_button("▶️ Resume", f"history:resume:{run_name}"))
        buttons.append(_button("📄 Open report", f"history:report:{run_name}"))
        await self.render_menu(
            chat_id,
            f"<b>{_h(run_name)}</b>\nTarget: {_h(item['target'])}\n"
            f"Status: <b>{_h(item['status'])}</b>\nFindings: <b>{item['findings']}</b>",
            [buttons, [_button("⬅️ History", "runs:list")]],
        )

    async def _settings(self, chat_id: int) -> None:
        settings = self.state.get("settings", {})
        mode = str(settings.get("scan_mode", "deep"))
        scope = str(settings.get("scope_mode", "auto"))
        rows = [
            [_button(f"Quick{' ✓' if mode == 'quick' else ''}", "settings:mode:quick"),
             _button(f"Standard{' ✓' if mode == 'standard' else ''}", "settings:mode:standard"),
             _button(f"Deep{' ✓' if mode == 'deep' else ''}", "settings:mode:deep")],
            [_button("Set budget", "settings:budget"), _button("Set max turns", "settings:turns")],
            [_button(f"Scope: {scope}", "settings:scope_menu"), _button("Set diff base", "settings:diff_base")],
            [_button("⬅️ Main menu", "menu:home")],
        ]
        await self.render_menu(
            chat_id,
            f"<b>Scan settings</b>\nMode: <b>{_h(mode)}</b>\n"
            f"Budget: <b>{_h(settings.get('max_budget_usd') or 'unlimited')}</b>\n"
            f"Max turns per agent: <b>{_h(settings.get('max_turns', 500))}</b>\n"
            f"Scope: <b>{_h(scope)}</b> · Diff base: <code>{_h(settings.get('diff_base') or 'default')}</code>",
            rows,
        )

    async def _consume_setting(self, chat_id: int, text: str) -> bool:
        pending = self.state.get("pending")
        if not isinstance(pending, dict) or pending.get("kind") != "setting":
            return False
        try:
            if pending.get("step") == "budget":
                value = float(text)
                if value < 0:
                    raise ValueError("Budget must be zero or positive")
                self.state["settings"]["max_budget_usd"] = value or None
            elif pending.get("step") == "turns":
                value = int(text)
                if value < 1:
                    raise ValueError("Turns must be a positive integer")
                self.state["settings"]["max_turns"] = value
            elif pending.get("step") == "diff_base":
                self.state["settings"]["diff_base"] = None if text.strip() == "-" else text.strip() or None
            self.state["pending"] = None
            await self._settings(chat_id)
        except ValueError as exc:
            await self.render_menu(
                chat_id,
                f"⚠️ {_h(str(exc))}\nPlease enter a valid value.",
                [[_button("Cancel", "settings:show")]],
            )
        return True

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        query_id = str(callback.get("id", ""))
        sender = callback.get("from", {})
        if sender.get("id") != self.config.admin_id:
            await self.answer_callback(query_id, "Not authorized", show_alert=True)
            return
        message = callback.get("message", {})
        chat = message.get("chat", {})
        if chat.get("type") != "private":
            await self.answer_callback(query_id, "Use ARES in a private chat.", show_alert=True)
            return
        chat_id = int(chat.get("id", self.config.admin_id))
        message_id = message.get("message_id")
        if isinstance(message_id, int):
            self.state["ui_message_id"] = message_id
            self.state["ui_chat_id"] = chat_id
        data = str(callback.get("data", ""))
        parts = data.split(":")
        action = parts[0] if parts else ""
        try:
            if data == "menu:home":
                await self.show_home(chat_id)
            elif data == "mcp:list" or data == "mcp:status":
                await self._show_mcp_menu(chat_id)
            elif data == "mcp:add":
                await self._begin_mcp_add(chat_id)
            elif data == "mcp:cancel":
                self.state["pending"] = None
                await self._show_mcp_menu(chat_id)
            elif action == "mcp" and len(parts) == 3 and parts[1] == "view":
                await self._show_mcp_detail(chat_id, int(parts[2]))
            elif action == "mcp" and len(parts) == 3 and parts[1] == "delete":
                index = int(parts[2])
                servers = self.state.get("mcp_servers", [])
                if isinstance(servers, list) and 0 <= index < len(servers):
                    servers.pop(index)
                await self._show_mcp_menu(chat_id)
            elif action == "mcp" and len(parts) == 3 and parts[1] == "transport":
                pending = self.state.get("pending")
                if not isinstance(pending, dict) or pending.get("kind") != "mcp":
                    raise ValueError("MCP setup has expired. Start again.")
                draft = pending.setdefault("draft", {})
                if parts[2] == "http":
                    draft["transport"] = "http"
                    pending["step"] = "http_url"
                    await self.render_menu(chat_id, "<b>MCP · HTTP endpoint</b>\nSend the full Streamable HTTP URL.", [[_button("Cancel", "mcp:cancel")]])
                elif parts[2] == "stdio":
                    draft["transport"] = "stdio"
                    pending["step"] = "stdio_command"
                    await self.render_menu(chat_id, "<b>MCP · STDIO command</b>\nSend the executable name or path that the Runner should launch.", [[_button("Cancel", "mcp:cancel")]])
                else:
                    raise ValueError("Unknown MCP transport")
            elif action == "mcp" and len(parts) == 3 and parts[1] == "auth":
                pending = self.state.get("pending")
                if not isinstance(pending, dict) or pending.get("kind") != "mcp":
                    raise ValueError("MCP setup has expired. Start again.")
                if parts[2] == "none":
                    pending["step"] = "allowed_tools"
                    await self.render_menu(chat_id, "<b>MCP tool allowlist · optional</b>\nEnter tool names separated by commas, or send <code>-</code> to allow all tools.", [[_button("Allow all tools", "mcp:tools:all"), _button("Cancel", "mcp:cancel")]])
                elif parts[2] == "bearer":
                    pending["step"] = "bearer_token"
                    await self.render_menu(chat_id, "<b>MCP Bearer token</b>\nSend the token. The submitted message is deleted after receipt and saved encrypted.", [[_button("Cancel", "mcp:cancel")]])
                else:
                    raise ValueError("Unknown MCP authentication option")
            elif action == "mcp" and len(parts) == 3 and parts[1] == "args" and parts[2] == "skip":
                pending = self.state.get("pending")
                if not isinstance(pending, dict) or pending.get("kind") != "mcp":
                    raise ValueError("MCP setup has expired. Start again.")
                pending.setdefault("draft", {})["args"] = []
                pending["step"] = "stdio_env"
                await self.render_menu(chat_id, "<b>STDIO environment · optional</b>\nSend one KEY=VALUE pair per line, or skip.", [[_button("Skip environment", "mcp:env:skip"), _button("Cancel", "mcp:cancel")]])
            elif action == "mcp" and len(parts) == 3 and parts[1] == "env" and parts[2] == "skip":
                pending = self.state.get("pending")
                if not isinstance(pending, dict) or pending.get("kind") != "mcp":
                    raise ValueError("MCP setup has expired. Start again.")
                pending["step"] = "allowed_tools"
                await self.render_menu(chat_id, "<b>MCP tool allowlist · optional</b>\nEnter tool names separated by commas, or send <code>-</code> to allow all tools.", [[_button("Allow all tools", "mcp:tools:all"), _button("Cancel", "mcp:cancel")]])
            elif action == "mcp" and len(parts) == 3 and parts[1] == "tools" and parts[2] == "all":
                pending = self.state.get("pending")
                if not isinstance(pending, dict) or pending.get("kind") != "mcp" or pending.get("step") != "allowed_tools":
                    raise ValueError("MCP setup has expired. Start again.")
                pending["step"] = "notes"
                await self.render_menu(chat_id, "<b>MCP purpose · optional</b>\nDescribe what this connection is for, or skip.", [[_button("Skip notes", "mcp:notes:skip"), _button("Cancel", "mcp:cancel")]])
            elif action == "mcp" and len(parts) == 3 and parts[1] == "notes" and parts[2] == "skip":
                pending = self.state.get("pending")
                if not isinstance(pending, dict) or pending.get("kind") != "mcp" or pending.get("step") != "notes":
                    raise ValueError("MCP setup has expired. Start again.")
                await self._consume_pending(chat_id, "-")
            elif data == "models:list":
                await self._models_menu(chat_id)
            elif data == "provider:add":
                await self._begin_provider(chat_id)
            elif data == "provider:cancel":
                self.state["pending"] = None
                await self._models_menu(chat_id)
            elif action == "provider" and len(parts) == 3 and parts[1] == "back":
                pending = self.state.get("pending")
                if not isinstance(pending, dict) or pending.get("kind") != "provider":
                    raise ValueError("Provider setup has expired. Start again.")
                if parts[2] == "name":
                    pending["step"] = "name"
                    await self.render_menu(chat_id, "<b>Add model provider · 1/3</b>\nEnter the provider display name.", [[_button("Cancel", "provider:cancel")]])
                elif parts[2] == "base_url":
                    pending["step"] = "base_url"
                    await self.render_menu(chat_id, "<b>Add model provider · 2/3</b>\nSend the API Base URL again.", [[_button("Cancel", "provider:cancel")]])
                else:
                    raise ValueError("Unknown provider setup step")
            elif action == "provider" and len(parts) == 3 and parts[1] == "refresh":
                await self._refresh_provider_catalog(parts[2])
                await self._provider_models(chat_id, parts[2], 0)
            elif action == "provider" and len(parts) == 3 and parts[1] == "view":
                try:
                    await self._refresh_provider_catalog(parts[2])
                except Exception as exc:
                    logger.warning(
                        "Provider model refresh failed for %s: %s",
                        parts[2],
                        type(exc).__name__,
                    )
                    await self._provider_models(
                        chat_id,
                        parts[2],
                        0,
                        notice="⚠️ Could not refresh the catalog; showing saved models.",
                    )
                else:
                    await self._provider_models(chat_id, parts[2], 0)
            elif action == "provider" and len(parts) == 3 and parts[1] == "delete":
                provider_id = parts[2]
                provider = self.state.get("providers", {}).get(provider_id)
                if not isinstance(provider, dict):
                    raise ValueError("Provider was not found")
                await self.render_menu(
                    chat_id,
                    f"⚠️ <b>Remove provider?</b>\n"
                    f"Provider: <b>{_h(provider.get('name', provider_id))}</b>\n"
                    "Its saved API token and model catalog will be removed. "
                    "A scan already running will continue with its loaded configuration.",
                    [[_button("🗑 Confirm removal", f"provider:delete:confirm:{provider_id}")],
                     [_button("Cancel", f"provider:view:{provider_id}")]],
                )
            elif (
                action == "provider" and len(parts) == 4
                and parts[1] == "delete" and parts[2] == "confirm"
            ):
                provider_id = parts[3]
                if not remove_provider(self.state, provider_id):
                    raise ValueError("Provider was already removed or no longer exists")
                await self._models_menu(chat_id)
            elif action == "model" and len(parts) == 4 and parts[1] == "page":
                await self._provider_models(chat_id, parts[2], int(parts[3]))
            elif action == "model" and len(parts) == 4 and parts[1] == "select":
                provider = self.state.get("providers", {}).get(parts[2])
                index = int(parts[3])
                models = provider.get("models", []) if isinstance(provider, dict) else []
                if not isinstance(models, list) or not 0 <= index < len(models):
                    raise ValueError("Model selection is no longer valid")
                self.state["active_provider_id"] = parts[2]
                self.state["active_model"] = str(models[index])
                await self.show_home(chat_id)
            elif data == "scan:new":
                self.state["pending"] = {"kind": "scan", "step": "target"}
                await self.render_menu(
                    chat_id,
                    "<b>New Strix scan</b>\n"
                    "Send one or more targets, one per line. Supported target types include URLs, domains, IPs, Git repositories and runner-accessible local/API-spec paths.\n"
                    "Only test systems you own or are authorized to assess.",
                    [[_button("Cancel", "scan:cancel")]],
                )
            elif data == "scan:cancel":
                self.state["pending"] = None
                await self.show_home(chat_id)
            elif data == "scan:files_done":
                pending = self.state.get("pending")
                if isinstance(pending, dict) and pending.get("kind") == "scan" and pending.get("step") == "attachments":
                    self.state["pending"] = None
                    await self._launch_scan(
                        chat_id,
                        pending.get("targets", []),
                        str(pending.get("instruction", "")),
                        pending.get("workspace_files", []),
                    )
                else:
                    raise ValueError("There is no scan setup waiting for files")
            elif data == "run:dashboard":
                snap = self.adapter.snapshot()
                await self.render_menu(
                    chat_id,
                    f"<b>Strix live control</b>\nRun: <code>{_h(snap.get('run_name') or 'none')}</code>\n"
                    f"Status: <b>{_h(snap.get('status'))}</b>",
                    self._run_keyboard(),
                )
            elif data == "run:agents":
                await self._show_agents(chat_id)
            elif data == "run:findings":
                await self._show_findings(chat_id)
            elif data == "run:events":
                await self._show_events(chat_id)
            elif data == "run:viewer":
                await self._open_viewer(chat_id)
            elif action == "agent" and len(parts) == 3 and parts[1] == "events":
                await self._show_agent_events(chat_id, parts[2])
            elif action == "finding" and len(parts) == 3 and parts[1] == "view":
                await self._show_finding(chat_id, int(parts[2]))
            elif data == "run:pause":
                await self.adapter.pause_budget()
                await self.render_menu(chat_id, "⏸ Budget pause requested.", self._run_keyboard())
            elif data == "run:resume_budget":
                await self.adapter.resume_budget()
                await self.render_menu(chat_id, "▶️ Budget pause released.", self._run_keyboard())
            elif data == "run:stop":
                await self.adapter.stop_scan()
                await self.show_home(chat_id)
            elif data == "run:report":
                await self.send_long(
                    chat_id,
                    html.escape(self.adapter.report_markdown() or "The report is not available yet."),
                    self._run_keyboard(),
                )
            elif data == "runs:list":
                await self._show_history(chat_id)
            elif action == "history" and len(parts) == 3 and parts[1] == "view":
                await self._show_run_history_item(chat_id, parts[2])
            elif action == "history" and len(parts) == 3 and parts[1] == "resume":
                await self._resume_scan(chat_id, parts[2])
            elif action == "history" and len(parts) == 3 and parts[1] == "report":
                from strix.core.paths import run_dir_for
                path = run_dir_for(parts[2]) / "penetration_test_report.md"
                try:
                    report = path.read_text(encoding="utf-8")
                except OSError:
                    report = "Report not found."
                await self.send_long(chat_id, html.escape(report or "Report is empty."))
            elif action == "agent" and len(parts) == 3 and parts[1] == "view":
                await self._show_agent(chat_id, parts[2])
            elif action == "agent" and len(parts) == 3 and parts[1] == "message":
                self.state["pending"] = {"kind": "steer", "agent_id": parts[2]}
                await self.render_menu(chat_id, "Send the instruction to deliver to this agent. Use /cancel to abort.", [[_button("Cancel", "scan:cancel")]])
            elif action == "agent" and len(parts) == 3 and parts[1] == "stop":
                await self.adapter.stop_agent(parts[2])
                await self.render_menu(chat_id, "Agent stop request processed.", self._run_keyboard())
            elif action == "settings" and len(parts) == 3 and parts[1] == "mode":
                if parts[2] in {"quick", "standard", "deep"}:
                    self.state["settings"]["scan_mode"] = parts[2]
                    await self._settings(chat_id)
            elif data == "settings:show":
                await self._settings(chat_id)
            elif data == "settings:scope_menu":
                scope = str(self.state.get("settings", {}).get("scope_mode", "auto"))
                await self.render_menu(
                    chat_id,
                    "<b>Scope mode</b>\nAuto: Strix decides whether diff scope applies.\n"
                    "Diff: focus on changed files when a Git base is available.\n"
                    "Full: assess the full code target.",
                    [[_button(f"Auto{' ✓' if scope == 'auto' else ''}", "settings:scope:auto"),
                      _button(f"Diff{' ✓' if scope == 'diff' else ''}", "settings:scope:diff"),
                      _button(f"Full{' ✓' if scope == 'full' else ''}", "settings:scope:full")],
                     [_button("⬅️ Settings", "settings:show")]],
                )
            elif action == "settings" and len(parts) == 3 and parts[1] == "scope":
                if parts[2] not in {"auto", "diff", "full"}:
                    raise ValueError("Unknown scope mode")
                self.state["settings"]["scope_mode"] = parts[2]
                await self._settings(chat_id)
            elif data == "settings:diff_base":
                self.state["pending"] = {"kind": "setting", "step": "diff_base"}
                await self.render_menu(chat_id, "Send the Git branch or commit to compare against, e.g. <code>origin/main</code>. Send <code>-</code> to clear.", [[_button("Cancel", "menu:home")]])
            elif data == "settings:budget":
                self.state["pending"] = {"kind": "setting", "step": "budget"}
                await self.render_menu(chat_id, "Send the maximum LLM budget in USD, or 0 for unlimited.", [[_button("Cancel", "menu:home")]])
            elif data == "settings:turns":
                self.state["pending"] = {"kind": "setting", "step": "turns"}
                await self.render_menu(chat_id, "Send the maximum number of turns per agent.", [[_button("Cancel", "menu:home")]])
            elif data == "engine:info":
                await self.render_menu(
                    chat_id,
                    "<b>Engine</b>\nOfficial Strix Python runtime · pinned to 1.7.0\n"
                    "ARES connects to Strix Runner, AgentCoordinator, TUI Controller, "
                    "event projection and report state. No Strix engine source is copied or patched.",
                    self.home_keyboard(),
                )
            else:
                raise ValueError("Unknown menu action")
        except Exception as exc:
            logger.exception("ARES callback action failed")
            await self.answer_callback(query_id, f"ARES: {str(exc)[:150]}", show_alert=True)
        else:
            await self.answer_callback(query_id)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        sender = message.get("from", {})
        if sender.get("id") != self.config.admin_id:
            return
        chat = message.get("chat", {})
        if chat.get("type") != "private":
            return
        chat_id = int(chat.get("id", self.config.admin_id))
        pending = self.state.get("pending")
        document = message.get("document")
        if (
            isinstance(pending, dict)
            and pending.get("kind") == "scan"
            and pending.get("step") == "attachments"
            and isinstance(document, dict)
        ):
            filename = Path(str(document.get("file_name") or "workspace-file.bin")).name
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[:100] or "workspace-file.bin"
            destination = Path(".ares-runtime") / "uploads" / f"{int(time.time())}-{safe_name}"
            try:
                saved = await self.telegram.download_document(str(document.get("file_id", "")), destination)
                pending.setdefault("workspace_files", []).append(str(saved))
                await self.render_menu(
                    chat_id,
                    f"✅ Added <code>{_h(filename)}</code>. "
                    f"Files attached: <b>{len(pending['workspace_files'])}</b>",
                    [[_button("Continue ➡️", "scan:files_done"), _button("Cancel", "scan:cancel")]],
                )
            except Exception as exc:
                await self.render_menu(chat_id, f"⚠️ Could not add document: <code>{_h(str(exc)[:500])}</code>", [[_button("Continue", "scan:files_done"), _button("Cancel", "scan:cancel")]])
            return
        text = str(message.get("text") or "").strip()
        if not text:
            return
        pending = self.state.get("pending")
        if (
            isinstance(pending, dict)
            and (
                (pending.get("kind") == "provider" and pending.get("step") == "token")
                or (pending.get("kind") == "mcp" and pending.get("step") in {"bearer_token", "stdio_env"})
            )
            and isinstance(message.get("message_id"), int)
        ):
            try:
                await self.telegram.call(
                    "deleteMessage",
                    {"chat_id": chat_id, "message_id": message["message_id"]},
                )
            except httpx.HTTPError:
                logger.warning("Could not delete a submitted provider/MCP token message")
        command = text.split()[0].split("@", 1)[0].lower()
        if command in {"/start", "/menu"}:
            self.state["pending"] = None
            await self.show_home(chat_id)
            return
        if command == "/cancel":
            self.state["pending"] = None
            await self.show_home(chat_id)
            return
        if command == "/models":
            await self._models_menu(chat_id)
            return
        if command == "/mcp":
            await self._show_mcp_menu(chat_id)
            return
        if command == "/status":
            snap = self.adapter.snapshot()
            await self.render_menu(
                chat_id,
                "<b>ARES status</b>\n"
                "Telegram polling: <b>active</b>\n"
                f"Runner window: <b>{self.config.runtime_minutes} minutes</b>\n"
                f"Strix scan: <b>{_h(snap.get('status', 'idle'))}</b>\n"
                f"Run: <code>{_h(snap.get('run_name') or 'none')}</code>",
                self.home_keyboard(),
            )
            return
        if command == "/scan":
            self.state["pending"] = {"kind": "scan", "step": "target"}
            await self.render_menu(chat_id, "<b>New Strix scan</b>\nSend target(s), one per line. Only systems you are authorized to test.", [[_button("Cancel", "scan:cancel")]])
            return
        if command == "/runs":
            await self._show_history(chat_id)
            return
        if command == "/events":
            await self._show_events(chat_id)
            return
        if command == "/resume":
            if len(text.split(maxsplit=1)) > 1:
                await self._resume_scan(chat_id, text.split(maxsplit=1)[1].strip())
            else:
                self.state["pending"] = {"kind": "resume"}
                await self.render_menu(chat_id, "Send the Strix run name to resume.", [[_button("Cancel", "menu:home")]])
            return
        if await self._consume_setting(chat_id, text):
            return
        if await self._consume_pending(chat_id, text):
            return
        await self.render_menu(chat_id, "Use the inline menu or /menu to continue.", self.home_keyboard())

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
        elif "message" in update:
            await self._handle_message(update["message"])

    async def run(self) -> None:
        await self.telegram.call("deleteWebhook", {"drop_pending_updates": False})
        offset = int(self.state.get("update_offset", 0))
        deadline = time.monotonic() + self.config.runtime_minutes * 60
        logger.info("ARES Telegram worker started for %s minutes", self.config.runtime_minutes)
        while time.monotonic() < deadline:
            try:
                updates = await self.telegram.call(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 20,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for update in updates or []:
                    update_id = int(update.get("update_id", 0))
                    try:
                        await self.handle_update(update)
                    except Exception:
                        logger.exception("Telegram update %s failed", update_id)
                    offset = max(offset, update_id + 1)
                    self.state["update_offset"] = offset
                    await self.store.save(self.state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram polling cycle failed: %s", type(exc).__name__)
                await asyncio.sleep(3)
        logger.info("ARES worker reached its planned handoff window")


async def async_main() -> None:
    config = Config.from_env()
    if not config.github_token or not config.github_repository:
        raise RuntimeError(
            "This deployment requires the automatic GitHub Actions GITHUB_TOKEN and GITHUB_REPOSITORY."
        )
    from pathlib import Path

    snapshot_path = Path("ares-state/strix-runs.enc")
    restore_snapshot(
        snapshot_path,
        Path("."),
        bot_token=config.telegram_token,
        repository=config.github_repository,
        admin_id=config.admin_id,
    )
    store = StateStore(
        bot_token=config.telegram_token,
        admin_id=config.admin_id,
        repository=config.github_repository,
        github_token=config.github_token,
        branch=config.github_ref,
        local_path=config.state_path,
    )
    state = await store.load()
    bot = ARESBot(config, store, state)
    try:
        await bot.run()
    finally:
        await bot.close()
        from ares.snapshot import create_snapshot

        create_snapshot(
            Path("strix_runs"),
            snapshot_path,
            bot_token=config.telegram_token,
            repository=config.github_repository,
            admin_id=config.admin_id,
        )
        await store.save(state)
        await store.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(async_main())
