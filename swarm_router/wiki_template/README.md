# Swarm Wiki

This repository is the canonical, versioned, local knowledge layer for Swarm
Platform V2. It is separate from application source and contains derived,
source-grounded Markdown—not credentials, private exports, runtime databases,
model files, conversations, uploads, or generated search indexes.

## Authority and provenance

Jira, Drive, repositories, approved decisions, and original files remain
authoritative. A wiki page is a derived explanation and must cite immutable
source manifests using `[[source:<source-id>]]`. Original source fixtures live
under `sources/originals/`; agents must never silently rewrite them or an
existing manifest.

Precedence is explicit:

1. An original authoritative source beats a generated summary.
2. A recorded approved decision remains controlling until explicitly
   superseded; mere source recency does not override it.
3. Supporting and generated sources cannot silently override authoritative or
   approved-decision sources.
4. Contradictions remain visible. A conflicted page names each claim and source
   in `## Conflicts`; no automatic winner is selected.
5. Verification status and confidence are independent. Confidence never implies
   verification.

## Canonical pages and proposals

Canonical pages live under `wiki/` and follow `schema/PAGE.md`. Material claims
require source citations. Internal links use `[[page:<page-id>]]`; filesystem
paths are not link identities.

Future automated writes create proposal files under `proposals/` by default.
Canonical changes require an explicit approved operation. Low-level writes are
validated, locked and atomic, but are not automatically committed. Git commits
are deliberate review checkpoints.

Superseded pages remain in Git and in `wiki/`, use
`verification_status: superseded`, and are referenced by the replacement's
`supersedes` list. They are discoverable history, not current guidance.

## Repository boundaries

- `sources/manifests/`: immutable source/version records.
- `sources/originals/`: approved source fixtures or future protected copies.
- `wiki/`: canonical pages.
- `proposals/`: unapproved future edits.
- `schema/`: governance and format contracts.
- `index/`: reserved for generated indexes; only its README is tracked now.
- `.locks/` and `.tmp/`: runtime-only and ignored.

Agents may validate, list, retrieve exact pages, create restricted backups, and
prepare proposals. They must not import private material, rewrite sources,
commit credentials, infer authority from confidence, hide contradictions,
publish generated indexes, or edit canonical pages without explicit approval.

## Git, backup, and recovery

`main` is the canonical branch. No remote is required. Every reviewed canonical
change should have a focused commit; raw source versions are append-only.

Use `owui-swarm wiki backup` to create a restricted Git bundle, current tracked
working-tree archive, manifest, metadata, and hashes. Dirty state is identified
explicitly. Use `owui-swarm wiki restore-verify BACKUP` to verify into a new
temporary directory; it never overwrites this repository or starts a service.
See `schema/BACKUP.md`.

Schema version: `1.0`.
