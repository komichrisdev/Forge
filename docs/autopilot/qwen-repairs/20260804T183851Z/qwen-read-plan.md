# Qwen Read Troubleshooting Plan

- Generated: `2026-08-04T18:46:09.706878+00:00`
- Model: `local-qwen36-35b-a3b-windows`

### Confirmed Root Causes
1. **Module Import Failure (SyntaxError)**: `swarm_router/developer.py` is truncated mid-statement inside `_compact_phase_messages`. This prevents the entire `developer` module from loading, which cascades to `personal.py` and crashes the unattended task scheduler.
2. **Worker Thread Crash on Client Disconnect**: `swarm_router/personal.py` repeatedly throws `BrokenPipeError` during response writes. The HTTP request handler lacks graceful socket error handling, causing worker threads to terminate unexpectedly and degrading service stability during unattended operation.
3. **Hard Failure in Model Routing**: The shipping loop shows `SwarmAdapterError: All eligible developer models failed for implementer.` The model eligibility or fallback routing logic is rejecting all available models for the `implementer` role, causing unattended tasks to fail closed instead of retrying or falling back.

### Exact Files & Symbols
- `swarm_router/developer.py`: `_compact_phase_messages` function, truncated at `groups =`
- `swarm_router/personal.py`: `PersonalHandler._write` method (line ~1354), `self.wfile.write(payload)`
- `swarm_router/developer.py` / `swarm_router/orchestrator.py`: Model eligibility/routing logic for the `implementer` role (triggering `SwarmAdapterError` on `FG-020`)

### Minimal Repair Handoff
1. **Restore `developer.py` Syntax**: Locate the truncated `_compact_phase_messages` function. Reconstruct the `groups =` assignment to properly map tool-call IDs to their execution indices or phase groups. Ensure the function returns a valid tuple `(list[dict[str, Any]], dict[str, Any])` and that all downstream references to `groups` are intact.
2. **Graceful Socket Handling in `personal.py`**: Wrap the `self.wfile.write(payload)` call in `_write` with a `try...except (BrokenPipeError, ConnectionResetError)` block. On catch, log a low-level debug message and return early to prevent thread termination.
3. **Fix Implementer Model Routing**: Audit the model catalog health checks and role-scoring logic. Ensure that at least one model marked for the `implementer` role passes the eligibility threshold. If all models are temporarily unhealthy, implement a deterministic fallback or explicit retry with backoff instead of raising a hard `SwarmAdapterError`.

### Focused Tests
1. **Import & Syntax Validation**: Run `python -c "from swarm_router.developer import DeveloperCoordinator"` to confirm the module loads without `SyntaxError` or `NameError`.
2. **Phase Compaction Unit Test**: Call `_compact_phase_messages` with a realistic sequence of system, handoff, worker, and control messages. Assert it returns a bounded message list and a provenance dictionary without raising exceptions.
3. **BrokenPipe Resilience Test**: Mock `self.wfile.write` to raise `BrokenPipeError` during a simulated `POST /api/personal-tasks` request. Verify the handler catches the exception, logs gracefully, and returns a clean HTTP 499/500 without crashing the `ThreadingHTTPServer` worker thread.
4. **Implementer Routing Integration Test**: Mock `ModelCatalog` to return a healthy implementer-capable model. Dispatch a task through `SwarmAutopilotAdapter.run_task` and verify it successfully routes to the implementer phase without raising `SwarmAdapterError`. Add a negative test where all implementer models are marked unhealthy to confirm graceful fallback or explicit retry behavior.
