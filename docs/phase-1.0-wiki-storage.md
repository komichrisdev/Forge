# Phase 1.0 — Versioned wiki storage checkpoint

Date: 2026-07-25
Application branch: `feat/swarm-platform-v2`
Application repository: `/home/komichris/openwebui-codex-swarm`
Canonical wiki: `/srv/swarm-wiki`

## Outcome

Phase 1.0 established a persistent, filesystem-backed wiki foundation as a
separate local Git repository. The Python application now provides strict page
and source-manifest models, deterministic parsing and serialization,
repository-wide validation, locked and atomic writes, explicit Git integration,
restricted backups, temporary restore verification, and exact-read CLI
commands.

No search index, SQLite database, ingestion workflow, OCR, Drive integration,
wiki MCP tool, HTTP endpoint, worktree engine, manager API, or frontend
integration was added. The existing Python dashboard, Node MCP service, Open
WebUI container, systemd units, firewall, and runtime databases were not
changed or restarted.

## Architecture decision

The canonical wiki is data, not application source or Swarm runtime state. It
therefore lives at `/srv/swarm-wiki` as an independent Git repository on
`main`, with no remote. This keeps knowledge history and recovery independent
from application deployments and prevents wiki data from entering the
application repository.

The application setting `OWUI_SWARM_WIKI_ROOT` selects an explicitly approved
alternative root. The default is `/srv/swarm-wiki`. Live configuration under
`/home/komichris/.config/owui-swarm` was not modified.

The Node MCP application remains separate and unchanged. Wiki storage is
implemented only in the Python application.

## Paths, ownership, and repositories

| Item | Value |
| --- | --- |
| Python implementation | `swarm_router/wiki.py` |
| CLI integration | `swarm_router/cli.py` |
| Packaged template | `swarm_router/wiki_template/` |
| Tests | `tests/test_wiki.py` |
| Operator guide | `docs/wiki-storage-foundation.md` |
| Canonical wiki root | `/srv/swarm-wiki` |
| Wiki owner/group | `komichris:komichris` |
| Wiki root mode | `0750` |
| Wiki branch | `main` |
| Wiki remote | none |
| Wiki initial commit | `548a5f02b34362cfa41283d17a9bcc20bbf75427` |
| Schema version | `1.0` |

The wiki repository is clean and contains 31 tracked files totaling 14,556
bytes. Runtime lock and temporary paths are ignored.

## Repository layout

```text
/srv/swarm-wiki/
  README.md
  .gitignore
  sources/
    README.md
    manifests/
    originals/
  wiki/
    projects/
    features/
    decisions/
    systems/
    research/
    glossary/
  proposals/
    README.md
  schema/
    VERSION
    PAGE.md
    SOURCE.md
    INGEST.md
    VERIFY.md
    RETRIEVE.md
    STYLE.md
    BACKUP.md
  index/
    README.md
  tests/
    fixtures/
```

`.locks/` and `.tmp/` are runtime-only. Generated SQLite files, caches, model
files, logs, backups, environment files, credentials, and editor/OS artifacts
are ignored. `index/` contains only its reservation README; no search database
exists.

## Canonical page schema

Canonical pages are UTF-8 Markdown with strict YAML front matter. The required
fields are:

- `id`: globally unique lowercase ASCII slug; exact filename `<id>.md`
- `title`: non-empty Unicode string
- `project`: lowercase ASCII slug
- `aliases`: unique string list; may be empty
- `jira_keys`: unique canonical uppercase keys; may be empty
- `source_refs`: non-empty unique list of existing source IDs
- `source_updated_at`: timezone-aware ISO 8601 normalized to UTC seconds
- `ingested_at`: timezone-aware ISO 8601 normalized to UTC seconds
- `verification_status`: `unverified`, `verified`, `conflicted`, or
  `superseded`
- `confidence`: integer from 0 through 100; independent of verification
- `tags`: non-empty unique lowercase-slug list
- `supersedes`: unique page-ID list; may be empty

Unknown fields, duplicate YAML keys, YAML aliases, malformed front matter,
invalid types, unknown statuses, invalid dates, noncanonical Jira keys,
duplicate values, oversized documents, traversal IDs, unsafe filenames,
absolute private filesystem references, broken links, broken source
references, missing superseded pages, and supersession cycles are rejected.

Material claims cite declared sources with `[[source:<source-id>]]`. Internal
links use `[[page:<page-id>]]`. Conflicted pages require `## Conflicts`;
superseded pages remain stored and must be referenced by a replacement.

## Source-manifest schema

Source manifests are strict UTF-8 YAML mappings with:

- `source_id`: globally unique lowercase slug; exact filename
  `<source_id>.yaml`
- `version`: positive integer
- `source_type`: `jira`, `drive`, `document`, `web`, `repository`, `decision`,
  `runbook`, or `other`
- `title`
- `locator`: credential-free HTTP(S), `external:<slug>`, or safe
  `sources/originals/...` relative path
