# Phase 1.2 — Deterministic SQLite FTS5 Wiki Search

Date: 2026-07-25
Application branch: `feat/swarm-platform-v2`
Application repository: `/home/komichris/openwebui-codex-swarm`
Canonical wiki: `/srv/swarm-wiki`
Derived database: `/srv/swarm-wiki/index/wiki.db`

## Outcome

Phase 1.2 adds a deterministic SQLite FTS5 index over the validated canonical
wiki. Canonical Markdown pages and immutable source manifests remain the only
source of truth. The SQLite database is derived data, excluded from canonical
Git history, and rebuildable from canonical content.

The implementation stays in Python and uses the standard-library `sqlite3`
module already present on this Debian host. No Node dependency, SQLite
extension package, or external search service was added.

## FTS5 support

Host `sqlite3` supports FTS5. The verification probe created a temporary FTS5
table, inserted a row, and queried it successfully before implementation work
started.

## Index schema

Schema version: `1`

The derived database contains:

- `index_metadata`
  Stores schema version, last build time, build mode, source Git commit/branch,
  indexed page count, source-manifest count, and a canonical filesystem
  freshness token.
- `pages`
  One row per canonical page keyed by stable `page_id`, including slug, title,
  project, verification state, confidence, timestamps, canonical relative path,
  content hash, list fields as JSON text, current/superseded flag, and index
  schema version.
- `page_aliases`
- `page_jira_keys`
- `page_tags`
- `page_source_refs`
- `page_supersedes`
  Flat helper tables for deterministic filtering and relationship lookups.
- `page_search`
  FTS5 table over `page_id`, `slug`, `title`, `aliases`, `jira_keys`, `tags`,
  `source_refs`, and Markdown body text.

The canonical wiki validator now allows the derived `index/wiki.db` path and
its SQLite sidecars while still rejecting other unexpected files under
`index/`.

## Build and drift design

Indexing always starts from a validated canonical wiki snapshot. If validation
fails, the index build fails and the previous database remains intact.

Build flow:

1. Validate the canonical wiki with the existing `WikiRepository` validator.
2. Parse canonical pages into the validated Python model.
3. Build either:
   - a full temp database, or
   - an incremental temp copy keyed by `page_id`, `content_hash`, and
     canonical path.
4. Run:
   - `PRAGMA integrity_check`
   - FTS integrity check
   - page/source count checks
   - content-hash and path parity checks
   - a representative FTS query
5. `fsync` the temp file and atomically replace `index/wiki.db`.

Incremental mode supports additions, edits, moves, and deletions. If the
existing database is missing, corrupt, or uses the wrong schema version, the
command falls back to a full rebuild automatically.

`wiki status` performs the heavier drift accounting and reports:

- index schema version
- canonical page count
- indexed page count
- last build timestamp
- freshness
- drift counts for added, changed, and removed canonical pages

`wiki search`, `wiki related`, and `wiki stale` use a cheaper filesystem token
check against the last successful index build so query latency stays reasonable
without silently serving obviously stale data.

Two deliberate ceilings are documented in code:

- incremental mode copies the whole database before patching it so readers keep
  the old inode and the final replace stays atomic;
- query-time freshness uses filesystem mtimes instead of content hashing, which
  is sufficient unless operators deliberately preserve mtimes while editing
  canonical files.

## Ranking and result shape

Search uses SQLite FTS5 BM25 ranking with deterministic ordering.

Ranking rules:

- exact `page_id` matches rank highest
- exact `slug` matches rank next
- exact Jira-key matches rank above lexical matches
- current pages rank above superseded pages at equal lexical rank
- BM25 then orders lexical matches
- `page_id` is the final deterministic tiebreaker

Output fields:

- `page_id`
- `title`
- `snippet`
- `score`
- `verification`
- `confidence`
- `jira_keys`
- `sources`
- `canonical_path`
- `current`

Malformed FTS syntax returns a clean error. Snippets come from deterministic
SQLite `snippet(...)` extraction over the Markdown body.

## CLI

New commands:

```bash
owui-swarm wiki index [--full]
owui-swarm wiki search <query> [--limit N] [--verification STATUS] [--min-confidence N] [--jira-key KEY]
owui-swarm wiki related <page-id> [--limit N]
owui-swarm wiki stale --days N
```

