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

Start the worker manually from Actions → ARES Telegram Runtime → Run workflow. Runtime workers share a non-cancelling concurrency group: a new dispatch waits for the current worker to complete its planned handoff instead of force-cancelling an active scan. Each worker runs for 340 minutes inside a 360-minute job limit, leaving time for graceful shutdown and final persistence.

## Provider setup

Open Models → Add model provider. The three prompts are exactly:

1. Display name (for example OpenRouter, Groq or My Gateway)
2. Base URL
3. Token

No JSON or LiteLLM provider identifier is requested. ARES discovers models from the provider's OpenAI-compatible /v1/models or /models endpoint and routes the model through Strix's OpenAI-compatible LiteLLM adapter. This onboarding path is for providers exposing that compatible API surface; proprietary APIs with different auth or model-list formats need a dedicated connector and are not assumed to work automatically.

Model pages show ten models at a time in two columns. You can refresh a provider's catalog without re-entering the token. The selected model is passed to Strix using its LiteLLM configuration.

## Shared AI provider router

ARES supports a shared provider pool rather than a single primary/backup pair. Choose one primary model in Models, then open **Configure shared AI Router** to add one compatible model route per additional provider. Adding or removing providers and changing route settings is done through Telegram; no code change is required.

Each Strix agent request uses the same scan-wide router. The default scheduler is weighted least-in-flight: it prefers a healthy provider with fewer active requests relative to its weight. Each route has a configurable weight (1–10) and per-provider parallel-request cap (1–8); the existing scan-wide `ARES_MAX_CONCURRENT_LLM_REQUESTS` limit remains the global upper bound (default 2, allowed 1–8).

Temporary capacity, network, timeout and server errors trigger an attempt on another healthy route. A failing provider enters an exponential cooldown (60 seconds up to 5 minutes); it is automatically reconsidered after cooldown and returns to normal service after a successful probe. Authentication, invalid-model, context and policy errors are not retried through other providers. Streams can switch routes only before their first event, so partial text or tool calls are never replayed.

All pool routes use the OpenAI-compatible Chat Completions interface and must match the primary model's Strix strict/non-strict tool-schema mode. ARES can check the schema mode and the configured API shape, but OpenAI-compatible model catalogs do not reliably expose context-window size or full tool/function-calling quality. Select models that support Strix tool calls and have enough context for the same scan; compatibility of every tool and context limit cannot be guaranteed from `/models` alone. A provider must expose `GET /models` or `GET /v1/models`.

The former encrypted single-backup settings remain readable and are treated as an additional shared-pool route at scan startup until replaced in Router settings. Existing provider credentials remain encrypted in ARES state.

## Runner handoff and recovery

ARES creates an encrypted durable Strix snapshot every 60 seconds by default (`ARES_CHECKPOINT_INTERVAL_SECONDS`, allowed 30–300 seconds). It also checkpoints before requesting a planned handoff and again after Strix has gracefully stopped its in-memory agent tasks. On Linux, SIGTERM is converted into the same application shutdown path. A new worker restores the latest successfully uploaded snapshot and automatically resumes runs whose saved intent is `running` or `handoff`.

The runtime workflow never cancels an active worker when another dispatch is queued. The configured worker window is 340 minutes with a 360-minute job timeout, so routine handoff has a safety margin. Manual `STOP` and completed runs are terminal and are not auto-resumed. If Strix has recorded a run as failed, ARES leaves it for inspection rather than looping automatic retries.

Recovery continues from the latest durable checkpoint, not from another process's live memory or an in-flight HTTP request. Work performed after the last confirmed checkpoint may need to be replayed; a provider request cannot be transferred byte-for-byte to the next Runner. ARES snapshots live SQLite agent sessions using SQLite's backup API, checks the copied database, and keeps the previous remote snapshot if a new checkpoint cannot be validated or uploaded.

## Scan controls

- Targets: one target per line; multiple targets are supported.
- Instructions: free-form test instructions.
- Workspace files: upload documents in the scan wizard; they are passed through Strix's native workspace-file resolution and extra-file runtime path.
- Scan mode: Quick, Standard or Deep.
- Scope: Auto, Diff or Full, with an optional diff base.
- Budget and max turns: configured in Settings and passed to Strix's runner.
- LLM request pressure: `ARES_MAX_CONCURRENT_LLM_REQUESTS` caps in-flight model requests across the scan (default `2`, allowed `1`–`8`). This bounds actual provider calls without blocking Strix's parent/child Agent orchestration.
- Live controls: inspect agents, events and per-agent transcript; steer or stop an agent; pause/resume budget; inspect findings; and view reports. Scan history supports per-run and bulk deletion with confirmation. Findings from saved runs support per-finding and bulk withdrawal through Strix, with deletion audit metadata retained. The active scan and its findings are protected from deletion.

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

While a scan is active, ARES writes a durable checkpoint every 60 seconds by default (configurable from 30 to 300 seconds), immediately after a scan starts or resumes, after a manual stop, and during graceful worker shutdown. The latest complete checkpoint remains in repository storage independently of the Actions run that produced it. The workflow artifact `ares-strix-state` is retained as a secondary, 30-day compatibility fallback; it is no longer the primary source of truth.

The encrypted lifecycle record distinguishes four intents:
- `running`: a scan was active; after an unexpected worker loss, ARES may attempt automatic recovery.
- `handoff`: the worker is shutting down normally; the next worker may attempt automatic recovery.
- `manual_stop`: the operator explicitly stopped the scan in Telegram; the next worker must not restart it automatically. The saved run remains in Scan history and can be resumed manually when Strix marks it resumable.
- `completed`: the scan finished; the next worker starts normally without resuming it.

On startup, ARES first validates and restores the durable repository snapshot, then falls back to a legacy Actions artifact when needed. If earlier runtime runs exist but neither source can be restored, startup is refused rather than silently opening an empty history. Automatic Resume is attempted only for a recorded `running` or `handoff` run that Strix itself marks resumable; failed recovery is recorded as `recovery_required` to avoid an endless restart loop.

The workflow uses non-cancelling concurrency so a new worker cannot automatically kill the current worker before its shutdown hooks can run. Scheduled handoffs remain subject to GitHub scheduling delays and dropped scheduled events; repository checkpoints protect the latest saved state, but cannot preserve activity that has not yet reached a checkpoint.

## History, findings and STOP safety

Deleting one history run or all saved runs requires an explicit confirmation. Bulk history cleanup skips the active run and does not delete encrypted provider/MCP settings or `.ares/state.enc`. Saved-run findings can be withdrawn individually or in bulk; ARES calls Strix 1.7.0's native `ReportState.delete_vulnerability_report` so the findings index and report artifacts are regenerated and the withdrawal reason is retained in the run record. Finding deletion is disabled while its scan is active to avoid racing Strix's report writer. The STOP control also requires a second confirmation; confirming it persists `manual_stop`, which is terminal and distinct from an automatic Runner handoff.
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
