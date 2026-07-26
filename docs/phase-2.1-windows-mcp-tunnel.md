# Phase 2.1 — Windows SSH Tunnel for Swarm MCP

Date: 2026-07-26

## Outcome

This phase defines a Windows-to-Debian SSH local forward for the existing
loopback-only MCP endpoint:

```text
http://127.0.0.1:8790/mcp
```

The Debian service remains bound to `127.0.0.1:8790`. The Windows helper binds
its forwarded port only to `127.0.0.1` as well. No LAN-facing MCP listener is
introduced.

## Architecture

The tunnel is a plain SSH local forward:

```text
ssh -N -L 127.0.0.1:8790:127.0.0.1:8790 <debian-user>@<server-lan-host>
```

The PowerShell helper wraps that command with:

- explicit loopback binding on the Windows side
- `ExitOnForwardFailure=yes`
- `ServerAliveInterval`
- `ServerAliveCountMax`
- optional `BatchMode=yes`
- optional `-i <identity-file>`
- port-occupancy checks that avoid killing unrelated listeners
- state tracking so only the helper-owned tunnel can be stopped
- a local MCP probe after the tunnel comes up

## Security model

- The tunnel is read-only access to the existing MCP endpoint.
- Debian keeps MCP on `127.0.0.1:8790`.
- Windows keeps the forwarded port on `127.0.0.1:8790`.
- No direct LAN exposure is enabled.
- No reverse proxy, firewall rule, Docker change, or systemd change is required.
- The helper stores no credentials.
- Private keys, tokens, and passwords must stay outside the repository.
- Cursor and Codex client configuration are separate later phases.

## Prerequisites

- Debian MCP is already running and healthy.
- Windows has an SSH client installed.
- The Debian user account and host name are known.
- SSH key authentication is recommended.
- Password authentication may still work for first-time setup when `-BatchMode`
  is not used, but credentials are never stored in the repository.

## Windows OpenSSH Client verification

On Windows:

```powershell
ssh -V
Get-Command ssh.exe
```

The helper checks `ssh.exe` itself before starting the tunnel.

## Manual tunnel command

```powershell
ssh -N -L 127.0.0.1:8790:127.0.0.1:8790 <debian-user>@<server-lan-host>
```

Recommended hardening for repeat use:

```powershell
ssh -N `
  -L 127.0.0.1:8790:127.0.0.1:8790 `
  -o ExitOnForwardFailure=yes `
  -o ServerAliveInterval=30 `
  -o ServerAliveCountMax=3 `
  <debian-user>@<server-lan-host>
```

## PowerShell helper

Start:

```powershell
docs/windows/start-swarm-mcp-tunnel.ps1 `
  -SshHost <server-lan-host> `
  -SshUser <debian-user>
```

With an identity file:

```powershell
docs/windows/start-swarm-mcp-tunnel.ps1 `
  -SshHost <server-lan-host> `
  -SshUser <debian-user> `
  -IdentityFile $env:USERPROFILE\.ssh\id_ed25519
```

Batch mode for a known-good key:

```powershell
docs/windows/start-swarm-mcp-tunnel.ps1 `
  -SshHost <server-lan-host> `
  -SshUser <debian-user> `
  -BatchMode
```

Stop:

```powershell
docs/windows/stop-swarm-mcp-tunnel.ps1
```

## Expected local endpoint

After the tunnel starts, the Windows client should use:

```text
http://127.0.0.1:8790/mcp
```

That endpoint is a loopback listener on Windows, not a public service.

## MCP verification

The helper probes the local endpoint after the tunnel is established and checks
for the read-only wiki tools:

- `wiki.search`
- `wiki.page`
- `wiki.related`
- `wiki.status`

The probe is a normal MCP JSON-RPC request to the loopback endpoint. It does
not mutate the wiki, the SQLite index, or the Debian service state.

## Health verification

Successful startup means:

- SSH created a loopback listener on Windows at `127.0.0.1:8790`
- the Debian MCP endpoint responded
- the live tool list included the four wiki tools above
- the helper wrote tunnel state for later shutdown

## Tool-list verification

The helper verifies the live tool list where practical. If the tunnel is up but
the tool list does not include the four wiki tools, startup fails.

## Clean shutdown

Use the companion stop script. It reads the helper-owned state file and stops
only the tracked SSH process. It does not kill arbitrary `ssh.exe` processes.

## Rollback

Rollback is straightforward:

1. stop the tunnel with `docs/windows/stop-swarm-mcp-tunnel.ps1`
2. close any Windows app that was using `http://127.0.0.1:8790/mcp`
3. leave Debian MCP on loopback only
4. do not change the Debian service, firewall, or reverse proxy

If the helper created a stale state file, the stop script removes only that
state when it can prove the tunnel process is gone.

## Troubleshooting

- Local port already occupied: another process is already bound to
  `127.0.0.1:8790`. Stop the other listener or choose a different local port.
- SSH authentication failure: re-check the host name, user name, and key
  permissions; the helper does not store secrets.
- Host-key confirmation: accept the host key only if you trust the Debian host.
- Changed host key: treat it as a security event and verify the server before
  reconnecting.
- Unreachable Debian server: confirm SSH connectivity to the server LAN host.
- Tunnel starts but MCP is unavailable: confirm Debian MCP is still healthy on
  `127.0.0.1:8790`.
- Tunnel drops during use: rerun the start script or investigate the SSH
  connection quality.
- Stale PID or state file: run the stop script; it removes stale helper-owned
  state when the process is already gone.
- Another SSH tunnel already using the port: stop the existing helper-owned
  tunnel first, or choose another local port.
- Windows Firewall prompts: the helper does not create an inbound LAN listener;
  only the local loopback port is used.

## Explicit boundaries

- Direct LAN exposure is not enabled.
- Port `8790` remains loopback-only on Debian.
- The helper contains no credentials.
- Users must not commit private keys or tokens.
- Cursor and Codex client configuration has not begun.

