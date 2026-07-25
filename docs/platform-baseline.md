# Swarm Platform V2 — Phase 0.1 Platform Baseline

Observed: 2026-07-25, America/New_York  
Host: `komiplex`  
Scope: read-only audit of the existing Swarm, Open WebUI, and relevant host state.  
Only this report and its parent `docs/` directory were created. No Git repository, package, configuration, database, service, container, firewall rule, bind address, or permission was changed. No service was restarted or reloaded.

## 1. Executive summary

The working Swarm source is:

`/home/komichris/openwebui-codex-swarm`

It is a complete, locally developed application tree, not a deployment-only directory. It contains a Python package, a TypeScript/Node MCP application, tests, installers, systemd templates, generated application builds, dependencies, and several dated source/database snapshots.

The installed `owui-swarm` command is not an independent copy. `/home/komichris/.local/bin/owui-swarm` is a shell wrapper that loads `/home/komichris/.config/owui-swarm/environment` and executes a Python console script in the source tree's virtual environment. Package metadata proves that `openwebui-codex-swarm 0.2.0` is installed editable from the same source directory. The running dashboard therefore imports the current Python source directly.

There is also a separate Node application, `owui-swarm-chatgpt-app 1.0.0`, built under the same source tree. It provides a live, read-only MCP server at `127.0.0.1:8790/mcp`.

Open WebUI is a separate Docker Compose application rooted at `/opt/open-webui`. It is healthy, reports version `0.10.2`, and listens directly on all host interfaces at port `3000`. It is not a reverse proxy and does not contain the Swarm source.

Current live Swarm interfaces are:

- Python dashboard/API: `127.0.0.1:8787`
- Read-only MCP/ChatGPT App: `127.0.0.1:8790/mcp`
- Upstream Open WebUI: `0.0.0.0:3000` and `[::]:3000`

No listener exists at the roadmap's earlier assumed port `18787`. The Swarm does not currently expose a `/health` route, OpenAI-compatible manager API, wiki, OCR service, worktree engine, file-ingestion API, or artifact-download API.

The source directory is not a Git repository and is not nested in a valid Git repository. No duplicate `openwebui-codex-swarm` source root was found under `/home`, `/opt`, or `/srv`. Existing backups provide useful partial rollback points, but there is no current full checkpoint, no verified Open WebUI archive, and no off-host backup identified.

## 2. Actual architecture

```text
LAN clients
    |
    +--> 192.168.2.12:3000
    |      Open WebUI 0.10.2
    |      Docker container, Compose root /opt/open-webui
    |
    +--> SSH tunnel when used
           |
           +--> 127.0.0.1:8787
           |      Python Swarm dashboard/API 0.2.0
           |      editable import from source tree
           |      calls Open WebUI for models/chat
           |
           +--> 127.0.0.1:8790/mcp
                  Node MCP App 1.0.0
                  read-only access to Swarm catalog/runs

Python Swarm
    |
    +--> ~/.config/owui-swarm/config.toml
    +--> ~/.config/owui-swarm/environment
    +--> ~/.local/share/owui-swarm/catalog.sqlite3
    +--> ~/.local/share/owui-swarm/runs/

Node MCP App
    |
    +--> read-only catalog.sqlite3
    +--> validated, bounded reads from runs/
```

The Python Swarm is a proposal orchestrator. It sends bounded prompts and selected context to Open WebUI models, records worker/judge results, and leaves final authority and repository access with Codex. It does not execute generated code.

The Node MCP application is intentionally separate and read-only. It does not call models or mutate Swarm state.

## 3. Source/work directory

`pwd` from the project directory returned:

`/home/komichris/openwebui-codex-swarm`

No other directory named `openwebui-codex-swarm` was found under `/home`, `/opt`, or `/srv`. The project's `backups/` directory contains partial historical snapshots, not another complete source root.

The current tree includes:

- Python source: `swarm_router/`
- Python tests: `tests/`
- Python packaging: `pyproject.toml`, generated `.egg-info`
- Installer scripts: `scripts/install.sh`, `scripts/install.ps1`
- systemd template: `systemd/owui-swarm-dashboard.service`
- example configuration: `config.example.toml`
- project/operator documentation: `README.md`
- quality fixtures: `swarm_router/benchmarks/quality-v1.json`
- Node/TypeScript MCP app: `chatgpt_app/`
- Node manifest and exact lockfile: `chatgpt_app/package.json`, `package-lock.json`
- generated Node server/widget builds: `chatgpt_app/build/`, `chatgpt_app/dist/`
- installed Node dependencies: `chatgpt_app/node_modules/`
- dated local snapshots: `backups/`

