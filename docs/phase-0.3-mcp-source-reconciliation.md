# Phase 0.3 MCP source reconciliation

Completed 2026-07-25 on branch `feat/swarm-platform-v2`.

## Outcome

The Node MCP TypeScript/widget sources reproducibly generate the preserved
deployed build. Two frozen-lock clean builds, a direct source/build comparison,
the live protocol contract, and an independent read-only review found no
semantic source/build drift. No TypeScript behavior change was justified or
made.

The committed generated output was explicitly regenerated only after all
checks passed. Its bytes and SHA-256 hashes remained unchanged. The live MCP
process was not restarted and continues to use the code loaded at its original
activation time.

## Source/build layout

- Node application: `chatgpt_app/`
- Server source: `chatgpt_app/src/`
- TypeScript entry: `chatgpt_app/src/main.ts`
- Generated server: `chatgpt_app/build/`
- Deployed entry: `chatgpt_app/build/main.js`
- Widget inputs: `chatgpt_app/widget.html`, `src/widget.ts`, `src/widget.css`
- Generated widget: `chatgpt_app/dist/widget.html`
- Exact map: `docs/mcp-source-build-map.md`

The Python orchestration/dashboard service remains separate.

## Previous and regenerated hashes

The previous and regenerated hashes are identical:

| Generated file | Before and after SHA-256 |
| --- | --- |
| `build/data.js` | `e0f46a03584f3045d63a951c31b14b4f3c081e9555b03198419b2fdc7ddb9e08` |
| `build/data.js.map` | `bf41af56d92328b8b8a9fe0489ed3e2249c18622133bc2e677e38eeea8c4db77` |
| `build/main.js` | `63bf787d72aa64c24268cd376a526b58fd7f9db97dd8b53c69df3d2bcc5400e9` |
| `build/main.js.map` | `67950a1eea1e9be06f9d50b3826d0a447d1049cfc5688f64320d37f5998b4e3d` |
| `build/server.js` | `d7f507016f41ac7a945f6c87c61005a0a7fc90dd343583596263a906f3cc032f` |
| `build/server.js.map` | `9a1bbf8c3825ace4fa01eac57071298fdcbf8a630c9ada498db0c035b9e23a38` |
| `dist/widget.html` | `177970d6685898847c4a38c16297ee62806fee2858be8dbb929aa2b40b06e9c1` |

## Drift classification and source changes

Every deployed JavaScript, source-map and widget file matched the two clean
candidates. The only checker normalization is the source path inside a
TypeScript source map when `--outDir` is deliberately outside the project;
source-map mappings and all other fields are exact.

- Missing deployed TypeScript behavior: none.
- Stale/obsolete compiled behavior: none found.
- Build-tool-only difference: isolated source-map location.
- Generated, formatting or unknown differences: none.
- TypeScript source files changed: none.

The detailed classification is in `docs/mcp-source-build-drift.md`.

## Contract baseline

The machine-readable live fixture is
`chatgpt_app/test/fixtures/mcp-live-contract.json`. It records server
`Swarm Control` 1.0.0, Streamable HTTP, tool/resource capabilities, the widget
resource, and exactly these seven read-only tools:

1. `get_swarm_status`
2. `list_swarm_runs`
3. `get_swarm_run_summary`
4. `get_swarm_run_details`
5. `list_swarm_models`
6. `get_swarm_model`
7. `render_swarm_control`

The fixture contains no production tool results or private runtime data.
Contract details and normalization rules are in
`docs/mcp-contract-baseline.md`.

## Toolchain and commands

- Node 20.19.2
- npm 9.2.0
- lockfile version 3
- TypeScript 5.8.3
- Vite 7.3.6
- MCP SDK 1.29.0
- MCP Apps SDK 1.7.4

```bash
cd /home/komichris/openwebui-codex-swarm/chatgpt_app
npm ci
npm run check
npm test
npm run test:mcp-contract
npm run test:mcp-parity
npm run check:mcp-build
npm run build:mcp          # explicit regeneration only
```

The no-drift and parity commands build in a temporary directory and never
overwrite committed output. Reproducibility evidence is in
`docs/mcp-build-reproducibility.md`.

## Verification results

- Frozen installs: two successful `npm ci` runs from the committed lockfile.
- Clean reproducibility: pass; two candidate trees were byte-identical.
- Deployed-versus-candidate generated output: pass; all seven files identical.
- Type checks: pass.
- Existing Node tests: pass.
- Contract test: pass.
- Committed-versus-candidate parity test: pass.
- No-drift check: pass.
- Existing Python tests: 14/14 pass.
- Python compile check: pass.
- Production databases used by new tests: none.
- Paid model calls: none.

