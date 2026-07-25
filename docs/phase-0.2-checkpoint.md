# Swarm Platform V2 Phase 0.2 checkpoint

Completed: 2026-07-25  
Host: `komiplex`

## Outcome

Phase 0.2 established a verified external restore point, documented local
source provenance, initialized a project-local repository, committed the
pre-V2 baseline, and created the implementation branch. No Platform V2
feature was implemented.

## Backup and restore verification

- Backup:
  `/home/komichris/backups/owui-swarm/20260725T205641Z`
- Owner and mode: `komichris:komichris`, 0700
- Manifest: `BACKUP-MANIFEST.md`
- Checksums: `SHA256SUMS`
- Restore report: `RESTORE-TEST.md`
- Restore result: **PASS**

The restore test verified every checksum then present, extracted the source
snapshot into an isolated sibling directory, confirmed package/source/test
files and the compiled MCP build, compared the wrapper byte-for-byte,
restored non-secret configuration structure and service metadata, and ran
`PRAGMA integrity_check` successfully against:

- the Swarm catalog backup;
- the Open WebUI database backup;
- the Chroma SQLite backup.

No restored service was launched. The temporary restore directory was
removed after the result was recorded; the actual backup remains.

The source snapshot excludes reproducible `.venv/`, `node_modules/`,
bytecode, test/build caches, logs, and temporary files. It includes the
current compiled Node MCP build and generated widget, which were not rebuilt.
Swarm configuration and private run artifacts are preserved only inside the
restricted backup. Open WebUI uploads, cache/model data, and raw conversation
contents were not copied.

## Provenance

Classification: **source archive provenance found**.

The local archive
`/home/komichris/Documents/openwebui-codex-swarm-v0.2.zip` contains an
18-file original Python project snapshot from 2026-07-18 and no Git metadata.
It is preserved in the external backup. The current source materially
extends and revises that archive. Existing in-tree step backups preserve
several intermediate local states.

Python installation metadata records an editable local install only:

```text
openwebui-codex-swarm 0.2.0
file:///home/komichris/openwebui-codex-swarm
```

No upstream repository URL, valid upstream checkout, complete local Git
history, Git objects, or remote was found. The new repository is therefore a
local baseline repository with incomplete prior history.

`/home/komichris/.git` is an invalid, empty directory with no `HEAD`,
`config`, objects, or children. Git reported that it was not a repository.
It was not modified. The new project-local `.git` takes precedence.

## Git checkpoint

- Repository root: `/home/komichris/openwebui-codex-swarm`
- Baseline root commit:
  `015b571b060e1bf0e782e02ba44f8f9e7bdedd7c`
- Baseline message: `chore: establish verified pre-v2 baseline`
- Git identity: `Codex <codex@local>`
- Identity source: existing local identity already used by another local
  repository; configured only in this repository
- Initial tracked files: 45
- Initial tracked blob bytes: 843,679
- Initial branch: `main`
- Implementation branch: `feat/swarm-platform-v2`
- Remote count: 0

This checkpoint document is a post-check artifact and therefore follows the
root baseline commit in a documentation-only commit. It does not alter
application behavior. Both `main` and `feat/swarm-platform-v2` are to be
advanced to that same documentation commit so the implementation branch
starts cleanly.

The candidate and staged file lists were reviewed in full. The staged set
contained the Python and TypeScript source, tests, docs, lockfiles, packaging
files, scripts, service templates, MCP source maps, `chatgpt_app/build/`, and
`chatgpt_app/dist/`.

Ignored categories include:

- Python and Node dependency environments;
- Python bytecode and package/test/build caches;
- local backups and archives;
- runtime databases and SQLite sidecars;
- runs, artifacts, uploads, runtime/data directories, and model files;
- `.env`, local `config.toml`, credentials, private keys, and secret
  directories;
- logs, temporary files, editor state, and OS metadata.

Secret-pattern, sensitive-filename, private-runtime, database, and
large-file checks found no material that should be removed from the tracked
set. No secret, runtime database, upload, private run artifact, or model file
is tracked.

## Validation

- Python: 14 tests passed.
- Node MCP: 1 test file passed.
- TypeScript: both no-emit checks passed.
- Node MCP rebuild: not run.
- Package install/update: not run.
- Database migration: not run.

The Python suite's first sandboxed run could not bind its ephemeral loopback
test server. The same unmodified suite passed outside the network-isolated
sandbox; this was an execution-environment restriction, not a test failure.

## Service-state comparison

Pre- and post-check state matched:

| Component | Before | After |
| --- | --- | --- |
| Swarm dashboard | PID 3602239, active since 16:20:28 EDT, `127.0.0.1:8787` | unchanged |
| Node MCP | PID 3602237, active since 16:20:28 EDT, `127.0.0.1:8790` | unchanged |
| Open WebUI process | PID 63261, started 2026-07-01 22:35:50 EDT | unchanged |
| Open WebUI container shim | container ID beginning `d1df2994`, PID 63229 | unchanged |
| Open WebUI | version 0.10.2, `0.0.0.0:3000` and `[::]:3000` | unchanged and healthy |
| Plex | active, `*:32400` | unchanged |
| qBittorrent | active, `*:8090` | unchanged |
| Docker | active | unchanged |
| UFW service | active/exited | unchanged |
| User linger | `no` | unchanged |
| Port 18787 | no listener | unchanged |

Dashboard `/`, `/api/models`, and `/api/runs` still return HTTP 200.
Dashboard `/health` still returns 404. MCP initialization at `/mcp` still
reports Swarm Control 1.0.0 and protocol 2025-06-18; MCP `/health` remains
absent. Open WebUI `/health` and `/api/version` still return HTTP 200.

The Swarm catalog size and modification time remained unchanged. Open WebUI
database, WAL, and Chroma modification times remained unchanged. The
compiled MCP JavaScript hashes match the pre-Git source snapshot. The Python
distribution remains version 0.2.0 installed editable from this repository.

No service was stopped, started, restarted, or reloaded. No container was
recreated. No systemd unit, bind address, firewall file, package, executable,
runtime configuration, or production database was changed.

## Unresolved risks

1. The compiled MCP build is newer than its TypeScript source and remains the
   only known representation of some live behavior. It is intentionally
   tracked and backed up; source/build reconciliation is deferred.
2. Interactive sudo authentication was unavailable. The backup includes
   raw Compose source, protected-file permission metadata, systemd state,
   container process/shim ID and start identity, but not Docker image
   IDs/digests, a full `docker inspect`, resolved Compose output, or UFW rule
   contents. `/etc/ufw/user.rules` and `user6.rules` retain their 2026-07-01
   modification times, and no firewall command was run.
3. Prior history is incomplete; the first commit records the installed local
   state rather than reconstructing unavailable authorship.
4. Open WebUI remains intentionally exposed on all interfaces at port 3000.
   Both Swarm interfaces remain unauthenticated but loopback-only.
5. Existing Open WebUI data ownership/modes and root-disk capacity risks from
   the Phase 0.1 baseline remain unchanged.

## Next formal roadmap task

Recommended Phase 0.3: reconcile the Node MCP TypeScript source with the
preserved compiled JavaScript in an isolated, reviewable change; establish a
reproducible no-drift build check; and verify that the rebuilt output matches
the live seven-tool schema before any deployment decision.

Do not begin the persistent wiki, FTS5 search, or another Platform V2 feature
until this checkpoint and the source/build reconciliation plan are reviewed.