Approximate project usage:

| Path | Size |
| --- | ---: |
| Whole source tree | 225 MiB |
| `chatgpt_app/node_modules` | 212 MiB |
| `.venv` | 12 MiB |
| `backups` | 564 KiB |
| Python source | 368 KiB |
| Python tests | 88 KiB |
| Node build | 80 KiB |
| bundled widget | 280 KiB |

No `AGENTS.md` exists in the project tree.

## 4. Installed executable and installation method

### User-facing command

`/home/komichris/.local/bin/owui-swarm`

Properties:

- regular file, not a symlink
- executable Bash wrapper
- mode `0755`
- owner `komichris:komichris`
- created/updated 2026-07-18 15:56 EDT

The wrapper:

1. exports variables from `/home/komichris/.config/owui-swarm/environment` when present;
2. executes `/home/komichris/openwebui-codex-swarm/.venv/bin/owui-swarm`.

### Console script

`/home/komichris/openwebui-codex-swarm/.venv/bin/owui-swarm`

This is a generated Python console script with a shebang pointing to the project virtual environment. It imports `swarm_router.cli:main`.

### Package installation

| Item | Finding |
| --- | --- |
| Distribution name | `openwebui-codex-swarm` |
| Distribution version | `0.2.0` |
| Source `__version__` | `0.2.0` |
| Environment | project-local `.venv` |
| Virtualenv creator | `uv 0.11.26` |
| Python | CPython 3.13.5 |
| Install mode | editable |
| Editable source | `/home/komichris/openwebui-codex-swarm` |
| pipx | not installed; not used |
| user-site pip | not used |
| copied binary | no |
| Node executable | not for the `owui-swarm` command |

`direct_url.json` explicitly records:

`file:///home/komichris/openwebui-codex-swarm`, editable `true`.

The install method matches `scripts/install.sh`: create/reuse `.venv`, run `pip install -e`, create the user-local wrapper, link the skill, and install the dashboard user unit.

## 5. Source-versus-installed relationship

### Python dashboard

The source and installed Python application are the same working copy:

- package metadata points to the source tree as an editable install;
- importing `swarm_router` resolves to `/home/komichris/openwebui-codex-swarm/swarm_router`;
- the live systemd command uses the source tree's virtualenv console script;
- the service started on 2026-07-25, after all observed source edits from 2026-07-18.

There is no separate Python package copy to become newer or older than the source. Editing Python files in this directory changes what the next process start will import.

The generated `.egg-info/SOURCES.txt` is stale relative to the current tree: it predates later additions such as `quality.py`, its tests, and quality fixtures. This does not redirect the editable runtime, but it means a future non-editable package build must regenerate metadata.

### Node MCP application

The live service executes:

`/usr/bin/node /home/komichris/openwebui-codex-swarm/chatgpt_app/build/main.js`

The generated build files are timestamped after their corresponding TypeScript sources, and the live MCP server reports the same server name/version and seven tools defined in current source. TypeScript checks and Node tests pass. Source maps do not embed source text, so exact source-to-build byte reproducibility was not proven during this no-build audit.

### Runtime data compatibility

The current Swarm catalog contains the five tables expected by current source:

- `models`
- `probe_history`
- `task_attempts`
- `quality_events`
- `benchmark_results`

Current tests include catalog migration coverage and pass. The catalog has no `PRAGMA user_version`, application ID, or explicit package-version marker, so its creator version cannot be independently proven. Dates and schema are consistent with the local 0.2.0 development sequence.

The active configuration exactly matches the current `config.example.toml` and contains no unknown section keys.

## 6. Runtime-data paths

Primary root:

`/home/komichris/.local/share/owui-swarm`

| Path | Purpose | Observed protection |
| --- | --- | --- |
| `catalog.sqlite3` | model registry, probes, task attempts, quality evidence, benchmarks | file `0600` |
| `runs/` | four existing run directories | directory and run data primarily `0700`/`0600` |
| `benchmarks/` | five existing benchmark directories | individual model result directories `0700` |
| `dashboard/server.json` | loopback bind/start metadata | file `0600` |

Approximate total: 616 KiB.

Run directories contain metadata and potentially sensitive prompts/results. Filenames and metadata were inventoried, but prompt, response, context, and user-content bodies were not included in this report.

## 7. Configuration paths

Primary root:

`/home/komichris/.config/owui-swarm`

