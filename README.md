# HackAther · ARES

ARES is a Telegram user interface and orchestration adapter for the official Strix runtime. It does not fork, patch, or reimplement Strix agents.

## Current architecture

- Telegram Bot API: inline controls, admin authorization, model selection and scan operations.
- Provider manager: stores named provider profiles and discovers model IDs through OpenAI-compatible model-list endpoints.
- Strix adapter: invokes the Strix Python runtime, its AgentCoordinator, Go-TUI Controller, live event projection, MCP request contracts and report state.
- State layer: encrypts provider credentials, MCP credentials and Telegram update offsets before persisting them through the GitHub Contents API.
- Runner workflow: keeps the Telegram polling process active on GitHub-hosted runners and preserves encrypted Strix run snapshots between worker sessions.

The Strix package is pinned to version 1.7.0. The adapter depends on Strix internal Python interfaces, so version pinning and CI compatibility tests are intentional.

## Required repository secrets

Only add these two repository Actions secrets:

1. TELEGRAM_BOT_TOKEN
2. ADMIN_ID

The workflow receives GITHUB_TOKEN and GITHUB_REPOSITORY automatically from GitHub Actions. The workflow grants the built-in token contents:write to store encrypted ARES state and manage the encrypted Strix snapshot release asset. No PAT or third user-managed secret is required.

Run the workflow manually from Actions, or use the five-minute schedule on the default branch. Runtime workflows share a non-cancelling concurrency group: a deployment-triggered run waits for the current worker to finish its graceful handoff instead of force-cancelling it. The next worker restores the most recent encrypted Strix snapshot before starting the updated bot. Commits that only update encrypted state are excluded from the push trigger.

## Provider setup

Open Models → Add model provider. The three prompts are exactly:

1. Display name (for example OpenRouter, Groq or My Gateway)
2. Base URL
3. Token

No JSON or LiteLLM provider identifier is requested. ARES discovers models from the provider's OpenAI-compatible /v1/models or /models endpoint and routes the model through Strix's OpenAI-compatible LiteLLM adapter. This onboarding path is for providers exposing that compatible API surface; proprietary APIs with different auth or model-list formats need a dedicated connector and are not assumed to work automatically.

Model pages show ten models at a time in two columns. You can refresh a provider's catalog without re-entering the token. The selected model is passed to Strix using its LiteLLM configuration.

## Scan controls

- Targets: one target per line; multiple targets are supported.
- Instructions: free-form test instructions.
- Workspace files: upload documents in the scan wizard; they are passed through Strix's native workspace-file resolution and extra-file runtime path.
- Scan mode: Quick, Standard or Deep.
- Scope: Auto, Diff or Full, with an optional diff base.
- Budget and max turns: configured in Settings and passed to Strix's runner.
- Live controls: inspect agents, inspect events and per-agent transcript, steer or stop an agent, pause/resume budget, stop the run, inspect findings, and view the report.

## MCP connections

Open MCP servers → Add MCP connection. ARES asks for the connection name, then lets you choose HTTP or STDIO. HTTP setup asks for the endpoint and whether a Bearer token is required; STDIO setup asks for the command, optional arguments and environment variables. Both flows optionally collect a purpose/notes field. No JSON is required. Before saving, ARES validates the collected fields against Strix's native MCP connection schema. Strix remains the MCP client and owns the actual connection lifecycle.

MCP credentials are included in encrypted ARES state. Only Bearer-token messages are deleted from the private Telegram chat after receipt where Telegram permits deletion.

## Strix runtime integration

ARES uses Strix's Python runtime and interface components:

- strix.core.runner.run_strix_scan
- strix.core.agents.AgentCoordinator
- strix.interface.tui.backend.controller.TuiController
- strix.interface.tui.backend.live_view.TuiLiveView
- strix.report.state.ReportState
- strix.tools.mcp.McpConnectionConfig / McpConnectionRequest

This preserves actual agent execution, event streaming, steering, per-agent stop, budget pause/resume, MCP connection management and Strix report artifacts. These interfaces are internal rather than a separately versioned public SDK. The exact Strix version is pinned to reduce compatibility drift.

See docs/FEATURE_PARITY.md for the current capability-by-capability status and known gaps.

## GitHub-hosted runner lifecycle

GitHub-hosted runners are temporary. ARES therefore treats each workflow run as a replaceable worker, not as permanent storage. Telegram update offsets and Provider/MCP records are encrypted and committed to `.ares/state.enc`. Strix run directories are archived with authenticated encryption and checkpointed as a rotating asset in the fixed GitHub pre-release `ares-state-snapshot`. The new asset is uploaded successfully before older snapshot assets are removed, so checkpoints do not create an ever-growing Git commit history.

While a scan is active, ARES writes a durable checkpoint every two minutes, immediately after a scan starts or resumes, after a manual stop, and during graceful worker shutdown. The latest complete checkpoint remains in repository storage independently of the Actions run that produced it. The workflow artifact `ares-strix-state` is retained as a secondary, 30-day compatibility fallback; it is no longer the primary source of truth.

The encrypted lifecycle record distinguishes four intents:
- `running`: a scan was active; after an unexpected worker loss, ARES may attempt automatic recovery.
- `handoff`: the worker is shutting down normally; the next worker may attempt automatic recovery.
- `manual_stop`: the operator explicitly stopped the scan in Telegram; the next worker must not restart it automatically. The saved run remains in Scan history and can be resumed manually when Strix marks it resumable.
- `completed`: the scan finished; the next worker starts normally without resuming it.

On startup, ARES first validates and restores the durable repository snapshot, then falls back to a legacy Actions artifact when needed. If earlier runtime runs exist but neither source can be restored, startup is refused rather than silently opening an empty history. Automatic Resume is attempted only for a recorded `running` or `handoff` run that Strix itself marks resumable; failed recovery is recorded as `recovery_required` to avoid an endless restart loop.

The workflow uses non-cancelling concurrency so a new worker cannot automatically kill the current worker before its shutdown hooks can run. Scheduled handoffs remain subject to GitHub scheduling delays and dropped scheduled events; repository checkpoints protect the latest saved state, but cannot preserve activity that has not yet reached a checkpoint.
## Security notes

- Provider and MCP records are encrypted with AES-GCM. The encryption key is derived from the Telegram bot token, repository identity and admin ID; rotating the bot token without migrating the encrypted state makes old state unreadable.
- The Strix run snapshot is encrypted with AES-GCM and stored as a rotating GitHub Release asset; the Actions artifact is only a secondary compatibility fallback.
- Never print tokens in logs. Use a private Telegram chat for credentials. Telegram bot chats are not end-to-end encrypted.
- Only the configured Telegram user ID is authorized to operate the bot.
- Uploaded Telegram workspace files are saved on the temporary runner and passed to the Strix sandbox as workspace context.
- Use Strix only against assets you own or have explicit authorization to test.

## Local checks

Install:

    python -m pip install -e ".[dev]"

Run:

    ruff check ares tests
    pytest -q

The GitHub Actions CI workflow runs both checks on pushes and pull requests.
