[CmdletBinding()]
param(
    [string]$ConfigPath = "$env:LOCALAPPDATA\ForgeImageWorker\config.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw "Config file not found: $ConfigPath" }
$cfg = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$stats = Invoke-RestMethod -Uri "http://$($cfg.comfyui_host):$($cfg.comfyui_port)/system_stats" -TimeoutSec 5
$queue = Invoke-RestMethod -Uri "http://$($cfg.comfyui_host):$($cfg.comfyui_port)/queue" -TimeoutSec 5
[ordered]@{
    comfyui = "ready"
    windows_url = "http://$($cfg.comfyui_host):$($cfg.comfyui_port)"
    debian_url = "http://$($cfg.debian_bind):$($cfg.debian_port)"
    queue_running = @($queue.queue_running).Count
    queue_pending = @($queue.queue_pending).Count
    system = $stats.system
} | ConvertTo-Json -Depth 8
