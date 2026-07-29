[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$StateRoot = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "ForgeImageWorker" } else { Join-Path $env:TEMP "ForgeImageWorker" }
$StatePath = Join-Path $StateRoot "state.json"
if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { throw "No Forge image worker state file found." }
$state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
foreach ($pid in @($state.ssh_pid, $state.comfyui_pid)) {
    if ($pid -and [int]$pid -gt 0) {
        Stop-Process -Id ([int]$pid) -Force -ErrorAction SilentlyContinue
    }
}
Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
Write-Host "Forge image worker stopped."
