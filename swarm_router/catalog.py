from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from contextlib import contextmanager
import json
import sqlite3
import statistics

from .config import ReliabilityConfig
from .quality import HALLUCINATION_CATEGORIES, POSITIVE_CATEGORIES, QUALITY_CATEGORIES


@dataclass(frozen=True)
class ModelRecord:
    model_id: str
    provider: str
    family: str
    kind: str
    capabilities: tuple[str, ...]
    enabled: bool
    available: bool
    context_length: int | None
    quality: int
    speed: int
    notes: str
    last_seen: str
    probe_status: str
    probe_ms: int | None
    probe_error: str
    last_probe: str
    last_successful_probe: str
    last_failure: str


def infer_model_metadata(model_id: str) -> tuple[str, tuple[str, ...]]:
    name = model_id.lower()
    non_chat = {
        "embedding": ("embed", "embedding", "bge-"),
        "reranker": ("rerank", "ranker"),
        "guardrail": ("guard", "safety", "shield", "pii", "detector", "reward"),
        "image": ("diffusion", "flux", "stable-diffusion", "image-gen"),
        "speech": ("tts", "asr", "whisper", "speech", "parakeet"),
        "ocr": ("ocr", "deplot"),
        "retrieval": ("retriever", "retrieval", "nemotron-parse", "nemoretriever-parse"),
        "specialist": ("ising-calibration", "riva-translate", "palmyra-fin", "palmyra-med"),
    }
    for kind, tokens in non_chat.items():
        if any(token in name for token in tokens):
            return kind, (kind,)

    capabilities: list[str] = ["chat"]
    if any(token in name for token in ("coder", "code", "codestral", "devstral")):
        capabilities.append("code")
    if any(token in name for token in ("reason", "thinking", "r1", "gpt-oss", "qwq")):
        capabilities.append("reasoning")
    if any(token in name for token in ("vision", "vl", "multimodal", "omni")):
        capabilities.append("vision")
    if any(token in name for token in ("instruct", "chat", "llama", "mistral", "qwen", "gemma", "nemotron", "gpt")):
        capabilities.append("general")
    return "chat", tuple(dict.fromkeys(capabilities))


def infer_provider_family(model_id: str, provider_hint: str = "") -> tuple[str, str]:
    provider = provider_hint.strip() or (model_id.split("/", 1)[0] if "/" in model_id else "unknown")
    leaf = model_id.rsplit("/", 1)[-1].lower()
    for token in ("deepseek", "minimax", "qwen", "nemotron", "mistral", "llama", "gemma", "phi", "gpt-oss"):
        if token in leaf:
            return provider, f"{provider}/{token}"
    return provider, f"{provider}/{leaf.split('-', 1)[0]}"


def _context_length(item: dict[str, Any]) -> int | None:
    candidates = [item.get("context_length")]
    info = item.get("info") if isinstance(item.get("info"), dict) else {}
    meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
    candidates.extend((info.get("context_length"), meta.get("context_length")))
    for value in candidates:
        try:
            parsed = int(value)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return None


