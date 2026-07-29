# Forge Architecture Overview

Forge Version: `0.12-dev`
Architecture Revision: `R12`

This is the starting point for developers new to Forge. It describes the platform shape and the intended boundaries between agents, providers, routing, execution, and future resilience work. It deliberately avoids implementation details.

## Vision

Forge is a provider-agnostic AI orchestration platform. Its job is to run useful long-lived AI automation without binding that automation to one vendor, one model, or one UI.

The core idea is simple: agents are stable identities; providers and models are interchangeable execution capacity.

Forge is designed for:

- stable logical agents with durable responsibilities;
- interchangeable cloud and local providers;
- resilient execution when models disappear, throttle, fail, or degrade;
- long-running automation that can pause, resume, retry, and hand off safely;
- local-first operation with private state under the user's control;
- cloud-capable expansion when remote providers add useful capacity.

## Core Architecture

```text
Forge
├── Core
│   ├── Router
│   ├── Scheduler
│   ├── Journal
│   ├── Memory
│   ├── Agent Registry
│   ├── Tool Runtime
│   └── Notifications
├── Providers
│   ├── NVIDIA
│   ├── OpenAI
│   ├── Anthropic
│   ├── Ollama
│   └── Future Providers
├── Agents
│   ├── Night Owl
│   ├── Crypto Keeper
│   ├── Media Manager
│   ├── Security Monitor
│   ├── Planner
│   ├── Judge
│   └── Future Agents
├── Dashboard
├── Local Image Generation
├── CLI
└── Open WebUI
```

### Core

Core contains the platform services that should remain stable as providers and agents change.

- Router: tracks provider inventories, model health, cooldowns, and eventually matches requested capabilities to eligible providers and models.
- Scheduler: stores local automation schedules, creates due personal-task occurrences, and keeps timers explicit and controlled. Scheduling does not execute external side effects directly.
- Journal: stores durable task lifecycle events, checkpoints, leases, handoff state, and recovery metadata.
- Memory: stores project knowledge, retrieved context, and summaries that agents can use without depending on hidden model state.
- Agent Registry: will define stable logical agents, their required capabilities, authority boundaries, and handoff contracts.
- Tool Runtime: will mediate access to external actions. It should enforce permissions, idempotency, auditability, and fail-safe behavior.
- Notifications: sends outbound operational messages through Forge-owned delivery records and deduplication. Discord is the first notifier. See [FORGE_DISCORD_NOTIFICATIONS.md](FORGE_DISCORD_NOTIFICATIONS.md).
- Dashboard: provides the owner-operated LAN operations surface for health, task history, schedules, Night Owl, notifications, agents, providers, and approved manual dispatch. See [FORGE_LAN_DASHBOARD.md](FORGE_LAN_DASHBOARD.md).
- Local Image Generation: submits one approved Forge task type to a fixed Windows ComfyUI preset through a reverse SSH tunnel, stores image artifacts outside SQLite, and exposes only indexed artifacts. See [FORGE_LOCAL_IMAGE_GENERATION.md](FORGE_LOCAL_IMAGE_GENERATION.md).

### Providers

Providers expose model inventory and inference capacity. A provider is not an agent. NVIDIA is the first provider integrated into the Forge Router model, but the architecture expects OpenAI, Anthropic, Ollama, and future providers to follow the same conceptual contract:

1. expose inventory;
2. describe models with provider-independent metadata;
3. report or infer capabilities;
4. track health and cooldown;
5. allow the Router to select only currently eligible models.

### Agents

Agents are permanent Forge identities with stable responsibilities. Examples include Night Owl, Crypto Keeper, Media Manager, Security Monitor, Planner, and Judge.

An agent describes what it needs and what it may do. It does not own a provider or model name. If a model disappears, the agent identity remains intact and the Router can later choose a different eligible model.

### Dashboard

The Dashboard is the local operator view. It should make routing state, task state, run history, model health, and recovery status visible without exposing secrets.

### CLI

The CLI is the operator control surface for explicit actions: diagnostics, provider refreshes, probes, cooldowns, task execution, wiki operations, and future journal/scheduler inspection.

### Open WebUI

Open WebUI is the user-facing chat surface and model gateway. Forge integrates with it, but Forge should not be architecturally dependent on Open WebUI as the only UI or provider path.

## Logical Agent Model

