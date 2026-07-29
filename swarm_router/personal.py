from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from time import monotonic, sleep
from typing import Any
from urllib.parse import unquote, urlparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid

from .catalog import ModelCatalog, ModelRecord
from .config import AppConfig
from .dashboard import DashboardApp
from .discord_notifications import DiscordError, notify_night_owl
from .image_generation import (
    ComfyUIClient,
    IMAGE_AGENT_ID,
    ImageGenerationError,
    build_workflow,
    notify_image_completion,
    store_artifact,
    validate_comfyui_requirements,
    validate_image_payload,
    validate_workflow,
    wait_for_output,
)
from .journal import CheckpointRecord, JournalEventType, SideEffectState, TaskJournal, validate_task_id
from .night_owl import NightOwlError, run_night_owl
from .orchestrator import SwarmOrchestrator
from .wiki import WikiRepository
from .wiki_search import WikiIndex, WikiSearchError


SAFE_TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
SAFE_JIRA = re.compile(r"\b[A-Z][A-Z0-9]{1,19}-[1-9][0-9]*\b")
COMMAND_RE = re.compile(
    r"\b(?:run|execute|exec|shell|bash|zsh|powershell|python|python3|pip|npm|apt|docker|systemctl|journalctl|ssh|scp|curl|wget)\b",
    re.I,
)
FILE_WRITE_RE = re.compile(
    r"\b(?:edit|modify|rewrite|delete|remove|rename|create|write|save|commit|push|merge|rebase|checkout)\b",
    re.I,
)
EXTERNAL_WRITE_RE = re.compile(
    r"\b(?:send|email|mail|create jira|update jira|post to jira|calendar invite|schedule with|upload to drive|write to drive)\b",
    re.I,
)
SCHEDULING_RE = re.compile(
    r"\b(?:schedule|remind me|set reminder|background monitor|keep watching|check every|cron)\b",
    re.I,
)
TASK_DIR_MODE = 0o700
FILE_MODE = 0o600
STREAM_STATUSES = {
    "planning": "Planning...\n",
    "retrieving_wiki_context": "Retrieving wiki context...\n",
    "consulting_workers": "Consulting workers...\n",
    "synthesizing": "Synthesizing...\n\n",
}
OPENAI_COMPAT_IGNORED_FIELDS = {
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "modalities",
}


