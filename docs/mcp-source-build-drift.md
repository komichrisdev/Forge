# MCP source/build drift report

Recorded 2026-07-25 for Swarm Platform V2 Phase 0.3.

## Conclusion

No material source/build drift exists. Two clean frozen-lock builds produced
all seven generated files byte-for-byte identical to the preserved deployed
build and committed output. The earlier observation that compiled files were
newer than TypeScript files was based on filesystem modification times; it did
not establish newer behavior.

No TypeScript behavior change is supported by the preserved build, live
contract, tests or clean-build evidence, so none was made.

## File-by-file classification

| Source | Generated file | Function or surface | Result | Classification |
| --- | --- | --- | --- | --- |
| `src/data.ts` | `build/data.js` | SQLite/run reads, validation, redaction and output shaping | Exact match | No difference |
| `src/data.ts` | `build/data.js.map` | Source mapping | Exact in full clean builds | No difference |
| `src/main.ts` | `build/main.js` | Loopback Streamable HTTP entry and lifecycle | Exact match | No difference |
| `src/main.ts` | `build/main.js.map` | Source mapping | Exact in full clean builds | No difference |
| `src/server.ts` | `build/server.js` | Seven tool definitions, handlers, errors and resource | Exact match | No difference |
| `src/server.ts` | `build/server.js.map` | Source mapping | Exact in full clean builds | No difference |
| `widget.html`, `src/widget.ts`, `src/widget.css`, `vite.config.ts` | `dist/widget.html` | MCP Apps UI | Exact match | No difference |

When the automated checker sends current source to an external temporary
`--outDir`, TypeScript writes a different relative source path into each map.
Only that path is normalized. This is category 3 (compiler-output location
only), not a semantic or generated-code difference.

## Requested difference classes

1. TypeScript source missing deployed behavior: none.
2. Compiled build containing stale or obsolete behavior: none found.
3. Build-tool/compiler-only difference: isolated source-map path only.
4. Generated-file difference: none.
5. Comment/formatting difference: none.
6. Unknown requiring review: none after byte comparison, protocol capture,
   tests and independent review.

## Compatibility evidence

- Live server reports `Swarm Control` 1.0.0 and exactly seven read-only tools.
- Complete live titles, descriptions, schemas, annotations, execution metadata
  and MCP Apps metadata match source-created server output.
- Representative synthetic calls and failure cases match between committed and
  isolated candidate builds.
- Existing tests pass without production data access.
- No tool, schema, endpoint, bind address, service unit or runtime-data behavior
  changed.

## Promotion decision

The verified candidate already equals the committed generated build, so
regeneration would produce no file change. The compiled build is therefore
considered reconciled without a gratuitous rewrite. Existing hashes remain the
post-reconciliation hashes.
