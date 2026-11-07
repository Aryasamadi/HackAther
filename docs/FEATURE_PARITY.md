# Strix feature parity matrix

This matrix tracks native Strix capabilities against the ARES Telegram adapter. ARES is an interface and orchestration adapter; the Strix engine remains the source of truth.

| Native Strix capability | ARES surface | Status |
|---|---|---|
| --target (repeatable) | New scan, one target per line | Implemented |
| --target-list | Multiline target entry | Implemented |
| --instruction | Scan instruction prompt | Implemented |
| --instruction-file | Upload as workspace context; full instruction-file semantics not yet mapped | Partial |
| --workspace-file | Upload Telegram documents into Strix workspace files | Implemented |
| --scan-mode | Quick / Standard / Deep settings | Implemented |
| --scope-mode | Auto / Diff / Full settings | Implemented |
| --diff-base | Git diff base setting | Implemented |
| --config | Provider Manager writes a native Strix config override | Implemented |
| --mcp-config | Native Strix MCP schema entries in encrypted ARES state | Implemented |
| --mcp-server / --mcp-exclude | Adapter selection parameters; UI selection controls remain to be added | Partial |
| --max-budget-usd | Scan budget setting + live pause/resume | Implemented |
| --max-turns | Maximum turns setting | Implemented |
| --resume | Scan history and resume; individual and bulk history deletion with confirmation | Implemented |
| Live agent graph and events | Agent list and readable live activity stream | Implemented |
| Agent steering and stop | Agent details and controls; explicit two-step confirmation for scan STOP | Implemented |
| Findings | List/detail; per-finding and bulk withdrawal for saved runs through Strix ReportState | Implemented |
| Reports and usage | Markdown report and LLM usage summary | Implemented |
| MCP connection lifecycle | Live status from Strix MCP status sink | Implemented |
| Strix Viewer | Not exposed in Telegram; runner-local URL requires a secure tunnel | Not mapped |
| Headless --fail-on | Not applicable to interactive Telegram sessions | Not mapped |
| CLI self-update | Managed by pinned package release, not exposed in Telegram | Not mapped |

## Explicit limitations

- The Strix Viewer listens on the GitHub-hosted runner. Its loopback URL is not reachable from a Telegram client without a separately configured secure tunnel, so it is intentionally not exposed as a Telegram control.
- MCP onboarding is a guided form: name, transport, HTTP endpoint or STDIO command, optional Bearer token, optional tool allowlist and purpose notes. The collected fields are validated using Strix's native connection schema. Advanced timing/concurrency fields retain Strix defaults and are not yet exposed as Telegram controls.
- Provider onboarding asks for display name, Base URL and token. Model discovery requires an OpenAI-compatible GET /v1/models or GET /models endpoint; non-compatible provider APIs are not assumed to work.
- Several Strix interfaces used for interactive control are internal package interfaces. The Strix dependency is pinned, and CI tests imports and adapter contracts to catch compatibility drift.
- GitHub-hosted Actions remain finite-lived workers. Artifacts and encrypted state support handoff and later resume; this does not create a guaranteed 24/7 process.
- Finding deletion is disabled for the currently active scan to avoid racing Strix's live report writer. Saved findings are withdrawn through Strix 1.7.0's native ReportState method, which rewrites indexes/artifacts and records deletion metadata. Bulk history cleanup preserves the active run and encrypted ARES settings.

Model-provider and MCP setup screens edit the same Telegram control message. Callback-driven validation errors use Telegram popup alerts; text-field validation errors are shown inline in the same panel. The matrix is intentionally candid: “implemented” means a Telegram operation is connected to the corresponding Strix capability, not that a live penetration test has been run on a customer target.
