[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:StateRoot = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "owui-swarm"
} else {
    Join-Path $env:TEMP "owui-swarm"
}
$script:StatePath = Join-Path $script:StateRoot "swarm-mcp-tunnel.json"

function Fail {
    param([string]$Message)
    throw $Message
}

function Get-ListeningPid {
    param([int]$Port)

    $getNetTcpConnection = Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue
    if ($getNetTcpConnection) {
        $entry = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($entry) {
            return [int]$entry.OwningProcess
        }
    }

    $pattern = "^\s*TCP\s+127\.0\.0\.1:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    $match = & netstat -ano -p tcp 2>$null | Select-String -Pattern $pattern | Select-Object -First 1
    if ($match) {
        return [int]$match.Matches[0].Groups[1].Value
    }

    return $null
}

function Get-ProcessCommandLine {
    param([int]$Pid)

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $Pid" -ErrorAction SilentlyContinue
    if (-not $process) {
        return $null
    }
    return [string]$process.CommandLine
}

function Read-State {
    if (-not (Test-Path -LiteralPath $script:StatePath -PathType Leaf)) {
        return $null
    }
    return Get-Content -LiteralPath $script:StatePath -Raw | ConvertFrom-Json
}

function Remove-State {
    Remove-Item -LiteralPath $script:StatePath -Force -ErrorAction SilentlyContinue
}

function Test-TunnelProcess {
    param(
        [int]$Pid,
        [pscustomobject]$State
    )

    $commandLine = Get-ProcessCommandLine -Pid $Pid
    if (-not $commandLine) {
        return $false
    }
    if ($commandLine -notmatch "(?i)ssh\.exe") {
        return $false
    }
    if ($commandLine -notmatch '\s-N(\s|$)') {
        return $false
    }
    if ($commandLine -notmatch [regex]::Escape("127.0.0.1:$($State.local_port):127.0.0.1:$($State.remote_port)")) {
        return $false
    }
    if ($commandLine -notmatch [regex]::Escape("ExitOnForwardFailure=yes")) {
        return $false
    }
    if ($commandLine -notmatch [regex]::Escape("ServerAliveInterval=")) {
        return $false
    }
    if ($commandLine -notmatch [regex]::Escape("ServerAliveCountMax=")) {
        return $false
    }
    if ($State.identity_file -and $commandLine -notmatch [regex]::Escape([string]$State.identity_file)) {
        return $false
    }
    if ($commandLine -notmatch [regex]::Escape("$($State.ssh_user)@$($State.ssh_host)")) {
        return $false
    }
    return $true
}

function Stop-SwarmMcpTunnel {
    $state = Read-State
    if (-not $state) {
        Fail "No Swarm MCP tunnel state file was found."
    }

    if (-not $state.pid -or -not $state.local_port -or -not $state.remote_port -or -not $state.ssh_host -or -not $state.ssh_user) {
        Fail "Tunnel state file is incomplete."
    }

    $pid = [int]$state.pid
    $listenerPid = Get-ListeningPid -Port ([int]$state.local_port)
    if ($listenerPid -and $listenerPid -ne $pid) {
        Fail "Local port $($state.local_port) is owned by PID $listenerPid, not the tracked tunnel PID $pid."
    }

    if (-not (Test-TunnelProcess -Pid $pid -State $state)) {
        if (-not (Get-ProcessCommandLine -Pid $pid)) {
            Remove-State
            Fail "The tracked tunnel PID $pid no longer exists; stale state removed."
        }
        Fail "The tracked PID $pid does not match the stored SSH tunnel settings."
    }

    Stop-Process -Id $pid -Force -ErrorAction Stop

    $deadline = (Get-Date).AddSeconds(10)
    do {
        if (-not (Get-ProcessCommandLine -Pid $pid)) {
            Remove-State
            Write-Host "Swarm MCP tunnel stopped (PID $pid)."
            return 0
        }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)

    Fail "Stop request sent, but PID $pid is still running."
}

exit (Stop-SwarmMcpTunnel)
