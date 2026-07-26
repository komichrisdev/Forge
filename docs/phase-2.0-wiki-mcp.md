# Phase 2.0 — Read-only Wiki MCP

Date: 2026-07-25

## Outcome

Phase 2.0 adds four read-only wiki MCP tools to the existing Node MCP server
source and committed build:

- `wiki.search`
- `wiki.page`
- `wiki.related`
- `wiki.status`

The Node layer stays thin. It does not parse markdown, read SQLite directly for
wiki queries, or duplicate ranking/validation logic. It delegates to the
existing Python wiki/search surface through the project console entrypoint:

- `swarm_router/wiki.py`
- `swarm_router/wiki_search.py`
- `.venv/bin/owui-swarm wiki ...`

Because service restarts are not permitted in this phase, the running MCP
process continues serving its already-loaded pre-Phase-2.0 code until a
separately approved restart occurs. The repository and committed generated build
are ready for that restart.

## Architecture

Canonical flow remains:

Markdown + manifests
→ Python validation and canonical model
→ derived SQLite FTS5 index
→ Node MCP transport

Node additions are limited to:

- argument validation
- subprocess delegation to the existing CLI
- deterministic public error messages
- MCP tool registration

The wiki tools use the configured wiki root from `OWUI_SWARM_WIKI_ROOT`, default
`/srv/swarm-wiki`.

## Exposed tools

### `wiki.search`

Inputs:

- `query`
- `limit`
- `verification`
- `minConfidence`
- `jiraKey`

Returns the existing search result payload from `WikiIndex.search()`, including:

- `page_id`
- `title`
- `snippet`
- `score`
- `verification`
- `confidence`
- `jira_keys`
- `tags`
- `sources`
- `canonical_path`
- `current`

### `wiki.page`

Inputs:

- exactly one of `pageId` or `slug`

Returns the existing canonical page view from `WikiRepository.page_view()`,
including:

- `metadata`
- `content`
- `canonical_path`
- `sources`
- `relationships`
- `aliases`
- `verification`
- `confidence`

### `wiki.related`

Inputs:

- `pageId`
- `limit`

Returns the existing related-page ranking from `WikiIndex.related()`.

### `wiki.status`

Inputs: none

Returns the existing repository status payload from `WikiRepository.status()`,
including:

- schema version
- canonical page count
- source manifest count
- Git state
- index presence/version/freshness/drift
- last build timestamp
- latest backup timestamp
- canonical wiki commit
- application commit

## Request examples

```json
{ "name": "wiki.search", "arguments": { "query": "ORBIT-7", "limit": 5 } }
```

```json
{ "name": "wiki.page", "arguments": { "pageId": "acme-orbit-overview" } }
```

```json
{ "name": "wiki.related", "arguments": { "pageId": "acme-orbit-overview", "limit": 5 } }
```

```json
{ "name": "wiki.status", "arguments": {} }
```

## Response examples

`wiki.search`:

```json
{
  "data": {
    "query": "ORBIT-7",
    "result_count": 1,
    "results": [
      {
        "page_id": "acme-orbit-cache-decision",
        "title": "Acme Orbit cache decision",
        "score": 2000017.053871,
        "verification": "verified",
        "confidence": 92,
        "canonical_path": "wiki/features/acme-orbit-cache-decision.md",
        "current": true
      }
    ]
  }
}
```

`wiki.page`:

```json
{
  "data": {
    "metadata": { "id": "acme-orbit-overview", "title": "Acme Orbit overview" },
    "canonical_path": "wiki/projects/acme-orbit-overview.md",
    "relationships": {
      "links_to": ["acme-orbit-cache-decision", "acme-orbit-recovery-runbook"]
    }
  }
}
```

## Security model

- Read-only only.
- No write, rebuild, backup, restore, import, rename, or delete tools.
- No filesystem path inputs are exposed.
- Page selectors are slug-validated.
- Invalid FTS syntax returns a deterministic validation error.
- Missing pages return deterministic not-found errors.
- Python tracebacks are not returned through MCP.
- Canonical markdown, manifests, and `index/wiki.db` are never modified through
  MCP.

## Tests and checks

Validated on 2026-07-25 with:

- full Python suite: `81` tests
- `npm test`
- `npm run test:mcp-contract`
- `npm run test:mcp-parity`
- `npm run check:mcp-build`
- `npm run build:mcp`

Added MCP coverage includes:

- new tool registration
- contract baseline update
- search filters/ranking
- page fetch by ID and slug
- related-page ranking
- status payload
- malformed requests
- not-found errors
- concurrent read-only calls
- Unicode queries
- larger result sets

## Limitations

- Live activation is deferred until the MCP service is restarted.
- The running service on 2026-07-25 still serves the previously loaded toolset.
- `wiki.page` slug lookup currently maps to the canonical page ID rules already
  enforced by the wiki repository.
- The Node layer depends on the project console entrypoint remaining available.

## Future write-capability discussion

Write tools are intentionally deferred. Any future write phase must keep:

- proposal-first behavior by default
- canonical-write approval boundaries
- atomic locked writes in Python only
- no direct markdown or SQLite mutation from Node
- explicit contract review before any write tool is exposed
