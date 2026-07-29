# Forge LAN Dashboard

Forge Version: `0.12-dev`
Architecture Revision: `R12`

The Forge LAN dashboard is the owner-operated operations interface for the Debian Forge server. It shows existing Forge state and exposes a small set of controlled actions. It is not a chat UI, a public admin panel, or a multi-user application.

## Relationship to Open WebUI

Forge has two browser interfaces:

- Forge Dashboard: operations, health, schedules, task history, Night Owl, notifications, agents, providers, approved dispatch, and approved local image generation.
- Open WebUI: direct model conversation, model selection, and file-assisted chat where Open WebUI already supports it.

Deployed URLs:

- Forge Dashboard: `http://192.168.2.12:8787`
- Open WebUI: `http://192.168.2.12:3000`

## Existing implementation reused

The milestone extends the existing Python stdlib dashboard in `swarm_router/dashboard.py` and preserves the existing service name:

- `owui-swarm-dashboard.service`
- port `8787`

No React, Node frontend build, second database, or replacement dashboard service was added.

## Binding and network limits

The deployed service listens on:

- `127.0.0.1:8787`
- `192.168.2.12:8787`

The detected LAN interface is `enp2s0` on `192.168.2.12/24`. The service template adds systemd network restrictions:

- `IPAddressDeny=any`
- `IPAddressAllow=localhost`
- `IPAddressAllow=192.168.2.0/24`

`ufw`, `nft`, and `iptables` are not installed on this host. The dashboard is therefore restricted by exact private-address binding, systemd IP address policy, owner authentication, and no IPv6 listener. It must not be exposed by router port forwarding, UPnP, public DNS, or a public reverse proxy.

Plain HTTP is acceptable only for this trusted LAN deployment. Public or remote access requires HTTPS and a separate security review.

## Authentication

The dashboard uses one owner secret stored outside Git:

```text
/home/komichris/.config/owui-swarm/dashboard.env
SWARM_DASHBOARD_TOKEN=<high-entropy secret>
```

Requirements:

- file mode `0600`;
- owned by the Forge service user;
- loaded by `owui-swarm-dashboard.service`;
- never committed;
- never copied into URLs, HTML, JavaScript, journal metadata, notification metadata, or logs.

Login creates an HTTP-only same-site session cookie with expiration. All write actions require CSRF.

## Pages

- Overview: Forge version, service health, Open WebUI status, Night Owl status, Discord status, provider summary, active/failed/orphan task counts.
- Tasks: recent journaled Forge tasks, personal task ID, task type, logical agent, status, schedule metadata, recovery status, event timeline, checkpoints, sanitized task details, related notifications.
- Schedules: schedule list, enabled state, trigger, timezone, next run, previous occurrence, overlap and misfire policies.
- Night Owl: schedule state, cadence, last run, last checkpoint, Discord report state, legacy cron state, rollback summary.
- Notifications: recent delivery records and unknown deliveries requiring manual review.
- Agents: read-only logical agent registry.
- Providers: provider health, inventory revisions, model capability metadata, quarantine/availability state.
- Dispatch: approved manual Forge task dispatch.
- Image Generation: fixed FLUX Schnell 768 Daily form, ComfyUI connection state, recent image tasks, and indexed artifact gallery.

## Controlled actions

Writes are deliberately narrow:

- enable schedule;
- disable schedule;
- run schedule now;
- Night Owl dry-run;
- Night Owl live run with stronger confirmation.

Every write requires login, CSRF, and an exact confirmation string. Schedule writes use `ScheduleStore`/`Scheduler`; task dispatch uses the personal-task API path. The dashboard does not invoke shell runners directly.

The dispatch allowlist currently supports only Night Owl. Payloads are structured and validated by the existing Night Owl validator. Arbitrary shell commands, executable paths, URLs, and secret fields are rejected.

Image generation uses its own fixed allowlist. The dashboard submits `image_generate` tasks through the personal backend; it never posts raw ComfyUI workflows or filesystem paths.

## Night Owl controls

Night Owl remains scheduled through Forge:

- schedule ID: `FS-20260728-000001`
- task type: `night_owl`
- agent: `night_owl`
- cadence: `0 */4 * * *`
- timezone: `America/New_York`

Dashboard dry-run uses the same personal-task backend and journal path as scheduled Night Owl work. Live execution requires the exact confirmation string `RUN NIGHT OWL LIVE`.

Legacy Night Owl cron entries are preserved but commented out for rollback.

## Notification visibility

Discord webhook values are never shown. The dashboard shows delivery IDs, event type, severity, state, side-effect state, related task, external message ID, and sanitized errors.

Unknown delivery states are highlighted for manual review and are not automatically retried.

## Service commands

```bash
systemctl --user daemon-reload
systemctl --user restart owui-swarm-dashboard.service
systemctl --user status owui-swarm-dashboard.service
journalctl --user -u owui-swarm-dashboard.service -n 100 --no-pager
```

Health checks:

```bash
curl -fsS http://127.0.0.1:8787/health
curl -fsS http://192.168.2.12:8787/health
```

## forge.local

`forge.local` is allowed by dashboard host-header validation, but this milestone does not rename the Debian host, modify router DNS, or create public DNS records. Use the LAN-IP URL unless Avahi or local DNS is configured and verified from a separate LAN client.

Optional local-DNS target:

```text
forge.local -> 192.168.2.12
```

## Troubleshooting

- `401`: login required or expired session.
- `403`: missing or invalid CSRF token for a write.
- `421`: Host header is not one of the private allowed names.
- Dashboard starts but LAN fails: verify `ss -tlnp`, systemd service status, and LAN routing to `192.168.2.12`.
- Open WebUI link fails: verify `http://192.168.2.12:3000`.

## Rollback

To roll back dashboard exposure without removing history:

```bash
systemctl --user stop owui-swarm-dashboard.service
```

Then restore the previous service command to bind only `127.0.0.1:8787` and restart the service.

This does not affect:

- Open WebUI;
- `owui-swarm-personal.service`;
- `forge-scheduler.service`;
- Night Owl schedule state;
- Discord notification delivery records.

## Image Generation

Implemented in the local image-generation MVP as the `Image Generation` page.
It submits only the approved `flux-schnell-768-daily` Forge task type through
the personal backend and serves only indexed artifacts.

The production dashboard validation generated `FT-20260728-000008`, displayed it in the gallery, opened its authenticated full-resolution artifact, and rejected unauthenticated artifact access.

## Deferred

- Codex delegation panel;
- inbound Discord control;
- public internet access;
- OAuth or multi-user RBAC;
- remote workers;
- dashboard chat replacement for Open WebUI.
