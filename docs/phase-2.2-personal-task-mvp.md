# Phase 2.2 — Personal Task MVP in Open WebUI

Date: 2026-07-26

## Initial state

- Application repository: `feat/swarm-platform-v2`
- Application commit at start: `39a16a9d8d77fd80cfabde609fd54b1bedfbed5f`
- Canonical wiki commit: `548a5f02b34362cfa41283d17a9bcc20bbf75427`
- Actual host listeners confirmed on Sunday, July 26, 2026:
  - `127.0.0.1:8787` dashboard
  - `127.0.0.1:8790` MCP
  - `0.0.0.0:3000` and `[::]:3000` Open WebUI
- The earlier “unreachable” result was a sandbox-network artifact, not a host outage.
- `/opt/open-webui/compose.yaml` shows a containerized Open WebUI deployment using:
  - `ghcr.io/open-webui/open-webui:main`
  - `OPENAI_API_BASE_URLS`
  - `OPENAI_API_KEYS`
- Installed local Open WebUI documentation at `/opt/open-webui/README.md` documents two supported registration methods:
  - Admin UI: `Admin Settings -> Connections -> OpenAI -> Manage -> Add New Connection`
  - env/compose-managed OpenAI-compatible provider endpoints
- Official Open WebUI documentation also confirms OpenAI-compatible provider registration and notes the `host.docker.internal` pattern when Docker needs to reach a host service.

## Working plan

1. Add a Python personal-task OpenAI-compatible backend that reuses:
   - `SwarmOrchestrator`
   - `ModelCatalog`
   - `DashboardApp`
   - `WikiRepository`
   - `WikiIndex`
2. Keep the service read-only and deterministic on unsupported requests.
3. Bind the backend to:
   - `127.0.0.1`
   - detected Docker bridge addresses only
4. Register `swarm-personal` in Open WebUI through the installed compose/env path.
5. Live-verify through Open WebUI's own API path after deployment.

## Material milestone 1

Architecture decision:

- Added a dedicated `swarm_router.personal` HTTP service instead of a second stack.
- Reused the existing swarm model routing and run artifacts rather than creating a parallel model registry or wiki parser.
- Added a user systemd template for the personal service.
- Added a new `[personal]` config section for limits, retention, auth token, and bind port.

Files changed so far:

- `swarm_router/config.py`
- `swarm_router/cli.py`
- `systemd/owui-swarm-personal.service`
- `scripts/install.sh`
- `config.example.toml`
- `docs/phase-2.2-personal-task-mvp.md`

Commands run so far:

- local repo and wiki cleanliness checks
- source inspection of:
  - `README.md`
  - `chatgpt_app/README.md`
  - Phase 2.0 and 2.1 reports
  - rollback documentation
  - `swarm_router/*`
  - `chatgpt_app/*`
  - `/opt/open-webui/compose.yaml`
  - `/opt/open-webui/README.md`

Open items:

- automated tests for the new backend
- deployment-time backup and rollback capture
- Open WebUI compose/env update
- service start/restart and live verification

## Material milestone 2

Backend implementation and validation:

- Added `swarm_router/personal.py` as the local OpenAI-compatible backend.
- Added `owui-swarm personal-serve`.
- Added `tests/test_personal.py`.
- Fixed two concrete defects under test:
  - streaming responses now close cleanly
  - wiki retrieval now falls back to exact Jira-key lookup inside a normal prompt
- Tightened task metadata to store bounded message metadata instead of public prompt previews.
- Removed unnecessary path leakage from `/health`.
- Made cancellation observable immediately and preserved cancelled state if work finishes later.
- Mapped failed background tasks back to controlled API error codes for synchronous callers.

Regression results:

- Personal backend tests: `10/10` passed
- Full Python suite: `98/98` passed
- Node/MCP checks passed:
  - `npm run check`
  - `npm test`
  - `npm run test:mcp-contract`
  - `npm run test:mcp-parity`
  - `npm run check:mcp-build`

Files changed so far:

- `README.md`
- `config.example.toml`
- `scripts/install.sh`
- `swarm_router/cli.py`
- `swarm_router/config.py`
- `swarm_router/personal.py`
- `systemd/owui-swarm-personal.service`
- `tests/test_personal.py`
- `docs/phase-2.2-personal-task-mvp.md`

Current blocker:

- Host-level Open WebUI inspection and modification require `sudo`, and this shell cannot supply the password non-interactively.

## Material milestone 3

User-side deployment changes applied:

- Backup created at:
  - `/home/komichris/backups/owui-swarm/phase-2.2-20260726T014456Z`
- Updated user config:
  - `~/.config/owui-swarm/config.toml` now includes `[personal]`
- Updated protected user environment:
  - `SWARM_PERSONAL_API_KEY` added without printing its value
- Installed user service:
  - `~/.config/systemd/user/owui-swarm-personal.service`
- Started only the new personal backend service

Live service state reached:

- `owui-swarm-personal.service` became active at:
  - `Sat 2026-07-25 21:46:11 EDT`
- Listener addresses confirmed:
  - `127.0.0.1:8788`
  - `172.17.0.1:8788`
  - `172.18.0.1:8788`
- Direct host-side checks reached:
  - `/health`
  - `/v1/models`

First live chat result:

- A direct local `POST /v1/chat/completions` returned HTTP `500`
- The personal task artifact at `2026-07-26T01:46:40Z` showed the local wrapper error:
  - `[Errno 17] File exists: '/home/komichris/.local/share/owui-swarm/runs/personal-task-ec6ba5cb12394f65'`
- The underlying first orchestrator attempt actually failed with:
  - `Open WebUI returned HTTP 400 for /api/chat/completions: {"detail":"ResourceExhausted: Worker local total request limit reached (48/48)"}`

Root-cause fix applied in source:

- Retry attempts now use distinct run IDs:
  - `personal-<task-id>-r0`
  - `personal-<task-id>-r1`
- This preserves the existing one-retry policy without colliding with the first failed run directory.

Deployment state after the fix:

- The source tree contains the retry fix.
- The running `owui-swarm-personal.service` has not yet been restarted to load it.
- Open WebUI registration has not started because `/opt/open-webui/.env` and Docker inspection still require `sudo`.

Rollback status:

- User-owned rollback inputs are preserved in the Phase 2.2 backup directory.
- Reverting the user-side deployment is straightforward:
  - stop `owui-swarm-personal.service`
  - remove or disable `owui-swarm-personal.service`
  - restore `~/.config/owui-swarm/` from the Phase 2.2 backup
