# Forge Discord Notifications

Forge Version: `0.12-dev`
Architecture Revision: `R12`

Forge Discord Notifications is the outbound-only notification path for operational Forge messages. It sends concise Discord webhook messages, records delivery state in SQLite, and prevents duplicate sends after restarts or repeated handlers.

Inbound Discord commands, bot accounts, slash commands, dashboard UI, and broad notification fan-out are not implemented.

## Purpose

Current production consumer:

- Night Owl reports and failures

Current policy:

- Empty successful Night Owl queue: no message.
- Night Owl report artifact after meaningful work: one summary message.
- Night Owl failure: one error message.
- Repeated processing with the same deduplication key: no duplicate send.

## Configuration

Canonical config:

```text
/home/komichris/.config/owui-swarm/discord.env
```

Required variable:

```text
FORGE_DISCORD_WEBHOOK_URL
```

Requirements:

- file mode `0600`
- owned by the Forge service user
- excluded from Git
- loaded by `owui-swarm-personal.service`
- not stored in task payloads
- not stored in journal metadata
- not printed by CLI JSON

The legacy Night Owl config no longer stores an active Discord webhook. Night Owl Jira credentials remain in:

```text
/home/komichris/.config/night-owl/env
```

## Delivery storage

Delivery state is persisted in the existing Forge SQLite catalog database.

Tables:

- `forge_notification_counters`
- `forge_notification_deliveries`

Delivery records include notification ID, deduplication key, destination type, event type, severity, title, message, timestamps, status, side-effect state, HTTP classification, external message ID, task/schedule references, sanitized errors, and metadata.

Notification IDs use:

```text
FN-YYYYMMDD-000001
```

## Side-effect safety

Discord sends are non-idempotent external side effects.

Forge records:

- `proposed` before attempting delivery
- `started` immediately before the HTTP request
- `confirmed` only after Discord accepts the message
- `unknown` when a transport failure may have occurred after transmission

Confirmed and unknown deliveries are not resent automatically.

## HTTP behavior

Request constraints:

- HTTPS only
- approved hosts: `discord.com`, `discordapp.com`
- path format: `/api/webhooks/<id>/<token>`
- explicit `User-Agent`
- explicit timeout
- bounded response body
- JSON payload only
- `allowed_mentions.parse` is empty
- oversized messages are truncated with a visible marker

Responses:

- HTTP 200 or 204: confirmed
- HTTP 403: permission denied
- HTTP 404: invalid webhook
- HTTP 429: bounded retry only when Discord provides a retry delay
- transport timeout/reset: unknown
- HTTP 5xx: unknown

## CLI

```bash
owui-swarm discord status
owui-swarm discord status --json
owui-swarm discord test
owui-swarm discord test --deduplication-key forge-discord-r10-production-test --json
owui-swarm notification list
owui-swarm notification show FN-20260728-000001
```

`discord test` sends one explicit labelled test message. It never runs at service startup.

## Night Owl integration

```text
Forge schedule
  ↓
Night Owl subprocess
  ↓
report.md artifact, when meaningful work or failure exists
  ↓
Forge notification object
  ↓
persistent delivery record
  ↓
Discord webhook
  ↓
confirmed, failed, or unknown delivery state
```

The Forge runner no longer calls the legacy `send_report.sh` path for webhook delivery. It leaves report artifacts for Forge to deliver once.

On confirmed report delivery, Forge archives `report.md` under the Night Owl `sent/` directory. On delivery failure, the report remains in place for manual review.

## Production validation

Performed on 2026-07-28.

- Canonical config exists with mode `0600`.
- Webhook GET with explicit `User-Agent`: HTTP 200.
- One explicit test notification: `FN-20260728-000001`.
- External Discord message ID: `1531784974527762605`.
- Duplicate test with the same deduplication key returned the existing delivery and did not send again.
- Night Owl empty run: `FT-20260728-000005`, completed without sending a Discord message.
- Scheduler service remained healthy.
- Night Owl schedule remained enabled with next run `2026-07-29T00:00:00Z`.
- Legacy Night Owl cron remained disabled.

## Troubleshooting HTTP 403

Observed root cause during R10 work:

- A no-`User-Agent` Python/urllib request received HTTP 403 from Discord.
- The same webhook returned HTTP 200 when Forge sent an explicit `User-Agent`.

If 403 returns again:

1. Run `owui-swarm discord status --json`.
2. Confirm `/home/komichris/.config/owui-swarm/discord.env` is mode `0600`.
3. Run exactly one `owui-swarm discord test --json`.
4. Inspect `owui-swarm notification list --json`.
5. Do not print or paste the webhook URL.

## Rollback

Disable Forge Discord sending:

```bash
mv /home/komichris/.config/owui-swarm/discord.env /home/komichris/.config/owui-swarm/discord.env.disabled
systemctl --user restart owui-swarm-personal.service
```

Disable Night Owl:

```bash
owui-swarm schedule disable FS-20260728-000001
```

Do not re-enable the legacy Night Owl Discord sender while Forge notification delivery is active.
