# Forge Router Provider Rotation

Forge Version: `0.3-dev`
Architecture Revision: `R3`

This milestone adds the first provider-agnostic Forge Router inventory layer. NVIDIA is the first configured provider, but the catalog stores generic provider/model metadata so future providers can use the same reconciliation path.

## Model identity

Logical Forge agents remain stable identities. They do not name providers or models.

```text
Logical Agent -> Required Capabilities -> Forge Router -> Selected Provider -> Current Model
```

This milestone only builds the inventory data needed for that future capability routing. It does not change routing policy to select by capability requests yet.

## Inventory rules

- Successful refreshes create a provider inventory revision.
- New models are quarantined and unavailable until a successful probe.
- Recovered models also re-enter quarantine before routing.
- A single successful inventory miss marks a model missing but keeps last-known-good availability.
- A second consecutive successful miss makes the model unavailable in live mode.
- Failed inventory refreshes record provider health/error and preserve the last-known-good model state.
- Provider cooldown is persisted separately from model cooldown.

## Generic model metadata

Catalog rows now track:

- `provider_id`
- `model_id`
- `display_name`
- provider-independent `capabilities`
- `context_length`
- tool, streaming, image, and reasoning support hints
- cost and latency hints
- health and cooldown state
- inventory revision and consecutive present/missing counters

Capabilities stay provider-independent: `coding`, `reasoning.high`, `reasoning.fast`, `vision`, `tool_use`, `image_generation`, `translation`, and `long_context`.

## CLI

Timers remain disabled. Commands are operator-invoked:

```bash
owui-swarm provider --provider-id nvidia status
owui-swarm provider --provider-id nvidia refresh --mode shadow
owui-swarm provider --provider-id nvidia refresh --mode live
owui-swarm provider --provider-id nvidia diff
owui-swarm provider --provider-id nvidia probe --new-and-recovered
owui-swarm provider --provider-id nvidia cooldown --minutes 30
owui-swarm provider --provider-id nvidia cooldown --clear
```

Unit tests use fake provider inventory data only. Live provider API checks remain manual/operator actions.
