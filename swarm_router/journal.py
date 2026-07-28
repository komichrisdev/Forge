from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable
import json
import re
import sqlite3
import uuid

from .agents import AGENT_ID_RE, AgentRegistry, default_registry


TASK_ID_RE = re.compile(r"^FT-\d{8}-\d{6}$")
REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,511}$")


class JournalEventType(str, Enum):
    TASK_CREATED = "TASK_CREATED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    TASK_STARTED = "TASK_STARTED"
    STAGE_STARTED = "STAGE_STARTED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    LEASE_GRANTED = "LEASE_GRANTED"
    LEASE_RENEWED = "LEASE_RENEWED"
    HEARTBEAT_RECORDED = "HEARTBEAT_RECORDED"
    HANDOFF_REQUESTED = "HANDOFF_REQUESTED"
    HANDOFF_COMPLETED = "HANDOFF_COMPLETED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_ORPHANED = "TASK_ORPHANED"
    RECOVERY_PROPOSED = "RECOVERY_PROPOSED"


class SideEffectState(str, Enum):
    NONE = "none"
    PROPOSED = "proposed"
    STARTED = "started"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class JournalEvent:
    event_id: str
    task_id: str
    event_type: str
    timestamp: str
    agent_id: str = ""
    run_id: str = ""
    stage: str = ""
    message: str = ""
    checkpoint_reference: str = ""
    side_effect_state: str = SideEffectState.NONE.value
    metadata: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CheckpointRecord:
    task_id: str
    stage: str
    agent_id: str
    timestamp: str
    checkpoint_reference: str
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self, registry: AgentRegistry | None = None) -> list[str]:
        issues: list[str] = []
        if not validate_task_id(self.task_id):
            issues.append("task_id is invalid")
        if not self.stage.strip():
            issues.append("stage is required")
        if not AGENT_ID_RE.fullmatch(self.agent_id):
            issues.append("agent_id is invalid")
        elif registry and registry.get(self.agent_id) is None:
            issues.append("agent_id is not registered")
        if not valid_reference(self.checkpoint_reference):
            issues.append("checkpoint_reference is invalid")
        if not self.summary.strip():
            issues.append("summary is required")
        if not isinstance(self.metadata, dict):
            issues.append("metadata must be an object")
        try:
            parsed = datetime.fromisoformat(self.timestamp)
            if parsed.tzinfo is None:
                issues.append("timestamp must include timezone")
        except ValueError:
            issues.append("timestamp must be ISO-8601")
        return issues

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_task_id(task_id: str) -> bool:
    return bool(TASK_ID_RE.fullmatch(task_id))


