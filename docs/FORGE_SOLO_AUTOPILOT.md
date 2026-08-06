# Forge DeepSeek Solo Autopilot

DeepSeek Solo pins `deepseek-ai/deepseek-v4-pro` to one engineering task from
inspection through external-review readiness. It has no planner, implementer,
reviewer, verifier, manager, judge, handoff, or fallback-model roles.

Durable state is stored under:

```text
~/.local/share/forge-solo/<TASK_ID>/
├── state.json
├── heartbeat.json
├── events.jsonl
├── runner.lock
└── review/
```

Each tick starts a fresh bounded context containing the task, compact checkpoint,
recent bounded terminal evidence, active-process state, and current Git status.
Context overflow increments the context epoch and resumes on a later tick.

The model must end every ordinary response with `CONTINUE`,
`READY_FOR_REVIEW`, or `BLOCKED`. `READY_FOR_REVIEW` creates a review bundle and
stops with all repository changes uncommitted.

The runner reuses the hardened Open Terminal client and Forge implementer command
policy. It explicitly prohibits `sed`, recursive grep, pipelines, compound
commands, arbitrary inline Python, commit, push, deployment, service restarts,
sudo, secret access, and task switching. A rejected command is returned to the
same DeepSeek model with replacement guidance. Three repetitions of the same
rejected command block the task.

The service and timer files in this repository are templates only. This build
does not install, enable, or start them.


## Server-side completion gates

`READY_FOR_REVIEW` is advisory until the runner verifies all of the following:

- the repository contains at least one change;
- every changed path is within the task manifest's `allowed_paths`;
- `git diff --check` is clean;
- a successful focused test is recorded;
- a successful full relevant test suite is recorded;
- successful final `git diff` and exact `git status --short` inspections are recorded;
- no terminal process remains active.

The runner also participates in the existing
`forge_developer_writer_lock` table. It renews the exact 30-minute lease before
model requests and terminal work, stops if the lease token is lost, and releases
the lease only on a terminal review or blocked state.


## Evidence freshness and orphan fencing

Every successful test, diff inspection, and status inspection is bound to a
digest of the current `HEAD`, tracked diff, staged diff, and untracked file
contents. Any later repository edit invalidates earlier evidence.

`git diff --check` is recorded separately and cannot satisfy the final diff
inspection requirement.

While Solo owns the shared writer lease, it also maintains a legitimate
`forge_developer_runs` row containing its exact lease token and durable active
process state. Existing developer stale-lock recovery therefore remains fenced
when an expired Solo lease still has an unresolved process.


## Active-process release fencing

Solo never releases the shared writer while its durable run row reports an
active process or while a pending callback exists. Scope violations retain the
exact lease until the process reaches a terminal result. Process polling also
precedes maximum-round handling, so context limits cannot orphan an active
writer.

The durable lease token is cleared only after an exact-token release succeeds.
A busy or mismatched release leaves the token intact for diagnosis and safe
recovery.
