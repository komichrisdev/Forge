# Qwen Read Troubleshooting Plan

- Generated: `2026-08-05T17:32:45.813697+00:00`
- Model: `local-qwen36-35b-a3b-windows`

===== DIAGNOSTIC REPORT =====

**Confirmed Root Cause**
Unhandled `BrokenPipeError` (and latent `ConnectionResetError`) in the personal service HTTP handler. When unattended clients (automation scripts, Open WebUI polling, or load balancers) disconnect due to timeout, network blips, or early termination, the server attempts to write the response payload to a closed socket. This raises `BrokenPipeError`, which propagates up the thread stack, prints noisy tracebacks, and can leave the request handler in an inconsistent state, disrupting unattended task completion and client-side retry logic.

**Exact Files & Symbols**
- `swarm_router/personal.py`
- `_write` method (approx. line 1354)
- `self.wfile.write(payload)`

**Minimal Repair**
Wrap the socket write operation in `swarm_router/personal.py`'s `_write` method with a targeted exception handler. This prevents the exception from bubbling up, ensures clean socket closure, and allows the server thread to exit gracefully without masking real failures or flooding logs.

**Focused Tests**
- `tests/test_personal.py`: Add a unit test for `PersonalHandler._write` that mocks `self.wfile.write` to raise `BrokenPipeError` and `ConnectionResetError`. Assert that the method returns cleanly without raising, and that the underlying socket is properly closed.
- Integration: Simulate a client disconnect (e.g., using `requests` with `timeout` or `Connection: close`) during a long-running `/api/runs` POST to verify the service logs a clean exit instead of a traceback.

**Handoff Notes**
- The fix is strictly localized to `swarm_router/personal.py`.
- No changes to `developer.py`, `autopilot_adapter.py`, or orchestration logic are required.
- After applying the patch, monitor `journalctl -u owui-swarm-personal` to confirm `BrokenPipeError` tracebacks cease. Unattended scripts should now receive consistent HTTP status codes or handle timeouts gracefully without server-side thread crashes.
