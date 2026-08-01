# FG-000 Forge Architecture Baseline

Date: 2026-08-01
Branch: `feature/swarm-developer`
Baseline HEAD: `1bbad6a124e64141f33256952e15c7c261855b2e`

This is the repository-wide baseline for the Planning implementation program.
Repository state and tests outrank earlier reports and model claims.

## Safety and repository state

- The primary worktree and Qwen worktree were clean at `1bbad6a`.
- `feature/swarm-developer` was two commits ahead of its remote tracking branch.
- Qwen Autopilot was paused, its timer was disabled and inactive, and no Qwen
  process was active before repository writes began.
- The interrupted Autopilot database was copied to
  `autopilot.sqlite3.before-sol-ownership-20260801T135100Z`; source and backup
  both had SHA-256
  `d26a1ca0f7b84d3ae63d9b22d051f49ec504cef2fd4ee2c9ea7e61288555c92d`.
- SOL acquired the existing Autopilot `tick.lock` before repository writes.
- Five unreachable Qwen context commits are preserved under
  `refs/archive/qwen-context/*`.
- Ignored archives, backups, virtual environments, build dependencies, and
  recovered research material were left in place.

## Backend modules and service boundaries

| Concern | Existing implementation to reuse |
| --- | --- |
| Configuration | `swarm_router/config.py`: configuration dataclasses and `load_config()` |
| OpenWebUI HTTP | `swarm_router/client.py`: `OpenWebUIClient`, `RequestFailure`, authenticated JSON requests |
| Context budgets | `swarm_router/context_budget.py`: `resolve_context_limit()`, `evaluate_context_budget()`, `preflight_check()` |
| Model catalog and routing | `swarm_router/catalog.py`: `ModelRecord`, `_context_length()`, `ModelCatalog.recommend()` and provider reconciliation |
| Provider inventory | `swarm_router/providers.py`: `ProviderInventory`, `OpenAICompatibleProvider` |
| Logical agents | `swarm_router/agents.py`: `AgentManifest`, `AgentRegistry`, built-in manifests; validation forbids provider/model coupling |
| Generic swarm runs | `swarm_router/orchestrator.py`: `SwarmOrchestrator`, run directory, events, worker and judge execution |
| Forge Developer runs | `swarm_router/developer.py`: `DeveloperCoordinator`, durable run schema, role routing, command policy, writer lease, pending calls, handoffs, compaction |
| Personal tasks | `swarm_router/personal.py`: `PersonalTaskManager`, task-file persistence, startup recovery, Night Owl and image flows |
| Durable journal | `swarm_router/journal.py`: `TaskJournal`, append-only events, checkpoints, leases, reconstruction, orphan/recovery views |
| Scheduler | `swarm_router/scheduler.py`: `ScheduleStore`, `Scheduler`, occurrences and scheduler lease |
| Notifications | `swarm_router/discord_notifications.py`: separate durable delivery and deduplication records |
| Image generation | `swarm_router/image_generation.py`: validated presets, ComfyUI client, contained artifact creation and lookup |
| Night Owl/Jira | `swarm_router/night_owl.py` and `swarm_router/night_owl_jira.py`; currently coupled and requires compatibility separation |
| Wiki | `swarm_router/wiki.py` canonical content and `swarm_router/wiki_search.py` atomic derived index |

Forge services are defined in:

- `systemd/owui-swarm-dashboard.service`
- `systemd/owui-swarm-personal.service`
- `systemd/forge-scheduler.service`
- `chatgpt_app/systemd/owui-swarm-chatgpt-app.service`

Production boundaries remain unchanged: the dashboard is owner-operated on
loopback/private LAN, OpenWebUI is the gateway, and ComfyUI is reached through
the fixed reverse-tunnel endpoint. The personal API binds loopback plus discovered
Docker bridge addresses in `serve_personal()`; the MCP API is loopback-only. The
personal API's wider local-host boundary and bearer token must be preserved and
hardened, not described as loopback-only. No Planning implementation may add an
arbitrary shell or filesystem endpoint.

## Frontend and dashboard

The operator dashboard is intentionally implemented with Python standard-library
HTTP plus inline HTML, CSS, and JavaScript in `swarm_router/dashboard.py`:

