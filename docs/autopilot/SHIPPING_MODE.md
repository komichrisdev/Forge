# Forge Autopilot Shipping Mode

This branch operates under an unattended good-enough shipping policy.

## Workers

- **Forge Swarm** is the primary implementation worker.
- **Qwen Mini** produces committed scaffolding for upcoming tasks.
- **Qwen Read** diagnoses Swarm failures and prepares repair plans.
- **Qwen Code** implements Qwen Read repair plans.

Qwen Read and Qwen Code execute sequentially because they share the Windows
model endpoint.

## Commit policy

Every worker's useful result is committed and pushed.

- Passing work uses a normal commit.
- Useful incomplete or failing work uses a `WIP:` commit.
- Failed approaches remain visible in Git history.
- Force pushing and history rewriting are prohibited.
- A human review is not required before a worker commits or pushes.

Swarm may still perform its own automated planner, reviewer, and verifier
phases. These are automated workflow stages, not human release gates.

## Deployment policy

Commits and pushes are automatic. Service reloads occur only after a successful
Swarm result or passing focused Qwen Code validation. Failed work is committed
but is not loaded into the running backend.

## Safety stops

Workers must stop rather than proceed when they encounter:

- exposed credentials or secret material;
- destructive host or infrastructure operations;
- Git history rewriting or force pushing;
- changes outside the authorized repository;
- live financial or crypto execution;
- unrecoverable Git corruption.

Ordinary test failures, imperfect output, and incomplete implementations are
recorded as follow-up work rather than discarded.