def valid_reference(value: str) -> bool:
    if not isinstance(value, str) or not REFERENCE_RE.fullmatch(value):
        return False
    if value.startswith("/") or "://" in value or "\x00" in value:
        return False
    return ".." not in Path(value).parts


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class TaskJournal:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        registry: AgentRegistry | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.registry = registry or default_registry()
        self._init_db()
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS forge_journal_counters (
                    day TEXT PRIMARY KEY,
                    next_value INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS forge_journal_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    transition_key TEXT UNIQUE,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    checkpoint_reference TEXT NOT NULL DEFAULT '',
                    side_effect_state TEXT NOT NULL DEFAULT 'none',
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS forge_journal_events_task ON forge_journal_events(task_id, sequence)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS forge_journal_events_type ON forge_journal_events(event_type, sequence)"
            )

    def now(self) -> datetime:
        return _utc(self.clock())

    def now_iso(self) -> str:
        return self.now().isoformat()

    def next_task_id(self) -> str:
        day = self.now().strftime("%Y%m%d")
        with self._connect() as db:
            row = db.execute("SELECT next_value FROM forge_journal_counters WHERE day=?", (day,)).fetchone()
            value = 1 if row is None else int(row["next_value"])
            if row is None:
                db.execute("INSERT INTO forge_journal_counters(day, next_value) VALUES (?, ?)", (day, value + 1))
            else:
                db.execute("UPDATE forge_journal_counters SET next_value=? WHERE day=?", (value + 1, day))
        return f"FT-{day}-{value:06d}"

    def append_event(
        self,
        task_id: str,
        event_type: str | JournalEventType,
        *,
        agent_id: str = "",
        run_id: str = "",
        stage: str = "",
        message: str = "",
        checkpoint_reference: str = "",
        side_effect_state: str | SideEffectState = SideEffectState.NONE,
        metadata: dict[str, Any] | None = None,
        transition_key: str = "",
        timestamp: str = "",
    ) -> JournalEvent:
        event_type_value = event_type.value if isinstance(event_type, JournalEventType) else str(event_type)
        side_effect_value = side_effect_state.value if isinstance(side_effect_state, SideEffectState) else str(side_effect_state)
        metadata = metadata or {}
        issues = self._validate_event(
            task_id, event_type_value, agent_id, stage, checkpoint_reference,
            side_effect_value, metadata, timestamp or self.now_iso(),
        )
        if issues:
            raise ValueError("; ".join(issues))
        event_id = f"FE-{uuid.uuid4().hex[:20]}"
        timestamp = timestamp or self.now_iso()
        with self._connect() as db:
            params = (
                event_id, transition_key or None, task_id, event_type_value, timestamp,
                agent_id, run_id, stage, message[:2000], checkpoint_reference,
                side_effect_value, json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            )
            if transition_key:
                db.execute(
                    """
                    INSERT OR IGNORE INTO forge_journal_events(
                        event_id, transition_key, task_id, event_type, timestamp,
                        agent_id, run_id, stage, message, checkpoint_reference,
                        side_effect_state, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
                row = db.execute(
                    "SELECT * FROM forge_journal_events WHERE transition_key=?",
                    (transition_key,),
                ).fetchone()
            else:
                db.execute(
                    """
                    INSERT INTO forge_journal_events(
                        event_id, transition_key, task_id, event_type, timestamp,
                        agent_id, run_id, stage, message, checkpoint_reference,
                        side_effect_state, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
                row = db.execute(
                    "SELECT * FROM forge_journal_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
        assert row is not None
        return self._row_to_event(row)

    def _validate_event(
        self,
        task_id: str,
        event_type: str,
        agent_id: str,
        stage: str,
        checkpoint_reference: str,
        side_effect_state: str,
        metadata: dict[str, Any],
        timestamp: str,
    ) -> list[str]:
        issues: list[str] = []
        if not validate_task_id(task_id):
            issues.append("task_id is invalid")
        if event_type not in {item.value for item in JournalEventType}:
            issues.append("event_type is invalid")
        if side_effect_state not in {item.value for item in SideEffectState}:
            issues.append("side_effect_state is invalid")
        if agent_id:
            if not AGENT_ID_RE.fullmatch(agent_id):
                issues.append("agent_id is invalid")
            elif self.registry.get(agent_id) is None:
                issues.append("agent_id is not registered")
        if event_type in {
            JournalEventType.TASK_ASSIGNED.value,
            JournalEventType.LEASE_GRANTED.value,
            JournalEventType.LEASE_RENEWED.value,
            JournalEventType.HEARTBEAT_RECORDED.value,
            JournalEventType.HANDOFF_REQUESTED.value,
            JournalEventType.HANDOFF_COMPLETED.value,
        } and not agent_id:
            issues.append("agent_id is required")
        if event_type == JournalEventType.STAGE_STARTED.value and not stage.strip():
            issues.append("stage is required")
        if checkpoint_reference and not valid_reference(checkpoint_reference):
            issues.append("checkpoint_reference is invalid")
        if event_type == JournalEventType.CHECKPOINT_CREATED.value and not checkpoint_reference:
            issues.append("checkpoint_reference is required")
        if not isinstance(metadata, dict):
            issues.append("metadata must be an object")
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                issues.append("timestamp must include timezone")
        except ValueError:
            issues.append("timestamp must be ISO-8601")
        return issues

    def add_checkpoint(self, checkpoint: CheckpointRecord, *, transition_key: str = "") -> JournalEvent:
        issues = checkpoint.validate(self.registry)
        if issues:
            raise ValueError("; ".join(issues))
        return self.append_event(
            checkpoint.task_id,
            JournalEventType.CHECKPOINT_CREATED,
            agent_id=checkpoint.agent_id,
            stage=checkpoint.stage,
            checkpoint_reference=checkpoint.checkpoint_reference,
            message=checkpoint.summary,
            metadata=checkpoint.metadata,
            transition_key=transition_key,
            timestamp=checkpoint.timestamp,
        )

    def grant_lease(self, task_id: str, agent_id: str, lease_seconds: int, *, transition_key: str = "") -> JournalEvent:
        return self._lease_event(task_id, agent_id, lease_seconds, JournalEventType.LEASE_GRANTED, transition_key)

    def renew_lease(self, task_id: str, agent_id: str, lease_seconds: int, *, transition_key: str = "") -> JournalEvent:
        return self._lease_event(task_id, agent_id, lease_seconds, JournalEventType.LEASE_RENEWED, transition_key)

    def record_heartbeat(self, task_id: str, agent_id: str, *, transition_key: str = "") -> JournalEvent:
        return self.append_event(
            task_id,
            JournalEventType.HEARTBEAT_RECORDED,
            agent_id=agent_id,
            metadata={"heartbeat_at": self.now_iso()},
            transition_key=transition_key,
        )

    def _lease_event(
        self, task_id: str, agent_id: str, lease_seconds: int,
        event_type: JournalEventType, transition_key: str,
    ) -> JournalEvent:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        expires = self.now() + timedelta(seconds=lease_seconds)
        return self.append_event(
            task_id,
            event_type,
            agent_id=agent_id,
            metadata={"lease_expires_at": expires.isoformat(), "lease_seconds": lease_seconds},
            transition_key=transition_key,
        )

    def events(self, task_id: str) -> list[JournalEvent]:
        if not validate_task_id(task_id):
            raise ValueError("task_id is invalid")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM forge_journal_events WHERE task_id=? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def list_tasks(self) -> list[dict[str, Any]]:
        task_ids = self._task_ids()
        return [self.reconstruct(task_id) for task_id in task_ids]

    def reconstruct(self, task_id: str) -> dict[str, Any]:
        events = self.events(task_id)
        status = "unknown"
        agents: list[str] = []
        last_event = ""
        created_at = ""
        updated_at = ""
        for event in events:
            last_event = event.event_type
            updated_at = event.timestamp
            if not created_at:
                created_at = event.timestamp
            if event.agent_id and event.agent_id not in agents:
                agents.append(event.agent_id)
            status = {
                JournalEventType.TASK_CREATED.value: "created",
                JournalEventType.TASK_ASSIGNED.value: "assigned",
                JournalEventType.TASK_STARTED.value: "running",
                JournalEventType.TASK_COMPLETED.value: "completed",
                JournalEventType.TASK_FAILED.value: "failed",
                JournalEventType.TASK_CANCELLED.value: "cancelled",
                JournalEventType.TASK_ORPHANED.value: "orphaned",
                JournalEventType.RECOVERY_PROPOSED.value: "recovery_proposed",
            }.get(event.event_type, status)
        return {
            "task_id": task_id,
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
            "last_event": last_event,
            "agents": agents,
            "event_count": len(events),
        }

    def checkpoints(self, task_id: str) -> list[CheckpointRecord]:
        result: list[CheckpointRecord] = []
        for event in self.events(task_id):
            if event.event_type != JournalEventType.CHECKPOINT_CREATED.value:
                continue
            result.append(CheckpointRecord(
                task_id=event.task_id,
                stage=event.stage,
                agent_id=event.agent_id,
                timestamp=event.timestamp,
                checkpoint_reference=event.checkpoint_reference,
                summary=event.message,
                metadata=event.metadata,
            ))
        return result

    def orphan_candidates(self) -> list[dict[str, Any]]:
        now = self.now()
        candidates: list[dict[str, Any]] = []
        for task in self.list_tasks():
            if task["status"] in {"completed", "failed", "cancelled"}:
                continue
            events = self.events(str(task["task_id"]))
            if any(event.event_type == JournalEventType.TASK_ORPHANED.value for event in events):
                candidates.append({**task, "orphan_status": "confirmed_orphan"})
                continue
            lease_expires_at = self._last_lease_expiration(events)
            if lease_expires_at and lease_expires_at < now:
                candidates.append({
                    **task,
                    "orphan_status": "suspected_orphan",
                    "lease_expires_at": lease_expires_at.isoformat(),
                })
        return candidates

    def recovery_status(self, task_id: str) -> dict[str, Any]:
        task = self.reconstruct(task_id)
        states = [event.side_effect_state for event in self.events(task_id)]
        if any(state in {SideEffectState.STARTED.value, SideEffectState.CONFIRMED.value} for state in states):
            replay_safety = "unsafe"
        elif SideEffectState.UNKNOWN.value in states:
            replay_safety = "requires_review"
        else:
            replay_safety = "safe"
        return {
            **task,
            "replay_safety": replay_safety,
            "recovery_allowed": replay_safety == "safe" and task["status"] in {"failed", "orphaned", "recovery_proposed"},
            "side_effect_states": states,
        }

    def _last_lease_expiration(self, events: Iterable[JournalEvent]) -> datetime | None:
        expires_at: datetime | None = None
        for event in events:
            value = event.metadata.get("lease_expires_at")
            if isinstance(value, str):
                try:
                    expires_at = datetime.fromisoformat(value)
                except ValueError:
                    pass
        return expires_at

    def _task_ids(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT task_id, MIN(sequence) AS first_sequence FROM forge_journal_events GROUP BY task_id ORDER BY first_sequence"
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> JournalEvent:
        return JournalEvent(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            task_id=str(row["task_id"]),
            event_type=str(row["event_type"]),
            timestamp=str(row["timestamp"]),
            agent_id=str(row["agent_id"]),
            run_id=str(row["run_id"]),
            stage=str(row["stage"]),
            message=str(row["message"]),
            checkpoint_reference=str(row["checkpoint_reference"]),
            side_effect_state=str(row["side_effect_state"]),
            metadata=json.loads(row["metadata"] or "{}"),
        )
