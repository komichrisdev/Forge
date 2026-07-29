from __future__ import annotations

from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from unittest.mock import patch
from urllib import error, request
import json
import os
import unittest

from swarm_router.catalog import ModelCatalog
from swarm_router.config import AppConfig, load_config
from swarm_router.journal import TaskJournal
from swarm_router.personal import PersonalError, PersonalHandler, PersonalTaskManager
from swarm_router.wiki import WikiRepository
from swarm_router.wiki_search import WikiIndex


PERSONAL_TOKEN = "test-personal-token"


class FakeDashboard:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.catalog = ModelCatalog(config.swarm.catalog_path)
        self.catalog.import_run_history(config.swarm.run_directory)

    def sync_models(self) -> list[dict[str, object]]:
        return self.list_models()

    def list_models(self) -> list[dict[str, object]]:
        return [self.catalog.as_dict(record, self.config.reliability) for record in self.catalog.list()]

    def list_runs(self) -> list[dict[str, object]]:
        return []


def write_config(root: Path) -> AppConfig:
    os.environ["OPEN_WEBUI_API_KEY"] = "test-openwebui-key"
    os.environ["SWARM_PERSONAL_API_KEY"] = PERSONAL_TOKEN
    config_path = root / "config.toml"
    config_path.write_text(
        f'''[openwebui]
base_url = "http://127.0.0.1:9"
api_key_env = "OPEN_WEBUI_API_KEY"
timeout_seconds = 3

[swarm]
run_directory = "{root / 'runs'}"
catalog_path = "{root / 'catalog.db'}"
max_workers = 3
max_parallel_workers = 2
worker_timeout_seconds = 1
judge_timeout_seconds = 1
max_context_chars = 12000
return_char_limit = 4000

[probe]
timeout_seconds = 1
max_parallel = 1

[reliability]
recent_attempt_window = 8
cooldown_after_consecutive_failures = 3
cooldown_minutes = 60

[dashboard]
metadata_directory = "{root / 'dashboard'}"

[personal]
task_directory = "{root / 'personal'}"
port = 8788
max_messages = 6
max_message_chars = 1200
max_conversation_chars = 4000
max_output_chars = 2000
max_wiki_context_chars = 3000
max_workers = 2
max_parallel_workers = 2
max_retries = 1
task_timeout_seconds = 2
worker_timeout_seconds = 1
max_active_tasks = 1
completed_task_retention = 20
event_history_retention = 40

[authority]
supervisor_name = "Codex"

[judge]
name = "integrator"
model = "fake/deepseek-judge-reason"
system = "integration clerk"

[[workers]]
name = "planner"
model = "fake/qwen-planner-instruct"
modes = ["auto", "general", "research"]
system = "planner"

[[workers]]
name = "critic"
model = "fake/mistral-critic-instruct"
modes = ["auto", "general", "research"]
system = "critic"

[[workers]]
name = "verifier"
model = "fake/qwen-verifier-reason"
modes = ["auto", "general", "research"]
system = "verifier"
''',
        encoding="utf-8",
    )
    return load_config(config_path)


def seed_catalog(config: AppConfig) -> None:
    catalog = ModelCatalog(config.swarm.catalog_path)
    entries = [
        {"id": "fake/qwen-planner-instruct", "provider": "fake", "supports_tools": True},
        {"id": "fake/mistral-critic-instruct", "provider": "fake", "supports_tools": True},
        {"id": "fake/qwen-verifier-reason", "provider": "fake", "supports_tools": True},
        {"id": "fake/deepseek-judge-reason", "provider": "fake", "supports_tools": True},
    ]
    catalog.sync(entries)
    for item in entries:
        catalog.record_probe(item["id"], "healthy", 10)


class PersonalApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.wiki_root = self.root / "wiki"
        repository = WikiRepository(self.wiki_root)
        repository.initialize(with_samples=True)
        WikiIndex(repository).build(full=True)
        self.previous_wiki_root = os.environ.get("OWUI_SWARM_WIKI_ROOT")
        os.environ["OWUI_SWARM_WIKI_ROOT"] = str(self.wiki_root)

        self.config = write_config(self.root)
        seed_catalog(self.config)
        self.dashboard_patch = patch("swarm_router.personal.DashboardApp", FakeDashboard)
        self.dashboard_patch.start()
        self.manager = PersonalTaskManager(self.config)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PersonalHandler)
        self.server.manager = self.manager  # type: ignore[attr-defined]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.dashboard_patch.stop()
        if self.previous_wiki_root is None:
            os.environ.pop("OWUI_SWARM_WIKI_ROOT", None)
        else:
            os.environ["OWUI_SWARM_WIKI_ROOT"] = self.previous_wiki_root
        self.temporary.cleanup()

    def api(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        auth: bool = True,
    ) -> tuple[int, dict[str, object]]:
        headers: dict[str, str] = {}
        if auth:
            headers["Authorization"] = f"Bearer {PERSONAL_TOKEN}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def stream(self, payload: dict[str, object]) -> str:
        req = request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {PERSONAL_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            return response.read().decode("utf-8")

    def wait_for_status(self, task_id: str, expected: str, timeout: float = 5.0) -> dict[str, object]:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            task = self.manager.task_view(task_id)
            if task["status"] == expected:
                return task
            sleep(0.05)
        self.fail(f"Timed out waiting for {task_id} to reach {expected}.")

    def test_health_and_models(self) -> None:
        status, health = self.api("GET", "/health", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["model_id"], "swarm-personal")
        self.assertNotIn("task_root", health)

        status, payload = self.api("GET", "/v1/models", auth=False)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

        status, payload = self.api("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"][0]["id"], "swarm-personal")
        self.assertEqual(
            [item["id"] for item in payload["data"]],
            ["swarm-personal", "swarm-developer"],
        )

    def test_non_stream_completion_and_task_endpoint(self) -> None:
        with patch("swarm_router.personal.SwarmOrchestrator.run", return_value=("Weekly plan", self.root, {"answer": "Weekly plan"})):
            status, payload = self.api(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "Turn these notes into a weekly plan"}],
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["choices"][0]["message"]["content"], "Weekly plan")
        task_id = str(payload["task_id"])
        status, task = self.api("GET", f"/api/personal-tasks/{task_id}")
        self.assertEqual(status, 200)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["profile"], "weekly_planning")
        self.assertEqual(task["selected_workers"], ["planner"])
        self.assertFalse(task["wiki_used"])
        self.assertEqual(task["message_metadata"][0]["role"], "user")
        self.assertNotIn("preview", json.dumps(task))
        forge_task_id = str(task["forge_task_id"])
        journal = TaskJournal(self.config.swarm.catalog_path)
        self.assertEqual(journal.reconstruct(forge_task_id)["status"], "completed")
        self.assertIn("TASK_CREATED", [event.event_type for event in journal.events(forge_task_id)])
        self.assertIn("TASK_ASSIGNED", [event.event_type for event in journal.events(forge_task_id)])
        self.assertEqual(journal.checkpoints(forge_task_id)[0].checkpoint_reference, f"personal/{task_id}/task.json")

    def test_openai_tool_fields_are_ignored_for_compatibility(self) -> None:
        with patch("swarm_router.personal.SwarmOrchestrator.run", return_value=("Hello", self.root, {"answer": "Hello"})):
            status, payload = self.api(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "Say hello"}],
                    "tools": [{"type": "function", "function": {"name": "noop", "parameters": {}}}],
                    "tool_choice": "auto",
                    "parallel_tool_calls": True,
                    "response_format": {"type": "text"},
                    "modalities": ["text"],
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["choices"][0]["message"]["content"], "Hello")

    def test_task_create_endpoint_queues_existing_personal_backend_work(self) -> None:
        with patch("swarm_router.personal.SwarmOrchestrator.run", return_value=("Queued", self.root, {"answer": "Queued"})):
            status, payload = self.api(
                "POST",
                "/api/personal-tasks",
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "Summarize this"}],
                    "metadata": {"schedule_id": "FS-20260728-000001"},
                },
            )
            self.assertEqual(status, 202)
            task = self.wait_for_status(str(payload["task_id"]), "completed")
        self.assertEqual(task["final_response"], "Queued")
        self.assertEqual(task["metadata"]["schedule_id"], "FS-20260728-000001")

    def test_transient_task_failure_retries_once(self) -> None:
        with patch(
            "swarm_router.personal.SwarmOrchestrator.run",
            side_effect=[RuntimeError("transient upstream failure"), ("Recovered", self.root, {"answer": "Recovered"})],
        ):
            status, payload = self.api(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "Say hello"}],
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["choices"][0]["message"]["content"], "Recovered")
        task = self.manager.task_view(str(payload["task_id"]))
        self.assertEqual(task["retry_count"], 1)

    def test_streaming_completion_emits_status_and_final_text(self) -> None:
        with patch("swarm_router.personal.SwarmOrchestrator.run", return_value=("Orbit answer", self.root, {"answer": "Orbit answer"})):
            body = self.stream(
                {
                    "model": "swarm-personal",
                    "stream": True,
                    "messages": [{"role": "user", "content": "What does ORBIT-7 say in the wiki?"}],
                }
            )
        self.assertIn("Planning...", body)
        self.assertIn("Retrieving wiki context...", body)
        self.assertIn("Consulting workers...", body)
        self.assertIn("Synthesizing...", body)
        self.assertIn("Orbit answer", body)
        self.assertIn("data: [DONE]", body)

    def test_wiki_context_used_and_irrelevant_task_skips_it(self) -> None:
        seen: list[list[str]] = []

        def fake_run(*_args: object, **kwargs: object) -> tuple[str, Path, dict[str, str]]:
            context_parts = kwargs["context_parts"]
            seen.append([label for label, _content in context_parts])
            return ("Answer", self.root, {"answer": "Answer"})

        with patch("swarm_router.personal.SwarmOrchestrator.run", side_effect=fake_run):
            status, wiki_payload = self.api(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "Summarize ORBIT-7 from the wiki"}],
                },
            )
            self.assertEqual(status, 200)
            status, plain_payload = self.api(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "Help me plan my week"}],
                },
            )
            self.assertEqual(status, 200)
        self.assertIn("wiki-context", seen[0])
        self.assertNotIn("wiki-context", seen[1])
        first = self.manager.task_view(str(wiki_payload["task_id"]))
        second = self.manager.task_view(str(plain_payload["task_id"]))
        self.assertTrue(first["wiki_used"])
        self.assertFalse(second["wiki_used"])
        self.assertIn("[wiki:", first["final_response"])

    def test_swarm_status_profile_adds_compact_status_context(self) -> None:
        captured: list[tuple[str, str]] = []

        def fake_run(*_args: object, **kwargs: object) -> tuple[str, Path, dict[str, str]]:
            captured.extend(kwargs["context_parts"])
            return ("Status answer", self.root, {"answer": "Status answer"})

        with patch("swarm_router.personal.SwarmOrchestrator.run", side_effect=fake_run):
            status, _payload = self.api(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "Show me model status and recent runs"}],
                },
            )
        self.assertEqual(status, 200)
        labels = [label for label, _content in captured]
        self.assertIn("swarm-status", labels)
        swarm_status = next(content for label, content in captured if label == "swarm-status")
        self.assertIn("healthy_models", swarm_status)
        self.assertNotIn("catalog.db", swarm_status)

    def test_invalid_messages_limits_and_model_validation(self) -> None:
        cases = [
            (
                {
                    "model": "missing-model",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                404,
                "model_not_found",
            ),
            (
                {
                    "model": "swarm-personal",
                    "messages": "hello",
                },
                400,
                "invalid_messages",
            ),
            (
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "tool", "content": "hello"}],
                },
                400,
                "invalid_messages",
            ),
            (
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "x" * 1300}],
                },
                400,
                "message_limit",
            ),
            (
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "a"}] * 7,
                },
                400,
                "message_limit",
            ),
        ]
        for payload, expected_status, expected_code in cases:
            with self.subTest(code=expected_code):
                status, body = self.api("POST", "/v1/chat/completions", payload)
                self.assertEqual(status, expected_status)
                self.assertEqual(body["error"]["code"], expected_code)

    def test_read_only_rejections_are_deterministic(self) -> None:
        prompts = {
            "please run bash ls -la": "This model is read-only. It cannot run shell commands, code, Docker, or system tools.",
            "edit the repo config and commit it": "This model is read-only. It cannot modify files, repositories, Git state, or local configuration.",
            "send an email and update jira": "This model cannot write to Jira, email, calendar, Drive, or other external systems.",
            "schedule a reminder every day": "This model cannot create reminders, schedules, recurring checks, or background monitoring.",
        }
        for prompt, expected in prompts.items():
            with self.subTest(prompt=prompt):
                status, payload = self.api(
                    "POST",
                    "/v1/chat/completions",
                    {
                        "model": "swarm-personal",
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["choices"][0]["message"]["content"], expected)

    def test_no_healthy_model_returns_503(self) -> None:
        root = Path(self.temporary.name) / "no-healthy"
        root.mkdir(parents=True, exist_ok=True)
        config = write_config(root)
        with patch("swarm_router.personal.DashboardApp", FakeDashboard):
            manager = PersonalTaskManager(config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), PersonalHandler)
        server.manager = manager  # type: ignore[attr-defined]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                data=json.dumps(
                    {
                        "model": "swarm-personal",
                        "messages": [{"role": "user", "content": "Help me plan my week"}],
                    }
                ).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {PERSONAL_TOKEN}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with self.assertRaises(error.HTTPError) as raised:
                request.urlopen(req, timeout=5)
            body = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(raised.exception.code, 503)
            self.assertEqual(body["error"]["code"], "no_healthy_model")
        finally:
            server.shutdown()
            server.server_close()

    def test_cancel_running_task_stays_cancelled(self) -> None:
        def slow_run(*_args: object, **_kwargs: object) -> tuple[str, Path, dict[str, str]]:
            sleep(0.3)
            return ("Too late", self.root, {"answer": "Too late"})

        with patch("swarm_router.personal.SwarmOrchestrator.run", side_effect=slow_run):
            task = self.manager.create_task(
                {
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "Help me plan my week"}],
                }
            )
            task_id = str(task["task_id"])
            self.wait_for_status(task_id, "running")
            status, payload = self.api("POST", f"/api/personal-tasks/{task_id}/cancel", {})
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "cancelled")
            self.wait_for_status(task_id, "cancelled")
            sleep(0.4)
            final = self.manager.task_view(task_id)
            self.assertEqual(final["status"], "cancelled")
            self.assertEqual(final["failure_category"], "cancelled")

    def test_startup_marks_stale_running_tasks_interrupted(self) -> None:
        root = Path(self.temporary.name) / "stale"
        root.mkdir()
        config = write_config(root)
        task_dir = Path(config.personal.task_directory) / "task-stale000000000"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(
            json.dumps(
                {
                    "task_id": "task-stale000000000",
                    "status": "running",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        with patch("swarm_router.personal.DashboardApp", FakeDashboard):
            manager = PersonalTaskManager(config)
        task = manager.task_view("task-stale000000000")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["failure_category"], "interrupted")


class PersonalApiDisconnectTest(unittest.TestCase):
    def test_stream_disconnect_cancels_the_task(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            wiki_root = root / "wiki"
            repository = WikiRepository(wiki_root)
            repository.initialize(with_samples=True)
            WikiIndex(repository).build(full=True)
            previous = os.environ.get("OWUI_SWARM_WIKI_ROOT")
            os.environ["OWUI_SWARM_WIKI_ROOT"] = str(wiki_root)
            config = write_config(root)
            seed_catalog(config)
            with patch("swarm_router.personal.DashboardApp", FakeDashboard):
                manager = PersonalTaskManager(config)
            server = ThreadingHTTPServer(("127.0.0.1", 0), PersonalHandler)
            server.manager = manager  # type: ignore[attr-defined]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def slow_run(*_args: object, **_kwargs: object) -> tuple[str, Path, dict[str, str]]:
                    sleep(0.3)
                    return ("Late answer", root, {"answer": "Late answer"})

                with patch("swarm_router.personal.SwarmOrchestrator.run", side_effect=slow_run):
                    req = request.Request(
                        f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                        data=json.dumps(
                            {
                                "model": "swarm-personal",
                                "stream": True,
                                "messages": [{"role": "user", "content": "Tell me about ORBIT-7"}],
                            }
                        ).encode("utf-8"),
                        headers={
                            "Authorization": f"Bearer {PERSONAL_TOKEN}",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    with request.urlopen(req, timeout=5) as response:
                        response.readline()
                    deadline = monotonic() + 5
                    while monotonic() < deadline:
                        task_dirs = sorted((root / "personal").iterdir())
                        if task_dirs:
                            task = manager.task_view(task_dirs[0].name)
                            if task["status"] == "cancelled":
                                break
                        sleep(0.05)
                    else:
                        self.fail("Timed out waiting for stream disconnect cancellation.")
            finally:
                server.shutdown()
                server.server_close()
                if previous is None:
                    os.environ.pop("OWUI_SWARM_WIKI_ROOT", None)
                else:
                    os.environ["OWUI_SWARM_WIKI_ROOT"] = previous


if __name__ == "__main__":
    unittest.main()
