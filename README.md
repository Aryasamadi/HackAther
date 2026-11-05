# HackAther · ARES

ARES is a Telegram user interface and orchestration adapter for the official Strix runtime. It does not fork, patch, or reimplement Strix agents.

## Current architecture

- Telegram Bot API: inline controls, admin authorization, model selection and scan operations.
- Provider manager: stores named provider profiles and discovers model IDs through OpenAI-compatible model-list endpoints.
- Strix adapter: invokes the Strix Python runtime, its AgentCoordinator, TUI Controller, event projection and report state.
- State layer: encrypts provider credentials and Telegram update offsets before persisting them through the GitHub Contents API.
- Runner workflow: keeps the Telegram polling process active on GitHub-hosted runners and preserves encrypted Strix run snapshots between worker sessions.

The Strix package is pinned to version 1.7.0. The adapter depends on Strix internal Python interfaces, so version pinning and CI compatibility tests are intentional.

## Required repository secrets

Only add these two repository Actions secrets:

1. TELEGRAM_BOT_TOKEN
2. ADMIN_ID

The workflow receives GITHUB_TOKEN and GITHUB_REPOSITORY automatically from GitHub Actions. The workflow grants the built-in token contents:write to store the encrypted ARES state file. No PAT or third user-managed secret is required.

Run the workflow manually once from Actions, or wait for the five-minute schedule.

## Provider setup

Open Models → Add provider. The three prompts are:

1. Provider
2. Base URL
3. Token

The Provider field supports a custom display name plus LiteLLM's provider identifier, separated by a vertical bar:

- OpenRouter Personal | openrouter
- My Gateway | openai
- Local Ollama | ollama

The provider must expose an OpenAI-compatible model listing endpoint at /v1/models or /models. ARES tries both. Model discovery is not a universal standard across all LLM services; providers without one of these endpoints cannot be auto-discovered by this three-field flow.

Model pages show ten models at a time in two columns. The selected model is translated into the provider/model format Strix expects.

## Strix runtime integration

ARES uses the supported package entry points where possible and reuses the same Python runtime components used by Strix's interactive interface:

- strix.core.runner.run_strix_scan
- strix.core.agents.AgentCoordinator
- strix.interface.tui.backend.controller.TuiController
- strix.interface.tui.live_view.TuiLiveView
- strix.report.state.ReportState

This preserves actual agent execution, event streaming, steering, per-agent stop, budget pause/resume and Strix report artifacts. These are internal Strix interfaces rather than a separately versioned public SDK. The exact Strix version is pinned to reduce compatibility drift.

## GitHub-hosted runner lifecycle

GitHub-hosted runners are temporary and have a finite job lifetime. The runtime is deliberately bounded below the workflow timeout. Scheduled workflow runs provide the next worker session. Telegram update offsets and Provider records are encrypted and committed to .ares/state.enc; Strix run directories are archived with authenticated encryption into a workflow artifact before shutdown and restored by the next worker.

A scan that is still active during a worker handoff is stopped gracefully. Its Strix run data is carried into the next runner and can be resumed from Scan history when Strix marks it resumable. This is durable handoff, not a claim of a permanently running server: GitHub scheduling can be delayed and scheduled jobs can be dropped during heavy load.

## Security notes

- Provider records are encrypted with AES-GCM. The encryption key is derived from the Telegram bot token, repository identity and admin ID; rotating the bot token without migrating the encrypted state makes old state unreadable.
- The Strix run snapshot is also encrypted before it is uploaded as a GitHub Actions artifact.
- Never print tokens in logs. Use a private Telegram chat for provider tokens. Telegram bot chats are not end-to-end encrypted.
- Only the configured Telegram user ID is authorized to operate the bot.
- Use Strix only against assets you own or have explicit authorization to test.

## Local checks

Install:

    python -m pip install -e ".[dev]"

Run:

    ruff check ares tests
    pytest -q

The GitHub Actions CI workflow runs both checks on pushes and pull requests.

## Phase boundary

Phase 2 establishes the Telegram interface, Provider manager, encrypted persistence, direct Strix runtime adapter, GitHub runner workflow and core tests. The exhaustive Telegram feature-parity pass for every Strix CLI/TUI/viewer capability, richer finding/report navigation, and full operational verification belong to the next phase.