class PersonalError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_seconds(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def _sync_wait_seconds(config: AppConfig) -> int:
    judge_timeout = min(config.personal.task_timeout_seconds, config.swarm.judge_timeout_seconds)
    margin = min(60, max(1, config.personal.task_timeout_seconds // 4))
    return max(config.personal.task_timeout_seconds, config.personal.worker_timeout_seconds + judge_timeout + margin)


def _message_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=TASK_DIR_MODE)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(content, encoding="utf-8")
    temp.chmod(FILE_MODE)
    os.replace(temp, path)
    path.chmod(FILE_MODE)


def _append_event(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=TASK_DIR_MODE)
    line = json.dumps(payload, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
    path.chmod(FILE_MODE)


def _string(value: Any) -> str:
    return str(value).replace("\x00", "").strip()


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return _string(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(_string(item["text"]))
            else:
                raise PersonalError("Messages must use plain text content only.", code="unsupported_content")
        return "\n".join(part for part in parts if part)
    raise PersonalError("Messages must use string content.", code="invalid_messages")


def _normalize_messages(messages: Any, config: AppConfig) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not messages:
        raise PersonalError("messages must be a non-empty array.", code="invalid_messages")
    if len(messages) > config.personal.max_messages:
        raise PersonalError(
            f"messages exceed the maximum of {config.personal.max_messages}.",
            code="message_limit",
        )
    normalized: list[dict[str, str]] = []
    total_chars = 0
    for item in messages:
        if not isinstance(item, dict):
            raise PersonalError("Each message must be an object.", code="invalid_messages")
        role = str(item.get("role", "")).strip()
        if role not in {"system", "user", "assistant"}:
            raise PersonalError("Only system, user, and assistant messages are supported.", code="invalid_messages")
        content = _message_text(item.get("content", ""))
        if not content:
            raise PersonalError("Message content must be non-empty.", code="invalid_messages")
        if len(content) > config.personal.max_message_chars:
            raise PersonalError(
                f"message exceeds the maximum of {config.personal.max_message_chars} characters.",
                code="message_limit",
            )
        total_chars += len(content)
        normalized.append({"role": role, "content": content})
    if total_chars > config.personal.max_conversation_chars:
        raise PersonalError(
            f"conversation exceeds the maximum of {config.personal.max_conversation_chars} characters.",
            code="conversation_limit",
        )
    return normalized


def _window(messages: list[dict[str, str]], max_chars: int) -> list[dict[str, str]]:
    system = [item for item in messages if item["role"] == "system"]
    others = [item for item in messages if item["role"] != "system"]
    kept = list(system)
    used = sum(len(item["content"]) for item in kept)
    for item in reversed(others):
        if used + len(item["content"]) > max_chars:
            break
        kept.append(item)
        used += len(item["content"])
    keep_ids = {id(item) for item in kept}
    result = [item for item in messages if id(item) in keep_ids]
    return result or messages[-1:]


def _transcript(messages: list[dict[str, str]]) -> str:
    return "\n\n".join(f"{item['role'].upper()}\n{item['content']}" for item in messages)


def _latest_user(messages: list[dict[str, str]]) -> str:
    for item in reversed(messages):
        if item["role"] == "user":
            return item["content"]
    raise PersonalError("At least one user message is required.", code="invalid_messages")


def _rejection(text: str) -> dict[str, str] | None:
    lowered = text.lower()
    if COMMAND_RE.search(text) and any(
        token in lowered
        for token in (" run ", "execute", "shell", "bash", "powershell", "python", "docker", "systemctl", "journalctl", "ssh")
    ):
        return {
            "category": "command_execution",
            "message": "This model is read-only. It cannot run shell commands, code, Docker, or system tools.",
        }
    if FILE_WRITE_RE.search(text) and any(
        token in lowered
        for token in ("file", "repo", "repository", "config", "git", "write", "edit", "modify", "delete", "commit", "push")
    ):
        return {
            "category": "file_modification",
            "message": "This model is read-only. It cannot modify files, repositories, Git state, or local configuration.",
        }
    if EXTERNAL_WRITE_RE.search(text):
        return {
            "category": "external_write",
            "message": "This model cannot write to Jira, email, calendar, Drive, or other external systems.",
        }
    if SCHEDULING_RE.search(text):
        return {
            "category": "scheduling",
            "message": "This model cannot create reminders, schedules, recurring checks, or background monitoring.",
        }
    return None


def _profile(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("weekly plan", "week plan", "week ahead", "weekly planning")):
        return "weekly_planning"
    if any(token in lowered for token in ("trip", "itinerary", "travel", "activity")):
        return "trip_planning"
    if any(token in lowered for token in ("compare", "pros and cons", "pros/cons", "option a", "option b")):
        return "comparison"
    if any(token in lowered for token in ("summarize", "summary", "summarise", "organize these notes")):
        return "summarization"
    if any(token in lowered for token in ("brainstorm", "ideas", "name ideas")):
        return "brainstorming"
    if any(token in lowered for token in ("checklist", "todo", "to-do")):
        return "checklist"
    if "wiki" in lowered or "orbit" in lowered or SAFE_JIRA.search(text):
        return "wiki_research"
    if any(token in lowered for token in ("run status", "recent runs", "model status", "provider status", "swarm status")):
        return "swarm_status"
    return "general"


def _profile_mode(profile: str) -> str:
    return "research" if profile in {"comparison", "wiki_research", "swarm_status"} else "general"


def _profile_roles(profile: str) -> list[str]:
    if profile in {"weekly_planning", "trip_planning", "checklist", "summarization"}:
        return ["planner"]
    if profile in {"comparison", "brainstorming"}:
        return ["planner", "critic"]
    if profile in {"wiki_research", "swarm_status"}:
        return ["planner", "verifier"]
    return ["planner"]


def _wiki_needed(profile: str, text: str) -> bool:
    lowered = text.lower()
    return profile == "wiki_research" or "wiki" in lowered or "orbit" in lowered or bool(SAFE_JIRA.search(text))


def _swarm_status_needed(profile: str, text: str) -> bool:
    lowered = text.lower()
    return profile == "swarm_status" or any(
        token in lowered for token in ("run status", "recent runs", "model status", "provider status", "swarm status")
    )


def _bridge_hosts() -> list[str]:
    try:
        result = subprocess.run(
            ["ip", "-4", "-brief", "addr", "show"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    hosts: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        interface = parts[0]
        if interface != "docker0" and not interface.startswith("br-"):
            continue
        address = parts[2]
        if "/" not in address:
            continue
        host = address.split("/", 1)[0]
        if host and host != "127.0.0.1":
            hosts.append(host)
    return sorted(set(hosts))


class PersonalTaskManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.catalog = ModelCatalog(config.swarm.catalog_path)
        self.journal = TaskJournal(config.swarm.catalog_path)
        self.dashboard = DashboardApp(config)
        self.root = Path(config.personal.task_directory).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=TASK_DIR_MODE)
        self.root.chmod(TASK_DIR_MODE)
        self.auth_token = os.environ.get(config.personal.auth_token_env, "").strip()
        if not self.auth_token:
            raise RuntimeError(
                f"{config.personal.auth_token_env} must be set before starting the personal service."
            )
        self._queue: Queue[str] = Queue()
        self._started = False
        self._lock = Lock()
        self._recover_interrupted_tasks()
        self._start_workers()

    def _recover_interrupted_tasks(self) -> None:
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            task = _read_json(path / "task.json", {})
            if not isinstance(task, dict) or task.get("status") not in {"queued", "running"}:
                continue
            task["status"] = "failed"
            task["updated_at"] = _utc_now()
            task["completion_time"] = task["updated_at"]
            task["failure_category"] = "interrupted"
            task["final_response"] = "Task interrupted by service restart."
            forge_task_id = str(task.get("forge_task_id") or self.journal.next_task_id())
            task["forge_task_id"] = forge_task_id
            _write_private(path / "task.json", json.dumps(task, indent=2, ensure_ascii=False) + "\n")
            _append_event(path / "events.jsonl", {"time": task["updated_at"], "event": "failed", "category": "interrupted"})
            self.journal.append_event(
                forge_task_id,
                JournalEventType.TASK_FAILED,
                agent_id="manager",
                message="Task interrupted by service restart.",
                metadata={"personal_task_id": str(task.get("task_id") or path.name), "category": "interrupted"},
                transition_key=f"personal:{path.name}:interrupted",
            )

    def _start_workers(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for index in range(self.config.personal.max_active_tasks):
                thread = Thread(target=self._worker_loop, name=f"swarm-personal-{index}", daemon=True)
                thread.start()

    def _task_dir(self, task_id: str) -> Path:
        if not SAFE_TASK_ID.fullmatch(task_id):
            raise PersonalError("Task not found.", status=404, code="not_found")
        return (self.root / task_id).resolve()

    def _task_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "task.json"

    def _message_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "messages.json"

    def _events_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "events.jsonl"

    def _cancel_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "cancel.requested"

    def _load_task(self, task_id: str) -> dict[str, Any]:
        task = _read_json(self._task_path(task_id), {})
        if not isinstance(task, dict) or not task:
            raise PersonalError("Task not found.", status=404, code="not_found")
        return task

    def _save_task(self, task_id: str, task: dict[str, Any]) -> None:
        _write_private(self._task_path(task_id), json.dumps(task, indent=2, ensure_ascii=False) + "\n")

    def _emit(self, task_id: str, event: str, **payload: Any) -> None:
        _append_event(self._events_path(task_id), {"time": _utc_now(), "event": event, **payload})

    def _forge_task_id(self, task_id: str) -> str:
        task = self._load_task(task_id)
        forge_task_id = str(task.get("forge_task_id") or "")
        if validate_task_id(forge_task_id):
            return forge_task_id
        forge_task_id = self.journal.next_task_id()
        task["forge_task_id"] = forge_task_id
        self._save_task(task_id, task)
        return forge_task_id

    def _journal_stage(self, task_id: str, stage: str, retry_count: int = 0) -> None:
        self.journal.append_event(
            self._forge_task_id(task_id),
            JournalEventType.STAGE_STARTED,
            agent_id="manager",
            stage=stage,
            metadata={"personal_task_id": task_id},
            transition_key=f"personal:{task_id}:stage:{stage}:r{retry_count}",
        )

    def _status_error(self, task: dict[str, Any]) -> tuple[int, str]:
        category = str(task.get("failure_category") or "")
        return {
            "cancelled": (409, "cancelled"),
            "command_execution": (400, "command_execution"),
            "file_modification": (400, "file_modification"),
            "external_write": (400, "external_write"),
            "scheduling": (400, "scheduling"),
            "invalid_messages": (400, "invalid_messages"),
            "unsupported_content": (400, "unsupported_content"),
            "message_limit": (400, "message_limit"),
            "conversation_limit": (400, "conversation_limit"),
            "unsupported_fields": (400, "unsupported_fields"),
            "no_healthy_model": (503, "no_healthy_model"),
            "timeout": (504, "timeout"),
            "interrupted": (500, "interrupted"),
            "internal": (500, "internal"),
            "task_state": (500, "task_state"),
        }.get(category, (500, category or "task_failed"))

    def _update(self, task_id: str, **fields: Any) -> dict[str, Any]:
        task = self._load_task(task_id)
        task.update(fields)
        task["updated_at"] = _utc_now()
        self._save_task(task_id, task)
        return task

    def _non_terminal(self) -> int:
        count = 0
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            task = _read_json(path / "task.json", {})
            if isinstance(task, dict) and task.get("status") in {"queued", "running"}:
                count += 1
        return count

    def create_task(self, body: dict[str, Any]) -> dict[str, Any]:
        model = str(body.get("model", "")).strip()
        if model != self.config.personal.model_id:
            raise PersonalError(
                f"Unsupported model: {model or '<missing>'}",
                status=404,
                code="model_not_found",
            )
        messages = _normalize_messages(body.get("messages"), self.config)
        if self._non_terminal() >= self.config.personal.max_active_tasks * 2:
            raise PersonalError(
                "The personal-task queue is full. Retry after an active task finishes.",
                status=429,
                code="queue_full",
            )
        trimmed = _window(messages, self.config.personal.max_conversation_chars)
        latest_user = _latest_user(trimmed)
        task_type = str(body.get("task_type", "personal_chat")).strip() or "personal_chat"
        task_payload = body.get("task_payload") if isinstance(body.get("task_payload"), dict) else {}
        rejection = None if task_type in {"night_owl", "image_generate"} else _rejection(latest_user)
        profile = task_type if task_type in {"night_owl", "image_generate"} else ("unsupported" if rejection else _profile(latest_user))
        task_metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        task_id = f"task-{uuid.uuid4().hex[:16]}"
        forge_task_id = self.journal.next_task_id()
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, mode=TASK_DIR_MODE)
        task_dir.chmod(TASK_DIR_MODE)
        _write_private(self._message_path(task_id), json.dumps(trimmed, indent=2, ensure_ascii=False) + "\n")
        now = _utc_now()
        task = {
            "task_id": task_id,
            "forge_task_id": forge_task_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "profile": profile,
            "input_message_count": len(trimmed),
            "input_char_count": sum(len(item["content"]) for item in trimmed),
            "estimated_input_tokens": _estimate_tokens(_transcript(trimmed)),
            "selected_workers": [],
            "selected_models": [],
            "selected_providers": [],
            "wiki_used": False,
            "wiki_page_ids": [],
            "start_time": None,
            "completion_time": None,
            "duration_ms": None,
            "retry_count": 0,
            "failure_category": "",
            "cancel_requested": False,
            "estimated_output_tokens": 0,
            "final_response": "",
            "message_metadata": [
                {
                    "role": item["role"],
                    "chars": len(item["content"]),
                    "sha256_prefix": _message_digest(item["content"]),
                }
                for item in trimmed
            ],
            "rejection": rejection,
            "model": model,
            "run_id": "",
            "metadata": task_metadata,
            "task_type": task_type,
            "agent_id": str(body.get("agent_id", "")).strip(),
            "task_payload": task_payload,
        }
        self._save_task(task_id, task)
        self._emit(task_id, "queued")
        self.journal.append_event(
            forge_task_id,
            JournalEventType.TASK_CREATED,
            message=f"Personal task created for profile {profile}.",
            metadata={
                "personal_task_id": task_id,
                "profile": profile,
                "model": model,
                "task_type": task_type,
                **task_metadata,
            },
            transition_key=f"personal:{task_id}:created",
        )
        self._queue.put(task_id)
        return task

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self._load_task(task_id)
        if task["status"] in {"completed", "failed", "cancelled"}:
            return task
        self._cancel_path(task_id).write_text("cancelled\n", encoding="utf-8")
        self._cancel_path(task_id).chmod(FILE_MODE)
        task["cancel_requested"] = True
        if task["status"] in {"queued", "running"}:
            task["status"] = "cancelled"
            task["failure_category"] = "cancelled"
            task["completion_time"] = _utc_now()
            self._emit(task_id, "cancelled")
            self.journal.append_event(
                self._forge_task_id(task_id),
                JournalEventType.TASK_CANCELLED,
                agent_id="manager",
                message="Personal task cancelled.",
                metadata={"personal_task_id": task_id},
                transition_key=f"personal:{task_id}:cancelled",
            )
        self._save_task(task_id, task)
        return task

    def task_view(self, task_id: str) -> dict[str, Any]:
        task = self._load_task(task_id)
        task["events"] = self.events(task_id)
        return task

    def events(self, task_id: str) -> list[dict[str, Any]]:
        raw = self._events_path(task_id)
        items: list[dict[str, Any]] = []
        try:
            for line in raw.read_text(encoding="utf-8").splitlines():
                value = json.loads(line)
                if isinstance(value, dict):
                    items.append(value)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return items[-self.config.personal.event_history_retention :]

    def _cancelled(self, task_id: str) -> bool:
        return self._cancel_path(task_id).exists()

    def _worker_loop(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                task = self._load_task(task_id)
                if task["status"] == "cancelled" or self._cancelled(task_id):
                    self._update(
                        task_id,
                        status="cancelled",
                        cancel_requested=True,
                        failure_category="cancelled",
                        completion_time=_utc_now(),
                    )
                    self._emit(task_id, "cancelled")
                    continue
                self._run_task(task_id)
            finally:
                self._queue.task_done()

    def _run_task(self, task_id: str) -> None:
        started = monotonic()
        task = self._update(task_id, status="running", start_time=_utc_now())
        self._emit(task_id, "planning")
        retry_count = int(task.get("retry_count") or 0)
        forge_task_id = self._forge_task_id(task_id)
        self.journal.append_event(
            forge_task_id,
            JournalEventType.TASK_STARTED,
            agent_id="manager",
            metadata={"personal_task_id": task_id, "retry_count": retry_count},
            transition_key=f"personal:{task_id}:started:r{retry_count}",
        )
        self.journal.grant_lease(
            forge_task_id,
            "manager",
            self.config.personal.task_timeout_seconds,
            transition_key=f"personal:{task_id}:lease:r{retry_count}",
        )
        self._journal_stage(task_id, "planning", retry_count)
        try:
            if task.get("task_type") == "night_owl":
                self._run_night_owl_task(task_id, task, forge_task_id, started, retry_count)
                return
            if task.get("task_type") == "image_generate":
                self._run_image_task(task_id, task, forge_task_id, started, retry_count)
                return
            if task.get("rejection"):
                self._complete(
                    task_id,
                    str(task["rejection"]["message"]),
                    profile=str(task["profile"]),
                    selected_workers=[],
                    selected_models=[],
                    selected_providers=[],
                    wiki_used=False,
                    wiki_page_ids=[],
                    run_id="",
                    started=started,
                )
                return
            messages = _read_json(self._message_path(task_id), [])
            if not isinstance(messages, list) or not messages:
                raise PersonalError("Task messages are unavailable.", status=500, code="task_state")
            latest_user = _latest_user(messages)
            self.dashboard.sync_models()
            roles, overrides, reasons, selected = self._select_models(str(task["profile"]))
            context_parts = [("conversation", _transcript(messages))]
            wiki_used = False
            wiki_page_ids: list[str] = []
            if _swarm_status_needed(str(task["profile"]), latest_user):
                context_parts.append(("swarm-status", json.dumps(self._swarm_status_context(), indent=2, ensure_ascii=False)))
            if _wiki_needed(str(task["profile"]), latest_user):
                self._emit(task_id, "retrieving_wiki_context")
                self._journal_stage(task_id, "retrieving_wiki_context", retry_count)
                wiki_context, wiki_page_ids = self._wiki_context(latest_user)
                if wiki_context:
                    wiki_used = True
                    context_parts.append(("wiki-context", wiki_context))
            if self._cancelled(task_id):
                self.cancel(task_id)
                return
            self._emit(task_id, "consulting_workers")
            self._journal_stage(task_id, "consulting_workers", retry_count)
            run_id = f"personal-{task_id}-r{int(task.get('retry_count') or 0)}"
            for role in (*roles, "__judge__"):
                logical_agent = "judge" if role == "__judge__" else role
                record = selected[role]
                self.journal.append_event(
                    forge_task_id,
                    JournalEventType.TASK_ASSIGNED,
                    agent_id=logical_agent,
                    run_id=run_id,
                    metadata={
                        "personal_task_id": task_id,
                        "role": role,
                        "model_id": record.model_id,
                        "provider": record.provider,
                    },
                    transition_key=f"personal:{task_id}:assigned:{run_id}:{logical_agent}",
                )
            run_config = replace(
                self.config,
                swarm=replace(
                    self.config.swarm,
                    max_workers=min(self.config.personal.max_workers, self.config.swarm.max_workers),
                    max_parallel_workers=min(self.config.personal.max_parallel_workers, self.config.swarm.max_parallel_workers),
                    worker_timeout_seconds=min(self.config.personal.worker_timeout_seconds, self.config.swarm.worker_timeout_seconds),
                    judge_timeout_seconds=min(self.config.personal.task_timeout_seconds, self.config.swarm.judge_timeout_seconds),
                    return_char_limit=min(self.config.personal.max_output_chars, self.config.swarm.return_char_limit),
                ),
            )
            bounded, _run_dir, parsed = SwarmOrchestrator(run_config).run(
                objective=latest_user,
                mode=_profile_mode(str(task["profile"])),
                acceptance=self._acceptance(str(task["profile"]), wiki_used),
                context_parts=context_parts,
                requested_workers=roles,
                role_model_overrides=overrides,
                judge_model_override=selected["__judge__"].model_id,
                selection_reasons=reasons,
                run_id=run_id,
            )
            self._emit(task_id, "synthesizing")
            self._journal_stage(task_id, "synthesizing", retry_count)
            answer = str(parsed.get("answer", "")).strip() or bounded.strip()
            if wiki_used and wiki_page_ids:
                refs = " ".join(f"[wiki:{page_id}]" for page_id in wiki_page_ids[:5])
                if refs not in answer:
                    answer = f"{answer}\n\nReferences: {refs}".strip()
            self._complete(
                task_id,
                answer[: self.config.personal.max_output_chars],
                profile=str(task["profile"]),
                selected_workers=roles,
                selected_models=[selected[role].model_id for role in (*roles, "__judge__")],
                selected_providers=[selected[role].provider for role in (*roles, "__judge__")],
                wiki_used=wiki_used,
                wiki_page_ids=wiki_page_ids,
                run_id=run_id,
                started=started,
            )
        except PersonalError as exc:
            self._fail(task_id, str(exc), exc.code, started)
        except Exception as exc:
            current = self._load_task(task_id)
            if current["retry_count"] < self.config.personal.max_retries and not self._cancelled(task_id):
                self._update(task_id, retry_count=int(current["retry_count"]) + 1)
                self._emit(task_id, "retrying", category="task_failed")
                self.journal.append_event(
                    self._forge_task_id(task_id),
                    JournalEventType.RECOVERY_PROPOSED,
                    agent_id="manager",
                    message="Existing personal-task retry path proposed one retry.",
                    metadata={"personal_task_id": task_id, "category": "task_failed"},
                    transition_key=f"personal:{task_id}:recovery-proposed:r{int(current['retry_count'])}",
                )
                self._queue.put(task_id)
                return
            category = "cancelled" if self._cancelled(task_id) else "internal"
            self._fail(task_id, str(exc), category, started)
        finally:
            self._prune_completed()

    def _run_night_owl_task(
        self,
        task_id: str,
        task: dict[str, Any],
        forge_task_id: str,
        started: float,
        retry_count: int,
    ) -> None:
        payload = task.get("task_payload") if isinstance(task.get("task_payload"), dict) else {}
        agent_id = str(task.get("agent_id") or "night_owl")
        run_id = f"night-owl-{task_id}-r{retry_count}"
        dry_run = bool(payload.get("dry_run", str(payload.get("mode", "dry_run")) != "live"))
        self.journal.append_event(
            forge_task_id,
            JournalEventType.TASK_ASSIGNED,
            agent_id=agent_id,
            run_id=run_id,
            metadata={"personal_task_id": task_id, "handler": "night_owl"},
            transition_key=f"personal:{task_id}:assigned:{run_id}:night_owl",
        )
        self.journal.append_event(
            forge_task_id,
            JournalEventType.STAGE_STARTED,
            agent_id=agent_id,
            run_id=run_id,
            stage="night_owl_subprocess",
            side_effect_state=SideEffectState.NONE if dry_run else SideEffectState.STARTED,
            metadata={"personal_task_id": task_id, "dry_run": dry_run},
            transition_key=f"personal:{task_id}:stage:night_owl_subprocess:r{retry_count}",
        )
        try:
            result = run_night_owl(payload)
        except NightOwlError as exc:
            self._fail(task_id, str(exc), "night_owl", started)
            return
        self.journal.add_checkpoint(
            CheckpointRecord(
                task_id=forge_task_id,
                stage="night_owl_subprocess",
                agent_id=agent_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                checkpoint_reference=result.checkpoint_reference,
                summary=f"Night Owl subprocess {result.status}.",
                metadata={
                    "personal_task_id": task_id,
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "duration_ms": result.duration_ms,
                },
            ),
            transition_key=f"personal:{task_id}:checkpoint:night_owl:r{retry_count}",
        )
        self.journal.append_event(
            forge_task_id,
            JournalEventType.STAGE_STARTED,
            agent_id=agent_id,
            run_id=run_id,
            stage="night_owl_finished",
            side_effect_state=result.side_effect_state,
            message=f"Night Owl subprocess {result.status}.",
            metadata={
                "personal_task_id": task_id,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "duration_ms": result.duration_ms,
            },
            transition_key=f"personal:{task_id}:stage:night_owl_finished:r{retry_count}",
        )
        output = (
            f"Night Owl {result.status}.\n\n"
            f"Return code: {result.returncode}\n"
            f"Timed out: {result.timed_out}\n"
            f"Checkpoint: {result.checkpoint_reference}\n\n"
            f"STDOUT\n{result.stdout or '-'}\n\n"
            f"STDERR\n{result.stderr or '-'}"
        )
        delivery = None
        try:
            delivery = notify_night_owl(
                self.config.swarm.catalog_path,
                result=result,
                task=task,
                task_id=task_id,
                forge_task_id=forge_task_id,
                agent_id=agent_id,
            )
        except DiscordError as exc:
            delivery = {"status": "failed", "error_summary": str(exc)}
        if delivery:
            output += f"\n\nDISCORD\nnotification={delivery['notification_id']} status={delivery['status']} classification={delivery['http_classification'] or '-'}"
        if result.status == "completed":
            if delivery and delivery["status"] != "confirmed" and not delivery.get("duplicate_suppressed"):
                self._fail(task_id, output, "discord_notification", started)
                return
            self._complete(
                task_id,
                output[: self.config.personal.max_output_chars],
                profile="night_owl",
                selected_workers=[agent_id],
                selected_models=[],
                selected_providers=[],
                wiki_used=False,
                wiki_page_ids=[],
                run_id=run_id,
                started=started,
            )
        else:
            self._fail(task_id, output, "night_owl_timeout" if result.timed_out else "night_owl", started)

    def _run_image_task(
        self,
        task_id: str,
        task: dict[str, Any],
        forge_task_id: str,
        started: float,
        retry_count: int,
    ) -> None:
        agent_id = str(task.get("agent_id") or IMAGE_AGENT_ID)
        run_id = f"image-{task_id}-r{retry_count}"
        try:
            payload = validate_image_payload(task.get("task_payload") if isinstance(task.get("task_payload"), dict) else {})
            workflow = build_workflow(payload)
            issues = validate_workflow(workflow)
            if issues:
                raise ImageGenerationError("; ".join(issues), category="workflow_invalid")
            client = ComfyUIClient(
                self.config.image_generation.comfyui_base_url,
                connect_timeout=self.config.image_generation.connect_timeout_seconds,
                request_timeout=self.config.image_generation.request_timeout_seconds,
            )
            self.journal.append_event(
                forge_task_id,
                JournalEventType.TASK_ASSIGNED,
                agent_id=agent_id,
                run_id=run_id,
                metadata={"personal_task_id": task_id, "handler": "image_generate", "preset_id": payload.preset_id},
                transition_key=f"personal:{task_id}:assigned:{run_id}:image",
            )
            self.journal.append_event(
                forge_task_id,
                JournalEventType.STAGE_STARTED,
                agent_id=agent_id,
                run_id=run_id,
                stage="connection_validated",
                metadata={"personal_task_id": task_id, "comfyui_base_url": self.config.image_generation.comfyui_base_url},
                transition_key=f"personal:{task_id}:stage:connection:r{retry_count}",
            )
            status = client.status()
            if status.state == "offline":
                raise ImageGenerationError(status.detail or "ComfyUI is offline", category="windows_offline")
            self._emit(task_id, "checkpoint", stage="connection_validated")
            remote_issues = validate_comfyui_requirements(client.object_info())
            if remote_issues:
                raise ImageGenerationError("; ".join(remote_issues), category="model_missing")
            self.journal.append_event(
                forge_task_id,
                JournalEventType.STAGE_STARTED,
                agent_id=agent_id,
                run_id=run_id,
                stage="workflow_validated",
                metadata={"personal_task_id": task_id, "preset_id": payload.preset_id},
                transition_key=f"personal:{task_id}:stage:workflow:r{retry_count}",
            )
            self._emit(task_id, "checkpoint", stage="workflow_validated")
            self.journal.append_event(
                forge_task_id,
                JournalEventType.STAGE_STARTED,
                agent_id=agent_id,
                run_id=run_id,
                stage="workflow_submission",
                side_effect_state=SideEffectState.PROPOSED,
                metadata={"personal_task_id": task_id, "preset_id": payload.preset_id},
                transition_key=f"personal:{task_id}:stage:submit-proposed:r{retry_count}",
            )
            self.journal.append_event(
                forge_task_id,
                JournalEventType.STAGE_STARTED,
                agent_id=agent_id,
                run_id=run_id,
                stage="workflow_submission",
                side_effect_state=SideEffectState.STARTED,
                metadata={"personal_task_id": task_id},
                transition_key=f"personal:{task_id}:stage:submit-started:r{retry_count}",
            )
            try:
                prompt_id = client.submit(workflow)
            except ImageGenerationError as exc:
                self.journal.append_event(
                    forge_task_id,
                    JournalEventType.STAGE_STARTED,
                    agent_id=agent_id,
                    run_id=run_id,
                    stage="workflow_submission",
                    side_effect_state=SideEffectState.UNKNOWN,
                    message=str(exc),
                    metadata={"personal_task_id": task_id},
                    transition_key=f"personal:{task_id}:stage:submit-unknown:r{retry_count}",
                )
                raise ImageGenerationError(str(exc), category="unknown_submission")
            self._update(task_id, comfyui_prompt_id=prompt_id, seed=payload.seed, preset_id=payload.preset_id, progress=0)
            self.journal.append_event(
                forge_task_id,
                JournalEventType.STAGE_STARTED,
                agent_id=agent_id,
                run_id=run_id,
                stage="workflow_submitted",
                side_effect_state=SideEffectState.CONFIRMED,
                metadata={"personal_task_id": task_id, "comfyui_prompt_id": prompt_id},
                transition_key=f"personal:{task_id}:stage:submit-confirmed:r{retry_count}",
            )

            def progress(mark: int) -> None:
                self._update(task_id, progress=mark)
                self._emit(task_id, "progress", progress=mark)
                self.journal.add_checkpoint(
                    CheckpointRecord(
                        task_id=forge_task_id,
                        stage="generation_progress",
                        agent_id=agent_id,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        checkpoint_reference=f"image/{forge_task_id}/progress-{mark}",
                        summary=f"Image generation progress {mark}%.",
                        metadata={"personal_task_id": task_id, "comfyui_prompt_id": prompt_id, "progress": mark},
                    ),
                    transition_key=f"personal:{task_id}:checkpoint:image-progress:{mark}",
                )

            image_ref = wait_for_output(
                client,
                prompt_id,
                timeout_seconds=self.config.image_generation.generation_timeout_seconds,
                poll_interval_seconds=self.config.image_generation.poll_interval_seconds,
                progress=progress,
            )
            image_bytes = client.retrieve_output(image_ref, self.config.image_generation.max_image_bytes)
            result = store_artifact(self.config, forge_task_id, prompt_id, payload, image_bytes)
            result = replace(result, duration_ms=int((monotonic() - started) * 1000))
            self._update(
                task_id,
                progress=100,
                artifact_dir=result.artifact_dir,
                image_path=result.image_path,
                thumbnail_path=result.thumbnail_path,
                metadata_path=result.metadata_path,
                checksum_sha256=result.checksum_sha256,
            )
            self.journal.add_checkpoint(
                CheckpointRecord(
                    task_id=forge_task_id,
                    stage="artifact_stored",
                    agent_id=agent_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    checkpoint_reference=f"artifacts/images/{forge_task_id}/metadata.json",
                    summary="Generated image artifact stored.",
                    metadata={
                        "personal_task_id": task_id,
                        "comfyui_prompt_id": prompt_id,
                        "preset_id": payload.preset_id,
                        "seed": payload.seed,
                        "sha256": result.checksum_sha256,
                    },
                ),
                transition_key=f"personal:{task_id}:checkpoint:image-artifact",
            )
            delivery = None
            if payload.notification_requested:
                delivery = notify_image_completion(
                    self.config,
                    task_id=task_id,
                    forge_task_id=forge_task_id,
                    result=result,
                    dashboard_url=f"/api/images/artifacts/{forge_task_id}/original",
                )
            answer = (
                f"Image generation completed.\n"
                f"Forge task: {forge_task_id}\n"
                f"ComfyUI prompt: {prompt_id}\n"
                f"Preset: {payload.preset_id}\n"
                f"Seed: {payload.seed}\n"
                f"Artifact: {result.artifact_dir}\n"
                f"SHA-256: {result.checksum_sha256}"
            )
            if delivery:
                answer += f"\nDiscord: {delivery['status']}"
            self._complete(
                task_id,
                answer[: self.config.personal.max_output_chars],
                profile="image_generate",
                selected_workers=[agent_id],
                selected_models=[],
                selected_providers=[],
                wiki_used=False,
                wiki_page_ids=[],
                run_id=run_id,
                started=started,
            )
        except ImageGenerationError as exc:
            if task.get("task_payload", {}).get("notification_requested"):
                try:
                    notify_image_completion(
                        self.config,
                        task_id=task_id,
                        forge_task_id=forge_task_id,
                        result=None,
                        failure_category=exc.category,
                    )
                except Exception:
                    pass
            self._fail(task_id, str(exc), exc.category, started)

    def _complete(
        self,
        task_id: str,
        answer: str,
        *,
        profile: str,
        selected_workers: list[str],
        selected_models: list[str],
        selected_providers: list[str],
        wiki_used: bool,
        wiki_page_ids: list[str],
        run_id: str,
        started: float,
    ) -> None:
        if self._cancelled(task_id):
            self._fail(task_id, "Task cancelled.", "cancelled", started)
            return
        self._update(
            task_id,
            status="completed",
            completion_time=_utc_now(),
            duration_ms=int((monotonic() - started) * 1000),
            profile=profile,
            selected_workers=selected_workers,
            selected_models=selected_models,
            selected_providers=selected_providers,
            wiki_used=wiki_used,
            wiki_page_ids=wiki_page_ids,
            final_response=answer,
            estimated_output_tokens=_estimate_tokens(answer),
            run_id=run_id,
            failure_category="",
        )
        self._emit(task_id, "completed")
        forge_task_id = self._forge_task_id(task_id)
        self.journal.add_checkpoint(
            CheckpointRecord(
                task_id=forge_task_id,
                stage="completed",
                agent_id="manager",
                timestamp=datetime.now(timezone.utc).isoformat(),
                checkpoint_reference=f"personal/{task_id}/task.json",
                summary="Personal task completed.",
                metadata={"personal_task_id": task_id, "run_id": run_id},
            ),
            transition_key=f"personal:{task_id}:checkpoint:completed",
        )
        self.journal.append_event(
            forge_task_id,
            JournalEventType.TASK_COMPLETED,
            agent_id="manager",
            run_id=run_id,
            checkpoint_reference=f"personal/{task_id}/task.json",
            message="Personal task completed.",
            metadata={"personal_task_id": task_id},
            transition_key=f"personal:{task_id}:completed",
        )

    def _fail(self, task_id: str, message: str, category: str, started: float) -> None:
        status = "cancelled" if category == "cancelled" else "failed"
        payload = {
            "status": status,
            "completion_time": _utc_now(),
            "duration_ms": int((monotonic() - started) * 1000),
            "failure_category": category,
            "final_response": "" if status == "cancelled" else message[: self.config.personal.max_output_chars],
            "estimated_output_tokens": _estimate_tokens(message),
        }
        self._update(task_id, **payload)
        self._emit(task_id, "cancelled" if status == "cancelled" else "failed", category=category)
        self.journal.append_event(
            self._forge_task_id(task_id),
            JournalEventType.TASK_CANCELLED if status == "cancelled" else JournalEventType.TASK_FAILED,
            agent_id="manager",
            message=message[:2000],
            metadata={"personal_task_id": task_id, "category": category},
            transition_key=f"personal:{task_id}:{status}:{category}",
        )

    def _prune_completed(self) -> None:
        finished: list[tuple[str, Path]] = []
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            task = _read_json(path / "task.json", {})
            if isinstance(task, dict) and task.get("status") in {"completed", "failed", "cancelled"}:
                finished.append((str(task.get("completion_time") or task.get("updated_at") or ""), path))
        finished.sort(reverse=True)
        for _stamp, path in finished[self.config.personal.completed_task_retention :]:
            shutil.rmtree(path, ignore_errors=True)

    def _select_models(
        self, profile: str
    ) -> tuple[list[str], dict[str, str], dict[str, str], dict[str, ModelRecord]]:
        roles = _profile_roles(profile)[: self.config.personal.max_workers]
        selected: dict[str, ModelRecord] = {}
        overrides: dict[str, str] = {}
        reasons: dict[str, str] = {}
        used_families: set[str] = set()
        excluded_models: set[str] = {self.config.personal.model_id}
        available = [
            record for record in self.catalog.list()
            if record.enabled and record.available and record.kind == "chat"
            and record.probe_status == "healthy" and record.model_id != self.config.personal.model_id
        ]
        if not available:
            raise PersonalError(
                "No healthy chat-capable worker models are available.",
                status=503,
                code="no_healthy_model",
            )
        for role in roles:
            candidates = self.catalog.recommend(
                _profile_mode(profile),
                1,
                self.config.reliability,
                role,
                excluded_models=excluded_models,
                used_families=used_families,
            )
            if not candidates:
                raise PersonalError(
                    f"No healthy model is available for role {role}.",
                    status=503,
                    code="no_healthy_model",
                )
            record = candidates[0]
            selected[role] = record
            overrides[role] = record.model_id
            reasons[role] = self.catalog.recommendation_reason(
                record, _profile_mode(profile), self.config.reliability, role
            )
            excluded_models.add(record.model_id)
            used_families.add(record.family)
        judge_candidates = self.catalog.recommend(
            _profile_mode(profile),
            1,
            self.config.reliability,
            "__judge__",
            excluded_models=excluded_models,
            used_families=used_families,
        )
        if not judge_candidates:
            judge_candidates = self.catalog.recommend(
                _profile_mode(profile),
                1,
                self.config.reliability,
                "__judge__",
                excluded_models={self.config.personal.model_id},
            )
        if not judge_candidates:
            raise PersonalError(
                "No healthy judge model is available.",
                status=503,
                code="no_healthy_model",
            )
        selected["__judge__"] = judge_candidates[0]
        reasons["__judge__"] = self.catalog.recommendation_reason(
            judge_candidates[0],
            _profile_mode(profile),
            self.config.reliability,
            "__judge__",
        )
        return roles, overrides, reasons, selected

    def _swarm_status_context(self) -> dict[str, Any]:
        return {
            "captured_at": _utc_now(),
            "model_count": len(self.dashboard.list_models()),
            "healthy_models": [
                {
                    "model_id": item["model_id"],
                    "provider": item["provider"],
                    "family": item["family"],
                    "recommended_roles": item.get("recommended_roles", []),
                }
                for item in self.dashboard.list_models()
                if item.get("probe_status") == "healthy"
            ][:8],
            "recent_runs": [
                {
                    "run_id": item["run_id"],
                    "status": item["status"],
                    "mode": item["mode"],
                    "events": item["events"],
                }
                for item in self.dashboard.list_runs()[:8]
            ],
        }

    def _wiki_context(self, query: str) -> tuple[str, list[str]]:
        repository = WikiRepository()
        index = WikiIndex(repository)
        results: list[dict[str, Any]] = []
        queries = [query]
        jira_matches = SAFE_JIRA.findall(query)
        for jira in jira_matches:
            if jira not in queries:
                queries.append(jira)
        for candidate in queries:
            try:
                search = index.search(candidate, limit=3)
            except (WikiSearchError, ValueError):
                continue
            value = search.get("results", [])
            if isinstance(value, list) and value:
                results = value
                break
        if not isinstance(results, list) or not results:
            return "", []
        budget = self.config.personal.max_wiki_context_chars
        parts: list[str] = []
        page_ids: list[str] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            page_id = str(item.get("page_id", "")).strip()
            if not page_id:
                continue
            snippet = str(item.get("snippet", "")).strip()
            block = f"{page_id}: {item.get('title', '')}\nSnippet: {snippet}\n"
            if len("".join(parts)) + len(block) > budget:
                continue
            parts.append(block)
            page_ids.append(page_id)
        if page_ids and (SAFE_JIRA.search(query) or page_ids[0] in query):
            try:
                page = repository.page_view(page_id=page_ids[0])
            except Exception:
                page = None
            if isinstance(page, dict):
                body = _string(page.get("content", ""))
                full = (
                    f"Full page {page_ids[0]}:\n"
                    f"Title: {page.get('metadata', {}).get('title', '')}\n"
                    f"Verification: {page.get('verification', '')}\n"
                    f"Body: {body[: max(0, budget - len(''.join(parts)) - 64)]}\n"
                )
                if len("".join(parts)) + len(full) <= budget:
                    parts.append(full)
        return "\n".join(parts).strip(), page_ids

    def _acceptance(self, profile: str, wiki_used: bool) -> str:
        rule = {
            "weekly_planning": "Produce a practical weekly plan with prioritized next actions and a concise checklist.",
            "trip_planning": "Compare practical options and return a concrete plan from supplied facts only.",
            "comparison": "Compare the options directly with pros, cons, and a recommendation.",
            "summarization": "Summarize the supplied text accurately and organize it clearly.",
            "brainstorming": "Produce concrete ideas with trade-offs and next steps.",
            "checklist": "Return a concise checklist and next steps.",
            "wiki_research": "Use wiki evidence when relevant and cite supporting page IDs in the answer.",
            "swarm_status": "Summarize current swarm models and recent runs without claiming hidden system state.",
            "general": "Answer directly and keep the result actionable.",
        }.get(profile, "Answer directly and keep the result actionable.")
        wiki_rule = "Use only compact wiki evidence supplied in context." if wiki_used else "Skip wiki claims unless context supplies them."
        return (
            f"{rule} Distinguish facts, assumptions, recommendations, and limitations when material. "
            f"{wiki_rule} Never claim external actions occurred. Never include hidden reasoning."
        )


class PersonalHandler(BaseHTTPRequestHandler):
    server_version = "OWUISwarmPersonal/0.2"

    @property
    def manager(self) -> PersonalTaskManager:
        return self.server.manager  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _write(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, data: Any) -> None:
        self._write(status, _json_bytes(data), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, code: str) -> None:
        self._json(status, {"error": {"message": message, "type": code, "code": code}})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise PersonalError("Request body must be valid JSON.", code="invalid_json") from exc
        if not isinstance(value, dict):
            raise PersonalError("Request body must be a JSON object.", code="invalid_json")
        return value

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header.startswith("Bearer ") and header.removeprefix("Bearer ").strip() == self.manager.auth_token

    def _stream_chunk(
        self,
        request_id: str,
        model: str,
        created: int,
        text: str,
        finish_reason: str | None = None,
    ) -> None:
        chunk = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text} if text else {}, "finish_reason": finish_reason}],
        }
        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream_task(self, task_id: str, request_id: str, created: int, model: str) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        seen = 0
        emitted: set[str] = set()
        self._stream_chunk(request_id, model, created, "")
        try:
            while True:
                events = self.manager.events(task_id)
                for event in events[seen:]:
                    seen += 1
                    name = str(event.get("event", ""))
                    if name in STREAM_STATUSES and name not in emitted:
                        emitted.add(name)
                        self._stream_chunk(request_id, model, created, STREAM_STATUSES[name])
                    if name in {"failed", "cancelled", "completed"}:
                        task = self.manager.task_view(task_id)
                        finish = "cancelled" if name == "cancelled" else "stop"
                        if name != "cancelled":
                            self._stream_chunk(request_id, model, created, str(task.get("final_response", "")), finish)
                        else:
                            self._stream_chunk(request_id, model, created, "", finish)
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        return
                task = self.manager.task_view(task_id)
                if task["status"] in {"completed", "failed", "cancelled"}:
                    continue
                sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            self.manager.cancel(task_id)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/health":
                self._json(
                    200,
                    {
                        "status": "ok",
                        "model_id": self.manager.config.personal.model_id,
                        "port": self.manager.config.personal.port,
                    },
                )
                return
            if path == "/v1/models":
                if not self._authorized():
                    self._error(401, "Bearer token required.", "unauthorized")
                    return
                self._json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": self.manager.config.personal.model_id,
                                "object": "model",
                                "created": 0,
                                "owned_by": "openwebui-codex-swarm",
                            }
                        ],
                    },
                )
                return
            if not self._authorized():
                self._error(401, "Bearer token required.", "unauthorized")
                return
            if path.startswith("/api/personal-tasks/") and path.endswith("/events"):
                task_id = path.removeprefix("/api/personal-tasks/").removesuffix("/events").strip("/")
                self._json(200, {"task_id": task_id, "events": self.manager.events(task_id)})
                return
            if path.startswith("/api/personal-tasks/"):
                task_id = path.removeprefix("/api/personal-tasks/").strip("/")
                self._json(200, self.manager.task_view(task_id))
                return
            self._error(404, "Not found.", "not_found")
        except PersonalError as exc:
            self._error(exc.status, str(exc), exc.code)

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            if path != "/health" and not self._authorized():
                self._error(401, "Bearer token required.", "unauthorized")
                return
            if path == "/v1/chat/completions":
                body = {key: value for key, value in self._body().items() if key not in OPENAI_COMPAT_IGNORED_FIELDS}
                task = self.manager.create_task(body)
                request_id = f"chatcmpl-{task['task_id']}"
                created = _epoch_seconds(str(task["created_at"]))
                if body.get("stream", False):
                    self._stream_task(task["task_id"], request_id, created, str(task["model"]))
                    return
                deadline = monotonic() + _sync_wait_seconds(self.manager.config)
                while monotonic() < deadline:
                    current = self.manager.task_view(str(task["task_id"]))
                    if current["status"] in {"failed", "cancelled"}:
                        status, code = self.manager._status_error(current)
                        raise PersonalError(
                            str(current.get("final_response") or "Task failed."),
                            status=status,
                            code=code,
                        )
                    if current["status"] == "completed":
                        content = str(current.get("final_response", ""))
                        completion_tokens = int(current.get("estimated_output_tokens") or _estimate_tokens(content))
                        prompt_tokens = int(current.get("estimated_input_tokens") or 0)
                        self._json(
                            200,
                            {
                                "id": request_id,
                                "object": "chat.completion",
                                "created": created,
                                "model": str(task["model"]),
                                "choices": [
                                    {
                                        "index": 0,
                                        "message": {"role": "assistant", "content": content},
                                        "finish_reason": "stop",
                                    }
                                ],
                                "usage": {
                                    "prompt_tokens": prompt_tokens,
                                    "completion_tokens": completion_tokens,
                                    "total_tokens": prompt_tokens + completion_tokens,
                                },
                                "task_id": task["task_id"],
                            },
                        )
                        return
                    sleep(0.1)
                self.manager.cancel(str(task["task_id"]))
                raise PersonalError("Task timed out before completion.", status=504, code="timeout")
            if path == "/api/personal-tasks":
                task = self.manager.create_task(self._body())
                self._json(202, task)
                return
            if path.startswith("/api/personal-tasks/") and path.endswith("/cancel"):
                task_id = path.removeprefix("/api/personal-tasks/").removesuffix("/cancel").strip("/")
                self._json(200, self.manager.cancel(task_id))
                return
            self._error(404, "Not found.", "not_found")
        except PersonalError as exc:
            self._error(exc.status, str(exc), exc.code)


def serve_personal(config: AppConfig) -> None:
    manager = PersonalTaskManager(config)
    if config.personal.loopback_host != "127.0.0.1":
        raise RuntimeError("Personal task loopback host must be 127.0.0.1.")
    servers: list[ThreadingHTTPServer] = []
    for host in dict.fromkeys([config.personal.loopback_host, *_bridge_hosts()]):
        try:
            server = ThreadingHTTPServer((host, config.personal.port), PersonalHandler)
        except OSError:
            if host == config.personal.loopback_host:
                raise
            continue
        server.manager = manager  # type: ignore[attr-defined]
        servers.append(server)
    if not servers:
        raise RuntimeError("No personal-task listener could be started.")
    print("Swarm personal model:")
    for server in servers:
        address = server.server_address
        print(f"- http://{address[0]}:{address[1]}")
    threads = [Thread(target=server.serve_forever, daemon=True) for server in servers]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
