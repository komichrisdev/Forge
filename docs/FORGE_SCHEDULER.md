# Forge Scheduler

Forge Version: `0.7-dev`
Architecture Revision: `R7`

The Forge Scheduler is the local timer layer for Forge automations. It decides when work should be queued, then submits a normal task to the existing personal-task backend.

It is deliberately not a workflow engine. It does not run shell commands, call Discord, invoke Codex, submit image workflows, move media, or perform recovery. Those actions belong to task handlers added in later milestones.

## Purpose

The scheduler supports Forge v1 automations such as:

- Night Owl project triage;
- Crypto Keeper summaries;
- Plex Media Manager maintenance prompts;
- Security Monitor checks;
- local image-generation jobs;
- Codex delegation requests;
- Discord report preparation.

The execution path is:

```text
Schedule due
  ↓
Schedule occurrence claimed
  ↓
Personal task created
  ↓
Task journal records lifecycle
  ↓
Existing worker/orchestrator processes the task
```

## Storage

Schedules and occurrences live in the existing Forge SQLite catalog database.

Tables:

- `forge_schedule_counters`: central schedule ID generation.
- `forge_schedules`: persistent schedule definitions.
- `forge_schedule_occurrences`: one row per claimed, missed, skipped, failed, or created occurrence.
- `forge_scheduler_leases`: single-machine scheduler lease.

Initialization is idempotent and does not modify existing Phase 2.2 or journal rows destructively.

## IDs

Schedule IDs use:

```text
FS-YYYYMMDD-000001
```

Occurrence IDs are derived from the schedule ID and scheduled UTC time, making duplicate ticks and restarts converge on the same occurrence row.

## Trigger types

Supported v1 triggers:

- `one_time`: runs once at a timezone-aware `run_at`.
- `interval`: supports `every_seconds`, `every_minutes`, `every_hours`, and `every_days`.
- `cron`: supports five-field cron expressions using a constrained parser.

Cron support intentionally covers only:

- `*`
- `*/n`
- integers
- comma lists
- numeric ranges

It does not support names, seconds, `L`, `#`, or calendar extensions.

## Timezones and DST

Schedules store an explicit timezone. Occurrence times are stored in UTC.

Cron matching is evaluated in the configured timezone and converted back to UTC. Ambiguous fall-back wall times run once by skipping the second folded local time. Nonexistent spring-forward local times are skipped to the next valid match.

CLI output shows the configured timezone and UTC next-run time.

## Misfire policy

Supported policies:

- `skip`: record the missed occurrence and do not create a task.
- `run_once`: coalesce downtime into one catch-up task for the latest missed occurrence.

The scheduler never creates an unlimited backlog after downtime.

## Overlap policy

Supported policies:

- `skip`: if the previous task from the same schedule is still non-terminal, record the new occurrence as skipped.
- `wait`: leave the due time in place and report the schedule as waiting until the previous task becomes terminal.

Parallel execution is not implemented in this milestone.

## Journal integration

Scheduler-created personal tasks include structured metadata:

- `schedule_id`
- `occurrence_id`
- `scheduled_for`
- `trigger_type`
- `misfire_policy`
- `overlap_policy`

The existing personal-task backend writes normal task journal events. The scheduler does not add duplicate task lifecycle event types.

## CLI

Examples:

```bash
owui-swarm schedule validate schedule.json --json
owui-swarm schedule create schedule.json
owui-swarm schedule list
owui-swarm schedule show FS-20260728-000001
owui-swarm schedule disable FS-20260728-000001
owui-swarm schedule enable FS-20260728-000001
owui-swarm schedule run-now FS-20260728-000001
owui-swarm schedule occurrences FS-20260728-000001
owui-swarm scheduler status
owui-swarm scheduler tick --json
owui-swarm scheduler run
```

Read-only schedule inspection does not require Open WebUI credentials. Task-submitting commands use the normal Forge configuration and personal-task backend.

## Service operation

The repository includes `systemd/forge-scheduler.service`.

The service:

- runs `owui-swarm scheduler run`;
- uses the existing Forge config and environment file;
- restarts after unexpected failure;
- logs through journald;
- writes only under the existing Forge state directory.

No real schedules are created or enabled by installing the service.

## Restart behavior

Occurrence uniqueness is enforced by SQLite. Repeated ticks and scheduler restarts do not create duplicate tasks for the same schedule and scheduled time.

The scheduler uses a SQLite-backed local lease so only one scheduler instance processes occurrences for the same database at a time.

## Safety limits

The scheduler stores structured payloads only. It rejects shell-command payload fields and does not execute arbitrary commands.

Current deliberate non-features:

- distributed workers;
- remote scheduler coordination;
- automatic task reassignment;
- automatic retry execution;
- Discord writes;
- Codex delegation execution;
- ComfyUI workflow submission;
- dashboard UI.

Those features should consume schedule occurrences and journal metadata through explicit task handlers in later milestones.
