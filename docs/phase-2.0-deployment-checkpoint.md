# Phase 2.0 Deployment Checkpoint

Captured after restarting only the Node MCP service at `127.0.0.1:8790`.

## Before-state

| Component | State before restart |
| --- | --- |
| Application repository | `feat/swarm-platform-v2`, clean |
| Wiki repository | `main`, clean |
| Application commit | `97dadda5620f7552f8820eb84b48efb0c622e897` |
| Wiki commit | `548a5f02b34362cfa41283d17a9bcc20bbf75427` |
| MCP PID | `3602237` |
| MCP listener | `127.0.0.1:8790` |
| MCP activation time | `Sat Jul 25 16:20:28 2026` |
| Dashboard PID | `3602239` |
| Dashboard listener | `127.0.0.1:8787` |
| Dashboard activation time | `Sat Jul 25 16:20:28 2026` |
| Open WebUI listener | `0.0.0.0:3000`, `[::]:3000` |
| Open WebUI port-forward PIDs | `63198`, `63206` |
| Open WebUI health | HTTP `200 OK` on `http://127.0.0.1:3000/` |

The pre-restart MCP tool list was the existing seven-tool Swarm surface:

- `get_swarm_model`
- `get_swarm_run_details`
- `get_swarm_run_summary`
- `get_swarm_status`
- `list_swarm_models`
- `list_swarm_runs`
- `render_swarm_control`

## Restart action

Restarted only:

```bash
systemctl --user restart owui-swarm-chatgpt-app.service
```

No dashboard, Open WebUI, Docker, firewall, or wiki restart was performed.

## After-state

| Component | State after restart |
| --- | --- |
| MCP PID | `3761995` |
| MCP listener | `127.0.0.1:8790` |
| MCP activation time | `Sat Jul 25 20:17:35 2026` |
| Dashboard PID | `3602239` unchanged |
| Dashboard listener | `127.0.0.1:8787` unchanged |
| Dashboard activation time | `Sat Jul 25 16:20:28 2026` unchanged |
| Open WebUI listener | `0.0.0.0:3000`, `[::]:3000` unchanged |
| Open WebUI port-forward PIDs | `63198`, `63206` unchanged |
| Open WebUI health | still HTTP `200 OK` |

No unexpected listeners appeared after the restart.

## Live MCP tool list

The live MCP server now exposes exactly these eleven read-only tools:

- `get_swarm_model`
- `get_swarm_run_details`
- `get_swarm_run_summary`
- `get_swarm_status`
- `list_swarm_models`
- `list_swarm_runs`
- `render_swarm_control`
- `wiki.page`
- `wiki.related`
- `wiki.search`
- `wiki.status`

There are no write tools on the live surface.

## Live verification

### `wiki.status`

The live response reported:

- `root`: `/srv/swarm-wiki`
- `schema_version`: `1.0`
- `git.commit`: `548a5f02b34362cfa41283d17a9bcc20bbf75427`
- `application_git.commit`: `97dadda5620f7552f8820eb84b48efb0c622e897`
- `page_count`: `3`
- `source_manifest_count`: `3`
- `proposal_count`: `0`
- `validation`: `valid`
- `issue_count`: `0`
- `lock.locked`: `false`
- `lock.file_exists`: `true`
- `latest_backup`: `20260725T233712974336Z`
- `index.present`: `true`
- `index.path`: `/srv/swarm-wiki/index/wiki.db`
- `index.schema_version`: `1`
- `index.canonical_page_count`: `3`
- `index.indexed_page_count`: `3`
- `index.last_build`: `2026-07-25T23:38:51Z`
- `index.freshness`: `current`
- `index.drift`: `0 added`, `0 changed`, `0 removed`
- `index.validation`: `valid`

### `wiki.search`

Representative live searches returned:

- Body-text term `Synthetic fixture`
  - `result_count: 1`
  - top result: `acme-orbit-recovery-runbook`
  - canonical path: `wiki/systems/acme-orbit-recovery-runbook.md`

