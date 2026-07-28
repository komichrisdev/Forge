# Forge Resilience Architecture Review

Forge Version: `0.3-dev`
Architecture Revision: `R3`

## Status

This document starts the Resilience phase after the Phase 2.2 stable Open WebUI deployment.

- Baseline commit: `5b877a0` (`Phase 2.2: Stable Open WebUI deployment`)
- Baseline tag: `phase-2.2-stable`
- Work branch: `feature/resilience-handoff`
- Package reviewed: `swarm-resilience-handoff-avatar-pack-v3.zip`
- Package manifest verification: 44 entries, 0 checksum mismatches

No runtime behavior, service unit, timer, routing policy, Open WebUI connection, or production data path is changed by this architecture review.

## Naming rule

The long-term project name is Forge. Existing installed names such as `owui-swarm`, `swarm_router`, `swarm-personal`, service units, config paths, and runtime directories stay unchanged unless a future change has a functional reason to touch them.

New Resilience architecture, modules, docs, and features may use Forge naming. Avoid rename-only churn.

## Core architecture

Forge must separate stable work identity from provider execution.

```text
Logical Agent
  -> requested capabilities
  -> provider router
  -> current best eligible model
  -> bounded attempt
  -> durable event/checkpoint/handoff
```

A provider model is never an agent. NVIDIA or any future provider may add, remove, throttle, rename, or degrade models without changing task identity, permissions, audit history, or agent continuity.

## Stable logical agents

Logical agents are permanent Forge identities. Initial examples:

- `night_owl`
- `crypto_keeper`
- `media_manager`
- `security_monitor`
- `planner`
- `manager`
- `judge`
- `researcher`

Each logical agent needs a versioned manifest with:

- stable `agent_id`
- display name
- domain tags
- requested capabilities
- allowed and prohibited tools
- risk tier and approval policy
- input/output contract
- checkpoint policy
- handoff contract
- acceptance requirements
- active/deprecated state

Provider/model rotation must not change this manifest identity. A replacement runtime model receives only the authority allowed by the logical-agent manifest intersected with task policy.

## Capability-based routing

Agents request capabilities, not model names. Provider-specific model IDs are selected after capability matching.

Initial capability vocabulary:

- `reasoning.high`
- `reasoning.fast`
- `coding`
- `vision`
- `tool_use`
- `image_generation`
- `translation`
- `long_context`

The catalog should distinguish:

- claimed capabilities inferred from provider metadata or model naming
- measured capabilities from probes and benchmarks
- operator-reviewed capabilities
- task-specific requirements

Routing eligibility should require current availability, health freshness, cooldown state, quarantine state, capability match, role policy, and side-effect safety. Ranking can then use existing quality evidence, latency, reliability, and family diversity.

## Provider resilience layer

Provider resilience extends the existing catalog and quality-routing path. It must not create a second catalog.

Required first implementation:

1. Shadow inventory reconciliation against OpenAI-compatible `GET /v1/models`.
2. New models enter quarantine.
3. Missing models require two successful inventory misses before live unavailability.
4. Failed inventory preserves last known routing state.
5. Recovered models return through quarantine/probe.
6. Capacity, timeout, 429, model-not-found, and 5xx failures can fail over to bounded alternate candidates.
7. Authentication, malformed requests, validation failures, policy rejections, local tool failures, and deterministic acceptance failures fail closed.
8. Attempt chains are persisted and queryable.

Shadow mode records observations and proposed changes without changing live route eligibility.

## Durable handoff layer

Provider failover alone is not enough. Forge needs durable task state that survives model loss, process restart, context limits, and worker replacement.

The durable layer should add:

- append-only task/stage events
- materialized checkpoints
- artifact/evidence manifests with hashes
- logical-agent and provider/model attempt records
- leases and heartbeats
- validated handoff envelopes
- side-effect boundary state

Handoff envelopes are generated from committed state, not from hidden model context. A receiver validates the envelope before receiving a lease or tools and returns one of:

- `ACCEPT`
- `REJECT`
- `NEEDS_REPLAN`
- `NEEDS_HUMAN`

## Side-effect safety

The Resilience layer must never duplicate non-idempotent work.

Side-effect boundary states:

- `none`
- `proposed`
- `started`
- `confirmed`
- `unknown`

Rules:

- `none` can resume from the next safe action.
- `proposed` can reconstruct a proposal.
- `started` requires deterministic verification before replay.
- `confirmed` continues after the recorded result.
- `unknown` stops for verification or human review.
- Financial orders, bets, wallet transfers, firewall changes, destructive file operations, merges, and deployments are never replayed automatically.
- Idempotent retries require executor support and a stable idempotency key.

## Rollout order

Use small commits, one logical feature per commit.

1. `Resilience: Architecture review`
   - This document and review output only.
2. `Router: NVIDIA provider rotation`
   - Shadow inventory, quarantine, capability-aware eligibility, failover classifications, CLI status.
3. `Resilience: Worker handoff`
   - Logical-agent registry, manifest validation, handoff envelope validation, read-only CLI.
4. `Resilience: Persistent task journal`
   - Stage ledger, checkpoints, leases, orphan detection in shadow mode.
5. `Forge: Documentation update`
   - Operator docs after implementation behavior exists.

Avatar generation from the package is deferred until local image-generation capacity is available and explicitly gated. It must not block or alter Resilience routing/handoff work.

## Backwards compatibility

Phase 2.2 must remain stable throughout this phase.

- `swarm-personal` remains available through Open WebUI.
- Existing `owui-swarm` commands keep their current behavior.
- Existing config and runtime paths remain valid.
- New timers and services stay disabled by default.
- External provider tests remain opt-in; default tests use fixtures.
- No Plex, qBittorrent, UFW, Fail2ban, crypto exchange, Open WebUI production schedule, or public listener changes are part of this phase.

## Architecture risks to resolve during implementation

- Current routing uses a compact capability model; Resilience needs measured-vs-claimed capability state without overbuilding a parallel catalog.
- Existing run artifacts are file-based; durable leases and queryable handoff chains likely need a small SQLite journal or a focused extension of the current catalog/run persistence.
- Synchronous personal-chat behavior must stay compatible while longer worker+judge attempts and failover chains run.
- Capacity failures currently cool down one exhausted model; broader provider-level circuit breakers need persistence and clear operator visibility.
- Side-effect tests must use fixtures only. Do not touch real media, firewall, trading, scheduler, or external write systems.

## Verification for this review

For this docs-only milestone:

```bash
git diff --check
```

Implementation milestones must also run the relevant Python unit tests and fixture tests before commit.
