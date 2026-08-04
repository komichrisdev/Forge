# Qwen Read Troubleshooting Plan

- Generated: `2026-08-04T01:25:47.729422+00:00`
- Model: `local-qwen36-35b-a3b-windows`

### Confirmed Root Causes

1. **Fatal Module Import Failure**: `swarm_router/developer.py` is truncated mid-function, causing an `ImportError` on every service start. This completely blocks unattended operation and is the direct cause of the `HTTP 502` model failures observed in the shipping loop (the local developer API cannot bind or serve requests).
2. **Worker Thread Crash on Client Disconnect**: `swarm_router/personal.py` lacks socket error handling during response writes. When clients (e.g., Open WebUI or automated schedulers) disconnect before a long-running task completes, the HTTP worker thread crashes, triggering the frequent `systemd` restarts seen in the journal.
3. **Secondary Runtime Failure**: The shipping loop reports `All eligible developer models failed for implementer`. This is a downstream routing/availability issue that will surface once the import crash is resolved, but it is currently masked by the startup failure.

### Exact Files & Symbols

| File | Symbol / Location | Failure Mode |
|------|-------------------|--------------|
| `swarm_router/developer.py` | `_compact_phase_messages` (ends at `groups =`) | `SyntaxError` / `ImportError` halts module load |
| `swarm_router/personal.py` | `_write` (line ~1354, `self.wfile.write(payload)`) | `BrokenPipeError` crashes `ThreadingHTTPServer` worker threads |
| `swarm_router/autopilot_adapter.py` | `SwarmAutopilotAdapter.run_task` | Returns `SwarmAdapterError` on `HTTP 502` (cascade from above) |

### Minimal Repair Handoff

1. **Restore `swarm_router/developer.py`**: Locate the missing implementation for `_compact_phase_messages`. The function must compute `groups` (likely tracking valid assistant/tool-call pairs for token budgeting) and return the tuple `(messages, provenance)` as declared in the signature. Ensure the function body is complete and syntactically valid before committing.
2. **Graceful Socket Handling in `swarm_router/personal.py`**: Wrap `self.wfile.write(payload)` inside `_write` with a `try/except BrokenPipeError` block. On capture, log a warning and return early to prevent worker thread termination. This stabilizes the service during unattended client timeouts.
3. **Verify Local API Binding**: After fixing the import, confirm `http://127.0.0.1:8788/v1/chat/completions` responds with `200 OK` before re-running the shipping loop. The `HTTP 502` will resolve once the developer coordinator loads successfully.

### Focused Tests

1. **Import Sanity Check**: Run `python -c "from swarm_router.developer import DeveloperCoordinator, _compact_phase_messages"` to confirm zero syntax/import errors.
2. **Thread Resilience Test**: Simulate a client disconnect during a mocked `/api/personal-tasks` POST. Verify the server process remains alive, logs a `BrokenPipeError` warning, and continues accepting new requests without `systemd` restarts.
3. **Protocol Cleanup Validation**: Execute `tests/test_developer.py` to ensure `_compact_phase_messages` correctly prunes orphaned tool responses, preserves handoff provenance, and respects `input_limit` constraints.
4. **Shipping Loop Re-entry**: Trigger a single `run_task` cycle with a mocked healthy developer model. Confirm the loop progresses through `planner -> implementer -> reviewer -> verifier` without `SwarmAdapterError` or `HTTP 502` cascades.