- `FORGE_HTML` is the page shell and navigation.
- `DashboardApp` supplies bounded read models and approved mutations.
- the request handler owns authentication, CSRF, request limits, and headers.

The separate read-only ChatGPT MCP App uses:

- `chatgpt_app/src/data.ts`
- `chatgpt_app/src/server.ts`
- `chatgpt_app/src/main.ts`
- `chatgpt_app/src/widget.ts`
- `chatgpt_app/src/widget.css`

The dashboard currently has a sticky page header but not shared sticky table
headers. Navigation still separates Tasks, Night Owl, Notifications, Agents,
Providers, and Dispatch. Those are implementation gaps, not new frontend stacks.

## API, authentication, and CSRF conventions

Dashboard GET and POST routing is centralized in `swarm_router/dashboard.py`.
Existing controls that later work must preserve include:

- private/allowlisted Host validation;
- constant-time owner-secret validation and login throttling;
- `HttpOnly; SameSite=Strict` session cookies;
- CSRF tokens on authenticated POST operations;
- security headers and a 64 KiB request-body ceiling;
- redacted, bounded API responses;
- legacy `X-Swarm-Token` compatibility.

The personal API uses bearer authentication for task, model, and completion
endpoints. The MCP App is loopback-only and relies on an authenticated tunnel if
exposed. New destructive actions must use the dashboard's existing session and
CSRF boundary and must fail closed.

## Artifact and image contracts

`swarm_router/image_generation.py` currently provides:

- an input allowlist and bounded prompt fields;
- approved fixed ComfyUI presets;
- output filename and artifact-root containment;
- original image, WebP thumbnail, `metadata.json`, and a recorded SHA-256;
- direct-child artifact lookup and authenticated dashboard serving.

The current writer is not immutable: a repeated task ID can overwrite the original,
failed thumbnail or metadata creation can leave a partial directory, and reads do
not verify the recorded digest. It also does not yet provide generalized artifact
authorization, stable pagination, retention, collections, soft-delete/restore,
scoped bulk operations, streamed ZIP limits, pin/protection state, or preset-version
compatibility. Artifact atomicity and immutability are a blocking repair before
gallery mutation, extending artifact IDs rather than accepting paths.

## Journal, durable run state, and active runs

`TaskJournal` is the intended canonical execution ledger for lifecycle, checkpoint,
lease, side-effect, and recovery events. It is not yet the only authoritative store:
personal-task files are written separately from journal events, and generic swarm
runs remain file-ledger based. Crashes can therefore leave cross-store disagreement.
The repair must reconcile those stores into explicit durable states; work items and
Planning features must not create another execution ledger.

Durable active-state sources already exist:

- `TaskJournal.reconstruct()` and journal events;
- Developer run status and phase in `DeveloperCoordinator`;
- personal-task JSON plus startup interruption recovery;
- scheduler occurrence state.

`DashboardApp` and the MCP App also expose legacy file-inferred run state. Missing
`final.md`/`failure.txt` is not proof of activity. The Active Runs contract must
consolidate durable sources, surface cross-store disagreement, and label
stale/unknown state explicitly.

## Database, migrations, and feature flags

The catalog SQLite database co-locates model/provider inventory, health and
quality evidence, the task journal, scheduler state, notifications, and Developer
run/lease/pending-call state. Schema owners currently use idempotent `CREATE TABLE`
and local `ALTER TABLE` checks.

There is no centralized catalog schema version, transactional upgrade/backup
framework, tested downgrade path, or general feature-flag system. The wiki search
index is the exception: it uses `PRAGMA user_version` and atomic replacement.

Before any schema-dependent or destructive feature, add the smallest catalog
migration/version mechanism that supports fixture upgrade and documented rollback.
Destructive gallery and attachment mutations need explicit fail-closed flags;
existing `enabled` fields are not a substitute. These are tracked as blocking
foundation tasks in the implementation matrix.

## Model and provider routing

`ModelCatalog.recommend()` combines capability, availability, health, cooldown,
reliability, quality, latency, and provider family. Developer-specific role
routing is in `DeveloperCoordinator`; logical agent identity is already separate
from provider and model identity.