| Path | Purpose | Mode/owner |
| --- | --- | --- |
| `config.toml` | active Swarm configuration | `0600`, `komichris:komichris` |
| `environment` | provider secret environment file | `0600`, `komichris:komichris` |
| `config.toml.pre-routing-20260718T201211Z` | pre-routing configuration snapshot | `0644`, `komichris:komichris` |

The configuration directory itself is `0775`. Sensitive active files are correctly restricted to the owner.

Configured non-secret structure:

- Open WebUI base: `http://127.0.0.1:3000`
- chat endpoint consumed by Swarm: `/api/chat/completions`
- health endpoint consumed by Swarm: `/health`
- models endpoint consumed by Swarm: `/api/models`
- run/catalog paths under `~/.local/share/owui-swarm`
- dashboard bind `127.0.0.1:8787`
- optional dashboard token variable name
- four worker roles and one judge role
- bounded worker count, timeouts, context, output, retry, reliability, and cooldown settings

## 8. Open WebUI relationship

Open WebUI is separate from Swarm.

Root:

`/opt/open-webui`

| Path | Purpose |
| --- | --- |
| `compose.yaml` | single-service Open WebUI Compose definition |
| `.env` | real Compose/provider secrets |
| `.env.example` | placeholder variable names |
| `README.md` | local operator instructions |
| `data/` | Open WebUI database, uploads, vector database, and caches |

The Compose service:

- uses `ghcr.io/open-webui/open-webui:main`;
- names the container `open-webui`;
- maps host port `3000` to container port `8080`;
- uses `restart: unless-stopped`;
- mounts `./data` at `/app/backend/data`;
- disables the Ollama API;
- configures OpenAI and NVIDIA OpenAI-compatible provider bases.

The live service:

- returned `{"status": true}` from `/health`;
- reported Open WebUI version `0.10.2` from `/api/version`;
- required authentication at `/api/models` and `/api/chat/completions`;
- is consumed by Swarm as the upstream model/provider gateway.

`/opt/open-webui/owui-swarm` does not exist.

## 9. Services, processes and ports

### Swarm user services

| Unit | State | PID/command | Bind |
| --- | --- | --- | --- |
| `owui-swarm-dashboard.service` | enabled, active/running | Python `owui-swarm ... serve` | `127.0.0.1:8787` |
| `owui-swarm-chatgpt-app.service` | enabled, active/running | Node `chatgpt_app/build/main.js` | `127.0.0.1:8790` |

Both:

- run as `komichris`;
- use `Restart=on-failure`;
- use `UMask=0077`;
- were started 2026-07-25 16:20 EDT;
- are managed by user-level systemd;
- have no user-level timers.

The user has `Linger=no`. Enabled user services therefore depend on an active user manager/login session rather than guaranteed boot-time linger.

### Relevant system services

| Unit | State | Operational caution |
| --- | --- | --- |
| `docker.service` | enabled, active/running | hosts Open WebUI |
| `plexmediaserver.service` | enabled, active/running | long-lived media service |
| `qbittorrent-nox.service` | enabled, active/running | long-lived downloader |
| `qbittorrent-portfw.service` | enabled, active/exited | maintains PIA/UFW forwarded port |
| `piavpn.service` | enabled, active/running | VPN and qBittorrent routing |
| `ufw.service` | enabled, active/exited | firewall enabled |
| `nvidia-persistenced.service` | enabled oneshot, currently inactive/dead after successful setup | persistence mode is enabled |

### Relevant listeners

| Bind | Owner/purpose | Accessibility |
| --- | --- | --- |
| `127.0.0.1:8787` | Swarm dashboard/API | loopback only |
| `127.0.0.1:8790` | Swarm read-only MCP | loopback only |
| `0.0.0.0:3000`, `[::]:3000` | Open WebUI Docker proxy | all host interfaces; LAN binding confirmed |
| `*:32400` | Plex | all host interfaces |
| `*:8090` | qBittorrent WebUI | all host interfaces |
| `0.0.0.0:22`, `[::]:22` | SSH | all host interfaces |
| `10.190.0.6:20790` TCP/UDP | qBittorrent forwarded port | PIA interface only |

The host LAN address is `192.168.2.12/24`. Open WebUI is directly bound on that interface. Public internet reachability cannot be concluded from bind state alone because exact UFW and router/NAT rules were unavailable.

No active Plex transcoder/FFmpeg process and no material GPU workload were observed at the audit instant.

## 10. Containers and reverse proxy

One live Docker/containerd workload was directly observed:

- container process: `uvicorn open_webui.main:app`
- internal bind: `0.0.0.0:8080`
- Docker proxy: host port `3000`
- container ID observed from `containerd-shim`: `d1df29941cb2...`
- configured image tag: `ghcr.io/open-webui/open-webui:main`

