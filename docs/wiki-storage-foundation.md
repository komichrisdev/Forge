# Wiki storage foundation

## Scope and architecture

Phase 1.0 adds filesystem-backed, versioned wiki storage to the Python
orchestration application. The canonical data root is `/srv/swarm-wiki`, owned
by `komichris:komichris` with mode `0750`. It is a separate Git repository so
knowledge history, backup, and access policy remain independent of application
source and runtime databases.

No listener, SQLite database, search index, MCP tool, ingestion worker, OCR
path, Drive mirror, or Open WebUI integration is added. The Node MCP service
remains unchanged.

`OWUI_SWARM_WIKI_ROOT` selects another root when explicitly set. Otherwise the
CLI uses `/srv/swarm-wiki`. The default backup parent is
`/home/komichris/backups/swarm-wiki`.

## Repository layout

```text
/srv/swarm-wiki/
  README.md
  .gitignore
  sources/
    README.md
    manifests/                 immutable YAML source/version records
    originals/                 approved source bytes or safe fixtures
  wiki/
    projects/
    features/
    decisions/
    systems/
    research/
    glossary/
  proposals/                   future unapproved edits
  schema/                      versioned format and governance contracts
  index/README.md              future generated-index boundary
  tests/fixtures/
  .locks/                      runtime-only, ignored
  .tmp/                        runtime-only, ignored
```

The packaged template lives at `swarm_router/wiki_template/`. Synthetic sample
content is copied only by `wiki init --with-samples`.

## Schemas and governance

Schema version `1.0` is machine-validated by `swarm_router/wiki.py` and
documented in the wiki's `schema/` directory.

Canonical Markdown pages require exact YAML front matter fields:
`id`, `title`, `project`, `aliases`, `jira_keys`, `source_refs`,
`source_updated_at`, `ingested_at`, `verification_status`, `confidence`, `tags`,
and `supersedes`. Page IDs are globally unique lowercase slugs and map exactly
to `<id>.md`. Timestamps are UTC ISO 8601 seconds. Verification is one of
`unverified`, `verified`, `conflicted`, or `superseded`; confidence is an
integer from 0 through 100 and does not imply verification.

Source manifests require `source_id`, `version`, `source_type`, `title`,
`locator`, source and ingestion timestamps, SHA-256 checksum metadata,
`media_type`, `authority`, `supersedes`, and `notes`. Existing manifests cannot
be overwritten through the storage API. Changed source bytes require a new
source/version ID.

Material claims cite `[[source:<source-id>]]`; internal page links use
`[[page:<page-id>]]`. Validation resolves all links and source references,
checks global uniqueness, enforces exact filenames, and rejects supersession
cycles.

Original authoritative sources outrank generated summaries. An explicit
approved decision remains controlling until explicitly superseded; recency
alone does not replace it. Contradictions remain visible on a `conflicted`
page, with competing claims and citations under `## Conflicts`.

Future automated edits belong under `proposals/`. Canonical edits and Git
commits are explicit approved operations. Agents must not import private
exports, credentials, conversations, uploads, model files, runtime databases,
or generated indexes.

## Filesystem safety

The storage layer rejects traversal IDs, unsafe locators, absolute private
paths, symlinks, symlinked roots, and multiply linked protected files.
Directory traversal uses retained directory descriptors with `O_NOFOLLOW`.

Writers take one persistent repository-wide `flock` at
`.locks/repository.lock`. A process exit releases the kernel lock; stale
metadata is informational and the lock file is never deleted based on PID or
age.

Canonical writes serialize deterministically into a same-directory temporary
file, flush and `fsync` it, parse it again, validate the repository-wide
candidate state, atomically replace the target, and `fsync` the parent
directory. A failed write leaves the prior canonical bytes unchanged. Source
creation uses no-overwrite link semantics beneath the same lock.

## CLI

```bash
owui-swarm wiki init [--with-samples]
owui-swarm wiki validate
owui-swarm wiki status [--backup-root PATH]
owui-swarm wiki get PAGE_ID
owui-swarm wiki list
owui-swarm wiki backup [--backup-root PATH]
owui-swarm wiki restore-verify BACKUP
```

Add `wiki --root PATH` before the subcommand to select a root explicitly.
Wiki dispatch occurs before normal Swarm configuration loading, so it does not
read the Open WebUI API key, model catalog, or private run history.

`wiki validate` emits deterministic tab-separated errors:
`code`, `file`, `page/source ID`, `field`, and a concise message. It never dumps
page bodies. `wiki get` performs exact-ID retrieval only; `wiki list` returns
compact metadata in ID order. Neither command performs search.

## Initialization

The canonical parent requires one narrow privileged operation:

```bash
sudo install -d -o komichris -g komichris -m 0750 /srv/swarm-wiki
```

Normal application and Git work then runs as `komichris`. Initialization
accepts only an absent, empty, or already recognized valid root and refuses
unknown content. Git is initialized separately on `main`, with repository-local
identity `Codex <codex@local>` and no remote.

## Backup and restore verification

`wiki backup` creates a unique UTC-named mode-`0700` directory. It records the
schema/tool version, source branch and commit, page/source counts, repository
status, a Git bundle containing all refs, current tracked working files, a
human-readable manifest, and SHA-256 checksums. Dirty tracked state is preserved
and explicitly warned about; untracked content is excluded.

Locks, temporary files, generated indexes, caches, model files, application
runtime data, and secrets are excluded. Backups must remain private.

`wiki restore-verify` first verifies all checksums and the Git bundle, then
clones and overlays tracked working files in a new temporary directory. It
rejects unsafe archive or metadata paths, runs full wiki validation, and
confirms the expected commit and page/source counts. It never overwrites
`/srv/swarm-wiki` or starts a service.

## Validation

```bash
cd /home/komichris/openwebui-codex-swarm
.venv/bin/python -m unittest tests.test_wiki -v
.venv/bin/python -m unittest discover -s tests -v
```

The wiki tests use temporary directories only. They cover strict parsing,
reference and supersession validation, traversal and symlink rejection,
immutable manifests, atomic failure behavior, process-level locking,
deterministic serialization and errors, Git status, backup integrity, and
temporary restore verification.
