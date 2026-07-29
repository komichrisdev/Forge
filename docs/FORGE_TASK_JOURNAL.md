# Forge Task Journal

Forge Version: `0.12-dev`
Architecture Revision: `R12`

The Forge Task Journal is the persistent execution history for local Forge automation. It records what happened, who owned it, where checkpoints live, and whether recovery would be safe after a restart.

It is intentionally small. It is not a distributed workflow engine, scheduler, message broker, or automatic recovery system.

## Why it exists

Forge v1 needs durable inspection for:

- scheduled automations;
- local image-generation jobs;
- Codex task offloading;
- Discord notification decisions;
- LAN dashboard visibility.

Those features need a shared record of task identity and lifecycle without tying tasks to transient model IDs or hidden model context.

## Storage

The journal uses SQLite in the existing Forge catalog database. Events are append-only: once written, historical event rows are not edited or silently deleted.

The schema is initialized idempotently and coexists with provider inventory, model health, probes, quality evidence, and existing Phase 2.2 data.

## Task IDs

Forge task IDs use this format:

```text
FT-YYYYMMDD-000001
```

Example:

```text
FT-20260728-000001
```

The ID is:

- generated centrally by the journal;
- unique inside the journal database;
- sortable enough for operations;
- safe for logs, URLs, Discord messages, and dashboard display.

Existing personal-task IDs are preserved. Personal tasks now store both the existing API task ID and the Forge task ID.

## Event model

Journal events contain:

- `event_id`
- `task_id`
- `event_type`
- `timestamp`
- `agent_id`
- `run_id`
- `stage`
- `message`
- `checkpoint_reference`
- `side_effect_state`
- `metadata`

Initial event types:

- `TASK_CREATED`
- `TASK_ASSIGNED`
- `TASK_STARTED`
- `STAGE_STARTED`
- `CHECKPOINT_CREATED`
- `LEASE_GRANTED`
- `LEASE_RENEWED`
- `HEARTBEAT_RECORDED`
- `HANDOFF_REQUESTED`
- `HANDOFF_COMPLETED`
- `TASK_COMPLETED`
- `TASK_FAILED`
- `TASK_CANCELLED`
- `TASK_ORPHANED`
- `RECOVERY_PROPOSED`

Duplicate transition keys prevent recording the same retried transition twice while preserving append-only event history.

## Leases and heartbeats

The journal can record:

- lease grants;
- lease renewals;
- heartbeats;
- lease expiration;
- suspected orphan state;
- confirmed orphan state when an explicit `TASK_ORPHANED` event exists.

Orphan detection is read-only shadow evaluation. It reports candidates but does not requeue, reassign, retry, cancel, or mutate tasks.

## Checkpoints

Checkpoint events store references to artifacts, not large generated files.

Checkpoint records include:

- task ID;
- stage;
- agent ID;
- timestamp;
- checkpoint reference;
- summary;
- metadata.

References must be safe relative artifact references. Absolute paths, traversal, empty references, and URL-like references are rejected.

## Side-effect boundaries

Each event records one side-effect boundary state:

- `none`
- `proposed`
- `started`
- `confirmed`
- `unknown`

Recovery inspection uses these states:

- `none` and `proposed`: replay-safe from the journal's perspective;
- `started` and `confirmed`: unsafe to replay automatically;
- `unknown`: requires manual review.

This protects future non-idempotent actions such as Discord posts, Codex task starts, media moves/deletes, image publication, and external-service changes.

## Existing runtime integration

The personal-task backend records journal events at stable transitions:

- task creation;
- task start;
- lease grant;
- stage start;
- model-role assignment;
- checkpoint creation;
- task completion;
- task failure;
- task cancellation;
- existing retry proposal.

The OpenAI-compatible API remains unchanged. Existing personal-task IDs remain valid.

## CLI

Read-only commands:

```bash
owui-swarm journal list
owui-swarm journal show FT-20260728-000001
owui-swarm journal events FT-20260728-000001
owui-swarm journal checkpoints FT-20260728-000001
owui-swarm journal orphans
owui-swarm journal recovery-status FT-20260728-000001
```

Each command supports `--json` for future dashboard and Discord summary consumers.

## What recovery does not do yet

This milestone does not implement:

- scheduler changes;
- automatic task reassignment;
- automatic retry execution;
- distributed workers;
- remote workers;
- Discord integration;
- dashboard UI;
- image-generation integration;
- Codex delegation.

Those features can consume the journal later.
