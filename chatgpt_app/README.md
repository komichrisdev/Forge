# Swarm Control ChatGPT App

`Swarm Control` is a strictly read-only MCP server and native MCP Apps widget for the existing Open WebUI Codex Swarm data. It does not call Open WebUI, execute models, mutate routing data, or iframe the dashboard.

## Architecture and stack

ChatGPT will later connect through a Windows-managed secure HTTPS tunnel to `http://127.0.0.1:8790/mcp`. The Debian service reads the SQLite catalog with SQLite `readonly` plus `query_only`, and reads only validated artifacts below the configured run root.

The app uses TypeScript, Node 20, the official `@modelcontextprotocol/sdk`, and the official `@modelcontextprotocol/ext-apps` bridge. A vanilla Vite single-file widget avoids a UI framework, remote assets, and runtime CDN dependencies. `better-sqlite3` provides explicit read-only database access because Node 20 has no built-in SQLite module.

Protocol references: [MCP Apps build guide](https://modelcontextprotocol.io/extensions/apps/build) and [MCP Apps SDK quickstart](https://apps.extensions.modelcontextprotocol.io/api/documents/quickstart.html).

## Install and build

```bash
cd /home/komichris/openwebui-codex-swarm/chatgpt_app
npm ci
npm run check
npm run check:mcp-build
install -m 644 systemd/owui-swarm-chatgpt-app.service \
  /home/komichris/.config/systemd/user/owui-swarm-chatgpt-app.service
systemctl --user daemon-reload
systemctl --user enable --now owui-swarm-chatgpt-app.service
```

The runtime is isolated under this directory (`node_modules`, `build`, and `dist`). It does not load `/home/komichris/.config/owui-swarm/environment` and needs no Open WebUI API key.

## Commands and endpoint

```bash
npm run check                 # TypeScript checks
npm run build:mcp             # Explicitly regenerate committed build/ and dist/
npm run check:mcp-build       # Isolated build; fail on generated-file drift
npm test                      # Unit, schema, bridge, and in-memory MCP tests
npm run test:mcp-contract     # Public schema/metadata baseline
npm run test:mcp-parity       # Committed build versus isolated candidate
RUN_HTTP_INTEGRATION=1 npm test  # Includes real loopback HTTP initialization
systemctl --user status owui-swarm-chatgpt-app.service
systemctl --user restart owui-swarm-chatgpt-app.service
```

Endpoint: `http://127.0.0.1:8790/mcp`. No other app endpoint or public bind exists.

`src/`, `widget.html`, and `vite.config.ts` are authoritative. Generated `build/`
and `dist/` files are committed because the service runs `build/main.js`; do not
edit them by hand. Regenerate explicitly with `npm run build:mcp`, then run the
contract, parity, and no-drift checks. Public contract changes require review of
`test/fixtures/mcp-live-contract.json`.

## Read-only tools

- `get_swarm_status`
- `list_swarm_runs`
- `get_swarm_run_summary`
- `get_swarm_run_details`
- `list_swarm_models`
- `get_swarm_model`
- `render_swarm_control`

Every tool advertises `readOnlyHint: true`, `destructiveHint: false`, and `openWorldHint: false`. There are no dispatch, probe, mutation, filesystem, shell, configuration, or service-control tools.

Model-visible `structuredContent.data` stays concise. `get_swarm_run_details` places prompts, responses, timelines, context accounting, reliability, quality, judge, and benchmark evidence in `_meta.swarmControl.detail`. `render_swarm_control` similarly puts its initial runs and catalog pages in widget-only `_meta`.

## Widget

The versioned resource is `ui://swarm-control/v1/widget.html` with `text/html;profile=mcp-app`. Views include Overview, Recent runs, Run details, worker cards, judge/final evidence, and Model catalog. Refresh, filtering, pagination, and detail loading use the standard MCP Apps `App.callServerTool()` bridge. Polling runs only for a displayed active run, every 15 seconds. Raw responses are collapsed initially.

The widget is self-contained. Its CSP has no connect, resource, frame, image, or font origins; the HTML also denies external resources, frames, forms, and base URIs. It uses no analytics, remote images, fonts, nested frames, or dashboard embedding.

## Data sources and security boundaries

- Catalog: `/home/komichris/.local/share/owui-swarm/catalog.sqlite3`
- Runs: `/home/komichris/.local/share/owui-swarm/runs/<validated-run-id>/`
- Configuration only through `SWARM_DB_PATH` and `SWARM_RUN_DIR` in the service unit

SQL is fixed and parameterized. Pagination is bounded to 100. Run IDs and artifact filenames are allowlisted, resolved paths must remain under the run root, individual files are capped at 256 KiB, displayed prompts/responses at 48,000 characters, and detailed metadata at 700,000 bytes. Truncation is reported. Browser rendering uses `textContent`, not untrusted HTML.

Returned copies redact authorization and cookie headers, bearer/basic tokens, known credential assignments and object keys, JWT-like strings, private keys, common token prefixes, and long credential-like values. Stored SQLite rows and artifacts are never changed. Protected environment contents, arbitrary paths, unrelated environment variables, SSH/tunnel credentials, and Open WebUI credentials are outside the app boundary.

## MCP Inspector

With the service running:

```bash
./node_modules/.bin/mcp-inspector --cli http://127.0.0.1:8790/mcp \
  --transport http --method tools/list
./node_modules/.bin/mcp-inspector --cli http://127.0.0.1:8790/mcp \
  --transport http --method resources/list
./node_modules/.bin/mcp-inspector --cli http://127.0.0.1:8790/mcp \
  --transport http --method tools/call --tool-name render_swarm_control
```

The Inspector package declares Node 22.7.5 or newer, while this host currently runs Node 20.19.2. The pinned Inspector 1.0.0 CLI passed tools, resources, resource-read, and widget-render validation on this host, but a future Inspector upgrade may require a Node runtime upgrade.

## Windows integration prerequisite

Do not register the app until Debian validation passes. The Windows session must later provide an authenticated secure HTTPS MCP tunnel whose upstream is exactly `127.0.0.1:8790`, without exposing ports 3000, 8787, or 8790 directly. Tunnel credentials stay outside this project and service.

## Troubleshooting

```bash
journalctl --user -u owui-swarm-chatgpt-app.service -n 100 --no-pager
ss -ltnp | grep 8790
systemctl --user is-active owui-swarm-chatgpt-app.service
npm run check && npm test && npm run test:mcp-parity && npm run check:mcp-build
```

`Swarm catalog is unavailable` means the configured SQLite file is absent or unreadable. `Swarm run directory is unavailable` means the run root cannot be read. The app returns bounded public errors and does not log artifact contents.

## Disable or uninstall only this app

```bash
systemctl --user disable --now owui-swarm-chatgpt-app.service
rm /home/komichris/.config/systemd/user/owui-swarm-chatgpt-app.service
systemctl --user daemon-reload
```

Removing `/home/komichris/openwebui-codex-swarm/chatgpt_app` is optional and does not affect the dashboard, catalog, historical runs, Open WebUI, or swarm Python core.

## Known limitations

- Single-user local Debian service only.
- No authentication is added at loopback; the later HTTPS integration must authenticate access.
- Artifact formats are discovered from the current swarm layout and unknown future files are ignored.
- Quality scores remain provisional when evidence is sparse.
- No live push: active-run detail polling is 15 seconds.
- Tunnel creation and ChatGPT registration are intentionally deferred to the Windows integration step.
