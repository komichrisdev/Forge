[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SshHost,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SshUser,

    [ValidateRange(1, 65535)]
    [int]$LocalPort = 8790,

    [ValidateRange(1, 65535)]
    [int]$RemotePort = 8790,

    [ValidateScript({
        if ([string]::IsNullOrWhiteSpace($_)) {
            return $true
        }
        if (-not (Test-Path -LiteralPath $_ -PathType Leaf)) {
            throw "Identity file not found: $_"
        }
        return $true
    })]
    [string]$IdentityFile,

    [switch]$BatchMode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:StateRoot = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "owui-swarm"
} else {
    Join-Path $env:TEMP "owui-swarm"
}
$script:StatePath = Join-Path $script:StateRoot "swarm-mcp-tunnel.json"
$script:ExpectedTools = @("wiki.search", "wiki.page", "wiki.related", "wiki.status")

function Fail {
    param([string]$Message)
    throw $Message
}

function Resolve-SshExe {
    $candidate = Get-Command ssh.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $candidate) {
        Fail "ssh.exe is not installed or is not on PATH."
    }
    return $candidate.Source
}

function Ensure-StateRoot {
    New-Item -ItemType Directory -Force -Path $script:StateRoot | Out-Null
}

function Resolve-IdentityFile {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }
    return (Resolve-Path -LiteralPath $Path).Path
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

function Test-TunnelProcess {
    param(
        [int]$Pid,
        [int]$LocalPort,
        [int]$RemotePort,
        [string]$SshHost,
        [string]$SshUser,
        [string]$IdentityPath,
        [switch]$BatchMode
    )

    $commandLine = Get-ProcessCommandLine -Pid $Pid
    if (-not $commandLine) {
        return $false
    }

    $forward = [regex]::Escape("127.0.0.1:$LocalPort:127.0.0.1:$RemotePort")
    if ($commandLine -notmatch "(?i)ssh\.exe") {
        return $false
    }
    if ($commandLine -notmatch '\s-N(\s|$)') {
        return $false
    }
    if ($commandLine -notmatch $forward) {
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
    if ($BatchMode.IsPresent -and $commandLine -notmatch [regex]::Escape("BatchMode=yes")) {
        return $false
    }
    if (-not $BatchMode.IsPresent -and $commandLine -match [regex]::Escape("BatchMode=yes")) {
        return $false
    }
    if (-not [string]::IsNullOrWhiteSpace($IdentityPath) -and $commandLine -notmatch [regex]::Escape($IdentityPath)) {
        return $false
    }
    if ($commandLine -notmatch [regex]::Escape("$SshUser@$SshHost")) {
        return $false
    }
    return $true
}

function Invoke-McpRequest {
    param(
        [int]$Port,
        [string]$Method,
        [object]$Params
    )

    $body = @{
        jsonrpc = "2.0"
        id = 1
        method = $Method
        params = $Params
    } | ConvertTo-Json -Depth 12

    Invoke-RestMethod -Uri "http://127.0.0.1:$Port/mcp" -Method Post -ContentType "application/json" -Headers @{
        Accept = "application/json, text/event-stream"
    } -Body $body -ErrorAction Stop
}

function Test-McpTunnel {
    param([int]$Port)

    $initialize = Invoke-McpRequest -Port $Port -Method "initialize" -Params @{
        protocolVersion = "2025-11-25"
        capabilities = @{}
        clientInfo = @{
            name = "owui-swarm-mcp-tunnel"
            version = "1.0.0"
        }
    }
    if (-not $initialize.result.serverInfo.name) {
        Fail "MCP initialize did not return server information."
    }

    $tools = Invoke-McpRequest -Port $Port -Method "tools/list" -Params @{}
    $toolNames = @($tools.result.tools | ForEach-Object { $_.name })
    foreach ($expected in $script:ExpectedTools) {
        if ($toolNames -notcontains $expected) {
            Fail "Missing MCP tool after tunnel startup: $expected"
        }
    }

    return [ordered]@{
        initialize = $initialize.result.serverInfo
        tools = $toolNames
    }
}

function Read-State {
    if (-not (Test-Path -LiteralPath $script:StatePath -PathType Leaf)) {
        return $null
    }
    return Get-Content -LiteralPath $script:StatePath -Raw | ConvertFrom-Json
}

function Write-State {
    param(
        [int]$Pid,
        [string]$IdentityPath
    )

    $state = [ordered]@{
        pid = $Pid
        ssh_host = $SshHost
        ssh_user = $SshUser
        local_port = $LocalPort
        remote_port = $RemotePort
        identity_file = $IdentityPath
        local_url = "http://127.0.0.1:$LocalPort/mcp"
        started_utc = (Get-Date).ToUniversalTime().ToString("o")
    }
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
}

function Remove-State {
    Remove-Item -LiteralPath $script:StatePath -Force -ErrorAction SilentlyContinue
}

function Start-SwarmMcpTunnel {
    Ensure-StateRoot
    $sshExe = Resolve-SshExe
    $resolvedIdentity = Resolve-IdentityFile -Path $IdentityFile

    $existingPid = Get-ListeningPid -Port $LocalPort
    if ($existingPid) {
        if (Test-TunnelProcess -Pid $existingPid -LocalPort $LocalPort -RemotePort $RemotePort -SshHost $SshHost -SshUser $SshUser -IdentityPath $resolvedIdentity -BatchMode:$BatchMode) {
            $verified = Test-McpTunnel -Port $LocalPort
            Write-Host "Swarm MCP tunnel already running on http://127.0.0.1:$LocalPort/mcp (PID $existingPid)."
            Write-Host ("Verified tools: {0}" -f ($verified.tools -join ", "))
            return 0
        }
        Fail "Local port $LocalPort is already occupied by PID $existingPid. Stop that listener before starting the tunnel."
    }

    $stale = Read-State
    if ($stale -and $stale.pid) {
        $stalePid = [int]$stale.pid
        if (-not (Get-ProcessCommandLine -Pid $stalePid)) {
            Remove-State
        }
    }

    $arguments = @(
        "-N",
        "-L", "127.0.0.1:$LocalPort:127.0.0.1:$RemotePort",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3"
    )
    if ($BatchMode.IsPresent) {
        $arguments += @("-o", "BatchMode=yes")
    }
    if ($resolvedIdentity) {
        $arguments += @("-i", $resolvedIdentity)
    }
    $arguments += "$SshUser@$SshHost"

    $process = Start-Process -FilePath $sshExe -ArgumentList $arguments -PassThru -NoNewWindow

    $deadline = (Get-Date).AddSeconds(20)
    do {
        if ($process.HasExited) {
            Fail "ssh.exe exited before the tunnel became ready. Re-run with -BatchMode only after interactive setup succeeds."
        }

        $listenerPid = Get-ListeningPid -Port $LocalPort
        if ($listenerPid -eq $process.Id) {
            $verified = Test-McpTunnel -Port $LocalPort
            Write-State -Pid $process.Id -IdentityPath $resolvedIdentity
            Write-Host "Swarm MCP tunnel ready on http://127.0.0.1:$LocalPort/mcp (PID $($process.Id))."
            Write-Host ("Verified tools: {0}" -f ($verified.tools -join ", "))
            return 0
        }

        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    try {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    } finally {
        Remove-State
    }
    Fail "SSH tunnel did not become ready within 20 seconds."
}

exit (Start-SwarmMcpTunnel)