- `source_updated_at`
- `ingested_at`
- `checksum`: lowercase SHA-256
- `checksum_algorithm`: exactly `sha256`
- `media_type`
- `authority`: `authoritative`, `approved-decision`, `supporting`, or
  `generated`
- `supersedes`
- `notes`

URL credentials, credential-like query parameters, absolute paths, backslashes,
and `..` traversal are rejected. Relative original files must exist and match
their checksum. Existing manifests cannot be overwritten through the storage
API. A changed source requires a new source/version manifest whose version is
greater than the superseded version.

## Governance

Jira, Drive, repositories, approved decisions, and original files remain
authoritative. The wiki is a derived, source-grounded knowledge layer.

An original authoritative source outranks a generated summary. A recorded
approved decision remains controlling until explicitly superseded; a newer
timestamp alone does not override it. Supporting/generated material cannot
silently replace authoritative material. Contradictions remain visible and no
automatic winner is selected. Verification status is never inferred from
confidence.

Future automated writes create proposals by default. Canonical edits and Git
commits require an explicit approved operation. Agents must not silently
rewrite originals or source manifests, import private exports, or commit
credentials, conversations, uploads, runtime databases, model files, or
generated indexes.

## Transaction and locking design

All protected paths remain beneath a configured nonsymlink wiki root. Directory
access uses retained descriptors with `O_DIRECTORY` and `O_NOFOLLOW`.
Canonical pages, manifests, and traversed repository files reject symlinks;
protected files also reject extra hard links.

Writers use one persistent repository-wide `fcntl.flock` at
`.locks/repository.lock`. Kernel lock release after process exit is the stale
lock recovery policy; PID/age metadata never causes lock-file deletion.

Page writes:

1. serialize deterministically;
2. parse the serialized candidate;
3. acquire the repository lock;
4. validate the repository-wide candidate graph;
5. create a same-directory temporary file with `O_EXCL|O_NOFOLLOW`;
6. write, flush, `fsync`, and parse it again;
7. atomically replace the target with `os.replace`;
8. `fsync` the parent directory and clean temporary state.

Failed validation leaves the prior page byte-identical. Manifest creation uses
the same checks with atomic no-overwrite link semantics. Git commits remain an
explicit higher-level operation and run beneath the repository lock.

## CLI

```text
owui-swarm wiki init [--with-samples]
owui-swarm wiki validate
owui-swarm wiki status [--backup-root PATH]
owui-swarm wiki get PAGE_ID
owui-swarm wiki list
owui-swarm wiki backup [--backup-root PATH]
owui-swarm wiki restore-verify BACKUP
```

`wiki --root PATH` may select a root explicitly. Wiki dispatch occurs before
normal Swarm configuration loading, so these commands do not load the Open
WebUI API key, model catalog, or private run history.

Validation errors are compact and deterministically ordered by file, code, ID,
field, and message. `get` is exact-ID retrieval; `list` returns metadata in
page-ID order. Neither command performs search.

## Synthetic fixture inventory

Only fictional Acme Orbit fixtures were initialized:

| Canonical page | Source manifest | Purpose |
| --- | --- | --- |
| `acme-orbit-overview` | `src-orbit-charter-v1` | project overview |
| `acme-orbit-cache-decision` | `src-orbit-cache-decision-v1` | approved feature decision |
| `acme-orbit-recovery-runbook` | `src-orbit-runbook-v1` | unverified recovery runbook |

The matching original text files are synthetic. The fixtures demonstrate
aliases, Unicode, generic Jira-style keys, source citations, internal links,
tags, verification states, confidence, and decision/supersession fields. No
real project name, private person, production endpoint, Jira record, Drive URL,
conversation, upload, prompt, or credential is present.

## Backup and restore verification

Backup:

`/home/komichris/backups/swarm-wiki/20260725T224601299811Z`

The directory is owned by `komichris:komichris`, has mode `0700`, and uses
mode `0600` files. Its apparent disk use is 36 KiB. It contains:

- `wiki.bundle` with all Git refs;
- `working-tree.tar.gz` with current tracked files;
- `metadata.json`;
- `git-status.txt`;
- `BACKUP-MANIFEST.md`;
- `SHA256SUMS`.

The source repository was clean. Restore verification passed in a newly created
temporary directory: checksums passed, the Git bundle verified and cloned,
full wiki validation passed, commit
`548a5f02b34362cfa41283d17a9bcc20bbf75427` matched, and counts were exactly
three pages and three manifests. The temporary restore was removed; the actual
backup remains.

Backups exclude locks, temporary files, generated indexes, caches, untracked
files, application runtime state, models, and secrets. Dirty tracked state is
included only with an explicit warning.

## Dependency and toolchain changes

One dependency was added: `PyYAML==6.0.3`. It is pinned in `pyproject.toml` and
the new `uv.lock`. `uv lock --check` passes and no unrelated Python package was
updated. No Node dependency or lockfile changed, and no global package was
installed or updated.

