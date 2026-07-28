# Forge Night Owl Migration

Forge Version: `0.9-dev`
Architecture Revision: `R9`

This records the first enabled production Forge automation: Night Owl runs as a Forge task type submitted by the persistent scheduler and recorded in the task journal.

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

The legacy script uses `flock` to prevent concurrent runs, `timeout` to bound runtime, and writes JSONL logs plus a final message file. Dry-run mode validates prerequisites without processing Jira work.

Forge live execution uses the repo-owned runner:

- `/home/komichris/openwebui-codex-swarm/scripts/night-owl/run_nightly.sh`

That runner preserves the existing lock, timeout, state, Codex, and report behavior, but repairs Jira queue access by using the existing private Jira REST credentials before invoking Codex.

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

The live production payload points to the Forge-owned runner and keeps secrets out of the schedule payload.

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
Runner validates Jira queue through REST
  ↓
stdout/stderr are captured, size-limited, and redacted
  ↓
Task completes or fails normally
```

The scheduler does not execute Night Owl directly. It submits a normal personal task to the running backend over the authenticated loopback API so task execution survives the scheduler CLI process exiting.

If the Jira queue is empty, the runner writes a queue snapshot and exits successfully without starting Codex or sending Discord. If the queue has work, the runner starts Codex with the Night Owl skill and an explicit REST-based Jira instruction.

## Jira repair

Root cause:

- The legacy Night Owl prompt instructed spawned Codex to use `atlassian_rovo.searchJiraIssuesUsingJql`.
- The Atlassian MCP connector available to Codex is granted to `qublixgames.atlassian.net`, not `komichris.atlassian.net`.
- Direct MCP reproduction against `https://komichris.atlassian.net` returned `INVALID_ARGUMENT`.
- Existing private REST credentials in `/home/komichris/.config/night-owl/env` successfully authenticated to `komichris.atlassian.net`.

Repair:

- Added a stdlib Jira REST preflight helper in Forge.
- The Forge runner reads the existing Night Owl credential file at runtime.
- Queue checks use bounded REST calls to `/rest/api/3/myself` and `/rest/api/3/search/jql`.
- JQL is generated from a validated project key and the two supported statuses only: `In Progress` and `To Do`.
- Authentication, permission, rate-limit, timeout, network, and query-validation failures are classified.
- Queue snapshots store masked account identity, issue keys, summaries, status buckets, and counts.
- No token, password, authorization header, cookie, or webhook URL is written to Git, task payloads, logs, or journal metadata.

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

- Jira REST preflight: read-only
- dry-run prerequisite checks: read-only
- local state/log writes: idempotent write
- Jira updates: non-idempotent write
- GitHub pushes: non-idempotent write
- Discord reports: non-idempotent write
- file deletion/move: not part of the inspected Night Owl script

Forge does not automatically retry or replay uncertain non-idempotent Night Owl runs.

## Forge schedule

Production schedule:

- schedule ID: `FS-20260728-000001`
- name: `Night Owl Jira queue check`
- task type: `night_owl`
- agent: `night_owl`
- trigger: cron `0 */4 * * *`
- timezone: `America/New_York`
- misfire policy: `skip`
- overlap policy: `skip`
- payload mode: live
- runner: `/home/komichris/openwebui-codex-swarm/scripts/night-owl/run_nightly.sh`
- timeout: 14,400 seconds
- state: enabled
- next validated run: `2026-07-29T00:00:00Z`

Controlled live validation on 2026-07-28 completed with an empty queue:

- occurrence: `FO-20260728-000001-20260728T214244Z`
- personal task: `task-08e49acdfea04db2`
- Forge task: `FT-20260728-000004`
- Night Owl checkpoint: `night-owl/20260728T214244Z`
- personal checkpoint: `personal/task-08e49acdfea04db2/task.json`
- Jira snapshot: `/home/komichris/.local/share/owui-swarm/night-owl/20260728T214244Z-jira-queue.json`
- result: completed
- eligible issues: 0

## Service deployment

The scheduler runs as a user systemd service:

- service: `forge-scheduler.service`
- unit path: `/home/komichris/.config/systemd/user/forge-scheduler.service`
- command: `%h/openwebui-codex-swarm/.venv/bin/owui-swarm --config %h/.config/owui-swarm/config.toml scheduler run`
- database: `/home/komichris/.local/share/owui-swarm/catalog.sqlite3`
- Night Owl Forge state: `/home/komichris/.local/share/owui-swarm/night-owl`
- logs: user journald

It starts cleanly with the enabled Night Owl schedule and preserves the Open WebUI and personal backend services.

## Credential and service behavior

Credentials stay outside Git:

- Forge backend API key: `/home/komichris/.config/owui-swarm/environment`
- Night Owl Jira and Discord config: `/home/komichris/.config/night-owl/env`
- GitHub CLI auth: `/home/komichris/.config/gh/hosts.yml`

The Forge service runs under the existing user systemd scope. `ProtectHome=read-only` allows reading private Night Owl config while keeping writes constrained to `/home/komichris/.local/share/owui-swarm`.

## Preflight results

Jira:

- REST `/myself`: HTTP 200
- REST queue JQL for `In Progress`: HTTP 200, 0 issues
- REST queue JQL for `To Do`: HTTP 200, 0 issues
- identity: `ch***@gmail.com`

GitHub:

- `gh auth status -h github.com`: authenticated as `komichrisdev`
- `/home/komichris/misc`: `git ls-remote --heads origin` succeeded
- `/home/komichris/crypto-keeper`: `git ls-remote --heads origin` succeeded

Discord:

- webhook configured: yes
- metadata GET: HTTP 403
- one labelled test send: HTTP 403
- safe-fail policy: existing report-send failure leaves the report queued and makes the Night Owl task fail instead of reporting false success

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

- No automatic retry, replay, handoff, Discord integration, dashboard UI, or Codex delegation changes are implemented here.
- Discord delivery currently returns HTTP 403; Night Owl treats report-send failure as a task failure when a report exists.
- If the queue contains work, Codex will use REST for KomiChris Jira because the Atlassian MCP connector is not granted to that cloud.
