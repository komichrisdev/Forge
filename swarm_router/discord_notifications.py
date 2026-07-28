from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib import error, parse, request
import json
import os
import re
import shlex
import socket
import sqlite3
import time


CONFIG_FILE = Path.home() / ".config/owui-swarm/discord.env"
WEBHOOK_ENV = "FORGE_DISCORD_WEBHOOK_URL"
NOTIFICATION_ID_RE = re.compile(r"^FN-\d{8}-\d{6}$")
SEVERITIES = {"info", "success", "warning", "error"}
TERMINAL_STATUSES = {"confirmed", "failed", "unknown", "skipped"}
MAX_CONTENT = 1900
MAX_RESPONSE = 4096
USER_AGENT = "Forge/0.10 DiscordNotifier"


class DiscordError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscordConfig:
    configured: bool
    config_path: str
    valid: bool
    webhook_url: str = ""
    host: str = ""
    mode: str = ""
    issues: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "config_path": self.config_path,
            "valid": self.valid,
            "host": self.host,
            "mode": self.mode,
            "issues": self.issues,
            "webhook_url": "<redacted>" if self.webhook_url else "",
        }


@dataclass(frozen=True)
class Notification:
    notification_id: str
    event_type: str
    severity: str
    title: str
    message: str
    task_id: str = ""
    forge_task_id: str = ""
    schedule_id: str = ""
    occurrence_id: str = ""
    agent_id: str = ""
    timestamp: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    deduplication_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_env(path: Path | None = None) -> dict[str, str]:
    path = path or CONFIG_FILE
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        parsed = shlex.split(raw, comments=True)
        if len(parsed) == 1:
            values[key.strip()] = parsed[0]
    return values


def validate_webhook(url: str) -> list[str]:
    issues: list[str] = []
    parsed = parse.urlparse(url)
    if parsed.scheme != "https":
        issues.append("webhook URL must use HTTPS")
    if parsed.netloc not in {"discord.com", "discordapp.com"}:
        issues.append("webhook host must be discord.com or discordapp.com")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 4 or parts[:2] != ["api", "webhooks"] or not parts[2] or not parts[3]:
        issues.append("webhook path must be /api/webhooks/<id>/<token>")
    return issues


def load_config(path: Path | None = None) -> DiscordConfig:
    path = path or CONFIG_FILE
    mode = oct(path.stat().st_mode & 0o777) if path.exists() else ""
    values = load_env(path)
    url = values.get(WEBHOOK_ENV, "") or os.environ.get(WEBHOOK_ENV, "")
    issues = []
    if not path.exists():
        issues.append("canonical Discord config file is missing")
    elif mode != "0o600":
        issues.append("canonical Discord config file mode must be 0600")
    if not url:
        issues.append(f"{WEBHOOK_ENV} is required")
    else:
        issues.extend(validate_webhook(url))
    return DiscordConfig(
        configured=bool(url),
        config_path=str(path),
        valid=not issues,
        webhook_url=url,
        host=parse.urlparse(url).netloc if url else "",
        mode=mode,
        issues=issues,
    )


def redact(text: str, config: DiscordConfig | None = None) -> str:
    value = text
    if config and config.webhook_url:
        value = value.replace(config.webhook_url, "<redacted>")
    value = re.sub(r"https://(?:discord|discordapp)\.com/api/webhooks/[^\s'\"<>]+", "<redacted>", value)
    return value[:1000]


def safe_content(title: str, message: str, severity: str) -> str:
    content = f"[Forge {severity}] {title}\n{message}".strip()
    content = content.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    if len(content) <= MAX_CONTENT:
        return content
    return content[: MAX_CONTENT - 24].rstrip() + "\n...[truncated by Forge]"


def classify_status(status: int) -> str:
    if status in {200, 204}:
        return "accepted"
    if status == 403:
        return "permission_denied"
    if status == 404:
        return "invalid_webhook"
    if status == 429:
        return "rate_limited"
    if 500 <= status <= 599:
        return "server_error_unknown"
    return "http_error"


