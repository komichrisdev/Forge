from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable
from urllib import request
from zoneinfo import ZoneInfo
import json
import os
import re
import signal
import sqlite3
import uuid

from .agents import default_registry
from .config import AppConfig
from .night_owl import validate_night_owl_payload


SCHEDULE_ID_RE = re.compile(r"^FS-\d{8}-\d{6}$")
OCCURRENCE_ID_RE = re.compile(r"^FO-[A-Za-z0-9._:-]{8,96}$")
TERMINAL_TASK_STATES = {"completed", "failed", "cancelled"}


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    schedule_id: str
    name: str
    description: str
    task_type: str
    agent_id: str
    enabled: bool
    trigger_type: str
    trigger_configuration: dict[str, Any]
    timezone: str
    payload: dict[str, Any]
    misfire_policy: str = "run_once"
    overlap_policy: str = "skip"
    created_at: str = ""
    updated_at: str = ""
    last_due_at: str = ""
    last_run_at: str = ""
    next_run_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Schedule":
        trigger_configuration = data.get("trigger_configuration") or {}
        payload = data.get("payload") or {}
        metadata = data.get("metadata") or {}
        return cls(
            schedule_id=str(data.get("schedule_id", "")).strip(),
            name=str(data.get("name", "")).strip(),
            description=str(data.get("description", "")).strip(),
            task_type=str(data.get("task_type", "")).strip(),
            agent_id=str(data.get("agent_id", "")).strip(),
            enabled=bool(data.get("enabled", True)),
            trigger_type=str(data.get("trigger_type", "")).strip(),
            trigger_configuration=dict(trigger_configuration) if isinstance(trigger_configuration, dict) else {},
            timezone=str(data.get("timezone", "UTC")).strip() or "UTC",
            payload=dict(payload) if isinstance(payload, dict) else {},
            misfire_policy=str(data.get("misfire_policy", "run_once")).strip() or "run_once",
            overlap_policy=str(data.get("overlap_policy", "skip")).strip() or "skip",
            created_at=str(data.get("created_at", "")).strip(),
            updated_at=str(data.get("updated_at", "")).strip(),
            last_due_at=str(data.get("last_due_at", "")).strip(),
            last_run_at=str(data.get("last_run_at", "")).strip(),
            next_run_at=str(data.get("next_run_at", "")).strip(),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "name": self.name,
            "description": self.description,
            "task_type": self.task_type,
            "agent_id": self.agent_id,
            "enabled": self.enabled,
            "trigger_type": self.trigger_type,
            "trigger_configuration": self.trigger_configuration,
            "timezone": self.timezone,
            "payload": self.payload,
            "misfire_policy": self.misfire_policy,
            "overlap_policy": self.overlap_policy,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_due_at": self.last_due_at,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "metadata": self.metadata,
        }


