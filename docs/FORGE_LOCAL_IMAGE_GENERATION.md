# Forge Local Image Generation

Forge Version: 0.12-dev
Architecture Revision: R12

Status: production MVP validated on July 28, 2026 with one CLI generation and one dashboard generation through the reverse SSH tunnel.

## Discovered LocalGen State

Debian host:

- Forge repository: `/home/komichris/openwebui-codex-swarm`
- Debian LAN IP: `192.168.2.12`
- Dashboard: `127.0.0.1:8787` and `192.168.2.12:8787`
- Personal backend: `127.0.0.1:8788`
- MCP endpoint: `127.0.0.1:8790`
- ComfyUI tunnel listener: `127.0.0.1:18188`

Windows image host:

- ComfyUI install path: `C:\AI\LocalGen\app\ComfyUI_windows_portable\ComfyUI\main.py`
- ComfyUI version: `0.28.0`
- Python: `3.12.10`
- PyTorch: `2.9.1+rocm7.2.1`
- Deployment: local portable
- GPU: AMD Radeon RX 9070 XT
- VRAM detected by ComfyUI: `17095983104` bytes
- System RAM detected by ComfyUI: `34268721152` bytes
- ComfyUI remains bound to Windows loopback on `127.0.0.1:8188`
- Startup flags observed in `/system_stats`: `--windows-standalone-build --fp32-vae --reserve-vram 1`

Existing repository material:

- Windows-to-Debian MCP tunnel docs and scripts exist under `docs/windows/`.
- Forge image-worker companion scripts are under `windows/forge-image-worker/`.
- The ComfyUI reverse tunnel is currently manual for the MVP.

Known background from the owner:

- FLUX.1 Schnell FP8 validated
- 768x768 daily preset validated and exercised through Forge
- 1024x1024 high-memory preset validated with operational limits
- Z-Image-Turbo and Animagine XL4 installed but deferred
- Earlier stable flags included `--disable-pinned-memory`, `--disable-async-offload`, and `--disable-dynamic-vram`; they were not present in the observed production process.

## Connection Design

Selected design: Windows-initiated reverse SSH tunnel.

```text
Forge on Debian
http://127.0.0.1:18188
    |
    | reverse SSH tunnel, authenticated by SSH
    v
Windows ComfyUI
http://127.0.0.1:8188
```

ComfyUI must not be exposed on the public internet. Router port forwarding is not used. Direct LAN access to raw ComfyUI is not required for the selected design.

Verified tunnel command:

```bash
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:18188:127.0.0.1:8188 komichris@192.168.2.12
```

The Debian listener was verified on `127.0.0.1:18188` only. Raw ComfyUI was not exposed on a LAN listener, public DNS, router port forward, or public reverse proxy.

Windows helper scripts live in:

```text
windows/forge-image-worker/
```

They contain templates only, no secrets or private keys.

## Approved Preset

Only one preset is implemented:

- preset ID: `flux-schnell-768-daily`
- model: FLUX.1 Schnell FP8
- checkpoint name in template: `flux1-schnell-fp8.safetensors`
- size: 768x768
- sampler: `euler`
- scheduler: `simple`
- steps: 4
- CFG: 1.0
- images: 1
- output filename prefix: `forge_flux_schnell_768`
- required nodes: `CheckpointLoaderSimple`, `EmptySD3LatentImage`, `KSampler`, `CLIPTextEncode`, `VAEDecode`, `SaveImage`

The dashboard and CLI accept only:

- `preset_id`
- `prompt`
- `negative_prompt`
- `seed`
- `notification_requested`

Unknown fields, URLs, paths, workflow JSON, arbitrary models, output paths, and script values are rejected.

## Task Flow

```text
Dashboard or CLI
  -> personal task backend
  -> image_generate task
  -> image_generator logical agent
  -> ComfyUI health check
  -> fixed workflow submission
  -> progress checkpoints
  -> artifact storage
  -> optional Discord completion notification
```

Workflow submission is recorded as an external side effect:

- `proposed` before submission
- `started` immediately before HTTP POST
- `confirmed` after ComfyUI returns a prompt ID
- `unknown` if transmission may have happened without confirmation

Unknown submissions are not automatically retried.

## Artifacts

Artifacts are stored outside SQLite:

```text
~/.local/share/owui-swarm/artifacts/images/<forge-task-id>/
    output.png
    metadata.json
    thumbnail.webp
```

Metadata records:

- Forge task ID
- ComfyUI prompt ID
- preset ID
- seed
- dimensions when available from the image header
- creation timestamp
- SHA-256 checksum