Extended command:

```bash
owui-swarm wiki status
```

Wiki command output remains JSON by default for backward compatibility with the
Phase 1.0 CLI pattern.

## Real-root verification

Canonical wiki status after the final rebuild:

- Git branch: `main`
- Git commit: `548a5f02b34362cfa41283d17a9bcc20bbf75427`
- Git dirty: `false`
- Canonical page count: `3`
- Source-manifest count: `3`
- Index freshness: `current`
- Drift: `added=0 changed=0 removed=0`

Final real-root database state:

- Path: `/srv/swarm-wiki/index/wiki.db`
- Owner/group: `komichris:komichris`
- Mode: `0640`
- Size: `69,632` bytes
- Last build: `2026-07-25T23:38:51Z`

Representative real-root queries returned the expected cache decision page for
`ORBIT-7`, related results for `acme-orbit-overview`, and all three synthetic
pages as stale for `--days 30`.

## Tests

Python:

- `python -m compileall -q -f swarm_router tests`
- `python -m unittest discover -s tests -v`
- Result: `79/79` passed

New wiki search coverage adds:

- FTS5 detection
- empty-repository indexing
- page ID, Jira, alias, tag, phrase, body, and Unicode search
- exact-match ranking
- deterministic ordering
- filters
- superseded-page visibility
- malformed-query handling
- incremental add/edit/move/delete updates
- schema-mismatch rebuild fallback
- corrupt-database rebuild fallback
- atomic replace failure handling
- concurrent reader safety
- source-manifest immutability during indexing
- traversal-style input rejection
- CLI JSON output
- stale-index detection
- deterministic 500-page synthetic fixture generation

Node MCP regression checks:

- `npm run check`
- `npm test`
- `npm run test:mcp-contract`
- `npm run test:mcp-parity`
- `npm run check:mcp-build`

All passed with no Node source or generated-build changes required.

## Benchmarks

Benchmark corpus: `503` synthetic canonical pages in a temporary wiki root
(`3` sample pages plus `500` generated fixture pages).

Results:

- full rebuild: `819.184 ms`
- incremental rebuild with one edited page: `792.438 ms`
- benchmark database size: `630,784` bytes
- median search latency: `9.488 ms`
- p95 search latency: `10.12 ms`

The current incremental implementation still copies the whole database before
patching it, so rebuild time is close to full-rebuild time. The benefit in this
phase is safe atomic replacement and simple deterministic behavior, not large
write-time savings.

## Backup and restore

Verified Phase 1.2 backup:

`/home/komichris/backups/swarm-wiki/20260725T233712974336Z`

Restore verification result:

- verified: `true`
- source commit: `548a5f02b34362cfa41283d17a9bcc20bbf75427`
- page count: `3`
- source-manifest count: `3`
- dirty backup: `false`

The canonical wiki backup still excludes the derived SQLite index. After a
restore, rebuild the index explicitly:

```bash
owui-swarm wiki --root /srv/swarm-wiki index --full
```

## Recovery and rebuild instructions

Rebuild the current canonical root in place:

```bash
owui-swarm wiki --root /srv/swarm-wiki index --full
```

Allow incremental maintenance:

```bash
owui-swarm wiki --root /srv/swarm-wiki index
```

If search reports a stale or missing index:

1. run `owui-swarm wiki validate`
2. inspect `owui-swarm wiki status`
3. rebuild with `owui-swarm wiki index --full`

If the database is corrupted or schema-mismatched, the normal `wiki index`
command automatically falls back to a full rebuild.

## Lexical search limits

This is lexical FTS5 search only. It does not do:

- semantic retrieval
- embeddings
- synonym expansion
- typo tolerance beyond tokenizer behavior
- cross-document reasoning
- source-aware ranking beyond exact identifiers, current-page preference, and
  canonical metadata filters

## Future embedding criteria

Do not add embeddings until at least one of these is true:

- lexical recall is measurably inadequate on real wiki tasks
- the page corpus grows enough that curated lexical metadata is no longer
  sufficient
- a concrete retrieval evaluation shows persistent misses that FTS5 filters and
  schema improvements cannot solve

When that phase starts, keep canonical content unchanged, keep the SQLite index
rebuildable, and treat any embedding/vector store as another derived layer.