The Docker daemon is active and an Open WebUI overlay mount is present. The current user cannot read `/var/run/docker.sock`, and passwordless sudo is unavailable. Therefore these items remain unverified:

- `docker ps` runtime labels;
- exact Compose project name from Docker metadata;
- current image ID/digest;
- image creation date and restart count.

The Compose root and container process strongly identify this as the `/opt/open-webui` deployment.

No active or installed nginx, Caddy, Traefik, or Apache reverse proxy was detected. Open WebUI uses direct Docker port publication. Swarm ports remain loopback-only.

UFW is active. Exact rules could not be read without root access. The qBittorrent port-forward service is known to mutate UFW rules for the PIA forwarded port, so firewall changes must not be made independently of that service.

## 11. Storage, databases and logs

### Swarm storage

`/home/komichris/.local/share/owui-swarm/catalog.sqlite3`

- SQLite 3.46.1
- 164 KiB
- journal mode `DELETE`
- no schema version pragma
- current five-table schema matches source expectations

Per-run logs and artifacts are stored beneath `runs/<run-id>/`, including event JSONL, task metadata, context accounting, worker/judge artifacts, and final synthesis. They are private and may contain sensitive work material.

Swarm service logs go to the user journal:

- `journalctl --user -u owui-swarm-dashboard.service`
- `journalctl --user -u owui-swarm-chatgpt-app.service`

### Open WebUI storage

`/opt/open-webui/data` is approximately 896 MiB:

| Item | Approximate size/purpose |
| --- | --- |
| `cache/` | 889 MiB; model/audio/image caches |
| `uploads/` | 2.7 MiB; one observed uploaded file |
| `webui.db` | 712 KiB; primary Open WebUI SQLite database |
| `webui.db-wal` | 4.0 MiB; active WAL |
| `webui.db-shm` | 32 KiB; active shared-memory file |
| `vector_db/chroma.sqlite3` | 184 KiB; Chroma metadata/FTS database |

The Open WebUI database uses WAL mode and reports Alembic revision `42e2978c7933`. Its schema includes authentication, API-key, chat, message, file, knowledge, model, tool, function, automation, calendar, and related tables. No private rows were read.

The Chroma database has migration records through:

- `sysdb` version 10;
- `metadb` version 6;
- `embeddings_queue` version 2.

It already uses SQLite FTS5 internally for Chroma embedding metadata. This is not the planned Swarm V2 wiki index.

Open WebUI application logs are held by Docker and normally read with `docker compose logs`; Docker socket access was unavailable in this session.

### Ownership and local privacy risk

Swarm runtime files are appropriately private (`0700` directories and `0600` files).

Open WebUI's `.env` is restricted (`0600`, `nobody:nogroup`). However, `webui.db`, the Chroma database, the observed upload, and their parent data directories are readable by all local users (`0644` files under `0755` directories). Because `webui.db` contains private application data and credential-related tables, this is a material local confidentiality risk. It must be addressed only in a reviewed hardening phase because changing ownership/modes could affect the container.

### Host capacity

- root filesystem: 3.7 TiB total, 2.7 TiB used, 862 GiB available, 76% used;
- `/srv/media` is on the root filesystem, not a separate mount;
- no separate network/media mount was observed.

## 12. API, MCP and health capabilities

### Python dashboard at `127.0.0.1:8787`

Operational:

- `GET /` — dashboard HTML
- `GET /api/models`
- `GET /api/runs`
- `GET /api/runs/:run-id`
- `POST /api/models/sync`
- `POST /api/models/probe`
- `POST /api/models/update`
- `POST /api/runs`

The API returned HTTP 200 for model and run listings during the audit. Response bodies containing run/model data were not included in this report.

Not present:

- `/health`
- `/mcp`
- `/v1/models`
- `/v1/chat/completions`
- `/api/jobs`
- `/api/files`
- `/api/artifacts`

The dashboard supports asynchronous run dispatch and run detail, but not a general task queue, cancellation API, worktree execution, or authorized artifact download.

Dashboard token support exists in source through `X-Swarm-Token`, but the configured token environment variable is not present in the Swarm environment file. API authentication is therefore disabled. Loopback binding is the current protection.

### MCP at `127.0.0.1:8790/mcp`

Operational Streamable HTTP MCP server:

- protocol initialization succeeded;
- server name `Swarm Control`;
- server version `1.0.0`;
- protocol version negotiated as `2025-11-25`;
- one MCP Apps widget resource is available.

Live tools:

1. `get_swarm_status`
2. `list_swarm_runs`
3. `get_swarm_run_summary`
4. `get_swarm_run_details`
5. `list_swarm_models`
6. `get_swarm_model`
7. `render_swarm_control`

All seven tools advertise read-only, non-destructive, closed-world annotations. No dispatch, probe, update, delete, file, shell, or service-control MCP tool exists.

The MCP service has no application-level authentication and relies on exact loopback binding. It does not expose `/health`, OpenAI-compatible routes, jobs, files, or artifacts.

### Open WebUI at port 3000

Operational:

- `GET /health`
- `GET /api/version`
- authenticated `/api/models`
- authenticated `/api/chat/completions`

Unknown frontend paths return the Open WebUI HTML application with HTTP 200, so `/v1/models` and `/api/jobs` GET responses were confirmed as HTML fallback pages, not verified APIs.

## 13. Dependency and package inventory

### Host

| Component | Version/state |
| --- | --- |
| Debian | 13.6 (`trixie`) |
| Kernel | `6.12.74+deb13+1-amd64` |
| CPU | Intel Core i7-7700, 4 cores/8 threads |
| RAM | 23 GiB |
| Swap | 23 GiB |
| GPU | NVIDIA GeForce RTX 3060, 12 GiB |
| NVIDIA driver | 550.163.01 |
| NVIDIA-reported CUDA compatibility | 12.4 |
| Audit-time GPU usage | 22 MiB, 0% utilization |
| Docker package/client | 26.1.5 |
| Docker Compose | 2.26.1 |
| containerd | 1.7.24 |
| runc | 1.1.15 |
| NVIDIA Container Toolkit | not installed |
| Python | 3.13.5 |
| system pip | 25.1.1 |
| project-venv pip | 26.1.2 |
| uv | 0.11.26 |
| pipx | absent |
| Poetry | absent |
| Node.js | 20.19.2 |
| npm | 9.2.0 |
| pnpm | absent |
| yarn | absent |
| sqlite3 CLI | absent |
| Python SQLite | 3.46.1 |
| SQLite FTS5 | available and creation-tested |
| rclone | 1.60.1 |

### Swarm packages

- Python distribution: `openwebui-codex-swarm 0.2.0`
- Python runtime dependencies: standard library only
- Node application: `owui-swarm-chatgpt-app 1.0.0`
- MCP SDK: `@modelcontextprotocol/sdk 1.29.0`
- MCP Apps bridge: `@modelcontextprotocol/ext-apps 1.7.4`
- `better-sqlite3 12.11.1`
- Express 5.1.0
- Zod 3.25.76
- TypeScript 5.8.3
- Vite 7.3.6

### Validation

- Python: 14/14 tests passed outside the network-restricted sandbox.
- TypeScript checks: passed.
- Node tests: passed.
- Live Open WebUI health/version: passed.
- Live Swarm dashboard/API status: passed.
- Live MCP initialize/tools/resources: passed.

No production model call, probe, dispatch, package build, or service restart was performed.

## 14. Secrets-loading method, fully redacted

No secret value was printed, copied, stored in this report, or hashed.

| Component | Variable names | Loading mechanism | Assessment |
| --- | --- | --- | --- |
| Python Swarm CLI/wrapper | `OPEN_WEBUI_API_KEY` | shell wrapper sources `~/.config/owui-swarm/environment` | file is `0600`; appropriate |
| Swarm dashboard user service | `OPEN_WEBUI_API_KEY` | systemd `EnvironmentFile=%h/.config/owui-swarm/environment` | file is `0600`; appropriate |
| Optional dashboard auth | `SWARM_DASHBOARD_TOKEN` | config names the variable; value would come from environment | variable absent; auth disabled |
| Node MCP app | none | unit contains only non-secret data-path environment entries | no provider secret access |
| Open WebUI | `WEBUI_SECRET_KEY`, `OPENAI_API_KEY`, `NVIDIA_API_KEY`, optional `DEFAULT_MODELS` | Docker Compose substitutes `/opt/open-webui/.env` | `.env` is `0600`; no Docker secrets used |

Additional findings:

- no relevant provider/Swarm secret variable was present in the audit shell;
- Swarm `config.toml` contains configuration and role definitions, not the API key;
- Open WebUI provider records may also exist inside Open WebUI's protected application state; no rows were read;
- no `secrets:` declaration exists in the Compose file;
- no system-level Swarm environment file or credential file was found;
- the Open WebUI `.env` owner is unusual (`nobody:nogroup`) but its mode is restrictive and the root-run Compose process can read it.

## 15. Source-control status

Confirmed:

- `/home/komichris/openwebui-codex-swarm/.git` is absent;
- `git rev-parse` fails from the project and its parents;
- the project is not nested in a valid Git repository;
- `/home/komichris/.git` exists as a directory but is not a valid Git repository;
- nearby valid repositories exist, but no nearby Git-backed Swarm copy was found;
- no Git branch, remote, commit, tag, or source URL can be recovered from this tree.

Package provenance is local only:

- editable `file://` install from the current source path;
- package metadata has no homepage or repository URL;
- source/package version is `0.2.0`;
- Node app version is `1.0.0`.

Without Git, exact user-created versus original files cannot be proven. Strong local evidence indicates iterative user work on 2026-07-18:

- `backups/pre-install-*`, `step3-*`, and `step4-*` preserve earlier source states;
- current Python modules differ from those snapshots;
- quality routing/tests were added after earlier snapshots;
- the entire `chatgpt_app/` was added after the pre-install snapshot;
- installed configuration has a pre-routing snapshot;
- generated package metadata predates later source changes.

No Git operation was performed.

## 16. Existing roadmap-feature matrix

| Roadmap component | Classification | Evidence |
| --- | --- | --- |
| Swarm health endpoint | absent | no `/health` on 8787 or 8790; Open WebUI health is separate |
| Dashboard | operational | live loopback dashboard/API on 8787 |
| Native MCP/ChatGPT widget | operational | live read-only MCP on 8790 with seven tools |
| Shared wiki MCP | absent | current MCP exposes only Swarm status/catalog/run reads |
| OpenAI-compatible Swarm Manager API | absent | no Swarm `/v1/models` or `/v1/chat/completions` |
| File ingestion | absent | selected context can be supplied to CLI/dashboard, but no persistent ingestion service/API |
| Run/task API | partial | run dispatch/list/detail exists; no queue, cancellation, state machine, or worktrees |
| Worker routing | operational | catalog, probes, explicit/automatic routing, reliability cooldowns |
| Manager/critic flow | partial | planner/implementer/critic/verifier plus judge proposals; no execution/review gate engine |
| Model-provider registry | operational | SQLite catalog populated from Open WebUI |
| Versioned local wiki | absent | no `/srv/swarm-wiki` or wiki source/index |
| Swarm wiki/search | absent | host FTS5 exists; Open WebUI Chroma FTS is separate |
| OCR | absent | no local OCR service/package/container detected |
| Google Drive mirror | absent | rclone exists; no Swarm publisher or mirror detected |
| Wiki Bridge API/site | absent | no bridge routes or site |
| Isolated worktree engine | absent | no task worktree source/storage/service |
| Open WebUI integration | partial | Swarm consumes Open WebUI; it is not exposed back as a selectable manager model/Pipe |
| Artifact handling | partial | private run artifacts and bounded MCP reads exist; no authorized download API |
| Authentication | partial | Open WebUI auth exists; Swarm services rely on loopback, dashboard token unset, MCP has no app auth |
| Audit logging | partial | per-run event log and catalog evidence exist; no platform-wide audit facility |
| Metrics/evaluation | partial | reliability, latency, quality events, and five compact benchmarks; no platform evaluation dashboard/API |
| Backup/recovery tooling | partial | manual snapshots and local docs; no complete automated backup/restore workflow |
| Waifu Workbench | absent | no separate themed frontend |

## 17. Current backup and rollback options

Existing Swarm snapshots:

| Snapshot | Size | Coverage |
| --- | ---: | --- |
| `backups/pre-install-20260718T194352Z` | 144 KiB | earlier Python source/scripts/config example |
| `backups/step3-20260718T214231Z` | 188 KiB | selected source/tests/config plus pre-migration catalog |
| `backups/step4-20260718T221430Z` | 212 KiB | later selected source/tests/config plus pre-migration catalog |
| `backups/chatgpt-app-20260719T002032Z` | 16 KiB | limited ChatGPT app snapshot |

Installed systemd unit files exactly match their source templates, providing a useful configuration recovery reference.

Limitations:

- snapshots are inside the same non-Git source tree and on the same disk;
- none is a verified complete current checkpoint;
- the latest source contains changes after some snapshots;
- no current full archive of `.venv`, Node dependencies/build, runtime data, or configuration was identified;
- no `/opt/open-webui-data-*.tar.gz` backup was found;
- no Swarm backup timer or cron task exists;
- the Open WebUI image uses mutable tag `main`, and its current digest is not recorded;
- Open WebUI's active WAL means copying only `webui.db` would not be a safe consistent rollback point.

The Open WebUI README documents a stop/archive/start backup procedure, but no execution was performed.

