# Forge Night Owl Migration

Forge Version: `0.8-dev`
Architecture Revision: `R8`

This records the first production Forge automation migration: Night Owl runs as a Forge task type submitted by the persistent scheduler and recorded in the task journal.

## Existing Night Owl automation

Inspected on 2026-07-28.

Night Owl is implemented as a Codex skill at:

- `/home/komichris/.codex/skills/night-owl/SKILL.md`
- `/home/komichris/.codex/skills/night-owl/scripts/run_nightly.sh`
- `/home/komichris/.codex/skills/night-owl/scripts/send_report.sh`
- `/home/komichris/.codex/skills/night-owl/scripts/self_test.sh`

The automation processes Jira project `KAN`, prioritizing In Progress work before To Do work, then uses Codex and the `codex-colage` skill to inspect, implement, and report eligible work. It may update Jira, push Git repositories, write local run journals, and send Discord reports.

Inputs and configuration:

- Night Owl config: `/home/komichris/.config/night-owl/env`
- Project map: `/home/komichris/.codex/skills/night-owl/projects.json`
- Legacy state/output: `/home/komichris/.local/state/night-owl`
- Forge-run state/output: `/home/komichris/.local/share/owui-swarm/night-owl`

External services used:

- Jira: `https://komichris.atlassian.net`
- GitHub repositories listed in `projects.json`
- Discord webhook from the Night Owl environment file
- Codex CLI

The existing script uses `flock` to prevent concurrent runs, `timeout` to bound runtime, and writes JSONL logs plus a final message file. Dry-run mode validates prerequisites without processing Jira work.

## Previous trigger

The legacy trigger was user crontab based, with `CRON_TZ=America/New_York`.

Previous cadence:

- every four hours: `0 */4 * * * /home/komichris/.codex/skills/night-owl/scripts/run_nightly.sh`
- daily report: `0 7 * * * /home/komichris/.codex/skills/night-owl/scripts/send_report.sh`

Current legacy status at migration time: both Night Owl cron entries were already commented out with the note `paused 2026-07-28: logged out of Jira`. A snapshot exists at `/tmp/night-owl-cron-paused`.

## Forge task type

Forge adds the `night_owl` task type for the permanent logical agent `night_owl`.

Payload fields are structured and validated:

- `operation`: currently `run_nightly`
- `mode`: `dry_run` or `live`
- `dry_run`: boolean
- `script_path`: optional approved `run_nightly.sh` path
- `state_dir`: optional approved Night Owl state directory
- `timeout_seconds`: bounded subprocess timeout
- `run_hours`: Night Owl run window

Unknown fields are rejected. Arbitrary shell commands are not accepted.

## Execution path

```text
Forge schedule due
  ↓
Forge schedule occurrence claimed
  ↓
Personal task submitted to the running Forge backend
  ↓
Task journal records creation, assignment, start, checkpoints, and terminal state
  ↓
Night Owl handler invokes run_nightly.sh with subprocess argv
  ↓
stdout/stderr are captured, size-limited, and redacted
  ↓
Task completes or fails normally
```

The scheduler does not execute Night Owl directly. It submits a normal personal task to the running backend over the authenticated loopback API so task execution survives the scheduler CLI process exiting.

## Journal behavior

Night Owl tasks record:

- scheduler metadata: `schedule_id`, `occurrence_id`, `scheduled_for`, `trigger_type`
- assignment to logical agent `night_owl`
- subprocess start stage
- subprocess checkpoint reference
- subprocess completion stage
- normal task completion or failure event

Dry-run executions use side-effect state `none`. Live executions enter `started` before subprocess launch and become `confirmed` only after successful completion. Failed or timed-out live executions use `unknown`, which makes automated replay unsafe.

## Side-effect classification

Night Owl actions:

- dry-run prerequisite checks: read-only
- local state/log writes: idempotent write
- Jira updates: non-idempotent write
- GitHub pushes: non-idempotent write
- Discord reports: non-idempotent write
- file deletion/move: not part of the inspected Night Owl script

Forge does not automatically retry or replay uncertain non-idempotent Night Owl runs.

## Forge schedule

Production schedule created:

- schedule ID: `FS-20260728-000001`
- name: `Night Owl Jira queue check`
- task type: `night_owl`
- agent: `night_owl`
- trigger: cron `0 */4 * * *`
- timezone: `America/New_York`
- misfire policy: `skip`
- overlap policy: `skip`
- initial payload: dry-run
- initial state: disabled

The schedule is disabled until a live Night Owl run is safe. Recent live Night Owl logs showed Jira query failures, so enabling a production cadence would create duplicate or failed automation without adding value.

## Service deployment

The scheduler runs as a user systemd service:

- service: `forge-scheduler.service`
- unit path: `/home/komichris/.config/systemd/user/forge-scheduler.service`
- command: `%h/openwebui-codex-swarm/.venv/bin/owui-swarm --config %h/.config/owui-swarm/config.toml scheduler run`
- database: `/home/komichris/.local/share/owui-swarm/catalog.sqlite3`
- Night Owl Forge state: `/home/komichris/.local/share/owui-swarm/night-owl`
- logs: user journald

It starts cleanly with no enabled schedules and preserves the Open WebUI and personal backend services.

## CLI inspection

Useful commands:

```bash
owui-swarm schedule list
owui-swarm schedule show FS-20260728-000001
owui-swarm schedule occurrences FS-20260728-000001
owui-swarm scheduler status
owui-swarm journal list
owui-swarm journal show <task-id>
owui-swarm journal events <task-id>
owui-swarm journal checkpoints <task-id>
owui-swarm journal recovery-status <task-id>
```

Use `--json` for machine-readable output.

## Rollback

The legacy files and configuration are preserved.

To keep Forge from running Night Owl:

```bash
owui-swarm schedule disable FS-20260728-000001
systemctl --user disable --now forge-scheduler.service
```

To restore the legacy cron trigger, edit the user crontab and uncomment the two preserved Night Owl lines:

```bash
crontab -e
```

Reference snapshot:

```text
/tmp/night-owl-cron-paused
```

Do not enable both Forge and legacy cron for the same four-hour cadence.

## Current limitations

- Live Night Owl cadence remains disabled until Jira authentication/query behavior is verified.
- No automatic retry, replay, handoff, Discord integration, dashboard UI, or Codex delegation changes are implemented here.
- This migration wraps the proven Night Owl script; it does not rewrite Night Owl internals.
