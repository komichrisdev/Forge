[CmdletBinding()]
param(
    [string]$ConfigPath = "$env:LOCALAPPDATA\ForgeImageWorker\config.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$StateRoot = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "ForgeImageWorker" } else { Join-Path $env:TEMP "ForgeImageWorker" }
$StatePath = Join-Path $StateRoot "state.json"

function Fail([string]$Message) { throw $Message }

function Load-Config {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { Fail "Config file not found: $ConfigPath" }
    Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
}

function Test-Port([string]$HostName, [int]$Port) {
    try {
        $client = [Net.Sockets.TcpClient]::new()
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(1000)) { $client.Close(); return $false }
        $client.EndConnect($async)
        $client.Close()
        return $true
    } catch { return $false }
}

function Resolve-SshExe {
    $ssh = Get-Command ssh.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ssh) { Fail "ssh.exe is not installed or on PATH." }
    $ssh.Source
}

$cfg = Load-Config
New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null

if (-not (Test-Port $cfg.comfyui_host ([int]$cfg.comfyui_port))) {
    foreach ($path in @($cfg.comfyui_root, $cfg.python_exe, $cfg.main_py)) {
        if (-not (Test-Path -LiteralPath $path)) { Fail "Required ComfyUI path is missing: $path" }
    }
    $args = @(
        $cfg.main_py,
        "--listen", $cfg.comfyui_host,
        "--port", [string]$cfg.comfyui_port,
        "--fp32-vae",
        "--disable-pinned-memory",
        "--disable-async-offload",
        "--disable-dynamic-vram",
        "--reserve-vram", "1"
    )
    $stdout = Join-Path $StateRoot "comfyui.out.log"
    $stderr = Join-Path $StateRoot "comfyui.err.log"
    $comfy = Start-Process -FilePath $cfg.python_exe -ArgumentList $args -WorkingDirectory $cfg.comfyui_root -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
} else {
    $comfy = $null
}

$deadline = (Get-Date).AddSeconds(60)
while (-not (Test-Port $cfg.comfyui_host ([int]$cfg.comfyui_port))) {
    if ((Get-Date) -gt $deadline) { Fail "ComfyUI did not become ready." }
    Start-Sleep -Milliseconds 500
}

$sshArgs = @(
    "-N",
    "-R", "$($cfg.debian_bind):$($cfg.debian_port):$($cfg.comfyui_host):$($cfg.comfyui_port)",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3"
)
if ($cfg.batch_mode) { $sshArgs += @("-o", "BatchMode=yes") }
if ($cfg.identity_file) { $sshArgs += @("-i", [string]$cfg.identity_file) }
$sshArgs += "$($cfg.debian_user)@$($cfg.debian_host)"

$ssh = Start-Process -FilePath (Resolve-SshExe) -ArgumentList $sshArgs -PassThru
Start-Sleep -Seconds 2
if ($ssh.HasExited) { Fail "SSH tunnel exited before it became ready." }

$state = [ordered]@{
    comfyui_pid = if ($comfy) { $comfy.Id } else { 0 }
    ssh_pid = $ssh.Id
    debian_url = "http://$($cfg.debian_bind):$($cfg.debian_port)"
    windows_url = "http://$($cfg.comfyui_host):$($cfg.comfyui_port)"
    started_utc = (Get-Date).ToUniversalTime().ToString("o")
}
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatePath -Encoding UTF8
Write-Host "Forge image worker ready. Debian URL: $($state.debian_url)"