Provider support remains primarily one OpenWebUI inventory surface. The recovered
Model Monitor handoff is a design input for Agents & Models, not authority to add a
second screen or duplicate catalog.

## Tests and browser coverage

The repository uses Python `unittest`. The verified discovery command is:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

The dashboard tests use loopback `ThreadingHTTPServer` integration and therefore
need a test environment that permits local sockets. There is no Playwright,
Selenium, Cypress, Puppeteer, jsdom, or equivalent real-browser harness. Current
tests cannot prove keyboard navigation, visible focus, responsive behavior,
back/forward navigation, deep links, or refresh recovery; those are explicit global
acceptance gates in the implementation matrix.

Frontend checks are defined by `chatgpt_app/package.json` and include TypeScript,
Node tests, generated-output parity, MCP parity, and production build.

## Baseline verification evidence

All results below are from `1bbad6a` on 2026-08-01 and were rerun after the FG-000
documentation corrections. They are command results recorded by SOL, not claims
that a read-only reviewer can reproduce: Python and Node tests require a writable
temporary directory, and dashboard tests require permitted loopback sockets. Final
acceptance reruns every command in a writable, loopback-capable environment.

| Command | Exit | Result |
| --- | ---: | --- |
| `.venv/bin/python -m unittest -v tests/test_context_budget.py tests/test_context_budget_reviewed.py tests/test_context_budget_integration.py tests/test_context_budget_final.py tests/test_tool_call_id_normalization.py tests/test_message_compaction.py tests/test_client.py tests/test_developer.py` | 0 | 171 tests passed |
| `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | 0 | 332 tests passed in 28.832s |
| `.venv/bin/python -m compileall -q swarm_router tests` | 0 | compiled successfully |
| `git diff --check` | 0 | clean |
| `npm test` in `chatgpt_app` | 0 | 1 Node suite passed |
| `npm run check` in `chatgpt_app` | 0 | both TypeScript checks passed |
| `npm run build` in `chatgpt_app` | 0 | 133 modules; production build passed |
| `npm run check:mcp-build` | 0 | generated output matched committed build/dist |
| `npm run test:mcp-parity` | 0 | 1 parity suite passed |

The Python suite initially produced 25 loopback `PermissionError` errors inside
the restricted sandbox. The same commands passed with host loopback access; those
errors are an execution-environment baseline, not repository failures.

## Confirmed baseline defects and gaps

- `19dfb0c` repairs and tests the `1bbad6a` handoff provenance, atomic tool-group
  cleanup, newest terminal-evidence retention, and retry-objective defects. The
  remaining context repair scope is overflow classification, conservative metadata
  resolution, and catalog-context propagation.
- Personal task state and journal events are non-atomic; generic runs remain
  file-ledger based, so legacy activity can become stale or disagree across stores.
- Developer tool-result acceptance does not reject an expired writer lease.
- Cancellation is observed after image and Night Owl side effects rather than
  interrupting them, and Night Owl timeout cleanup does not terminate a process group.
- The personal API request reader is unbounded and its Docker-bridge bind boundary
  needs explicit hardening and tests; bearer-token comparison is not constant-time.
- Artifact creation can overwrite an existing master or leave partial state, and
  reads do not verify the recorded digest.
- Scheduler leases are not renewed during a long tick, and malformed/empty handler
  results can be recorded as successful occurrences. Ambiguous submission failures
  can duplicate side effects on retry, and manual runs bypass scheduler lease fencing.
- Missing ComfyUI history is converted into fabricated percentage progress; future
  progress must expose only real lifecycle states.
- No real browser/E2E harness exists.
- Catalog migrations and destructive feature flags are absent.
- Night Owl remains partly coupled to Jira.
- Image lifecycle, collection, queue, and bulk-operation contracts are absent.
- Dashboard-started generic runs use an unjournalled daemon thread.
- Installer coverage does not install every repository service unit.
- Package/dashboard/version strings have drifted.
- Live OpenWebUI, Discord, Jira, ComfyUI, tunnel, and user-systemd behavior remain
  deployment acceptance checks, not unit-test claims.

The row-level status, assignments, review gates, and rollover checkpoint are in
[`FORGE_IMPLEMENTATION_MATRIX.md`](FORGE_IMPLEMENTATION_MATRIX.md).
