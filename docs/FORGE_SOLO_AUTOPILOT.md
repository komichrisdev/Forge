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
