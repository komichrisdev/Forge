# MCP public contract baseline

Captured 2026-07-25 from the running loopback MCP service for Swarm Platform V2
Phase 0.3.

## Capture method and privacy boundary

The baseline was obtained through the MCP Streamable HTTP protocol at the
existing `/mcp` endpoint. Capture used initialization, `tools/list`,
`resources/list`, and `resources/templates/list`. Two synthetic invalid calls
captured the unknown-tool and missing-required-field error shapes.

No operational Swarm tool was called against production data. No run, model,
prompt, artifact, database row, token, credential or absolute private data path
was captured.

The normalized machine-readable fixture is:

`chatgpt_app/test/fixtures/mcp-live-contract.json`

Normalization sorts object keys and tool/resource declarations for comparison.
It removes no semantic schema field. The transport is recorded by type without
an absolute endpoint. Error messages are reduced to stable JSON-RPC code and
`isError` shape; request IDs, process data and timestamps are not stored.

## Server and transport

- Server name: `Swarm Control`
- Server version: `1.0.0`
- Transport: stateless Streamable HTTP with JSON responses
- Capabilities: tools and resources, both with `listChanged: true`
- Resource templates: none
- Declared resource: `ui://swarm-control/v1/widget.html`
- Resource MIME type: `text/html;profile=mcp-app`

The widget resource declares an empty-origin CSP and `prefersBorder: true`.

## Seven-tool contract

1. `get_swarm_status` — empty input; concise run, reliability and quality
   snapshot.
2. `list_swarm_runs` — optional bounded pagination, status, task-mode, model and
   date filters.
3. `get_swarm_run_summary` — required `runId`, length 1–128.
4. `get_swarm_run_details` — required `runId`, length 1–128; bounded detail is
   returned in widget-only metadata.
5. `list_swarm_models` — optional bounded pagination, search, enabled,
   chat-compatible, family and recommended-role filters.
6. `get_swarm_model` — required `modelId`, length 1–300.
7. `render_swarm_control` — empty input; read-only widget overview.

Every tool declares:

- `readOnlyHint: true`
- `destructiveHint: false`
- `openWorldHint: false`
- task support forbidden
- model/app widget visibility
- output object with required `data` object and no extra top-level properties

Successful calls return model-visible JSON text plus
`structuredContent.data`. Detail and overview payloads intended only for the
widget use `_meta.swarmControl`.

## Error baseline

- Unknown tool: tool result with `isError: true`, JSON-RPC code `-32602`.
- Missing required field: tool result with `isError: true`, JSON-RPC code
  `-32602`.
- Declared unavailable-data errors: bounded public text.
- Unexpected handler failure: `Swarm data could not be read.`
- Uncaught HTTP transport failure: JSON-RPC code `-32603` with generic text.

The contract test compares every public tool field—name, title, description,
complete input/output schema, annotations, execution declaration and MCP Apps
metadata—plus server capabilities and resource declarations.

Any intentional contract change requires explicit review and an intentional
fixture update; generated output regeneration alone must not rewrite this
fixture.