class NotificationStore:
    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _db(self) -> Any:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._db() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS forge_notification_counters (
                    day TEXT PRIMARY KEY,
                    next_value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forge_notification_deliveries (
                    notification_id TEXT PRIMARY KEY,
                    deduplication_key TEXT NOT NULL UNIQUE,
                    destination_type TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    confirmed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    side_effect_state TEXT NOT NULL,
                    http_status INTEGER NOT NULL DEFAULT 0,
                    http_classification TEXT NOT NULL DEFAULT '',
                    external_message_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    forge_task_id TEXT NOT NULL DEFAULT '',
                    schedule_id TEXT NOT NULL DEFAULT '',
                    occurrence_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    error_summary TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS forge_notification_deliveries_created
                    ON forge_notification_deliveries(created_at);
                """
            )

    def next_notification_id(self) -> str:
        day = self.clock().astimezone(timezone.utc).strftime("%Y%m%d")
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT next_value FROM forge_notification_counters WHERE day = ?", (day,)).fetchone()
            value = int(row["next_value"]) if row else 1
            if row:
                connection.execute("UPDATE forge_notification_counters SET next_value = ? WHERE day = ?", (value + 1, day))
            else:
                connection.execute("INSERT INTO forge_notification_counters(day, next_value) VALUES(?, ?)", (day, value + 1))
        return f"FN-{day}-{value:06d}"

    def propose(self, item: Notification) -> tuple[dict[str, Any], bool]:
        issues = validate_notification(item)
        if issues:
            raise DiscordError("; ".join(issues))
        with self._db() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO forge_notification_deliveries(
                    notification_id, deduplication_key, destination_type, event_type, severity,
                    title, message, created_at, status, side_effect_state, task_id, forge_task_id,
                    schedule_id, occurrence_id, agent_id, metadata
                ) VALUES(?, ?, 'discord_webhook', ?, ?, ?, ?, ?, 'proposed', 'proposed', ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.notification_id,
                    item.deduplication_key,
                    item.event_type,
                    item.severity,
                    item.title,
                    item.message,
                    item.timestamp or format_time(self.clock()),
                    item.task_id,
                    item.forge_task_id,
                    item.schedule_id,
                    item.occurrence_id,
                    item.agent_id,
                    json.dumps(item.metadata, sort_keys=True),
                ),
            )
            row = connection.execute("SELECT * FROM forge_notification_deliveries WHERE deduplication_key = ?", (item.deduplication_key,)).fetchone()
        return self._row(row), cursor.rowcount == 1

    def start(self, notification_id: str) -> None:
        with self._db() as connection:
            connection.execute(
                "UPDATE forge_notification_deliveries SET status = 'started', side_effect_state = 'started', started_at = ? WHERE notification_id = ? AND status = 'proposed'",
                (format_time(self.clock()), notification_id),
            )

    def finish(
        self,
        notification_id: str,
        *,
        status: str,
        side_effect_state: str,
        http_status: int = 0,
        http_classification: str = "",
        external_message_id: str = "",
        error_summary: str = "",
    ) -> dict[str, Any]:
        confirmed_at = format_time(self.clock()) if status == "confirmed" else ""
        with self._db() as connection:
            connection.execute(
                """
                UPDATE forge_notification_deliveries
                SET status = ?, side_effect_state = ?, http_status = ?, http_classification = ?,
                    external_message_id = ?, error_summary = ?, confirmed_at = ?
                WHERE notification_id = ?
                """,
                (status, side_effect_state, http_status, http_classification, external_message_id, error_summary, confirmed_at, notification_id),
            )
            row = connection.execute("SELECT * FROM forge_notification_deliveries WHERE notification_id = ?", (notification_id,)).fetchone()
        return self._row(row)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._db() as connection:
            rows = connection.execute("SELECT * FROM forge_notification_deliveries ORDER BY created_at DESC, notification_id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def get(self, notification_id: str) -> dict[str, Any]:
        with self._db() as connection:
            row = connection.execute("SELECT * FROM forge_notification_deliveries WHERE notification_id = ?", (notification_id,)).fetchone()
        if row is None:
            raise DiscordError(f"Unknown notification: {notification_id}")
        return self._row(row)

    def status(self) -> dict[str, Any]:
        rows = self.list(100)
        return {
            "delivery_count": len(rows),
            "latest_success": next((row for row in rows if row["status"] == "confirmed"), None),
            "latest_failure": next((row for row in rows if row["status"] in {"failed", "unknown"}), None),
            "unknown": [row for row in rows if row["status"] == "unknown"],
        }

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["metadata"] = json.loads(data["metadata"] or "{}")
        return data


def validate_notification(item: Notification) -> list[str]:
    issues: list[str] = []
    if not NOTIFICATION_ID_RE.fullmatch(item.notification_id):
        issues.append("notification_id is invalid")
    if item.severity not in SEVERITIES:
        issues.append("severity is invalid")
    if not item.event_type.strip():
        issues.append("event_type is required")
    if not item.title.strip():
        issues.append("title is required")
    if not item.message.strip():
        issues.append("message is required")
    if not item.deduplication_key.strip():
        issues.append("deduplication_key is required")
    return issues


def send_http(
    webhook_url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 10,
    open_url: Callable[..., BinaryIO] | None = None,
) -> tuple[int, str, str]:
    open_url = open_url or request.urlopen
    url = webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true"
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with open_url(req, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE).decode("utf-8", "replace")
        external_id = ""
        if body:
            try:
                external_id = str(json.loads(body).get("id", ""))
            except json.JSONDecodeError:
                external_id = ""
        return int(getattr(response, "status", 200)), body, external_id


def deliver(
    store: NotificationStore,
    item: Notification,
    *,
    config_path: Path | None = None,
    open_url: Callable[..., BinaryIO] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 10,
) -> dict[str, Any]:
    config = load_config(config_path)
    row, created = store.propose(item)
    if not created and row["status"] in TERMINAL_STATUSES:
        row["duplicate_suppressed"] = True
        return row
    notification_id = str(row["notification_id"])
    if not config.valid:
        return store.finish(notification_id, status="failed", side_effect_state="proposed", http_classification="config_error", error_summary="; ".join(config.issues))

    content = safe_content(item.title, item.message, item.severity)
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    attempts = 0
    while attempts < 2:
        attempts += 1
        store.start(notification_id)
        try:
            status, body, external_id = send_http(config.webhook_url, payload, timeout=timeout, open_url=open_url)
            classification = classify_status(status)
            if classification == "accepted":
                return store.finish(notification_id, status="confirmed", side_effect_state="confirmed", http_status=status, http_classification=classification, external_message_id=external_id)
            if classification == "rate_limited" and attempts == 1:
                try:
                    retry_after = min(float(json.loads(body).get("retry_after", 0)), 2.0)
                except (ValueError, json.JSONDecodeError):
                    retry_after = 0
                if retry_after > 0:
                    sleep(retry_after)
                    continue
            return store.finish(notification_id, status="failed", side_effect_state="proposed", http_status=status, http_classification=classification, error_summary=redact(body, config))
        except error.HTTPError as exc:
            body = exc.read(MAX_RESPONSE).decode("utf-8", "replace")
            classification = classify_status(exc.code)
            if classification == "rate_limited" and attempts == 1:
                try:
                    retry_after = min(float(json.loads(body).get("retry_after", 0)), 2.0)
                except (ValueError, json.JSONDecodeError):
                    retry_after = 0
                if retry_after > 0:
                    sleep(retry_after)
                    continue
            side_effect = "unknown" if classification == "server_error_unknown" else "proposed"
            status_value = "unknown" if side_effect == "unknown" else "failed"
            return store.finish(notification_id, status=status_value, side_effect_state=side_effect, http_status=exc.code, http_classification=classification, error_summary=redact(body, config))
        except (TimeoutError, socket.timeout, error.URLError) as exc:
            return store.finish(notification_id, status="unknown", side_effect_state="unknown", http_classification="ambiguous_transport_error", error_summary=redact(str(exc), config))
    return store.get(notification_id)


def notification_from_store(store: NotificationStore, **data: Any) -> Notification:
    return Notification(notification_id=store.next_notification_id(), timestamp=format_time(store.clock()), **data)


def notify_night_owl(
    db_path: str | Path,
    *,
    result: Any,
    task: dict[str, Any],
    task_id: str,
    forge_task_id: str,
    agent_id: str,
) -> dict[str, Any] | None:
    store = NotificationStore(db_path)
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    state_dir = Path(str(result.metadata.get("state_dir", ""))).expanduser() if isinstance(result.metadata, dict) else Path()
    report = state_dir / "report.md"
    if result.status == "completed" and not report.exists():
        return None
    if report.exists():
        message = report.read_text(encoding="utf-8", errors="replace")
        event_type = "night_owl.report"
        severity = "success" if result.status == "completed" else "error"
        title = "Night Owl report"
        dedupe = f"night-owl:{forge_task_id}:report"
    else:
        message = f"Night Owl task failed. Return code: {result.returncode}. Timed out: {result.timed_out}."
        if result.stderr:
            message += f"\n{result.stderr[:500]}"
        event_type = "night_owl.failure"
        severity = "error"
        title = "Night Owl failed"
        dedupe = f"night-owl:{forge_task_id}:failure"
    row = deliver(
        store,
        notification_from_store(
            store,
            event_type=event_type,
            severity=severity,
            title=title,
            message=message,
            task_id=task_id,
            forge_task_id=forge_task_id,
            schedule_id=str(metadata.get("schedule_id", "")),
            occurrence_id=str(metadata.get("occurrence_id", "")),
            agent_id=agent_id,
            deduplication_key=dedupe,
            metadata={"source": "night_owl"},
        ),
    )
    if report.exists() and row["status"] == "confirmed":
        sent = report.parent / "sent"
        sent.mkdir(parents=True, exist_ok=True)
        report.replace(sent / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md")
    return row