class ModelCatalog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
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
                CREATE TABLE IF NOT EXISTS models (
                    model_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT '',
                    family TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    available INTEGER NOT NULL DEFAULT 1,
                    context_length INTEGER,
                    quality INTEGER NOT NULL DEFAULT 0,
                    speed INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    last_seen TEXT NOT NULL DEFAULT '',
                    probe_status TEXT NOT NULL DEFAULT 'untested',
                    probe_ms INTEGER,
                    probe_error TEXT NOT NULL DEFAULT '',
                    last_probe TEXT NOT NULL DEFAULT '',
                    last_successful_probe TEXT NOT NULL DEFAULT '',
                    last_failure TEXT NOT NULL DEFAULT ''
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS task_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    elapsed_ms INTEGER NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS task_attempts_model_time ON task_attempts(model_id, attempted_at DESC)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS task_attempts_run_role_model ON task_attempts(run_id, role, model_id)"
            )
            existing = {row[1] for row in db.execute("PRAGMA table_info(models)")}
            for name, definition in {
                "provider": "TEXT NOT NULL DEFAULT ''",
                "family": "TEXT NOT NULL DEFAULT ''",
                "available": "INTEGER NOT NULL DEFAULT 1",
                "context_length": "INTEGER",
                "last_probe": "TEXT NOT NULL DEFAULT ''",
                "last_successful_probe": "TEXT NOT NULL DEFAULT ''",
                "last_failure": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE models ADD COLUMN {name} {definition}")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS probe_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL,
                    probed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    elapsed_ms INTEGER NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS quality_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    category TEXT NOT NULL,
                    severity INTEGER NOT NULL,
                    judge_caught INTEGER NOT NULL DEFAULT 0,
                    reached_final INTEGER NOT NULL DEFAULT 0,
                    codex_verified INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, model_id, role, category, note)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS quality_events_model_role ON quality_events(model_id, role, created_at DESC)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS benchmark_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    benchmark_id TEXT NOT NULL,
                    benchmark_version INTEGER NOT NULL DEFAULT 1,
                    run_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    response_path TEXT NOT NULL DEFAULT '',
                    checks TEXT NOT NULL DEFAULT '{}',
                    dimensions TEXT NOT NULL DEFAULT '{}',
                    evaluator_source TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    evaluated_at TEXT NOT NULL,
                    UNIQUE(benchmark_id, run_id, model_id, role)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS benchmark_results_model_role ON benchmark_results(model_id, role, evaluated_at DESC)"
            )

    def sync(self, models: Iterable[str | dict[str, Any]]) -> list[ModelRecord]:
        now = datetime.now(timezone.utc).isoformat()
        items: dict[str, dict[str, Any]] = {}
        for value in models:
            item = {"id": value} if isinstance(value, str) else value
            model_id = item.get("id") or item.get("name")
            if model_id:
                items[str(model_id)] = item
        with self._connect() as db:
            db.execute("UPDATE models SET available=0")
            for model_id, item in sorted(items.items()):
                kind, capabilities = infer_model_metadata(model_id)
                provider, family = infer_provider_family(model_id, str(item.get("provider", "")))
                db.execute(
                    """
                    INSERT INTO models(model_id, provider, family, kind, capabilities, enabled, available, context_length, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(model_id) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        available=1,
                        provider=excluded.provider,
                        family=excluded.family,
                        context_length=COALESCE(excluded.context_length, models.context_length),
                        kind=CASE WHEN models.kind='unknown' THEN excluded.kind ELSE models.kind END,
                        capabilities=CASE WHEN models.capabilities='[]' THEN excluded.capabilities ELSE models.capabilities END
                    """,
                    (
                        model_id,
                        provider,
                        family,
                        kind,
                        json.dumps(capabilities),
                        0 if kind != "chat" else 1,
                        _context_length(item),
                        now,
                    ),
                )
        return self.list()

    def list(self) -> list[ModelRecord]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM models ORDER BY enabled DESC, kind, model_id"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get(self, model_id: str) -> ModelRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM models WHERE model_id=?", (model_id,)).fetchone()
        return None if row is None else self._row_to_record(row)

    def update(self, model_id: str, **fields: Any) -> ModelRecord:
        allowed = {"kind", "capabilities", "enabled", "quality", "speed", "notes", "context_length"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if "capabilities" in updates:
            updates["capabilities"] = json.dumps(list(updates["capabilities"]))
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        if not updates:
            record = self.get(model_id)
            if record is None:
                raise KeyError(model_id)
            return record
        assignments = ", ".join(f"{key}=?" for key in updates)
        values = list(updates.values()) + [model_id]
        with self._connect() as db:
            cursor = db.execute(f"UPDATE models SET {assignments} WHERE model_id=?", values)
            if cursor.rowcount == 0:
                kind, caps = infer_model_metadata(model_id)
                db.execute(
                    "INSERT INTO models(model_id, kind, capabilities) VALUES (?, ?, ?)",
                    (model_id, kind, json.dumps(caps)),
                )
                db.execute(f"UPDATE models SET {assignments} WHERE model_id=?", values)
        record = self.get(model_id)
        assert record is not None
        return record

    def record_probe(self, model_id: str, status: str, elapsed_ms: int, error: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """
                UPDATE models
                SET probe_status=?, probe_ms=?, probe_error=?, last_probe=?,
                    last_successful_probe=CASE WHEN ?='healthy' THEN ? ELSE last_successful_probe END,
                    last_failure=CASE WHEN ?='failed' THEN ? ELSE last_failure END
                WHERE model_id=?
                """,
                (status, elapsed_ms, error[:2000], now, status, now, status, now, model_id),
            )
            db.execute(
                "INSERT INTO probe_history(model_id, probed_at, status, elapsed_ms, error) VALUES (?, ?, ?, ?, ?)",
                (model_id, now, status, elapsed_ms, error[:2000]),
            )

    def probe_history(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT model_id, probed_at, status, elapsed_ms, error FROM probe_history ORDER BY id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_task_attempt(
        self,
        run_id: str,
        model_id: str,
        role: str,
        mode: str,
        status: str,
        elapsed_ms: int,
        retry_count: int = 0,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO task_attempts(
                    run_id, model_id, role, mode, attempted_at, status, elapsed_ms, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    model_id,
                    role,
                    mode,
                    datetime.now(timezone.utc).isoformat(),
                    status,
                    max(0, elapsed_ms),
                    max(0, retry_count),
                ),
            )

    def import_run_history(self, run_directory: str | Path) -> int:
        root = Path(run_directory).expanduser().resolve()
        imported = 0
        if not root.exists():
            return imported
        terminal = {
            "worker_returned": "success",
            "worker_failed": "failure",
            "judge_returned": "success",
            "judge_failed": "failure",
        }
        with self._connect() as db:
            for run_dir in root.iterdir():
                if not run_dir.is_dir() or not (run_dir / "events.jsonl").exists():
                    continue
                try:
                    task = json.loads((run_dir / "task.json").read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    task = {}
                mode = str(task.get("mode", "auto"))
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_name = str(event.get("event", ""))
                    if event_name not in terminal:
                        continue
                    role = "__judge__" if event_name.startswith("judge_") else str(event.get("agent", ""))
                    model_id = str(event.get("model", ""))
                    if not role or not model_id:
                        continue
                    category = str(event.get("failure_category", ""))
                    if not category and event_name.endswith("failed"):
                        message = str(event.get("error", "")).lower()
                        category = "timeout" if "timed out" in message else "failure"
                    status = category or terminal[event_name]
                    before = db.total_changes
                    db.execute(
                        """
                        INSERT OR IGNORE INTO task_attempts(
                            run_id, model_id, role, mode, attempted_at, status, elapsed_ms, retry_count
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_dir.name, model_id, role, mode,
                            str(event.get("time", "")), status,
                            max(0, int(event.get("duration_ms", 0))),
                            max(0, int(event.get("retry_count", 0))),
                        ),
                    )
                    imported += db.total_changes - before
        return imported

    def reliability_summary(
        self, model_id: str, policy: ReliabilityConfig
    ) -> dict[str, Any]:
        window = policy.recent_attempt_window
        with self._connect() as db:
            tasks = db.execute(
                """
                SELECT attempted_at, status, elapsed_ms
                FROM task_attempts WHERE model_id=? ORDER BY attempted_at DESC, id DESC LIMIT ?
                """,
                (model_id, window),
            ).fetchall()
            probes = db.execute(
                """
                SELECT status, elapsed_ms FROM probe_history
                WHERE model_id=? ORDER BY id DESC LIMIT ?
                """,
                (model_id, window),
            ).fetchall()
        statuses = [str(row["status"]) for row in tasks]
        successes = sum(status == "success" for status in statuses)
        timeouts = sum(status == "timeout" for status in statuses)
        protocol_failures = sum(status == "protocol" for status in statuses)
        capacity_failures = sum(status == "capacity" for status in statuses)
        consecutive_failures = 0
        for status in statuses:
            if status == "success":
                break
            consecutive_failures += 1
        successful_latencies = [int(row["elapsed_ms"]) for row in tasks if row["status"] == "success"]
        last_success = next(
            (str(row["attempted_at"]) for row in tasks if row["status"] == "success"), ""
        )
        probe_successes = sum(row["status"] == "healthy" for row in probes)
        penalty = 0.0
        if tasks and policy.enabled:
            penalty += (timeouts / len(tasks)) * policy.timeout_penalty
            penalty += ((len(tasks) - successes - timeouts) / len(tasks)) * policy.failure_penalty
            penalty += consecutive_failures * policy.consecutive_failure_penalty
            penalty += protocol_failures * policy.failure_penalty
        cooldown_until = ""
        in_cooldown = False
        if tasks and (statuses[0] == "capacity" or consecutive_failures >= policy.cooldown_after_consecutive_failures):
            latest = datetime.fromisoformat(str(tasks[0]["attempted_at"]))
            until = latest + timedelta(minutes=policy.cooldown_minutes)
            in_cooldown = until > datetime.now(timezone.utc)
            if in_cooldown:
                cooldown_until = until.isoformat()
        return {
            "recent_attempts": len(tasks),
            "recent_success_rate": None if not tasks else round(successes / len(tasks), 3),
            "recent_timeout_rate": None if not tasks else round(timeouts / len(tasks), 3),
            "consecutive_failures": consecutive_failures,
            "recent_protocol_failures": protocol_failures,
            "recent_capacity_failures": capacity_failures,
            "recent_median_latency_ms": None if not successful_latencies else int(statistics.median(successful_latencies)),
            "last_successful_completion": last_success,
            "probe_attempts": len(probes),
            "probe_success_rate": None if not probes else round(probe_successes / len(probes), 3),
            "reliability_penalty": round(penalty, 3),
            "cooldown": in_cooldown,
            "cooldown_until": cooldown_until,
        }

    def record_quality_event(
        self, run_id: str, model_id: str, role: str, mode: str, category: str,
        severity: int, judge_caught: bool = False, reached_final: bool = False,
        codex_verified: bool = False, note: str = "",
    ) -> None:
        if category not in QUALITY_CATEGORIES:
            raise ValueError(f"Unknown quality category: {category}")
        if severity not in {0, 1, 2, 3}:
            raise ValueError("Quality severity must be 0 through 3.")
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO quality_events(
                    run_id, model_id, role, mode, category, severity,
                    judge_caught, reached_final, codex_verified, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, model_id, role, mode, category, severity,
                    int(judge_caught), int(reached_final), int(codex_verified),
                    note[:500], datetime.now(timezone.utc).isoformat(),
                ),
            )

    def record_benchmark_result(
        self, benchmark_id: str, run_id: str, model_id: str, role: str, mode: str,
        response_path: str, checks: dict[str, Any], dimensions: dict[str, int],
        evaluator_source: str, note: str = "", benchmark_version: int = 1,
    ) -> None:
        if any(value not in {0, 1, 2} for value in dimensions.values()):
            raise ValueError("Benchmark dimensions must use the 0-2 scale.")
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO benchmark_results(
                    benchmark_id, benchmark_version, run_id, model_id, role, mode,
                    response_path, checks, dimensions, evaluator_source, note, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(benchmark_id, run_id, model_id, role) DO UPDATE SET
                    response_path=excluded.response_path,
                    checks=excluded.checks,
                    dimensions=excluded.dimensions,
                    evaluator_source=excluded.evaluator_source,
                    note=excluded.note,
                    evaluated_at=excluded.evaluated_at
                """,
                (
                    benchmark_id, benchmark_version, run_id, model_id, role, mode,
                    response_path, json.dumps(checks), json.dumps(dimensions),
                    evaluator_source, note[:500], datetime.now(timezone.utc).isoformat(),
                ),
            )

    def benchmark_results(
        self, limit: int = 100, model_id: str = ""
    ) -> list[dict[str, Any]]:
        where = "WHERE model_id=?" if model_id else ""
        params: tuple[Any, ...] = (model_id, max(1, limit)) if model_id else (max(1, limit),)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT benchmark_id, benchmark_version, run_id, model_id, role,
                mode, response_path, checks, dimensions, evaluator_source, note, evaluated_at
                FROM benchmark_results {where} ORDER BY evaluated_at DESC LIMIT ?""",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["checks"] = json.loads(item["checks"] or "{}")
            item["dimensions"] = json.loads(item["dimensions"] or "{}")
            result.append(item)
        return result

    def quality_summary(self, model_id: str, role: str = "") -> dict[str, Any]:
        clause = "AND role=?" if role else ""
        params: tuple[Any, ...] = (model_id, role) if role else (model_id,)
        with self._connect() as db:
            events = db.execute(
                f"""SELECT run_id, category, severity, judge_caught, reached_final,
                codex_verified, note, created_at FROM quality_events
                WHERE model_id=? {clause} ORDER BY created_at DESC LIMIT 50""",
                params,
            ).fetchall()
            results = db.execute(
                f"""SELECT benchmark_id, run_id, dimensions, evaluator_source, note,
                evaluated_at FROM benchmark_results WHERE model_id=? {clause}
                ORDER BY evaluated_at DESC LIMIT 20""",
                params,
            ).fetchall()
        dimensions = [
            value for row in results for value in json.loads(row["dimensions"] or "{}").values()
        ]
        role_score = None if not dimensions else round(sum(dimensions) / len(dimensions) * 5, 1)
        categories: dict[str, int] = {}
        for row in events:
            category = str(row["category"])
            categories[category] = categories.get(category, 0) + 1
        positive = sum(count for category, count in categories.items() if category in POSITIVE_CATEGORIES)
        negative_rows = [row for row in events if row["category"] not in POSITIVE_CATEGORIES]
        # Events and a benchmark review from one dispatch are annotations of
        # the same evidence, not independent observations.
        evidence_count = len(
            {str(row["run_id"]) for row in events}
            | {str(row["run_id"]) for row in results}
        )
        evidence_weight = min(evidence_count / 3, 1.0)
        severity_points = sum(int(row["severity"]) for row in negative_rows)
        repeats = sum(max(0, count - 1) for category, count in categories.items() if category not in POSITIVE_CATEGORIES)
        penalty = min(
            1.5,
            max(0.0, (severity_points + repeats - positive * 1.5) / max(evidence_count, 1))
            * 0.5 * evidence_weight,
        )
        score_term = 0.0 if role_score is None else ((role_score - 5.0) / 5.0) * 1.5 * evidence_weight
        contribution = round(score_term - penalty, 2)
        reviewed = max(1, len({str(row["run_id"]) for row in events}))
        hallucination_runs = len({str(row["run_id"]) for row in events if row["category"] in HALLUCINATION_CATEGORIES})
        final_defect_runs = len({str(row["run_id"]) for row in negative_rows if row["reached_final"]})
        caught = sum(bool(row["judge_caught"]) for row in negative_rows)
        failures = [
            f"{category} ({count})" for category, count in sorted(categories.items())
            if category not in POSITIVE_CATEGORIES
        ]
        strengths = [
            f"{category} ({count})" for category, count in sorted(categories.items())
            if category in POSITIVE_CATEGORIES
        ]
        return {
            "role": role or "all",
            "role_quality_score": role_score,
            "quality_evidence_count": evidence_count,
            "quality_provisional": evidence_count < 3,
            "known_strengths": strengths,
            "known_failure_categories": failures,
            "clean_candidate_rate": round(categories.get("clean_candidate", 0) / reviewed, 3),
            "hallucination_event_rate": round(hallucination_runs / reviewed, 3),
            "judge_catch_rate": None if not negative_rows else round(caught / len(negative_rows), 3),
            "final_synthesis_defect_rate": round(final_defect_runs / reviewed, 3),
            "quality_penalty": round(penalty, 2),
            "quality_contribution": contribution,
            "last_evaluation": str(results[0]["evaluated_at"]) if results else (str(events[0]["created_at"]) if events else ""),
        }

    def quality_profile(self, model_id: str) -> dict[str, Any]:
        roles = ("planner", "implementer", "critic", "verifier", "__judge__")
        by_role = {role: self.quality_summary(model_id, role) for role in roles}
        recommended = [
            role for role, item in by_role.items()
            if item["quality_evidence_count"] >= 3
            and item["role_quality_score"] is not None
            and item["role_quality_score"] >= 7
            and item["quality_contribution"] >= 0
        ]
        discouraged = [
            role for role, item in by_role.items()
            if item["quality_evidence_count"] >= 3 and item["quality_contribution"] <= -0.5
        ]
        return {
            **self.quality_summary(model_id),
            "quality_by_role": by_role,
            "recommended_roles": recommended,
            "discouraged_roles": discouraged,
        }

    def recommend(
        self,
        mode: str,
        count: int,
        policy: ReliabilityConfig | None = None,
        role: str = "",
        excluded_models: set[str] | None = None,
        used_families: set[str] | None = None,
    ) -> list[ModelRecord]:
        desired = {
            "code": "code",
            "research": "reasoning",
            "spec": "reasoning",
            "general": "general",
            "auto": "reasoning",
        }.get(mode, "general")
        if role == "implementer" and mode == "code":
            desired = "code"
        elif role in {"planner", "critic", "verifier", "__judge__"}:
            desired = "reasoning"
        records = [
            record for record in self.list()
            if record.enabled and record.available and record.kind == "chat"
            and record.probe_status == "healthy"
            and record.model_id not in (excluded_models or set())
        ]
        metrics = {
            record.model_id: self.reliability_summary(record.model_id, policy)
            for record in records
        } if policy else {}
        quality = {record.model_id: self.quality_summary(record.model_id, role) for record in records}
        if policy and policy.enabled:
            records = [record for record in records if not metrics[record.model_id]["cooldown"]]

        def score(record: ModelRecord) -> tuple[float, int, int, str]:
            capability_score = 5.0 if desired in record.capabilities else 0.0
            evidence = metrics.get(record.model_id, {})
            penalty = float(evidence.get("reliability_penalty", 0.0)) * 10
            latency = evidence.get("recent_median_latency_ms") or record.probe_ms or 10**9
            latency_penalty = 0.0
            if policy:
                latency_penalty = min(float(latency) / 200_000.0, 1.0) * policy.latency_weight
            return (
                capability_score + record.quality - penalty - latency_penalty
                + float(quality[record.model_id]["quality_contribution"])
                + (0.05 if used_families and record.family not in used_families else 0.0),
                -(int(latency)),
                -len(record.model_id),
                record.model_id,
            )
        return sorted(records, key=score, reverse=True)[: max(0, count)]

    def recommendation_reason(
        self, record: ModelRecord, mode: str, policy: ReliabilityConfig, role: str = ""
    ) -> str:
        desired = {"code": "code", "research": "reasoning", "spec": "reasoning", "general": "general", "auto": "reasoning"}.get(mode, "general")
        if role == "implementer" and mode == "code":
            desired = "code"
        elif role in {"planner", "critic", "verifier", "__judge__"}:
            desired = "reasoning"
        evidence = self.reliability_summary(record.model_id, policy)
        quality = self.quality_summary(record.model_id, role)
        capability = f"; {desired} capability" if desired in record.capabilities else ""
        latency_value = evidence["recent_median_latency_ms"] or record.probe_ms
        latency = f"; representative latency {latency_value} ms" if latency_value is not None else ""
        task_rate = evidence["recent_success_rate"]
        reliability = "; no recent task evidence" if task_rate is None else (
            f"; recent task success {task_rate:.0%}; timeout {evidence['recent_timeout_rate']:.0%}; "
            f"failure streak {evidence['consecutive_failures']}"
        )
        probe_rate = evidence["probe_success_rate"]
        probe_evidence = "" if probe_rate is None else f"; recent probe success {probe_rate:.0%}"
        return (
            f"Automatic: enabled, exposed, exact chat probe healthy{capability}; "
            f"quality {record.quality}/10{reliability}{probe_evidence}{latency}; reliability penalty "
            f"{evidence['reliability_penalty']:.2f}; role quality {quality['role_quality_score']}; "
            f"quality evidence {quality['quality_evidence_count']} ({'provisional' if quality['quality_provisional'] else 'established'}); "
            f"quality contribution {quality['quality_contribution']:+.2f}; strengths {', '.join(quality['known_strengths']) or 'none recorded'}; "
            f"failures {', '.join(quality['known_failure_categories']) or 'none recorded'}; family {record.family}; family diversity is secondary."
        )

    def explicit_override_reason(
        self, record: ModelRecord | None, policy: ReliabilityConfig
    ) -> str:
        if record is None:
            return "Explicit override bypassed automatic reliability recommendations; model has no catalog evidence."
        evidence = self.reliability_summary(record.model_id, policy)
        quality = self.quality_summary(record.model_id)
        warning = " cooldown active" if evidence["cooldown"] else ""
        return (
            "Explicit override bypassed automatic reliability recommendations; "
            f"recent success {evidence['recent_success_rate']}; timeout {evidence['recent_timeout_rate']}; "
            f"failure streak {evidence['consecutive_failures']}; penalty "
            f"{evidence['reliability_penalty']:.2f}; quality evidence {quality['quality_evidence_count']} "
            f"({'provisional' if quality['quality_provisional'] else 'established'});{warning or ' no cooldown'}."
        )

    def as_dict(
        self, record: ModelRecord, policy: ReliabilityConfig | None = None
    ) -> dict[str, Any]:
        result = asdict(record)
        if policy is not None:
            result.update(self.reliability_summary(record.model_id, policy))
            result.update(self.quality_profile(record.model_id))
        return result

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ModelRecord:
        return ModelRecord(
            model_id=str(row["model_id"]),
            provider=str(row["provider"]),
            family=str(row["family"]),
            kind=str(row["kind"]),
            capabilities=tuple(json.loads(row["capabilities"] or "[]")),
            enabled=bool(row["enabled"]),
            available=bool(row["available"]),
            context_length=None if row["context_length"] is None else int(row["context_length"]),
            quality=int(row["quality"]),
            speed=int(row["speed"]),
            notes=str(row["notes"]),
            last_seen=str(row["last_seen"]),
            probe_status=str(row["probe_status"]),
            probe_ms=None if row["probe_ms"] is None else int(row["probe_ms"]),
            probe_error=str(row["probe_error"]),
            last_probe=str(row["last_probe"]),
            last_successful_probe=str(row["last_successful_probe"]),
            last_failure=str(row["last_failure"]),
        )
