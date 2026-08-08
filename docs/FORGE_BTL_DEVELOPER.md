# Forge BTL Developer

BTL Developer is an experimental Milestone 1 replacement for the deprecated
multi-model `swarm-developer` workflow. It was created from
`feature/fg060-unified-agents-models-v1` and is disabled by default.

## Milestone 1 flow

```text
task
  -> Forge Manager creates btl/* branch and isolated worktree
  -> BTL Read plans with structured read-only tools
  -> BTL Code implements with structured read/write tools
  -> Forge Manager runs authoritative verification
  -> Forge Manager commits and pushes the exact btl/* branch
  -> ready_for_external_review
  -> stop
```

The same explicitly configured model is used for planning and implementation.
The operator must ensure BTL is active behind the shared Windows endpoint; Forge
does not select, recommend, rotate, or judge models for this workflow.

## Permissions and Git ownership

BTL Read can list, read, search, and inspect Git status/diffs. BTL Code can also
write files and replace exact text. All model paths are relative to the task
worktree. Absolute paths, traversal, symlinks, Git metadata, and credential paths
are rejected. Neither profile receives a terminal, arbitrary shell, network,
credential, Git-mutation, deployment, Docker, systemd, or sudo tool.

Ordinary structured-tool usage errors, such as reading a missing file or
searching a file as though it were a directory, are returned to the model as
bounded error results. A phase permits at most four recoverable errors, returns
at most two identical corrections, and stops on the third identical error.
Traversal, symlink, Git metadata, secret,
worktree-boundary, unavailable-tool, and other security errors remain fatal and
do not return probing guidance.

Forge Manager alone resolves the configured base, creates the isolated worktree,
verifies changes, commits, and pushes. Task branches use
`btl/<FT-task-id>-<bounded-lowercase-slug>`. The remote is always `origin`, and
the push destination is generated internally from the validated task branch.
Authoritative unittests require `/usr/bin/bwrap` and run with a read-only task
tree, empty HOME, no operator credentials, and no network. Forge fingerprints
the verified changes and refuses to commit if the tree changes afterward.

The default worktree root is:

```text
~/.local/share/owui-swarm/btl-worktrees
```

Task state is persisted beside that root so interrupted or failed work can be
inspected without trusting model output. Existing task state is never blindly
resumed or overwritten.

## Configuration and CLI

Enable only after review and intentional deployment:

```toml
[btl_developer]
enabled = false
model = "local-qwen36-35b-a3b-windows"
base_branch = "feature/btl-developer"
worktree_root = "~/.local/share/owui-swarm/btl-worktrees"
max_phase_turns = 12
planner_max_tokens = 4096
implementer_max_tokens = 8192
```

Run from the Forge checkout:

```bash
owui-swarm btl-dev run --prompt-file task.txt
```

The command reports the task ID, status, base SHA, task branch, worktree,
implementation commit, pushed SHA, and verification summary. The pushed `btl/*`
branch is then reviewed through the normal external GitHub review process.

## Explicit stop point

Milestone 1 does not merge, deploy, restart services, run a reviewer/fixer loop,
or escalate automatically to Qwen. A reviewer/fixer loop may be added in
Milestone 2 after this boundary is evaluated.