Only indexed artifact directories are served by the dashboard. Arbitrary filesystem browsing is not implemented.

## Dashboard

The Forge LAN dashboard adds an `Image Generation` page with:

- ComfyUI connection state
- queue depth
- fixed preset display
- prompt form
- optional negative prompt
- optional seed
- Discord notification checkbox
- explicit confirmation: `generate image`
- recent image tasks
- minimal gallery
- authenticated original-image and thumbnail routes

The page uses the existing authenticated session and CSRF protection. Dashboard route handlers submit normal Forge tasks and do not invoke ComfyUI directly.

## CLI

```bash
owui-swarm image status
owui-swarm image presets
owui-swarm image generate --prompt "neutral validation image" --confirm "generate image"
owui-swarm image jobs
owui-swarm image show FT-YYYYMMDD-NNNNNN
```

`--json` is supported for all image subcommands.

## Discord

When `notification_requested` is true, Forge sends one concise Discord message on completion or failure through the existing notification store.

Messages include:

- Forge task ID
- preset ID
- seed on success
- duration on success
- sanitized failure category on failure

Prompt text, webhook URLs, local filesystem paths, and repeated progress notifications are not sent.

## Windows Setup

1. Copy `windows/forge-image-worker/config.example.json` to a private Windows path.
2. Fill in the real ComfyUI paths and Debian SSH target.
3. Start the helper:

```powershell
.\start-forge-image-worker.ps1 -ConfigPath $env:LOCALAPPDATA\ForgeImageWorker\config.json
```

4. From Debian, verify:

```bash
owui-swarm image status
```

The expected healthy Forge URL is `http://127.0.0.1:18188`.

For the validated MVP, the owner may keep the tunnel open manually. Automatic Windows Task Scheduler startup is documented in the companion bundle but was not required for the production validation.

## Production Validation

- CLI generation: `FT-20260728-000007`, personal task `task-03931e74753c438d`, ComfyUI prompt `0bf93681-2b0f-41f1-8149-5f8b7a3cdb88`, seed `42012001`, checksum `50b52e562bd0f62dc97353814e6fb4ed7d4d4d7997a1951efa8bf501127af34a`.
- Dashboard generation: `FT-20260728-000008`, personal task `task-f12ea2d247dc494f`, ComfyUI prompt `6c61fd95-921b-42a8-8912-198cdd77027d`, seed `42012002`, checksum `2dac4b3481003e7eb904b08a86725f074a705cf566a451803e283b29d8c1cc28`.
- Artifact directories: `~/.local/share/owui-swarm/artifacts/images/FT-20260728-000007/` and `~/.local/share/owui-swarm/artifacts/images/FT-20260728-000008/`.
- Both outputs were validated as 768x768 PNG files with real WebP thumbnails.
- Dashboard artifact routes returned `401` without authentication and `404` for traversal or non-indexed task paths.
- Discord notification `FN-20260728-000004` was confirmed for the dashboard generation and duplicate suppression returned the same confirmed record.

## Restart

Debian:

```bash
systemctl --user restart owui-swarm-personal.service
systemctl --user restart owui-swarm-dashboard.service
systemctl --user restart forge-scheduler.service
```

Windows:

```powershell
.\stop-forge-image-worker.ps1
.\start-forge-image-worker.ps1 -ConfigPath $env:LOCALAPPDATA\ForgeImageWorker\config.json
```

Completed artifacts remain indexed by their metadata files. Prior unknown submissions are not replayed.

## Rollback

1. Stop the Windows helper.
2. If the tunnel is manual, close the Windows SSH window running the reverse tunnel.
3. Keep ComfyUI bound to Windows loopback.
4. Remove or ignore the `[image_generation]` config section.
5. Restart Forge personal and dashboard services.
6. Existing artifact files may remain; they are inert without dashboard links.

## Failure States

Forge classifies failures as:

- Windows offline
- tunnel unavailable
- ComfyUI unavailable
- ComfyUI starting
- workflow invalid
- model missing
- queue rejected
- generation timeout
- GPU or driver failure
- out of memory
- malformed output
- artifact copy failure
- notification failure

Current implementation maps the directly observable HTTP/client errors to stable categories and leaves GPU-specific categories for ComfyUI error text classification after live validation.

## Deferred

Deferred work:

- 1024x1024 generation
- batch generation
- arbitrary workflow upload
- arbitrary model selection
- Animagine XL4
- Z-Image-Turbo
- LoRA selection
- ControlNet
- inpainting
- image-to-image
- upscaling
- public sharing
- inbound Discord control
- automatic GPU retry

The known 1024x1024 memory limitation remains deferred.