## 18. Pre-existing user work

All pre-existing content was preserved, including:

- current Python Swarm source and tests;
- current Node MCP source, lockfile, dependencies, build, widget, and tests;
- four historical Swarm runs and five benchmark result directories;
- Swarm catalog and quality/reliability evidence;
- active and historical Swarm configuration;
- source/database snapshots under `backups/`;
- Open WebUI database, WAL, vector data, upload, and caches;
- Plex, qBittorrent, PIA, media, crypto-keeper, and scheduled automation state.

The audit did not read or report stored conversation bodies, uploaded document/image contents, run prompts, worker responses, user identities, email addresses, provider keys, or confidential work material.

## 19. Discrepancies from previous assumptions

| Previous assumption | Actual state |
| --- | --- |
| Swarm under `/opt/open-webui/owui-swarm` | source is `/home/komichris/openwebui-codex-swarm`; `/opt/open-webui/owui-swarm` does not exist |
| Swarm port `18787` | dashboard is `8787`; MCP is `8790`; `18787` is not listening |
| Swarm and Open WebUI are one tree | separate source/service and Compose/data roots |
| installed executable may be independent | Python install is editable from the source tree |
| no current MCP | a read-only MCP/ChatGPT App is already operational |
| likely one Swarm application | Python dashboard 0.2.0 and Node MCP app 1.0.0 are separate live processes |
| Git branch can be created in phase 0.2 | no repository or current branch exists; phase 0.2 must initialize carefully only after approval |
| TypeScript may be the existing Swarm core | core orchestration is Python; TypeScript currently implements the read-only MCP app |
| Open WebUI may be localhost-only | it is directly published on all IPv4/IPv6 interfaces at port 3000 |

The Google Sheet's original phase 0.1 row still carries the obsolete `/opt/open-webui/owui-swarm` and `18787` assumptions. This report and the current user prompt should supersede that row for implementation.

## 20. Risks and blockers

### High priority

1. **No source-control checkpoint.** Current source is operational but has no Git history or recoverable upstream provenance.
2. **No complete current backup.** Existing snapshots are partial, local, and same-disk.
3. **Open WebUI data is locally world-readable.** Database/upload files may expose private data to other local accounts.
4. **Open WebUI image is unpinned.** `main` prevents deterministic rebuild/rollback unless the current digest is recorded.

### Medium priority

5. **Swarm HTTP APIs have no active application authentication.** Loopback is the only current boundary; dashboard token support is configured but unset, and MCP has no app auth.
6. **User service boot persistence is uncertain.** Both Swarm units are enabled, but `Linger=no`.
7. **Swarm catalog lacks a schema/version marker.** Future migrations need explicit versioning and transactional backup.
8. **Generated Python package metadata is stale.** A future wheel/sdist may omit newer files unless metadata is regenerated and checked.
9. **No NVIDIA Container Toolkit.** A later containerized OCR/GPU phase cannot use Docker GPU access until separately installed/configured and approved.
10. **Root filesystem is already 76% used.** Wiki sources, OCR models, worktrees, artifacts, and backups need quotas/retention.

### Audit limitations requiring privileged follow-up

11. Exact UFW rules were unavailable; UFW is active, but LAN/public reachability cannot be fully proven.
12. Docker runtime metadata was unavailable; exact image digest, Compose labels, and restart count remain unknown.
13. Root crontab and root-only backup locations were not inspected.

These limitations do not block acceptance of phase 0.1, but they should be resolved before any network exposure, container change, or production deployment.

## 21. Proposed safe Git and backup strategy for phase 0.2

Do not initialize Git until the following plan is reviewed.

### A. Establish a rollback checkpoint first

1. Create a timestamped, permission-restricted backup outside the source tree, preferably:

   `/var/backups/owui-swarm/<UTC-timestamp>/`

2. Record a manifest containing paths, owners, modes, sizes, mtimes, package versions, service unit hashes, Open WebUI version, Compose file, configured image tag, and—after privileged access—the live image digest.
3. Back up the current source tree as an exact filesystem archive. Include generated `chatgpt_app/build`/`dist` for immediate rollback. Either include `.venv` and `node_modules` in a separate dependency archive or record them as reproducible from Python metadata and `package-lock.json`.
4. Back up non-secret configuration and installed user unit files.
5. Handle secret files separately:
   - never put them in Git;
   - either leave them in place and record only names/modes, or place encrypted/restricted copies in the backup after explicit approval;
   - keep backup directories `0700` and secret files `0600`.
