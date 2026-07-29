# Forge Image Worker

Windows-side helper bundle for the first Forge local image-generation preset.

## Configure

Copy `config.example.json` to a private location such as:

```powershell
$env:LOCALAPPDATA\ForgeImageWorker\config.json
```

Set `comfyui_root`, `python_exe`, `main_py`, `debian_host`, and `debian_user`.
Do not put private SSH keys in this repository.

## Start

```powershell
.\start-forge-image-worker.ps1 -ConfigPath $env:LOCALAPPDATA\ForgeImageWorker\config.json
```

The helper:

- starts ComfyUI on Windows loopback `127.0.0.1:8188` if it is not already listening;
- uses the conservative AMD startup profile from the helper script;
- opens a reverse SSH tunnel so Debian reaches it at `127.0.0.1:18188`;
- writes state under `%LOCALAPPDATA%\ForgeImageWorker`.

The production MVP was validated with a manually launched ComfyUI process at:

```text
C:\AI\LocalGen\app\ComfyUI_windows_portable\ComfyUI\main.py
```

Observed live flags were `--windows-standalone-build --fp32-vae --reserve-vram 1`.
The manual reverse tunnel command was:

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:18188:127.0.0.1:8188 komichris@192.168.2.12
```

## Health

```powershell
.\test-forge-image-worker.ps1 -ConfigPath $env:LOCALAPPDATA\ForgeImageWorker\config.json
```

## Stop

```powershell
.\stop-forge-image-worker.ps1
```

Rollback is just stopping the tunnel and ComfyUI process started by this helper.
If the tunnel was opened manually, close that SSH window instead.
