# Forge Agent Registry

Forge Version: `0.7-dev`
Architecture Revision: `R7`

The Agent Registry is the first implementation of Forge's logical agent system. It defines permanent agent identities separately from providers and models.

## Logical agents

A logical agent is a stable Forge identity with a purpose and authority boundary. Examples:

- `night_owl`
- `crypto_keeper`
- `media_manager`
- `security_monitor`
- `planner`
- `researcher`
- `judge`
- `manager`

Logical agents do not name models. A model is temporary execution capacity selected later by the Router.

```text
Agent
    ↓
Capabilities (future)
    ↓
Router
    ↓
Provider
    ↓
Model
```

This keeps task ownership, audit history, and handoff state stable when provider inventories change.

## Manifest

Each agent manifest contains:

- `agent_id`: permanent lowercase logical identity.
- `display_name`: human-readable name.
- `description`: short responsibility statement.
- `owner`: logical owner or team.
- `version`: manifest contract version.
- `enabled`: whether the agent is available for future routing.
- `supported_task_types`: task categories the agent can accept.
- `preferred_capabilities`: reserved provider-independent capability hints.
- `metadata`: extension object for future fields.

Validation rejects malformed manifests, duplicate agent IDs, empty required fields, invalid agent IDs, non-boolean enabled values, invalid list fields, and non-object metadata.

## Registry

The registry currently provides read-only operations:

- register manifests during registry construction;
- list registered agents;
- look up one agent;
- report registry status;
- validate manifests and registry shape.

This milestone does not migrate live workers, reassign execution, or alter provider/model selection.

## Handoff envelope

A handoff envelope records the intent to transfer task responsibility from one logical agent to another.

Fields:

- `task_id`
- `source_agent`
- `destination_agent`
- `timestamp`
- `reason`
- `context_reference`
- `checkpoint_reference`
- `metadata`

Validation rejects missing required fields, invalid or unknown agent IDs, timestamps without timezone, same-source/destination handoffs, and non-object metadata.

This is only a typed validation layer. It does not replay work, duplicate side effects, or move live execution.

## CLI

Read-only commands:

```bash
owui-swarm status
owui-swarm agent list
owui-swarm agent show planner
owui-swarm agent validate
owui-swarm agent validate ./agent.json
owui-swarm handoff validate ./handoff.json
```

These commands do not require Open WebUI credentials and do not call providers.

## Future scheduler relationship

The Scheduler will use logical agent IDs when deciding who should receive recurring or delayed work. It should not schedule directly against provider/model names.

## Future task journal relationship

The Persistent Task Journal will record logical agent ownership, checkpoints, handoff envelopes, attempt state, and final outcomes. A restarted task should resume from journaled state, not from hidden model memory.

## Compatibility

Existing Phase 2.2 workers, judge configuration, Open WebUI integration, personal backend behavior, provider inventory, and routing remain unchanged.