Parity covers all seven representative successful calls, resource output,
missing required fields, invalid types, unknown tools, declared unavailable
runtime data, and unexpected internal handler failures using synthetic
in-memory data.

## Files changed

- Added contract fixture and contract assertion.
- Added synthetic generated-build parity test.
- Added isolated standard-library build/check runner.
- Added explicit package scripts for build, contract, parity and drift checks.
- Updated `chatgpt_app/README.md` generated-file policy and commands.
- Added the four Phase 0.3 evidence documents and this report.
- Regenerated `build/` and `dist/`; content did not change, so Git records no
  generated-file diff.

No Python runtime source, TypeScript source, lockfile, systemd unit,
configuration, runtime database, artifact or backup file changed.

## Commits

- `c1bafb642c2cdfca358379857ae6f03d2ab0bba4` —
  `test: capture MCP contract and build parity`
- `c062a3438327d74a82979607772a4308476c6ab9` —
  `build: enforce reproducible MCP output`

The documentation commit that contains this report is intentionally reported
by Git and in the final Phase 0.3 handoff rather than self-referenced here.

## Live-service state

At the post-check:

- Dashboard PID `3602239`, active since 2026-07-25 16:20:28 EDT,
  `127.0.0.1:8787`.
- MCP PID `3602237`, active since 2026-07-25 16:20:28 EDT,
  `/usr/bin/node .../chatgpt_app/build/main.js`, `127.0.0.1:8790`.
- Open WebUI remained on `0.0.0.0:3000` and `[::]:3000`.
- Open WebUI container shim ID still begins `d1df2994`; its process and proxies
  retain their 2026-07-01 22:35:50 EDT start time.

No service was stopped, started, restarted or reloaded. No container, systemd
unit, bind address, firewall rule, global package or database migration was
changed. The running MCP process may continue to use already-loaded code until
a separately approved restart; generated bytes are unchanged, so this creates
no known behavioral discrepancy.

## Security and privacy review

Candidate files contain no API key, token, password, private key, environment
file, runtime database, production run data, prompt, artifact, Open WebUI
record, dependency tree or temporary build. The pre-existing test suite uses
obviously synthetic credential-shaped strings to verify redaction; they are not
real secrets. The live capture called only protocol metadata and synthetic
invalid requests.

## Rollback

No rollback is currently required. If these Phase 0.3 repository changes must
be abandoned:

1. Do not restart the live MCP process; it still has its original activation.
2. From `feat/swarm-platform-v2`, revert the Phase 0.3 commits or return the
   branch to the reviewed Phase 0.2 commit using a non-destructive branch
   operation approved at that time.
3. If generated output needs independent restoration, extract only
   `chatgpt_app/build/` and `chatgpt_app/dist/` from
   `/home/komichris/backups/owui-swarm/20260725T205641Z/source/openwebui-codex-swarm.tar.gz`
   into an isolated directory, verify its manifest/hashes, and then copy those
   directories while the MCP service is separately approved to be stopped.
4. Re-run type, contract, parity and no-drift checks.
5. Verify ports 8787, 8790 and 3000 without exposing any runtime content.

The broader recovery sequence remains in `docs/rollback-platform-v2.md`.

## Remaining risks

1. The frozen npm dependency tree reports 15 audit findings (6 moderate,
   9 high). Updating dependencies was prohibited and needs a dedicated reviewed
   dependency-hardening task.
2. Pinned MCP Inspector 1.0.0 declares Node 22.7.5 or newer while the service
   runtime is Node 20.19.2. The production dependencies and build pass on the
   declared Node `>=20.19`; Inspector compatibility should be resolved only in
   a dedicated toolchain task.
3. The widget is a single generated HTML file without a separate source map.
   Exact deterministic comparison detects drift, but debugging maps are absent.
4. No remote repository or off-host Git copy exists; the verified Phase 0.2
   external backup remains the recovery anchor.
5. The live process was intentionally not restarted, so restart-path validation
   of the regenerated output is deferred. Its bytes are unchanged from the
   currently loaded deployment.

## Next formal roadmap task

After review, begin Phase 1.0: design and implement the persistent versioned
local LLM wiki storage foundation, including schema/version boundaries,
transactional writes, backup/restore implications and tests. Do not add FTS,
ingestion, OCR, Drive, MCP write tools or Open WebUI integration until that
foundation is reviewed.
