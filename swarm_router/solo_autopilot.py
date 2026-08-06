"""Durable single-model engineering autopilot for Forge."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Mapping, Protocol
import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import uuid

from .autopilot_adapter import OpenTerminalClient, PROCESS_TOOLS
from .client import OpenWebUIClient
from .config import AppConfig, load_config
from .developer import DeveloperCoordinator, LOCK_SECONDS, STALE_SECONDS


DEFAULT_MODEL = "deepseek-ai/deepseek-v4-pro"
DEFAULT_CONFIG = Path.home() / ".config/owui-swarm/config.toml"
DEFAULT_ENV = Path.home() / ".config/owui-swarm/environment"
DEFAULT_DB = Path.home() / ".local/share/owui-swarm/catalog.sqlite3"
DEFAULT_STATE = Path.home() / ".local/share/forge-solo"
DEFAULT_REPO = Path.home() / "openwebui-codex-swarm"
DEFAULT_MANIFEST = Path.home() / "qwen-forge-autopilot/tasks/forge-planning.json"

FINAL = {"CONTINUE", "READY_FOR_REVIEW", "BLOCKED"}
ACTIVE = {"created", "pending", "queued", "running"}
TERMINAL = {
    "cancelled", "completed", "done", "error", "failed", "killed",
    "passed", "success", "succeeded", "terminated", "timeout",
}
CONTEXT_MARKERS = (
    "context length", "context size", "context window",
    "context_length_exceeded", "maximum context",
    "prompt is too long", "too many tokens",
)
CAPACITY_MARKERS = (
    "capacity", "internal server error", "overloaded",
    "provider unavailable", "quota", "rate limit",
    "resource exhausted", "resourceexhausted",
    "temporarily unavailable", "too many requests",
)
SECRET_RE = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|cookie|password|secret|token)"
    r"\b\s*(?::|=|\bis\b)\s*\S+"
)


class SoloError(RuntimeError):
    pass


class WriterBusy(SoloError):
    pass


class WriterLeaseLost(SoloError):
    pass


class WriterLease(Protocol):
    def acquire(
        self,
        lease_id: str = "",
        *,
        status: str = "running",
        active_process: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def release(
        self,
        lease_id: str,
        *,
        status: str = "completed",
    ) -> None: ...


class NoopWriterLease:
    def acquire(
        self,
        lease_id: str = "",
        *,
        status: str = "running",
        active_process: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "workspace": "/workspace/forge",
            "task_id": "noop",
            "lease_id": lease_id or "noop",
            "state": "locked",
        }

    def release(
        self,
        lease_id: str,
        *,
        status: str = "completed",
    ) -> None:
        return None


class SharedWriterLease:
    # Shares the established Forge writer-lock table without a fake run.

    workspace = "/workspace/forge"

    def __init__(
        self,
        database: Path,
        owner: str,
        model_id: str = DEFAULT_MODEL,
    ) -> None:
        self.database = database.expanduser()
        self.owner = owner
        self.model_id = model_id

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _expired(value: Any, current: datetime) -> bool:
        try:
            return datetime.fromisoformat(str(value)) <= current
        except ValueError:
            return True

    @staticmethod
    def _run_columns(db: sqlite3.Connection) -> set[str]:
        return {
            str(row[1])
            for row in db.execute(
                "PRAGMA table_info(forge_developer_runs)"
            )
        }

    def _sync_run(
        self,
        db: sqlite3.Connection,
        *,
        status: str,
        active_process: Mapping[str, Any] | None,
        lease_id: str,
    ) -> None:
        columns = self._run_columns(db)

        if not columns:
            raise WriterLeaseLost(
                "forge_developer_runs is unavailable for Solo fencing."
            )

        stamp = now()
        values: dict[str, Any] = {
            "task_id": self.owner,
            "status": status,
            "phase": "solo",
            "instruction": (
                "Durable DeepSeek Solo engineering task."
            ),
            "instruction_digest": digest(self.owner),
            "selected_model": self.model_id,
            "selected_provider": "openwebui",
            "attempts": "[]",
            "pending_tool_calls": "[]",
            "last_tool_summary": "",
            "created_at": stamp,
            "updated_at": stamp,
            "role_models": "{}",
            "role_outputs": "{}",
            "changed_files": "[]",
            "test_state": "not_started",
            "review_state": (
                "completed"
                if status == "ready_for_review"
                else "not_started"
            ),
            "failure_summary": "",
            "request_shape": "{}",
            "phase_evidence": "{}",
            "active_process": json.dumps(
                dict(active_process or {}),
                separators=(",", ":"),
                sort_keys=True,
            ),
            "writer_lease_id": lease_id,
            "resume_call_id": "",
            "resume_tool_results": "{}",
        }

        insert_columns = [
            name
            for name in values
            if name in columns
        ]

        required = {
            "task_id",
            "status",
            "updated_at",
            "active_process",
        }

        if not required.issubset(insert_columns):
            raise WriterLeaseLost(
                "forge_developer_runs lacks required Solo fencing columns."
            )

        update_columns = [
            name
            for name in insert_columns
            if name not in {
                "task_id",
                "created_at",
            }
        ]

        placeholders = ", ".join(
            "?"
            for _ in insert_columns
        )
        assignments = ", ".join(
            f"{name}=excluded.{name}"
            for name in update_columns
        )

        db.execute(
            f"""
            INSERT INTO forge_developer_runs(
                {", ".join(insert_columns)}
            )
            VALUES ({placeholders})
            ON CONFLICT(task_id) DO UPDATE SET
                {assignments}
            """,
            tuple(
                values[name]
                for name in insert_columns
            ),
        )

    def acquire(
        self,
        lease_id: str = "",
        *,
        status: str = "running",
        active_process: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = datetime.now(timezone.utc)
        expires = current + timedelta(seconds=LOCK_SECONDS)
        selected_lease = lease_id or uuid.uuid4().hex

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM forge_developer_writer_lock WHERE workspace=?",
                (self.workspace,),
            ).fetchone()

            if lease_id and (
                not row
                or str(row["task_id"]) != self.owner
                or str(row["lease_id"]) != lease_id
            ):
                raise WriterLeaseLost(
                    "DeepSeek Solo no longer owns the exact Forge writer lease."
                )

            if row:
                expired = self._expired(row["expires_at"], current)
                row_owner = str(row["task_id"])

                if row_owner == self.owner:
                    selected_lease = str(row["lease_id"]) or selected_lease
                else:
                    owner = db.execute(
                        """
                        SELECT status, updated_at, active_process
                        FROM forge_developer_runs
                        WHERE task_id=?
                        """,
                        (row_owner,),
                    ).fetchone()
                    pending = db.execute(
                        """
                        SELECT 1
                        FROM forge_developer_pending_calls
                        WHERE task_id=?
                        LIMIT 1
                        """,
                        (row_owner,),
                    ).fetchone()
                    try:
                        active = bool(
                            owner
                            and json.loads(str(owner["active_process"] or "{}"))
                        )
                    except (json.JSONDecodeError, TypeError):
                        active = True
                    owner_terminal = (
                        not owner
                        or str(owner["status"])
                        in {"completed", "failed", "cancelled", "blocked"}
                    )
                    try:
                        inactive = (
                            not owner
                            or datetime.fromisoformat(str(owner["updated_at"]))
                            <= current - timedelta(seconds=STALE_SECONDS)
                        )
                    except ValueError:
                        inactive = True
                    if pending or active or not (
                        expired and (owner_terminal or inactive)
                    ):
                        raise WriterBusy(
                            f"Forge writer is busy with run {row_owner}."
                        )
                    selected_lease = uuid.uuid4().hex

            self._sync_run(
                db,
                status=status,
                active_process=active_process,
                lease_id=selected_lease,
            )

            db.execute(
                """
                INSERT INTO forge_developer_writer_lock(
                    workspace, task_id, acquired_at, expires_at, lease_id
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace) DO UPDATE SET
                    task_id=excluded.task_id,
                    acquired_at=excluded.acquired_at,
                    expires_at=excluded.expires_at,
                    lease_id=excluded.lease_id
                """,
                (
                    self.workspace,
                    self.owner,
                    current.isoformat(),
                    expires.isoformat(),
                    selected_lease,
                ),
            )

        return {
            "workspace": self.workspace,
            "task_id": self.owner,
            "lease_id": selected_lease,
            "expires_at": expires.isoformat(),
            "state": "locked",
        }

    def release(
        self,
        lease_id: str,
        *,
        status: str = "completed",
    ) -> None:
        if not lease_id:
            return

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute(
                """
                SELECT task_id, lease_id
                FROM forge_developer_writer_lock
                WHERE workspace=?
                """,
                (self.workspace,),
            ).fetchone()

            if not row:
                raise WriterLeaseLost(
                    "DeepSeek Solo writer lease disappeared "
                    "before release."
                )

            if (
                str(row["task_id"]) != self.owner
                or str(row["lease_id"]) != lease_id
            ):
                raise WriterLeaseLost(
                    "DeepSeek Solo writer lease token no "
                    "longer matches."
                )

            pending = db.execute(
                """
                SELECT 1
                FROM forge_developer_pending_calls
                WHERE task_id=?
                LIMIT 1
                """,
                (self.owner,),
            ).fetchone()

            run = db.execute(
                """
                SELECT active_process
                FROM forge_developer_runs
                WHERE task_id=?
                """,
                (self.owner,),
            ).fetchone()

            try:
                active = bool(
                    run
                    and json.loads(
                        str(
                            run["active_process"]
                            or "{}"
                        )
                    )
                )
            except (
                json.JSONDecodeError,
                TypeError,
            ):
                active = True

            if pending or active:
                raise WriterBusy(
                    "DeepSeek Solo writer cannot be "
                    "released with in-flight work."
                )

            removed = db.execute(
                """
                DELETE FROM forge_developer_writer_lock
                WHERE workspace=?
                  AND task_id=?
                  AND lease_id=?
                """,
                (
                    self.workspace,
                    self.owner,
                    lease_id,
                ),
            ).rowcount

            if removed != 1:
                raise WriterLeaseLost(
                    "DeepSeek Solo writer lease could not "
                    "be released by exact token."
                )

            self._sync_run(
                db,
                status=status,
                active_process={},
                lease_id="",
            )


class Gateway(Protocol):
    model_id: str
    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]: ...


class Terminal(Protocol):
    def execute_tool_call(self, call: dict[str, Any]) -> str: ...


class Policy(Protocol):
    def validate(
        self,
        call: dict[str, Any],
        *,
        active_process: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    requirements: tuple[str, ...]
    allowed_paths: tuple[str, ...]

    @classmethod
    def load(cls, path: Path, task_id: str) -> "Task":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("tasks", []) if isinstance(payload, dict) else payload
        matches = [
            row for row in rows
            if isinstance(row, dict)
            and str(row.get("id") or row.get("task_id") or "") == task_id
        ]
        if len(matches) != 1:
            raise SoloError(f"Expected one {task_id} task; found {len(matches)}.")
        row = matches[0]
        return cls(
            task_id=task_id,
            title=str(row.get("title", task_id)),
            objective=str(row.get("objective", "")),
            acceptance_criteria=tuple(map(str, row.get("acceptance_criteria", []))),
            requirements=tuple(map(str, row.get("requirements", []))),
            allowed_paths=tuple(map(str, row.get("allowed_paths", []))),
        )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value: Any) -> str:
    return SECRET_RE.sub(
        lambda match: match.group(1) + "=[REDACTED]",
        str(value or ""),
    )


def bounded(value: Any, limit: int = 1800) -> str:
    text = redact(value)
    if len(text) <= limit:
        return text
    half = max(1, (limit - 80) // 2)
    return text[:half] + "\n...[bounded middle omitted]...\n" + text[-half:]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def append_event(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=check,
        timeout=120,
        env={**os.environ, "GIT_PAGER": "cat", "PAGER": "cat", "GIT_TERMINAL_PROMPT": "0"},
    )


def snapshot(repo: Path) -> dict[str, Any]:
    status = git(repo, "status", "--porcelain=v1", "--branch").stdout.rstrip()
    changes = [line for line in status.splitlines() if line and not line.startswith("##")]
    check = git(repo, "--no-pager", "diff", "--check", check=False)
    return {
        "head": git(repo, "rev-parse", "HEAD").stdout.strip(),
        "branch": git(repo, "branch", "--show-current").stdout.strip(),
        "status": status,
        "dirty": bool(changes),
        "changes": changes,
        "diff_stat": git(repo, "--no-pager", "diff", "--stat").stdout.rstrip(),
        "diff_check_rc": check.returncode,
        "diff_check": bounded(check.stdout + check.stderr, 2000),
    }


def normalized_task_path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    if text.startswith("/workspace/forge/"):
        text = text[len("/workspace/forge/"):]
    elif text == "/workspace/forge":
        text = "."
    while text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/") or "."
    if text.startswith("/") or ".." in Path(text).parts:
        raise SoloError(f"Unsafe allowed path: {value}")
    return text


def repository_changed_paths(repo: Path) -> list[str]:
    commands = (
        ("--no-pager", "diff", "--name-only"),
        ("--no-pager", "diff", "--cached", "--name-only"),
        ("ls-files", "--others", "--exclude-standard"),
    )
    paths: set[str] = set()
    for arguments in commands:
        result = git(repo, *arguments, check=False)
        if result.returncode != 0:
            raise SoloError(
                "Could not determine repository changed paths: "
                + bounded(result.stdout + result.stderr, 1000)
            )
        paths.update(
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        )
    return sorted(paths)


def path_is_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    normalized = normalized_task_path(path)
    for raw in allowed_paths:
        allowed = normalized_task_path(raw)
        if allowed == ".":
            return True
        if (
            normalized == allowed
            or normalized.startswith(allowed + "/")
            or fnmatch(normalized, allowed)
        ):
            return True
    return False


def repository_state_digest(repo: Path) -> str:
    hasher = hashlib.sha256()

    for arguments in (
        ("rev-parse", "HEAD"),
        ("--no-pager", "diff", "--binary"),
        ("--no-pager", "diff", "--cached", "--binary"),
    ):
        result = git(
            repo,
            *arguments,
            check=False,
        )

        if result.returncode != 0:
            raise SoloError(
                "Could not compute repository state digest: "
                + bounded(
                    result.stdout + result.stderr,
                    1200,
                )
            )

        hasher.update(
            "\0".join(arguments).encode(
                "utf-8"
            )
        )
        hasher.update(b"\0")
        hasher.update(
            result.stdout.encode(
                "utf-8",
                errors="replace",
            )
        )
        hasher.update(b"\0")

    untracked = git(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        check=False,
    )

    if untracked.returncode != 0:
        raise SoloError(
            "Could not inspect untracked files for state digest."
        )

    for relative in sorted(
        line.strip()
        for line in untracked.stdout.splitlines()
        if line.strip()
    ):
        hasher.update(
            relative.encode(
                "utf-8",
                errors="replace",
            )
        )
        hasher.update(b"\0")

        path = repo / relative

        if path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(
                    lambda: handle.read(1024 * 1024),
                    b"",
                ):
                    hasher.update(chunk)

        hasher.update(b"\0")

    return hasher.hexdigest()


TEST_RE = re.compile(
    r"\b(?:unittest|pytest|npm\s+(?:test|run\s+test))\b",
    re.I,
)
FULL_TEST_RE = re.compile(
    r"(?:\bunittest\s+discover\b|"
    r"\b(?:python\d*(?:\.\d+)?\s+-m\s+)?pytest"
    r"(?:\s+-[\w=-]+)*\s*$|"
    r"\bnpm\s+(?:test|run\s+test)\b)",
    re.I,
)
DIFF_EVIDENCE_RE = re.compile(
    r"\bgit\b[^\n]*\bdiff\b",
    re.I,
)
DIFF_CHECK_RE = re.compile(
    r"\bgit\b[^\n]*\bdiff\b[^\n]*--check\b",
    re.I,
)
STATUS_EVIDENCE_RE = re.compile(
    r"^\s*git\s+status\s+--short\s*$",
    re.I,
)
SUCCESS_STATES = {"completed", "done", "passed", "success", "succeeded"}


def evidence_kind(command: str) -> str:
    normalized = " ".join(command.split())

    if FULL_TEST_RE.search(normalized):
        return "full_test"

    if TEST_RE.search(normalized):
        return "focused_test"

    if DIFF_CHECK_RE.search(normalized):
        return "diff_check"

    if DIFF_EVIDENCE_RE.search(normalized):
        return "diff"

    if STATUS_EVIDENCE_RE.fullmatch(normalized):
        return "status"

    return ""


def catalog_context(path: Path, model: str) -> int | None:
    if not path.is_file():
        return None
    queries = (
        "SELECT context_length FROM models WHERE model_id=?",
        "SELECT context_length FROM model_catalog WHERE model_id=?",
    )
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            for query in queries:
                try:
                    row = db.execute(query, (model,)).fetchone()
                except sqlite3.Error:
                    continue
                if row and isinstance(row[0], int) and not isinstance(row[0], bool) and row[0] > 0:
                    return int(row[0])
    except sqlite3.Error:
        pass
    return None


def message_from(response: Mapping[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise SoloError("Provider returned no completion choice.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise SoloError("Provider returned no assistant message.")
    return message


def final_marker(content: Any) -> str | None:
    if not isinstance(content, str):
        return None
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return lines[-1] if lines and lines[-1] in FINAL else None


def call_name(call: Mapping[str, Any]) -> str:
    function = call.get("function")
    return str(function.get("name", "")).strip() if isinstance(function, dict) else ""


def call_args(call: Mapping[str, Any]) -> dict[str, Any]:
    function = call.get("function")
    raw = function.get("arguments") if isinstance(function, dict) else None
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


SAFE_INVENTORY_GLOBS = {
    "*.py",
    "*.js",
    "*.ts",
    "*.tsx",
    "*.vue",
    "*.svelte",
    "*.html",
    "*.json",
    "*.toml",
    "*.md",
}


def safe_inspection_replacement(
    command: str,
) -> str:
    try:
        tokens = shlex.split(
            command,
            posix=True,
        )
    except ValueError:
        return ""

    if (
        len(tokens) < 8
        or tokens[0] != "find"
    ):
        return ""

    path = tokens[1]
    root = "/workspace/forge"

    if path in {".", "./"}:
        relative_path = "."
    elif path.startswith("./"):
        relative_path = path[2:]
    elif path == root:
        relative_path = "."
    elif path.startswith(root + "/"):
        relative_path = path[len(root) + 1:]
    else:
        return ""

    if (
        not relative_path
        or relative_path.startswith("/")
        or ".." in Path(relative_path).parts
    ):
        return ""

    index = 2

    if (
        index < len(tokens)
        and tokens[index] == "-maxdepth"
    ):
        if index + 1 >= len(tokens):
            return ""

        try:
            depth = int(tokens[index + 1])
        except ValueError:
            return ""

        if depth < 1 or depth > 20:
            return ""

        index += 2

    if tokens[index:index + 2] != [
        "-type",
        "f",
    ]:
        return ""

    index += 2
    patterns: list[str] = []

    while index < len(tokens):
        if tokens[index] == "|":
            break

        if patterns:
            if tokens[index] != "-o":
                return ""
            index += 1

        if (
            index + 1 >= len(tokens)
            or tokens[index] != "-name"
        ):
            return ""

        pattern = tokens[index + 1]

        if pattern not in SAFE_INVENTORY_GLOBS:
            return ""

        if pattern not in patterns:
            patterns.append(pattern)

        index += 2

    if (
        not patterns
        or index >= len(tokens)
        or tokens[index] != "|"
    ):
        return ""

    tail = tokens[index + 1:]

    if not tail or tail[0] != "head":
        return ""

    if len(tail) == 2:
        raw_limit = tail[1]
        if raw_limit.startswith("-"):
            raw_limit = raw_limit[1:]
    elif (
        len(tail) == 3
        and tail[1] == "-n"
    ):
        raw_limit = tail[2]
    else:
        return ""

    try:
        limit = int(raw_limit)
    except ValueError:
        return ""

    if limit < 1 or limit > 200:
        return ""

    parts = [
        "git",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
    ]

    for pattern in patterns:
        pathspec = (
            pattern
            if relative_path == "."
            else f"{relative_path.rstrip('/')}/{pattern}"
        )
        parts.append(
            shlex.quote(pathspec)
        )

    return " ".join(parts)



def safe_git_log_replacement(
    command: str,
) -> str:
    try:
        tokens = shlex.split(
            command,
            posix=True,
        )
    except ValueError:
        return ""

    if not tokens or tokens[0] != "git":
        return ""

    index = 1

    if (
        index < len(tokens)
        and tokens[index] == "--no-pager"
    ):
        index += 1

    if (
        index >= len(tokens)
        or tokens[index] != "log"
    ):
        return ""

    arguments = tokens[index + 1:]

    if not arguments:
        return ""

    count: int | None = None
    oneline = False
    position = 0

    while position < len(arguments):
        token = arguments[position]

        if token == "--oneline":
            if oneline:
                return ""

            oneline = True
            position += 1
            continue

        raw_count = ""

        if token == "-n":
            if position + 1 >= len(arguments):
                return ""

            raw_count = arguments[position + 1]
            position += 2
        elif (
            token.startswith("-n")
            and len(token) > 2
        ):
            raw_count = token[2:]
            position += 1
        elif re.fullmatch(r"-[1-9]\d*", token):
            raw_count = token[1:]
            position += 1
        elif token == "--max-count":
            if position + 1 >= len(arguments):
                return ""

            raw_count = arguments[position + 1]
            position += 2
        elif token.startswith("--max-count="):
            raw_count = token.split("=", 1)[1]
            position += 1
        else:
            return ""

        if count is not None or not raw_count.isdigit():
            return ""

        count = int(raw_count)

    if (
        not oneline
        or count is None
        or count < 1
        or count > 100
    ):
        return ""

    return f"git log -n {count} --oneline"


def normalized_solo_tool_call(call: Mapping[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(call))
    if call_name(normalized) != "run_command":
        return normalized
    function = normalized.get("function")
    if not isinstance(function, dict):
        return normalized
    arguments = call_args(normalized)
    if not arguments:
        return normalized
    arguments["cwd"] = "/workspace/forge"
    key = (
        "command" if isinstance(arguments.get("command"), str)
        else "cmd" if isinstance(arguments.get("cmd"), str)
        else ""
    )
    if key:
        command = str(arguments[key])
        replacement = (
            safe_inspection_replacement(command)
            or safe_git_log_replacement(command)
        )
        if replacement:
            arguments[key] = replacement
    function["arguments"] = json.dumps(arguments, separators=(",", ":"))
    return normalized



def exact_status_call(
    call: Mapping[str, Any],
) -> bool:
    normalized = normalized_solo_tool_call(
        call
    )
    arguments = call_args(normalized)
    command = str(
        arguments.get("command")
        or arguments.get("cmd")
        or ""
    )

    return (
        call_name(normalized) == "run_command"
        and evidence_kind(command) == "status"
    )


def current_status_is_recorded(
    state: Mapping[str, Any],
    repo: Path,
) -> bool:
    current_digest = repository_state_digest(
        repo
    )

    for item in reversed(
        list(state.get("evidence", []))
    ):
        if (
            not isinstance(item, dict)
            or item.get("kind") != "status"
        ):
            continue

        return bool(
            item.get("success")
            and item.get(
                "repository_state_digest"
            )
            == current_digest
        )

    return False


def select_serial_tool_call(
    calls: list[dict[str, Any]],
    state: Mapping[str, Any],
    repo: Path,
) -> tuple[dict[str, Any], int, str]:
    if not calls:
        raise SoloError(
            "Cannot select from an empty tool-call list."
        )

    selected_index = 0
    reason = "first_call"

    if (
        len(calls) > 1
        and exact_status_call(calls[0])
        and current_status_is_recorded(
            state,
            repo,
        )
    ):
        for index, candidate in enumerate(
            calls[1:],
            start=1,
        ):
            if exact_status_call(candidate):
                continue

            selected_index = index
            reason = "skipped_redundant_status"
            break

    return (
        calls[selected_index],
        selected_index,
        reason,
    )


def policy_recovery_instruction(
    call: Mapping[str, Any],
    error: BaseException,
) -> str:
    original = call_args(call)
    normalized = call_args(normalized_solo_tool_call(call))
    original_command = str(original.get("command") or original.get("cmd") or "")
    normalized_command = str(normalized.get("command") or normalized.get("cmd") or "")
    if normalized_command and normalized_command != original_command:
        return (
            "Do not repeat the rejected command. Retry exactly one run_command with "
            f"command={normalized_command!r} and cwd='/workspace/forge'."
        )
    return (
        "Do not repeat the rejected command. "
        f"The policy error was: {bounded(error, 500)}. "
        "Use exactly one supported command with cwd='/workspace/forge'. "
        "For file inventory, use git ls-files --cached --others "
        "--exclude-standard -- followed by quoted globs and no pipeline. "
        "For recent history, use git log -n N --oneline with N from 1 to 100. "
        "For text search, use grep -n -m N PATTERN PATH."
    )


def active_from(result_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    status = str(payload.get("status", "")).lower()
    process_id = str(payload.get("id") or payload.get("process_id") or "").strip()
    if status not in ACTIVE or not process_id:
        return {}
    try:
        offset = max(0, int(payload.get("next_offset", 0) or 0))
    except (TypeError, ValueError):
        offset = 0
    return {"process_id": process_id, "status": status, "next_offset": offset, "updated_at": now()}


class Store:
    def __init__(self, root: Path, task_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id):
            raise SoloError("Unsafe task ID.")
        self.task_id = task_id
        self.root = root.expanduser() / task_id
        self.state = self.root / "state.json"
        self.heartbeat = self.root / "heartbeat.json"
        self.events = self.root / "events.jsonl"
        self.lock = self.root / "runner.lock"
        self.review = self.root / "review"
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any] | None:
        if not self.state.is_file():
            return None
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SoloError("State is not a JSON object.")
        return payload

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = now()
        atomic_json(self.state, state)

    def beat(self, state: dict[str, Any], event: str, **meta: Any) -> None:
        stamp = now()
        state["last_heartbeat"] = stamp
        atomic_json(
            self.heartbeat,
            {
                "timestamp": stamp,
                "task_id": self.task_id,
                "status": state.get("status"),
                "context_epoch": state.get("context_epoch"),
                "total_rounds": state.get("total_rounds"),
                "active_process": state.get("active_process", {}),
                "event": event,
                **meta,
            },
        )
        self.save(state)
        append_event(self.events, {"timestamp": stamp, "event": event, "task_id": self.task_id, **meta})


class OpenWebUIGateway:
    def __init__(self, client: OpenWebUIClient, model_id: str, max_tokens: int = 4096) -> None:
        self.client = client
        self.model_id = model_id
        self.max_tokens = max_tokens

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self.client.completion(
            {
                "model": self.model_id,
                "messages": messages,
                "tools": PROCESS_TOOLS,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "stream": False,
                "temperature": 0,
                "max_tokens": self.max_tokens,
            }
        )


def terminal_tool_schemas(
    tools: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}

    for tool in tools:
        function = (
            tool.get("function")
            if isinstance(tool, dict)
            else None
        )
        name = (
            function.get("name")
            if isinstance(function, dict)
            else None
        )
        parameters = (
            function.get("parameters")
            if isinstance(function, dict)
            else None
        )

        if (
            not isinstance(name, str)
            or not name
            or not isinstance(parameters, dict)
        ):
            raise SoloError(
                "Open Terminal tool schema is malformed."
            )

        if name in schemas:
            raise SoloError(
                "Open Terminal tool schema name is duplicated: "
                + name
            )

        schemas[name] = parameters

    return schemas


PROCESS_TOOL_SCHEMAS = terminal_tool_schemas(
    PROCESS_TOOLS
)


class ForgePolicy:
    def __init__(self, config: AppConfig) -> None:
        self.coordinator = DeveloperCoordinator(config)
        self.tool_schemas = PROCESS_TOOL_SCHEMAS

    def validate(
        self,
        call: dict[str, Any],
        *,
        active_process: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = normalized_solo_tool_call(call)
        result = self.coordinator._validate_tool_calls(
            [normalized],
            self.tool_schemas,
            "implementer",
            active_process=(
                dict(active_process)
                if active_process
                else None
            ),
        )
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise SoloError("Forge policy returned an invalid result.")
        return result[0]


class Runner:
    def __init__(
        self,
        task: Task,
        store: Store,
        gateway: Gateway,
        terminal: Terminal,
        policy: Policy,
        repo: Path,
        epoch_rounds: int = 10,
        max_rounds: int = 240,
        capacity_pause: int = 300,
        writer: WriterLease | None = None,
    ) -> None:
        self.task = task
        self.store = store
        self.gateway = gateway
        self.terminal = terminal
        self.policy = policy
        self.repo = repo.expanduser().resolve()
        self.epoch_rounds = epoch_rounds
        self.max_rounds = max_rounds
        self.capacity_pause = capacity_pause
        self.writer = writer or NoopWriterLease()

    def initial_state(self, snap: Mapping[str, Any]) -> dict[str, Any]:
        stamp = now()
        return {
            "schema_version": 1,
            "task_id": self.task.task_id,
            "title": self.task.title,
            "model_id": self.gateway.model_id,
            "status": "queued",
            "created_at": stamp,
            "updated_at": stamp,
            "last_heartbeat": "",
            "context_epoch": 0,
            "total_rounds": 0,
            "total_tool_calls": 0,
            "consecutive_failures": 0,
            "policy_rejections": {},
            "readiness_rejections": 0,
            "evidence": [],
            "writer_lease_id": "",
            "retry_after": "",
            "baseline_head": snap["head"],
            "baseline_branch": snap["branch"],
            "active_process": {},
            "checkpoint": "Inspect, plan, implement, test, self-review, then stop for external review.",
            "recent_results": [],
            "last_response": "",
            "last_error": "",
            "final_report": "",
            "review_bundle": "",
        }

    def load_state(self) -> dict[str, Any]:
        snap = snapshot(self.repo)
        state = self.store.load()
        if state is None:
            state = self.initial_state(snap)
            if snap["dirty"]:
                state["status"] = "blocked"
                state["last_error"] = "Repository was dirty before DeepSeek Solo started."
            self.store.save(state)
        if state.get("model_id") != self.gateway.model_id:
            raise SoloError("Pinned model changed; rotation is prohibited.")
        if snap["head"] != state.get("baseline_head"):
            state["status"] = "blocked"
            state["last_error"] = "Repository HEAD changed during the solo task."
            self.store.save(state)
        return state

    def system_prompt(self) -> str:
        return (
            f"You are DeepSeek Solo, sole engineering owner of {self.task.task_id}. "
            "Plan, inspect, implement, test, and self-review the task from start to finish. "
            "There are no planner, implementer, reviewer, verifier, manager, judge, handoff, "
            "or fallback agents. Never request another model.\n\n"
            "Use only the supplied Open Terminal tools. Request at most one tool call per "
            "model turn. Start no more than one process per model turn and poll a running "
            "process before starting another. Follow the Forge "
            "implementer command policy exactly.\n\n"
            "Do not use sed or rg. For file inventory use git ls-files --cached --others "
            "--exclude-standard -- followed by quoted globs and no pipeline. For recent "
            "history use exactly git log -n N --oneline with N from 1 to 100. For text "
            "inspection prefer cat, head, tail, or bounded grep such as "
            "grep -n -m 30 PATTERN PATH. Do not use recursive grep, pipelines, compound "
            "commands, command substitution, or arbitrary inline Python. After a policy "
            "rejection, choose a supported equivalent and continue with the same model.\n\n"
            "Do not commit, push, deploy, restart services, change systemd state, use sudo, "
            "access credentials, or change tasks. Leave changes uncommitted for external review.\n\n"
            "Any edit invalidates prior test and review evidence. After the final edit, rerun a "
            "focused test, the full relevant suite, a real Git diff inspection that is not only "
            "git diff --check, and exactly git status --short.\n\n"
            "Every response must end with exactly one final line: CONTINUE, READY_FOR_REVIEW, "
            "or BLOCKED. READY_FOR_REVIEW requires implementation, focused tests, relevant full "
            "tests, final diff inspection, and self-review."
        )

    def prompt(self, state: Mapping[str, Any], snap: Mapping[str, Any]) -> str:
        payload = {
            "task": asdict(self.task),
            "pinned_model": self.gateway.model_id,
            "context_epoch": state.get("context_epoch", 0),
            "total_rounds": state.get("total_rounds", 0),
            "checkpoint": bounded(state.get("checkpoint"), 4500),
            "last_response": bounded(state.get("last_response"), 2500),
            "active_process": state.get("active_process", {}),
            "recent_results": list(state.get("recent_results", []))[-10:],
            "evidence": list(state.get("evidence", []))[-20:],
            "last_error": bounded(state.get("last_error"), 2000),
            "writer_lease": {"owned": bool(state.get("writer_lease_id"))},
            "repository": {
                "head": snap["head"],
                "branch": snap["branch"],
                "status": bounded(snap["status"], 3000),
                "diff_stat": bounded(snap["diff_stat"], 2500),
                "diff_check_rc": snap["diff_check_rc"],
                "diff_check": snap["diff_check"],
            },
        }
        return (
            "Continue the same durable task from this authoritative checkpoint. "
            "Do not repeat completed work. Choose the next smallest useful action.\n\n"
            + json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        )

    def record_result(self, state: dict[str, Any], call: Mapping[str, Any], text: str) -> None:
        prior_active = dict(state.get("active_process") or {})
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"output": text}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        arguments = call_args(call)
        command = str(
            arguments.get("command")
            or prior_active.get("command")
            or ""
        )
        kind = evidence_kind(command)
        status = str(payload.get("status", "")).lower()
        exit_code = payload.get("exit_code")
        preview = (
            payload.get("output")
            or payload.get("stdout")
            or payload.get("message")
            or ""
        )
        recent = list(state.get("recent_results", []))
        recent.append(
            {
                "timestamp": now(),
                "tool": call_name(call),
                "arguments": {
                    key: bounded(value, 500)
                    for key, value in arguments.items()
                    if key not in {"env", "environment"}
                },
                "command": bounded(command, 1000),
                "evidence_kind": kind,
                "status": status,
                "exit_code": exit_code,
                "result_digest": digest(text),
                "output_preview": bounded(preview, 1800),
            }
        )
        state["recent_results"] = recent[-16:]
        state["total_tool_calls"] = int(state.get("total_tool_calls", 0)) + 1
        active = active_from(text)
        if active:
            active["command"] = command
            active["evidence_kind"] = kind
            state["active_process"] = active
            return
        if status in TERMINAL:
            state["active_process"] = {}
            if kind:
                evidence = list(state.get("evidence", []))
                evidence.append(
                    {
                        "timestamp": now(),
                        "kind": kind,
                        "command": bounded(command, 1200),
                        "status": status,
                        "exit_code": exit_code,
                        "success": exit_code == 0 and status in SUCCESS_STATES,
                        "result_digest": digest(text),
                        "repository_state_digest": (
                            repository_state_digest(self.repo)
                        ),
                    }
                )
                state["evidence"] = evidence[-100:]

    def poll(self, state: dict[str, Any]) -> bool:
        active = state.get("active_process")
        if not isinstance(active, dict) or not active.get("process_id"):
            state["active_process"] = {}
            return False
        call = {
            "id": "solo-poll-" + uuid.uuid4().hex,
            "type": "function",
            "function": {
                "name": "get_process_status",
                "arguments": json.dumps(
                    {
                        "process_id": active["process_id"],
                        "wait": 60,
                        "offset": int(active.get("next_offset", 0) or 0),
                    },
                    separators=(",", ":"),
                ),
            },
        }
        valid = self.policy.validate(
            call,
            active_process=active,
        )
        self.renew_writer(state)
        self.store.beat(state, "process_poll_started", process_id=active["process_id"])
        result = self.terminal.execute_tool_call(valid)
        self.record_result(state, valid, result)
        self.renew_writer(state)
        self.store.beat(state, "process_poll_finished", still_active=bool(state.get("active_process")))
        return bool(state.get("active_process"))

    def classify(self, error: BaseException) -> str:
        category = str(getattr(error, "category", "")).lower()
        text = str(error).lower()
        if category == "context_overflow" or any(marker in text for marker in CONTEXT_MARKERS):
            return "context"
        if category == "capacity" or any(marker in text for marker in CAPACITY_MARKERS):
            return "capacity"
        return "failure"

    def reject(self, state: dict[str, Any], call: Mapping[str, Any], error: BaseException) -> int:
        command = str(call_args(call).get("command", ""))
        key = digest(call_name(call) + "\n" + command)[:24]
        counters = dict(state.get("policy_rejections", {}))
        count = int(counters.get(key, 0)) + 1
        counters[key] = count
        state["policy_rejections"] = counters
        state["last_error"] = bounded(error, 1200)
        append_event(
            self.store.events,
            {
                "timestamp": now(),
                "event": "policy_rejected",
                "task_id": self.task.task_id,
                "tool": call_name(call),
                "command": bounded(command, 700),
                "error": bounded(error, 1000),
                "repeated": count,
            },
        )
        return count

    def renew_writer(self, state: dict[str, Any]) -> None:
        lock = self.writer.acquire(
            str(state.get("writer_lease_id", "")),
            status=str(
                state.get(
                    "status",
                    "running",
                )
            ),
            active_process=(
                state.get("active_process")
                if isinstance(
                    state.get("active_process"),
                    dict,
                )
                else {}
            ),
        )
        state["writer_lease_id"] = str(lock.get("lease_id", ""))
        if not state["writer_lease_id"]:
            raise WriterLeaseLost(
                "Forge writer acquisition returned no lease token."
            )

    def release_writer(
        self,
        state: dict[str, Any],
    ) -> None:
        lease_id = str(
            state.get(
                "writer_lease_id",
                "",
            )
        )

        if not lease_id:
            return

        self.writer.release(
            lease_id,
            status=str(
                state.get(
                    "status",
                    "completed",
                )
            ),
        )

        state["writer_lease_id"] = ""

    def scope_issues(self) -> list[str]:
        changed = repository_changed_paths(self.repo)
        if not changed:
            return []
        if not self.task.allowed_paths:
            return ["Task manifest has no allowed_paths for repository changes."]
        outside = [
            path
            for path in changed
            if not path_is_allowed(path, self.task.allowed_paths)
        ]
        if not outside:
            return []
        return ["Changed paths outside task scope: " + ", ".join(outside)]

    @staticmethod
    def latest_evidence(
        state: Mapping[str, Any],
        kind: str,
    ) -> Mapping[str, Any] | None:
        matches = [
            item
            for item in state.get("evidence", [])
            if isinstance(item, dict) and item.get("kind") == kind
        ]
        return matches[-1] if matches else None

    def readiness_issues(
        self,
        state: Mapping[str, Any],
        snap: Mapping[str, Any],
    ) -> list[str]:
        issues: list[str] = []
        if state.get("active_process"):
            issues.append("A terminal process is still active.")
        if not repository_changed_paths(self.repo):
            issues.append("No repository changes are present.")
        issues.extend(self.scope_issues())
        if int(snap.get("diff_check_rc", 1)) != 0:
            issues.append("git diff --check is not clean.")
        current_digest = repository_state_digest(
            self.repo
        )
        requirements = (
            ("focused_test", "No successful focused test is recorded for the current repository state."),
            ("full_test", "No successful full relevant test suite is recorded for the current repository state."),
            ("diff", "No successful final Git diff inspection is recorded for the current repository state."),
            ("status", "No successful final git status --short inspection is recorded for the current repository state."),
        )

        for kind, message in requirements:
            item = self.latest_evidence(state, kind)

            if (
                not item
                or not item.get("success")
                or item.get(
                    "repository_state_digest"
                )
                != current_digest
            ):
                issues.append(message)

        return issues

    def block_for_scope(
        self,
        state: dict[str, Any],
        event: str,
    ) -> dict[str, Any] | None:
        issues = self.scope_issues()

        if not issues:
            return None

        state["last_error"] = "; ".join(
            issues
        )

        if state.get("active_process"):
            state["status"] = "continue"
            self.store.beat(
                state,
                event,
                issues=issues,
                writer_retained=True,
                active_process=True,
            )
            return dict(state)

        state["status"] = "blocked"
        self.release_writer(state)
        self.store.beat(
            state,
            event,
            issues=issues,
            writer_retained=False,
        )
        return dict(state)

    def review_bundle(self, state: dict[str, Any], snap: Mapping[str, Any]) -> None:
        self.store.review.mkdir(parents=True, exist_ok=True)
        diff = git(self.repo, "--no-pager", "diff", "--binary", check=False)
        stat = git(self.repo, "--no-pager", "diff", "--stat", check=False)
        check = git(self.repo, "--no-pager", "diff", "--check", check=False)
        status = git(self.repo, "status", "--porcelain=v1", "--branch", check=False)
        (self.store.review / "implementation.diff").write_text(diff.stdout, encoding="utf-8")
        (self.store.review / "diff.stat").write_text(stat.stdout, encoding="utf-8")
        (self.store.review / "diff-check.txt").write_text(check.stdout + check.stderr, encoding="utf-8")
        (self.store.review / "git-status.txt").write_text(status.stdout, encoding="utf-8")
        report = (
            f"# DeepSeek Solo Review: {self.task.task_id}\n\n"
            f"- Model: `{self.gateway.model_id}`\n"
            f"- Baseline: `{state.get('baseline_head', '')}`\n"
            f"- Current HEAD: `{snap.get('head', '')}`\n"
            f"- Context epochs: `{state.get('context_epoch', 0)}`\n"
            f"- Model rounds: `{state.get('total_rounds', 0)}`\n"
            f"- Tool calls: `{state.get('total_tool_calls', 0)}`\n"
            f"- Diff check exit: `{check.returncode}`\n\n"
            "## Acceptance Criteria\n\n"
            + "".join(f"- [ ] {item}\n" for item in self.task.acceptance_criteria)
            + "\n## DeepSeek Final Report\n\n"
            + bounded(state.get("final_report"), 12000)
            + "\n\n## Git Status\n\n```text\n" + status.stdout
            + "\n```\n\n## Diff Summary\n\n```text\n" + stat.stdout
            + "\n```\n\nChanges remain uncommitted. No push, deployment, or service restart occurred.\n"
        )
        (self.store.review / "review.md").write_text(report, encoding="utf-8")
        state["review_bundle"] = str(self.store.review)
        atomic_json(self.store.review / "state.json", state)

    def tick(self) -> dict[str, Any]:
        with self.store.lock.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"task_id": self.task.task_id, "status": "busy"}
            return self._tick()

    def _tick(self) -> dict[str, Any]:
        state = self.load_state()
        if state.get("status") in {"ready_for_review", "blocked"}:
            try:
                self.release_writer(state)
            except WriterLeaseLost as error:
                state["last_error"] = (
                    bounded(state.get("last_error"), 1400)
                    + " "
                    + bounded(error, 500)
                ).strip()
            self.store.save(state)
            return dict(state)
        try:
            self.renew_writer(state)
        except WriterBusy as error:
            state["status"] = "paused_writer"
            state["last_error"] = bounded(error, 1200)
            self.store.beat(state, "writer_busy")
            return dict(state)
        except WriterLeaseLost as error:
            state["status"] = "blocked"
            state["last_error"] = bounded(error, 1200)
            state["writer_lease_id"] = ""
            self.store.beat(state, "writer_lease_lost")
            return dict(state)



        state["status"] = "running"
        self.store.beat(state, "tick_started", model=self.gateway.model_id)

        try:
            if state.get("active_process") and self.poll(state):
                state["status"] = "continue"
                self.store.beat(state, "waiting_for_process")
                return dict(state)

            scope_block = self.block_for_scope(
                state,
                "preexisting_scope_violation",
            )
            if scope_block is not None:
                return scope_block

            retry_after = str(state.get("retry_after", ""))
            if retry_after:
                try:
                    retry_time = datetime.fromisoformat(retry_after)
                except ValueError:
                    retry_time = None
                if retry_time and retry_time > datetime.now(timezone.utc):
                    return dict(state)
                state["retry_after"] = ""

            if int(state.get("total_rounds", 0)) >= self.max_rounds:
                state["status"] = "blocked"
                state["last_error"] = "Maximum total model rounds reached."
                self.release_writer(state)
                self.store.beat(state, "max_rounds")
                return dict(state)

            snap = snapshot(self.repo)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": self.system_prompt()},
                {"role": "user", "content": self.prompt(state, snap)},
            ]

            for _ in range(self.epoch_rounds):
                self.renew_writer(state)
                state["total_rounds"] = int(state.get("total_rounds", 0)) + 1
                self.store.beat(
                    state,
                    "model_request",
                    round=state["total_rounds"],
                    epoch=state.get("context_epoch", 0),
                )
                response = self.gateway.complete(messages)
                message = message_from(response)
                content = message.get("content")
                marker = final_marker(content)
                calls = message.get("tool_calls")
                state["last_response"] = bounded(content, 5000)

                if calls:
                    if (
                        not isinstance(calls, list)
                        or not calls
                        or not all(
                            isinstance(item, dict)
                            for item in calls
                        )
                    ):
                        raise SoloError(
                            "Provider returned malformed tool calls."
                        )

                    (
                        call,
                        selected_index,
                        selection_reason,
                    ) = select_serial_tool_call(
                        calls,
                        state,
                        self.repo,
                    )
                    ignored_calls = len(calls) - 1
                    call_id = str(call.get("id", "")).strip()
                    if not call_id:
                        raise SoloError("Tool call has no ID.")

                    assistant = {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [call],
                    }

                    if ignored_calls:
                        self.store.beat(
                            state,
                            "serial_tool_calls_trimmed",
                            accepted_call_id=call_id,
                            accepted_tool=call_name(call),
                            selected_index=selected_index,
                            selection_reason=selection_reason,
                            ignored_calls=ignored_calls,
                        )

                    try:
                        valid = self.policy.validate(call)
                        assistant["tool_calls"] = [valid]
                        if call_args(valid) != call_args(call):
                            self.store.beat(
                                state,
                                "tool_call_normalized",
                                original_command=bounded(
                                    call_args(call).get(
                                        "command",
                                        call_args(call).get("cmd", ""),
                                    ),
                                    800,
                                ),
                                normalized_command=bounded(
                                    call_args(valid).get(
                                        "command",
                                        call_args(valid).get("cmd", ""),
                                    ),
                                    800,
                                ),
                                normalized_cwd=call_args(valid).get("cwd", ""),
                            )
                    except BaseException as error:
                        repeated = self.reject(state, call, error)
                        messages.extend(
                            [
                                assistant,
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "content": json.dumps(
                                        {
                                            "status": "rejected",
                                            "error": bounded(error, 1000),
                                            "rejected_command": bounded(call_args(call).get("command", ""), 800),
                                            "instruction": policy_recovery_instruction(
                                                call,
                                                error,
                                            ),
                                        },
                                        separators=(",", ":"),
                                    ),
                                },
                            ]
                        )
                        if repeated >= 3:
                            state["status"] = "blocked"
                            state["last_error"] = "The same rejected command was repeated three times."
                            self.release_writer(state)
                            self.store.beat(state, "repeated_policy_rejection")
                            return dict(state)
                        continue

                    self.renew_writer(state)
                    self.store.beat(state, "tool_started", tool=call_name(valid))
                    result = self.terminal.execute_tool_call(valid)
                    self.record_result(state, valid, result)
                    self.renew_writer(state)
                    scope_block = self.block_for_scope(
                        state,
                        "tool_scope_violation",
                    )
                    if scope_block is not None:
                        return scope_block
                    messages.extend(
                        [
                            assistant,
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": result,
                            },
                        ]
                    )

                    if ignored_calls:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    (
                                        "The server accepted and executed only "
                                        "the first tool call from your previous "
                                        "response. Request at most one tool call "
                                        "per turn. Continue with the next required "
                                        "operation only after reviewing the tool "
                                        "result."
                                    )
                                    if selected_index == 0
                                    else (
                                        "The server accepted and executed only "
                                        "one tool call from your previous response. "
                                        "It skipped a redundant leading "
                                        "git status --short because current status "
                                        "evidence already existed, then selected "
                                        "the first later non-status call. Request "
                                        "at most one tool call per turn and continue "
                                        "from the executed result."
                                    )
                                ),
                            }
                        )

                    self.store.beat(
                        state,
                        "tool_finished",
                        tool=call_name(valid),
                        ignored_calls=ignored_calls,
                    )
                    if state.get("active_process"):
                        state["status"] = "continue"
                        self.store.beat(state, "process_persisted")
                        return dict(state)
                    continue

                state["checkpoint"] = bounded(content, 5000) or state.get("checkpoint", "")

                if marker == "READY_FOR_REVIEW":
                    snap = snapshot(self.repo)
                    issues = self.readiness_issues(state, snap)
                    if issues:
                        state["readiness_rejections"] = int(
                            state.get("readiness_rejections", 0)
                        ) + 1
                        state["last_error"] = (
                            "READY_FOR_REVIEW rejected: " + "; ".join(issues)
                        )
                        self.store.beat(
                            state,
                            "readiness_rejected",
                            issues=issues,
                            rejection=state["readiness_rejections"],
                        )
                        if state["readiness_rejections"] >= 5:
                            state["status"] = "blocked"
                            self.release_writer(state)
                            self.store.beat(
                                state,
                                "repeated_readiness_rejection",
                                issues=issues,
                            )
                            return dict(state)
                        messages.extend(
                            [
                                {"role": "assistant", "content": content},
                                {
                                    "role": "user",
                                    "content": (
                                        "The server-side review gate rejected "
                                        "READY_FOR_REVIEW. Resolve every item "
                                        "with terminal evidence, then continue. "
                                        "Unmet items: " + "; ".join(issues)
                                    ),
                                },
                            ]
                        )
                        continue
                    state["status"] = "ready_for_review"
                    state["final_report"] = bounded(content, 16000)
                    state["consecutive_failures"] = 0
                    self.release_writer(state)
                    self.review_bundle(state, snap)
                    self.store.beat(state, "ready_for_review", bundle=state["review_bundle"])
                    return dict(state)

                if marker == "BLOCKED":
                    state["status"] = "blocked"
                    state["final_report"] = bounded(content, 16000)
                    state["last_error"] = "DeepSeek reported a blocker."
                    self.release_writer(state)
                    self.store.beat(state, "model_blocked")
                    return dict(state)

                state["context_epoch"] = int(state.get("context_epoch", 0)) + 1
                state["status"] = "continue"
                state["consecutive_failures"] = 0
                self.store.beat(state, "context_rollover", marker=marker or "missing")
                return dict(state)

            state["context_epoch"] = int(state.get("context_epoch", 0)) + 1
            state["status"] = "continue"
            state["checkpoint"] = bounded(state.get("last_response"), 5000) or state.get("checkpoint", "")
            self.store.beat(state, "epoch_round_limit")
            return dict(state)

        except BaseException as error:
            kind = self.classify(error)
            state["last_error"] = bounded(error, 2000)

            if kind == "context":
                state["context_epoch"] = int(state.get("context_epoch", 0)) + 1
                state["status"] = "continue"
                self.store.beat(state, "context_overflow", error=state["last_error"])
                return dict(state)

            if kind == "capacity":
                state["status"] = "paused_capacity"
                state["retry_after"] = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self.capacity_pause)
                ).isoformat()
                self.store.beat(
                    state,
                    "capacity_pause",
                    error=state["last_error"],
                    retry_after=state["retry_after"],
                )
                return dict(state)

            failures = int(state.get("consecutive_failures", 0)) + 1
            state["consecutive_failures"] = failures
            if failures >= 3:
                state["status"] = "blocked"
                self.release_writer(state)
                self.store.beat(state, "repeated_failure", error=state["last_error"])
                return dict(state)

            state["status"] = "continue"
            state["context_epoch"] = int(state.get("context_epoch", 0)) + 1
            self.store.beat(state, "failure_retry", error=state["last_error"], failures=failures)
            return dict(state)


def build_gateway(config: AppConfig, model: str, db_path: Path) -> OpenWebUIGateway:
    client = OpenWebUIClient(
        config.openwebui.base_url,
        config.openwebui.endpoint,
        config.openwebui.api_key_env,
        config.openwebui.timeout_seconds,
        health_endpoint=config.openwebui.health_endpoint,
        models_endpoint=config.openwebui.models_endpoint,
        model_id=model,
        catalog_context=catalog_context(db_path, model),
    )
    return OpenWebUIGateway(client, model)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("command", choices=("tick", "status"))
    value.add_argument("--task-id", default="FG-060")
    value.add_argument("--model", default=DEFAULT_MODEL)
    value.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    value.add_argument("--repository", type=Path, default=DEFAULT_REPO)
    value.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    value.add_argument("--environment", type=Path, default=DEFAULT_ENV)
    value.add_argument("--catalog", type=Path, default=DEFAULT_DB)
    value.add_argument("--state-root", type=Path, default=DEFAULT_STATE)
    value.add_argument("--epoch-rounds", type=int, default=10)
    value.add_argument("--max-total-rounds", type=int, default=240)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = Store(args.state_root, args.task_id)

    if args.command == "status":
        state = store.load()
        print(
            json.dumps(
                state or {
                    "task_id": args.task_id,
                    "status": "not_started",
                    "state_directory": str(store.root),
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return 0

    load_env(args.environment)
    config = load_config(args.config, require_api_key=True)
    task = Task.load(args.manifest, args.task_id)
    runner = Runner(
        task,
        store,
        build_gateway(config, args.model, args.catalog),
        OpenTerminalClient(),
        ForgePolicy(config),
        args.repository,
        args.epoch_rounds,
        args.max_total_rounds,
        writer=SharedWriterLease(
            args.catalog,
            f"solo:{task.task_id}",
            args.model,
        ),
    )
    print(json.dumps(runner.tick(), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