def validate_schedule_id(schedule_id: str) -> bool:
    return bool(SCHEDULE_ID_RE.fullmatch(schedule_id))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleError(f"Invalid datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise ScheduleError(f"Datetime must include timezone: {value}")
    return parsed.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise ScheduleError(f"Invalid timezone: {name}") from exc


def _json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load_json(value: str) -> dict[str, Any]:
    data = json.loads(value or "{}")
    return data if isinstance(data, dict) else {}


def _validate_payload(payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    forbidden = {"command", "cmd", "shell", "shell_command"}
    if any(key in payload for key in forbidden):
        issues.append("payload must not contain shell command fields")
    if "messages" in payload:
        messages = payload["messages"]
        if not isinstance(messages, list) or not messages:
            issues.append("payload.messages must be a non-empty list")
        for item in messages if isinstance(messages, list) else []:
            if not isinstance(item, dict) or str(item.get("role", "")).strip() not in {"system", "user", "assistant"}:
                issues.append("payload.messages entries must have a supported role")
            if not isinstance(item, dict) or not str(item.get("content", "")).strip():
                issues.append("payload.messages entries must have non-empty content")
    elif not isinstance(payload.get("prompt"), str) or not payload.get("prompt", "").strip():
        issues.append("payload must include prompt or messages")
    return issues


def validate_schedule(schedule: Schedule, *, allow_empty_id: bool = False) -> list[str]:
    issues: list[str] = []
    if not allow_empty_id or schedule.schedule_id:
        if not validate_schedule_id(schedule.schedule_id):
            issues.append("schedule_id must match FS-YYYYMMDD-000001")
    if not schedule.name:
        issues.append("name is required")
    registry = default_registry()
    agent = registry.get(schedule.agent_id)
    if agent is None:
        issues.append("agent_id is not registered")
    elif not agent.enabled:
        issues.append("agent_id is disabled")
    if not schedule.task_type:
        issues.append("task_type is required")
    elif agent and schedule.task_type not in agent.supported_task_types:
        issues.append("task_type is not supported by agent_id")
    if schedule.trigger_type not in {"one_time", "interval", "cron"}:
        issues.append("trigger_type must be one_time, interval, or cron")
    try:
        _local_zone(schedule.timezone)
    except ScheduleError as exc:
        issues.append(str(exc))
    if schedule.misfire_policy not in {"skip", "run_once"}:
        issues.append("misfire_policy must be skip or run_once")
    if schedule.overlap_policy not in {"skip", "wait"}:
        issues.append("overlap_policy must be skip or wait")
    if schedule.task_type == "night_owl":
        issues.extend(validate_night_owl_payload(schedule.payload))
    else:
        issues.extend(_validate_payload(schedule.payload))
    try:
        if schedule.trigger_type == "one_time":
            parse_time(str(schedule.trigger_configuration.get("run_at", "")))
        elif schedule.trigger_type == "interval":
            _interval_seconds(schedule)
            if schedule.trigger_configuration.get("start_at"):
                parse_time(str(schedule.trigger_configuration.get("start_at")))
            elif schedule.created_at:
                parse_time(schedule.created_at)
        elif schedule.trigger_type == "cron":
            _parse_cron(str(schedule.trigger_configuration.get("expression", "")))
    except ScheduleError as exc:
        issues.append(str(exc))
    return issues


def _interval_seconds(schedule: Schedule) -> int:
    config = schedule.trigger_configuration
    seconds = int(config.get("every_seconds") or 0)
    seconds += int(config.get("every_minutes") or 0) * 60
    seconds += int(config.get("every_hours") or 0) * 3600
    seconds += int(config.get("every_days") or 0) * 86400
    if seconds <= 0:
        raise ScheduleError("interval trigger requires a positive duration")
    return seconds


def _field(text: str, minimum: int, maximum: int) -> tuple[set[int], bool]:
    if not text:
        raise ScheduleError("cron fields must be non-empty")
    if text == "*":
        return set(range(minimum, maximum + 1)), True
    values: set[int] = set()
    for part in text.split(","):
        try:
            if part.startswith("*/"):
                step = int(part[2:])
                if step <= 0:
                    raise ScheduleError("cron step must be positive")
                values.update(range(minimum, maximum + 1, step))
            elif "-" in part:
                left, right = part.split("-", 1)
                start, end = int(left), int(right)
                if start > end:
                    raise ScheduleError("cron range start must be <= end")
                values.update(range(start, end + 1))
            else:
                values.add(int(part))
        except ValueError as exc:
            raise ScheduleError("cron field must use integers, ranges, lists, or */n") from exc
    if any(value < minimum or value > maximum for value in values):
        raise ScheduleError("cron field value out of range")
    return values, False


def _parse_cron(expression: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    parts = expression.split()
    if len(parts) != 5:
        raise ScheduleError("cron expression must have five fields")
    minutes, _ = _field(parts[0], 0, 59)
    hours, _ = _field(parts[1], 0, 23)
    days, day_any = _field(parts[2], 1, 31)
    months, _ = _field(parts[3], 1, 12)
    weekdays, weekday_any = _field(parts[4], 0, 7)
    weekdays = {0 if value == 7 else value for value in weekdays}
    return minutes, hours, days, months, weekdays, day_any, weekday_any


def _cron_matches(local: datetime, expression: str) -> bool:
    minutes, hours, days, months, weekdays, day_any, weekday_any = _parse_cron(expression)
    cron_weekday = (local.weekday() + 1) % 7
    if local.minute not in minutes or local.hour not in hours or local.month not in months:
        return False
    day_match = local.day in days
    weekday_match = cron_weekday in weekdays
    if day_any and weekday_any:
        return True
    if day_any:
        return weekday_match
    if weekday_any:
        return day_match
    return day_match or weekday_match


def next_due(schedule: Schedule, after: datetime) -> str:
    after = after.astimezone(timezone.utc)
    if schedule.trigger_type == "one_time":
        run_at = parse_time(str(schedule.trigger_configuration.get("run_at", "")))
        return format_time(run_at) if run_at > after else ""
    if schedule.trigger_type == "interval":
        seconds = _interval_seconds(schedule)
        start = parse_time(str(schedule.trigger_configuration.get("start_at"))) if schedule.trigger_configuration.get("start_at") else parse_time(schedule.created_at)
        due = start + timedelta(seconds=seconds)
        if due <= after:
            elapsed = int((after - due).total_seconds())
            due += timedelta(seconds=((elapsed // seconds) + 1) * seconds)
        return format_time(due)
    zone = _local_zone(schedule.timezone)
    expression = str(schedule.trigger_configuration.get("expression", ""))
    cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cursor + timedelta(days=366)
    while cursor <= limit:
        local = cursor.astimezone(zone)
        if local.fold == 0 and _cron_matches(local, expression):
            return format_time(cursor)
        cursor += timedelta(minutes=1)
    raise ScheduleError("cron expression has no due time within 366 days")


def latest_due(schedule: Schedule, now: datetime) -> str:
    now = now.astimezone(timezone.utc)
    next_run = parse_time(schedule.next_run_at)
    if next_run > now:
        return ""
    if schedule.trigger_type == "one_time":
        return format_time(next_run)
    if schedule.trigger_type == "interval":
        seconds = _interval_seconds(schedule)
        elapsed = int((now - next_run).total_seconds())
        return format_time(next_run + timedelta(seconds=(elapsed // seconds) * seconds))
    zone = _local_zone(schedule.timezone)
    expression = str(schedule.trigger_configuration.get("expression", ""))
    cursor = now.replace(second=0, microsecond=0)
    limit = next_run - timedelta(minutes=1)
    while cursor > limit:
        local = cursor.astimezone(zone)
        if local.fold == 0 and _cron_matches(local, expression):
            return format_time(cursor)
        cursor -= timedelta(minutes=1)
    return format_time(next_run)


def occurrence_id(schedule_id: str, scheduled_for: str) -> str:
    stamp = scheduled_for.replace("-", "").replace(":", "").removesuffix("Z")
    return f"FO-{schedule_id[3:]}-{stamp}Z"


class ScheduleStore:
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
                CREATE TABLE IF NOT EXISTS forge_schedule_counters (
                    day TEXT PRIMARY KEY,
                    next_value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forge_schedules (
                    schedule_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    task_type TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    trigger_type TEXT NOT NULL,
                    trigger_configuration TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    misfire_policy TEXT NOT NULL,
                    overlap_policy TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_due_at TEXT NOT NULL DEFAULT '',
                    last_run_at TEXT NOT NULL DEFAULT '',
                    next_run_at TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS forge_schedule_occurrences (
                    occurrence_id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    transition_key TEXT NOT NULL UNIQUE,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(schedule_id, scheduled_for)
                );
                CREATE INDEX IF NOT EXISTS forge_schedule_occurrences_schedule
                    ON forge_schedule_occurrences(schedule_id, scheduled_for);
                CREATE TABLE IF NOT EXISTS forge_scheduler_leases (
                    lease_name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def next_schedule_id(self) -> str:
        day = self.clock().astimezone(timezone.utc).strftime("%Y%m%d")
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT next_value FROM forge_schedule_counters WHERE day = ?", (day,)).fetchone()
            value = int(row["next_value"]) if row else 1
            if row:
                connection.execute("UPDATE forge_schedule_counters SET next_value = ? WHERE day = ?", (value + 1, day))
            else:
                connection.execute("INSERT INTO forge_schedule_counters(day, next_value) VALUES(?, ?)", (day, value + 1))
            return f"FS-{day}-{value:06d}"

    def create(self, data: dict[str, Any]) -> Schedule:
        now = format_time(self.clock())
        raw = dict(data)
        raw.setdefault("created_at", now)
        raw.setdefault("updated_at", now)
        raw.setdefault("timezone", "UTC")
        raw.setdefault("misfire_policy", "run_once")
        raw.setdefault("overlap_policy", "skip")
        candidate = Schedule.from_dict(raw)
        issues = validate_schedule(candidate, allow_empty_id=not candidate.schedule_id)
        if issues:
            raise ScheduleError("; ".join(issues))
        raw.setdefault("schedule_id", self.next_schedule_id())
        schedule = Schedule.from_dict(raw)
        schedule = Schedule.from_dict({**schedule.to_dict(), "next_run_at": next_due(schedule, parse_time(schedule.created_at) - timedelta(seconds=1))})
        with self._db() as connection:
            connection.execute(
                """
                INSERT INTO forge_schedules(
                    schedule_id, name, description, task_type, agent_id, enabled, trigger_type,
                    trigger_configuration, timezone, payload, misfire_policy, overlap_policy,
                    created_at, updated_at, last_due_at, last_run_at, next_run_at, metadata
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule.schedule_id, schedule.name, schedule.description, schedule.task_type, schedule.agent_id,
                    1 if schedule.enabled else 0, schedule.trigger_type, _json(schedule.trigger_configuration),
                    schedule.timezone, _json(schedule.payload), schedule.misfire_policy, schedule.overlap_policy,
                    schedule.created_at, schedule.updated_at, schedule.last_due_at, schedule.last_run_at,
                    schedule.next_run_at, _json(schedule.metadata),
                ),
            )
        return schedule

    def list(self) -> list[Schedule]:
        with self._db() as connection:
            rows = connection.execute("SELECT * FROM forge_schedules ORDER BY schedule_id").fetchall()
        return [self._row_schedule(row) for row in rows]

    def get(self, schedule_id: str) -> Schedule:
        with self._db() as connection:
            row = connection.execute("SELECT * FROM forge_schedules WHERE schedule_id = ?", (schedule_id,)).fetchone()
        if row is None:
            raise ScheduleError(f"Unknown schedule: {schedule_id}")
        return self._row_schedule(row)

    def set_enabled(self, schedule_id: str, enabled: bool) -> Schedule:
        now = format_time(self.clock())
        with self._db() as connection:
            cursor = connection.execute(
                "UPDATE forge_schedules SET enabled = ?, updated_at = ? WHERE schedule_id = ?",
                (1 if enabled else 0, now, schedule_id),
            )
            if cursor.rowcount != 1:
                raise ScheduleError(f"Unknown schedule: {schedule_id}")
        return self.get(schedule_id)

    def occurrences(self, schedule_id: str) -> list[dict[str, Any]]:
        with self._db() as connection:
            rows = connection.execute(
                "SELECT * FROM forge_schedule_occurrences WHERE schedule_id = ? ORDER BY scheduled_for",
                (schedule_id,),
            ).fetchall()
        return [self._row_occurrence(row) for row in rows]

    def status(self, task_status: Callable[[str], str] | None = None) -> dict[str, Any]:
        schedules = self.list()
        now = self.clock().astimezone(timezone.utc)
        rows = []
        for schedule in schedules:
            occurrences = self.occurrences(schedule.schedule_id)
            state = "disabled" if not schedule.enabled else "healthy"
            if schedule.enabled and schedule.next_run_at and parse_time(schedule.next_run_at) <= now:
                state = "overdue"
            if schedule.enabled and self.previous_non_terminal(schedule.schedule_id, task_status):
                state = "blocked"
            rows.append({
                **schedule.to_dict(),
                "state": state,
                "last_occurrence": occurrences[-1] if occurrences else None,
            })
        return {"schedule_count": len(rows), "enabled_count": sum(1 for item in rows if item["enabled"]), "schedules": rows}

    def acquire_lease(self, owner: str, *, lease_seconds: int, lease_name: str = "scheduler") -> bool:
        now = self.clock().astimezone(timezone.utc)
        expires = format_time(now + timedelta(seconds=lease_seconds))
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT owner, expires_at FROM forge_scheduler_leases WHERE lease_name = ?", (lease_name,)).fetchone()
            if row and row["owner"] != owner and parse_time(row["expires_at"]) > now:
                return False
            connection.execute(
                """
                INSERT INTO forge_scheduler_leases(lease_name, owner, expires_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at, updated_at = excluded.updated_at
                """,
                (lease_name, owner, expires, format_time(now)),
            )
            return True

    def previous_non_terminal(self, schedule_id: str, task_status: Callable[[str], str] | None) -> bool:
        with self._db() as connection:
            row = connection.execute(
                """
                SELECT task_id, status FROM forge_schedule_occurrences
                WHERE schedule_id = ? AND task_id != ''
                ORDER BY scheduled_for DESC LIMIT 1
                """,
                (schedule_id,),
            ).fetchone()
        if row is None:
            return False
        if task_status is None:
            return row["status"] in {"claimed", "created"}
        status = task_status(str(row["task_id"]))
        return bool(status and status not in TERMINAL_TASK_STATES)

    def claim_occurrence(self, schedule: Schedule, scheduled_for: str, status: str, metadata: dict[str, Any]) -> dict[str, Any]:
        now = format_time(self.clock())
        oid = occurrence_id(schedule.schedule_id, scheduled_for)
        if not OCCURRENCE_ID_RE.fullmatch(oid):
            raise ScheduleError("Invalid occurrence_id")
        transition_key = f"schedule:{schedule.schedule_id}:{scheduled_for}"
        with self._db() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO forge_schedule_occurrences(
                    occurrence_id, schedule_id, scheduled_for, created_at, status, transition_key, metadata
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (oid, schedule.schedule_id, scheduled_for, now, status, transition_key, _json(metadata)),
            )
            row = connection.execute(
                "SELECT * FROM forge_schedule_occurrences WHERE schedule_id = ? AND scheduled_for = ?",
                (schedule.schedule_id, scheduled_for),
            ).fetchone()
        occurrence = self._row_occurrence(row)
        occurrence["claimed_now"] = cursor.rowcount == 1
        return occurrence

    def finish_occurrence(self, occurrence_id_value: str, *, status: str, task_id: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._db() as connection:
            row = connection.execute("SELECT metadata FROM forge_schedule_occurrences WHERE occurrence_id = ?", (occurrence_id_value,)).fetchone()
            if row is None:
                raise ScheduleError(f"Unknown occurrence: {occurrence_id_value}")
            existing = _load_json(row["metadata"])
            if metadata:
                existing.update(metadata)
            connection.execute(
                "UPDATE forge_schedule_occurrences SET status = ?, task_id = ?, metadata = ? WHERE occurrence_id = ?",
                (status, task_id, _json(existing), occurrence_id_value),
            )
            updated = connection.execute("SELECT * FROM forge_schedule_occurrences WHERE occurrence_id = ?", (occurrence_id_value,)).fetchone()
        return self._row_occurrence(updated)

    def advance(self, schedule: Schedule, due_at: str, now: datetime) -> Schedule:
        updated = {
            **schedule.to_dict(),
            "last_due_at": due_at,
            "updated_at": format_time(now),
            "next_run_at": next_due(schedule, now),
        }
        if schedule.trigger_type == "one_time":
            updated["enabled"] = False
            updated["next_run_at"] = ""
        with self._db() as connection:
            connection.execute(
                """
                UPDATE forge_schedules
                SET enabled = ?, updated_at = ?, last_due_at = ?, last_run_at = ?, next_run_at = ?
                WHERE schedule_id = ?
                """,
                (1 if updated["enabled"] else 0, updated["updated_at"], due_at, updated["last_run_at"], updated["next_run_at"], schedule.schedule_id),
            )
        return self.get(schedule.schedule_id)

    def mark_run(self, schedule_id: str, when: str) -> None:
        with self._db() as connection:
            connection.execute(
                "UPDATE forge_schedules SET last_run_at = ?, updated_at = ? WHERE schedule_id = ?",
                (when, format_time(self.clock()), schedule_id),
            )

    def _row_schedule(self, row: sqlite3.Row) -> Schedule:
        return Schedule(
            schedule_id=row["schedule_id"],
            name=row["name"],
            description=row["description"],
            task_type=row["task_type"],
            agent_id=row["agent_id"],
            enabled=bool(row["enabled"]),
            trigger_type=row["trigger_type"],
            trigger_configuration=_load_json(row["trigger_configuration"]),
            timezone=row["timezone"],
            payload=_load_json(row["payload"]),
            misfire_policy=row["misfire_policy"],
            overlap_policy=row["overlap_policy"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_due_at=row["last_due_at"],
            last_run_at=row["last_run_at"],
            next_run_at=row["next_run_at"],
            metadata=_load_json(row["metadata"]),
        )

    def _row_occurrence(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "occurrence_id": row["occurrence_id"],
            "schedule_id": row["schedule_id"],
            "scheduled_for": row["scheduled_for"],
            "created_at": row["created_at"],
            "task_id": row["task_id"],
            "status": row["status"],
            "transition_key": row["transition_key"],
            "metadata": _load_json(row["metadata"]),
        }


class Scheduler:
    def __init__(
        self,
        config: AppConfig,
        *,
        store: ScheduleStore | None = None,
        submit_task: Callable[[Schedule, dict[str, Any]], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] = utc_now,
        owner: str | None = None,
    ) -> None:
        self.config = config
        self.clock = clock
        self.store = store or ScheduleStore(config.swarm.catalog_path, clock=clock)
        self.owner = owner or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._submit_task = submit_task

    def task_status(self, task_id: str) -> str:
        path = Path(self.config.personal.task_directory).expanduser().resolve() / task_id / "task.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return ""
        return str(data.get("status") or "")

    def tick(self) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        if not self.store.acquire_lease(self.owner, lease_seconds=self.config.scheduler.lease_seconds):
            return {"owner": self.owner, "locked": False, "processed": []}
        processed: list[dict[str, Any]] = []
        for schedule in self.store.list():
            if not schedule.enabled or not schedule.next_run_at or parse_time(schedule.next_run_at) > now:
                continue
            due_at = latest_due(schedule, now) if schedule.misfire_policy == "run_once" else schedule.next_run_at
            if self.store.previous_non_terminal(schedule.schedule_id, self.task_status):
                if schedule.overlap_policy == "wait":
                    processed.append({"schedule_id": schedule.schedule_id, "status": "waiting", "scheduled_for": due_at})
                    continue
                occurrence = self.store.claim_occurrence(schedule, due_at, "skipped", {"reason": "overlap"})
                occurrence.pop("claimed_now", None)
                self.store.advance(schedule, due_at, now)
                processed.append(occurrence)
                continue
            if schedule.misfire_policy == "skip" and parse_time(schedule.next_run_at) < now:
                occurrence = self.store.claim_occurrence(schedule, schedule.next_run_at, "missed", {"reason": "misfire"})
                occurrence.pop("claimed_now", None)
                self.store.advance(schedule, schedule.next_run_at, now)
                processed.append(occurrence)
                continue
            occurrence = self.store.claim_occurrence(schedule, due_at, "claimed", self._schedule_metadata(schedule, due_at))
            if occurrence["status"] == "created":
                occurrence.pop("claimed_now", None)
                processed.append(occurrence)
                continue
            if not occurrence.pop("claimed_now", False):
                if occurrence["status"] == "claimed":
                    occurrence = self.store.finish_occurrence(
                        occurrence["occurrence_id"],
                        status="failed",
                        metadata={"error": "previous scheduler claim had no task_id; manual review required"},
                    )
                    self.store.advance(schedule, due_at, now)
                processed.append(occurrence)
                continue
            try:
                task = self._submit(schedule, occurrence)
                task_id = str(task.get("task_id") or "")
                occurrence = self.store.finish_occurrence(
                    occurrence["occurrence_id"],
                    status="created",
                    task_id=task_id,
                    metadata={"forge_task_id": str(task.get("forge_task_id") or "")},
                )
            except Exception as exc:
                occurrence = self.store.finish_occurrence(
                    occurrence["occurrence_id"],
                    status="failed",
                    metadata={"error": str(exc)[:500]},
                )
            self.store.advance(schedule, due_at, now)
            if occurrence["status"] == "created":
                self.store.mark_run(schedule.schedule_id, format_time(now))
            processed.append(occurrence)
        return {"owner": self.owner, "locked": True, "processed": processed}

    def run_once(self, schedule_id: str) -> dict[str, Any]:
        schedule = self.store.get(schedule_id)
        now = format_time(self.clock())
        occurrence = self.store.claim_occurrence(schedule, now, "claimed", {**self._schedule_metadata(schedule, now), "manual": True})
        if occurrence["status"] == "created":
            occurrence.pop("claimed_now", None)
            return occurrence
        if not occurrence.pop("claimed_now", False):
            return self.store.finish_occurrence(
                occurrence["occurrence_id"],
                status="failed",
                metadata={"error": "previous scheduler claim had no task_id; manual review required"},
            )
        try:
            task = self._submit(schedule, occurrence)
        except Exception as exc:
            return self.store.finish_occurrence(
                occurrence["occurrence_id"],
                status="failed",
                metadata={"error": str(exc)[:500]},
            )
        self.store.mark_run(schedule.schedule_id, now)
        return self.store.finish_occurrence(
            occurrence["occurrence_id"],
            status="created",
            task_id=str(task.get("task_id") or ""),
            metadata={"forge_task_id": str(task.get("forge_task_id") or "")},
        )

    def run_forever(self, stop: Event | None = None) -> None:
        stop = stop or Event()
        while not stop.is_set():
            self.tick()
            stop.wait(self.config.scheduler.poll_interval_seconds)

    def _submit(self, schedule: Schedule, occurrence: dict[str, Any]) -> dict[str, Any]:
        if self._submit_task:
            return self._submit_task(schedule, occurrence)
        body = json.dumps(self._task_body(schedule, occurrence)).encode("utf-8")
        token = os.environ.get(self.config.personal.auth_token_env, "")
        if not token:
            raise ScheduleError(f"{self.config.personal.auth_token_env} is required to submit scheduled tasks")
        url = f"http://{self.config.personal.loopback_host}:{self.config.personal.port}/api/personal-tasks"
        req = request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def _task_body(self, schedule: Schedule, occurrence: dict[str, Any]) -> dict[str, Any]:
        messages = schedule.payload.get("messages")
        if not messages:
            content = str(schedule.payload.get("prompt", "")).strip()
            if schedule.task_type == "night_owl":
                content = "Forge Night Owl automation occurrence."
            messages = [{"role": "user", "content": content}]
        metadata = self._schedule_metadata(schedule, str(occurrence["scheduled_for"]))
        metadata["occurrence_id"] = str(occurrence["occurrence_id"])
        return {
            "model": self.config.personal.model_id,
            "messages": messages,
            "task_type": schedule.task_type,
            "agent_id": schedule.agent_id,
            "task_payload": schedule.payload,
            "metadata": metadata,
        }

    def _schedule_metadata(self, schedule: Schedule, scheduled_for: str) -> dict[str, Any]:
        return {
            "schedule_id": schedule.schedule_id,
            "scheduled_for": scheduled_for,
            "trigger_type": schedule.trigger_type,
            "misfire_policy": schedule.misfire_policy,
            "overlap_policy": schedule.overlap_policy,
        }


def install_signal_handlers(stop: Event) -> None:
    def _stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