6. Use SQLite's online backup API for:
   - Swarm `catalog.sqlite3`;
   - Open WebUI `webui.db` while WAL is active;
   - Chroma `chroma.sqlite3`.
7. Archive the small Swarm run/benchmark tree separately.
8. Do not include Open WebUI caches, model weights, uploads, or `/srv/media` in the source checkpoint. Treat uploads as a separately approved private-data backup.
9. Verify archives by listing them and restoring to a temporary location without starting services.

### B. Resolve provenance before initialization

Ask whether an original archive, upstream repository, or earlier Git checkout exists elsewhere. If it does, compare against that source and preserve history instead of creating unrelated history.

Also confirm whether the invalid/empty `/home/komichris/.git` directory is intentional. Do not remove it silently.

### C. Prepare repository boundaries

Before `git init`, expand `.gitignore` at minimum for:

- `.venv/`
- `__pycache__/` and `*.py[cod]`
- `openwebui_codex_swarm.egg-info/`
- `chatgpt_app/node_modules/`
- generated build directories unless the user explicitly wants them versioned
- `backups/`
- local environment/config files
- runtime runs, databases, logs, artifacts, caches, and temporary worktrees

Run a secret/path review before the first index operation.

### D. Initialize only after approval

If no upstream history exists:

1. initialize this exact source root with a `main` branch;
2. add only intentional source, tests, manifests, lockfiles, templates, fixtures, and reviewed documentation;
3. create one baseline commit identifying observed versions and the external backup path;
4. create `feat/swarm-platform-v2`;
5. do not add runtime databases, historical run contents, dependencies, backups, generated secrets, or Open WebUI data.

The rollback document should include restoration of source/build, configuration, SQLite backups, and user units without automatically restarting services.

## 22. Recommended implementation sequence

Use the roadmap's dependency order, adjusted to the actual paths and current partial features:

1. **Phase 0.2:** privileged inventory completion, full backup, rollback rehearsal, reviewed Git initialization and feature branch.
2. **Wiki foundation:** canonical versioned wiki, schema/governance, then SQLite FTS5 index.
3. **Source-grounded ingestion:** proposal-only ingestion with immutable source references.
4. **Shared wiki MCP:** extend or deliberately replace the current read-only MCP boundary; keep loopback and add authentication before LAN access.
5. **Swarm Manager/Open WebUI MVP:** add the OpenAI-compatible manager API and register it with existing Open WebUI before building a custom frontend.
6. **Sanitized Drive mirror:** publish approved pages only; add redaction and non-destructive copy.
7. **Optional OCR:** schedule GPU use conservatively around Plex and verify NVIDIA container/runtime strategy first.
8. **Wiki Bridge API/site:** compact authenticated search/context UI.
9. **Isolated worktree/task engine:** only after Git, backups, quotas, command allowlists, and review gates exist.
10. **Manager/reviewer orchestration:** integrate existing proposal routing with typed tasks, bounded retries, and blocking review findings.
11. **Optional Waifu Workbench:** build over stable APIs while retaining Open WebUI as fallback.
12. **Evaluation and operations:** quality/cost tests, retention, backups, restore rehearsal, token rotation, and operator runbooks.

Existing routing, quality evidence, and read-only MCP should be reused where they fit rather than replaced speculatively.

## 23. Assumptions requiring confirmation

1. Is `/home/komichris/openwebui-codex-swarm` the intended permanent source root?
2. Does an original Git repository, downloadable archive, or upstream remote exist for this source?
3. Is the invalid/empty `/home/komichris/.git` directory intentional?
4. Should generated `chatgpt_app/build` and `dist` be tracked in the future Git repository, or rebuilt during deployment?
5. Should phase 0.2 include `.venv` and `node_modules` in a fast-rollback archive despite their reproducibility and size?
6. May phase 0.2 use root access to record Docker image digests, UFW rules, root schedules, and write `/var/backups/owui-swarm`?
7. Is Open WebUI intentionally LAN-accessible on `192.168.2.12:3000`, and is any router/NAT rule expected to expose it publicly?
8. Should the current read-only MCP app on port 8790 be retained as a separate service when the shared wiki MCP is introduced?
9. Should dashboard API authentication be enabled before any tunnel or LAN integration, or is loopback-only access the accepted baseline?
10. Are the dated snapshots under `backups/` known-good milestones, and may they be moved out of the future Git repository after a verified external backup?
11. May a later hardening task correct Open WebUI data ownership/modes after testing container compatibility?
12. Is boot-time availability required for the two user services, which would require a reviewed linger or system-service strategy?

---

Phase 0.1 stops here. No Git repository was initialized and no Swarm Platform V2 feature implementation was started.