- Exact page ID `acme-orbit-overview`
  - `result_count: 3`
  - first result: `acme-orbit-overview`
  - deterministic tie order then placed `acme-orbit-cache-decision`, then `acme-orbit-recovery-runbook`

- Exact slug `acme-orbit-cache-decision`
  - `result_count: 3`
  - first result: `acme-orbit-cache-decision`
  - deterministic tie order then placed `acme-orbit-overview`, then `acme-orbit-recovery-runbook`

- Jira key `ORBIT-7`
  - `result_count: 1`
  - top result: `acme-orbit-cache-decision`

- Unicode query `Órbita de ejemplo`
  - `result_count: 1`
  - top result: `acme-orbit-overview`

- No-match query `no-such-term-xyz`
  - `result_count: 0`

Observed properties:

- structured output only
- canonical paths are relative
- exact matches rank first
- ordering is deterministic
- no traceback exposure

### `wiki.page`

Verified page lookup by page ID:

- `acme-orbit-overview`
  - metadata returned with aliases, Jira keys, source refs, timestamps, verification status, confidence, tags, and supersedes
  - canonical path: `wiki/projects/acme-orbit-overview.md`
  - sources: `src-orbit-charter-v1`
  - relationships included `links_to`, `linked_from`, `supersedes`, and `superseded_by`
  - verification: `verified`
  - confidence: `95`

Verified page lookup by slug:

- `acme-orbit-cache-decision`
  - canonical path: `wiki/features/acme-orbit-cache-decision.md`
  - sources: `src-orbit-cache-decision-v1`
  - verification: `verified`
  - confidence: `98`

Missing page handling:

- `missing-page` returned a structured not-found error with message `Page not found: missing-page`

### `wiki.related`

Verified on a known page:

- `acme-orbit-overview`
  - related pages returned in deterministic order
  - top results:
    - `acme-orbit-cache-decision`
    - `acme-orbit-recovery-runbook`

Missing page handling:

- `missing-page` returned a structured not-found error with message `Page not found: missing-page`

## Verification suites

The pre-restart verification pass remained green:

- Python suite: `Ran 81 tests in 2.722s` → `OK`
- Node suite: `1 test file`, `1 test` passed
- `npm run check` passed
- `npm run test:mcp-contract` passed
- `npm run test:mcp-parity` passed
- `npm run check:mcp-build` passed

## Security verification

The live MCP surface rejected or failed safely for representative negative cases:

- malformed FTS query ``"unterminated`` → `Invalid FTS query: unterminated string`
- traversal-style page ID `../escape` → MCP input validation error on the `pageId` regex
- unknown tool name `__phase_0_3_unknown_tool__` → tool not found

No Python tracebacks were exposed.
No write tools were exposed.
No arbitrary filesystem, SQL, shell, import, rebuild, backup, or restore capability was present on the live MCP surface.

## Repository integrity

Post-restart checks remained clean:

- application repository: clean on `feat/swarm-platform-v2`
- wiki repository: clean on `main`
- wiki commit unchanged
- page files unchanged
- manifests unchanged
- SQLite index unchanged except for the expected live read-only checks
- no new backup or temporary files were created in the repositories
- no secrets or runtime artifacts were added to Git

## Open WebUI state

Open WebUI remained live at the existing `3000` listener with unchanged port-forward processes and an HTTP `200 OK` response.

Direct Docker-daemon inspection was not available from this environment without sudo password entry, so the checkpoint uses the unchanged listener, unchanged proxy PIDs, and successful HTTP health check as the observed state.

## Rollback readiness

Rollback remains the documented Phase 2.0 procedure:

1. stop only `owui-swarm-chatgpt-app.service`
2. restore the committed Node build if needed
3. restart only the MCP service
4. re-run the read-only wiki tool checks
5. leave the dashboard, Open WebUI, canonical wiki, and SQLite index untouched

The rollback procedure is documented in `docs/rollback-platform-v2.md`.

## Remaining risks

- Docker socket access was not available without sudo password entry, so container-level re-inspection was not performed in this checkpoint.
- Future MCP restarts still depend on the committed Phase 2.0 build remaining in sync with source and tests.
- Phase 2.1 has not begun.