The standard library provides models, path controls, locking, atomic writes,
Git subprocess integration, hashing, archives, and temporary restore
directories. PyYAML is used only for established YAML parsing/serialization,
with a strict loader that rejects aliases and duplicate keys.

## Test and validation results

- Wiki tests: 46 passed.
- Complete Python suite: 60 passed.
- Python compile check: passed.
- `uv lock --check`: passed.
- Node `npm run check`: passed.
- Existing Node tests: passed.
- MCP contract test: passed.
- MCP deployed/candidate parity test: passed.
- MCP no-drift build check: passed; committed `build/` and `dist/` match.
- Canonical `owui-swarm wiki validate`: passed with zero issues.
- Canonical Git state: clean `main`, no remote.
- Backup and temporary restore verification: passed.

Tests use synthetic fixtures, mocks, and temporary directories. They do not
read production runs, use a network model, make paid calls, change live
configuration, or restart a service.

The validation/transaction suite covers all required failure classes,
including missing/duplicate IDs, malformed YAML, invalid types/timestamps/state
and confidence, broken links/references, supersession failures and cycles,
traversal, symlink escape, immutable source overwrite, atomic failure behavior,
concurrent process locking, stale lock metadata, unexpected initialization
content, deterministic serialization/errors, Git status, backup checksums,
restore validation, corruption, empty/unknown content, Unicode, and Jira-key
normalization.

## Application commits

- `03a28c84ce829ca3c3cc68e9eecf3c5887d2bcc1` —
  `feat: add versioned wiki storage foundation`
- `cdd9ac66d6aeb1f289157b8410ad62bfad658341` —
  `test: cover wiki validation and transactions`
- `b15e9ba1d2d6e6ec8e6bbcce4b7a61bbf1b2bd8e` —
  `docs: document wiki governance and recovery`
- `e6c8ddaa72fe624468c1d0ed346e6e531f281fec` —
  `fix: keep wiki status read-only`

The checkpoint document is committed separately after these implementation
commits so their immutable SHAs can be recorded here. `main` remains at
`d876234b5944d0da2cb4c3a2e936669d91dc4508`; no application remote exists.

## Tracked and excluded application files

Application changes are limited to:

- `pyproject.toml` and `uv.lock`;
- `swarm_router/cli.py`;
- `swarm_router/wiki.py`;
- `swarm_router/wiki_template/`;
- `tests/test_wiki.py`;
- `README.md`;
- `docs/wiki-storage-foundation.md`;
- this checkpoint.

The Node MCP source, package files, fixtures, compiled build, generated output,
systemd definitions, live configuration, runtime databases, run artifacts,
Open WebUI data, and Phase 0 backups are unchanged.

## Privacy and secret review

Candidate application and wiki files were reviewed before commit. No API key,
token, password, private key, environment file, database, upload, run artifact,
model file, private source export, Open WebUI record, user prompt, production
endpoint, or unusually large file is tracked. Credential-related words occur
only in governance/validation code and documentation; no credential values are
present.

## Service-state verification

The final read-only host check found:

| Service | Verified state |
| --- | --- |
| Dashboard | PID `3602239`, active since `2026-07-25 16:20:28 EDT`, `127.0.0.1:8787` |
| Node MCP | PID `3602237`, same activation time, `127.0.0.1:8790`, entry point `chatgpt_app/build/main.js` |
| Open WebUI | PID `63261`, started `2026-07-01 22:35:50 EDT`, `0.0.0.0:3000` and `[::]:3000` |
| Container shim | PID `63229`, same July 1 start, container ID beginning `d1df2994` |

These match the Phase 0.3 checkpoint. No process was stopped, started,
restarted, or reloaded; no container was recreated. No systemd unit, bind
address, UFW rule, Docker configuration, runtime database, or Open WebUI file
was modified. `/etc/ufw/user.rules` and `user6.rules` retain their prior
2026-07-01 modification times. The Swarm catalog remains at its original
service-start modification time, so no migration or runtime write occurred.

## Known limitations and deferred risks

- The repository-wide lock serializes all local writers; this is intentional
  for the current low-throughput local wiki.
- Direct filesystem/Git edits can bypass the storage API and therefore remain
  an operator review boundary.
- Backups are local and no off-host remote exists.
- The canonical repository currently contains synthetic content only.
- Search, FTS5, ingestion, proposals tooling, and wiki MCP access are absent by
  phase scope.
- Existing npm audit findings remain deferred.
- MCP Inspector's different Node-version behavior remains deferred.
- Open WebUI LAN hardening and the detailed UFW inspection gap remain deferred.
- Python/Node MCP consolidation remains explicitly deferred.

None of these blocks the Phase 1.0 storage foundation.

## Exact next formal roadmap task

Phase 1.2 — implement deterministic SQLite FTS5 indexing and search over the
validated canonical wiki, without adding wiki MCP tools or ingestion in that
phase unless separately approved.