```text
Logical Agent
    ↓
Required Capabilities
    ↓
Forge Router
    ↓
Provider
    ↓
Model
```

A logical agent is a durable identity such as `Night Owl`, `Crypto Keeper`, `Media Manager`, `Security Monitor`, `Planner`, or `Judge`.

Logical agents never depend on model names because model names are operational details. Providers rename models, remove models, throttle traffic, introduce better replacements, and change capabilities over time. Tying an agent to a model would make identity, permissions, audit history, and task continuity fragile.

Instead, each logical agent should declare:

- its stable identity and purpose;
- required capabilities;
- allowed tools;
- prohibited actions;
- risk tier;
- handoff expectations;
- acceptance criteria.

The Router then maps those requirements to the best currently eligible execution option.

## Provider Model

```text
Provider
    ↓
Inventory
    ↓
Models
    ↓
Capabilities
    ↓
Health
    ↓
Cooldown
    ↓
Selection
```

A provider contributes model inventory. Each model is represented with provider-independent metadata such as capabilities, context length, streaming/tool/image support, health, cooldown, and inventory revision.

Inventory reconciliation is conservative:

- a successful refresh creates a new inventory revision;
- newly discovered models enter quarantine until validated;
- recovered models also re-enter quarantine;
- one successful inventory miss records uncertainty but preserves last-known-good availability;
- two consecutive successful misses can make a model unavailable;
- a failed refresh records provider health but does not delete or disable known-good models.

This protects Forge from transient provider outages and unstable provider catalogs.

## Task Lifecycle

The intended task lifecycle is:

```text
Task Created
    ↓
Scheduled
    ↓
Assigned
    ↓
Running
    ↓
Checkpoint
    ↓
Completed
```

Failure follows a recoverable path when safe:

```text
Failed
    ↓
Recoverable
    ↓
Retry
```

The Persistent Task Journal is the durable record for this lifecycle. It stores task creation, scheduler metadata, agent assignment, execution attempts, checkpoints, handoff envelopes, failure categories, retry decisions, and final outcomes.

The Journal should make recovery possible without trusting hidden model context. A restarted or replacement worker should resume from recorded state, not from memory that only existed inside a previous model response.

## Future Capability Routing

The long-term routing model is capability-based.

Models advertise capabilities:

- `reasoning.high`
- `reasoning.fast`
- `coding`
- `vision`
- `tool_use`
- `image_generation`
- `translation`
- `long_context`

Agents request capabilities. The Router matches those requests to eligible models using provider inventory, health, cooldown, quarantine state, quality evidence, latency, cost hints, and safety policy.

This keeps routing provider-independent and model-independent. A future provider can add useful capacity without requiring agent rewrites.

## Design Principles

- Provider agnostic: providers are interchangeable capacity, not platform identity.
- Model agnostic: model IDs are selected at runtime and should not define agent behavior.
- Stable agent identities: agents keep durable names, permissions, history, and responsibilities.
- Incremental development: add one architectural layer at a time.
- Small Git commits: one logical feature per commit.
- Test-driven changes: meaningful behavior changes require automated checks.
- Backward compatibility: stable deployments must keep working during Resilience work.
- Local-first operation: private state, operator control, and safe defaults belong locally.
- Cloud augmentation: cloud providers can add capacity without owning the system.
- Fail safe by default: uncertain state should pause, quarantine, or require validation instead of guessing.

## Roadmap

Near-term milestones:

- Worker handoff: define and validate logical-agent handoff envelopes. See [FORGE_AGENT_REGISTRY.md](FORGE_AGENT_REGISTRY.md).
- Persistent journal: persist lifecycle events, checkpoints, leases, and recovery state. See [FORGE_TASK_JOURNAL.md](FORGE_TASK_JOURNAL.md).
- Capability routing: let agents request provider-independent capabilities instead of model names.
- Scheduler: implemented as controlled local recurring and delayed execution. See [FORGE_SCHEDULER.md](FORGE_SCHEDULER.md).
- Night Owl migration: Night Owl now runs as the first enabled Forge scheduler workload. See [FORGE_NIGHT_OWL.md](FORGE_NIGHT_OWL.md).
- Additional providers: add OpenAI, Anthropic, Ollama, and other providers through the same provider model.
- Avatar pipeline: add visual identity assets without coupling them to execution models.
- Windows desktop integration: support local desktop workflows while preserving Forge authority and safety boundaries.
