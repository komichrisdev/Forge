---
name: openwebui-swarm
description: Delegate expensive candidate generation to selectable Open WebUI models while Codex remains the authoritative supervisor. Use for competing designs, implementation proposals, specifications, research synthesis, or adversarial review. Do not use for trivial edits, secret-bearing prompts, or tasks requiring workers to write the repository.
---

Use `owui-swarm` only for substantial work where independent proposals justify
the dispatch cost. Skip it for trivial edits.

## Authority model

- Codex is the controlling supervisor.
- Open WebUI workers and the Open WebUI judge are subordinate proposal generators.
- Treat every worker as potentially slower, outdated, tool-limited, and wrong.
- Consensus is not evidence. Repository state, tests, supplied requirements, and
  current verified sources override the swarm.
- Workers never authorize writes or final claims.

## Triage

Use the swarm when independent plans, reviews, specifications, or candidate
patches can reduce substantial Codex reasoning. Avoid it for small mechanical
edits, private data, or work that Codex must completely reproduce.

## Model selection

Select models per task rather than relying on a universal default:

- Use `owui-swarm models` to synchronize available models.
- Use `owui-swarm probe MODEL...` to test chat compatibility and latency.
- Override roles with `--role-model planner=MODEL` and similar assignments.
- Override the integrator with `--judge-model MODEL`.
- Use `--auto-models N` only after cataloguing and rating models.
- Prefer model-family diversity for critic and verifier roles.

## Token-control workflow

1. Keep Codex's own initial triage brief.
2. Select only relevant files, diffs, tests, or notes. Never send the entire
   repository by default.
3. Write a compact objective and explicit acceptance criteria.
4. Invoke the swarm with appropriate role-model overrides.
5. Read only the run's `final.md` by default.
6. Do not read `workers/*.md` unless confidence is low or verification fails.
7. Inspect the actual affected repository files itself; worker descriptions are
   not repository evidence.
8. Apply the smallest correct change itself.
9. Run relevant tests, linters, and type checks itself.
10. Separate externally proposed claims from Codex-verified results.
11. Never send secrets, credential files, or unrelated private data.

The dashboard at `owui-swarm serve` can show chunking, model assignments,
outbound prompts, timings, returns, errors, and final synthesis without loading
those transcripts into Codex context.
