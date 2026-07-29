from __future__ import annotations

from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from unittest.mock import patch
from urllib import error, request
import json
import os
import tempfile
import unittest

from swarm_router.catalog import ModelCatalog
from swarm_router.config import load_config
from swarm_router.dashboard import DashboardApp, Handler
from swarm_router.discord_notifications import NotificationStore, notification_from_store
from swarm_router.journal import JournalEventType
from swarm_router.night_owl import forge_script_root
from swarm_router.providers import ProviderModel, provider_items
from swarm_router.scheduler import ScheduleStore


def write_config(root: Path) -> Path:
    path = root / "config.toml"
    path.write_text(
        f'''[openwebui]
base_url = "http://127.0.0.1:9"
api_key_env = "OPEN_WEBUI_API_KEY"

[swarm]
run_directory = "{root / 'runs'}"
catalog_path = "{root / 'catalog.sqlite3'}"

[dashboard]
host = "127.0.0.1"
port = 8787
metadata_directory = "{root / 'dashboard'}"

[personal]
task_directory = "{root / 'personal-tasks'}"
auth_token_env = "SWARM_PERSONAL_API_KEY"

[scheduler]
timezone = "UTC"

[authority]
supervisor_name = "Codex"

[judge]
model = "judge/model"

[[workers]]
name = "planner"
model = "worker/model"
modes = ["auto"]
''',
        encoding="utf-8",
    )
    return path


class Response:
    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = json.dumps(body or {}).encode("utf-8")

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, *_args: Any) -> bytes:
        return self.body


class DashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"OPEN_WEBUI_API_KEY": "owui-token", "SWARM_DASHBOARD_TOKEN": "owner-secret", "SWARM_PERSONAL_API_KEY": "personal-token"})
        self.env.start()
        self.config = load_config(write_config(self.root), require_api_key=False)
        self.app = DashboardApp(self.config)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = self.app  # type: ignore[attr-defined]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.tmp.cleanup()

    def call(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        cookie: str = "",
        csrf: str = "",
        host: str | None = None,
    ) -> tuple[int, dict[str, Any], str]:
        data = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
        headers = {"Host": host or f"127.0.0.1:{self.server.server_port}", "Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        req = request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=3) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}, response.headers.get("Set-Cookie", "")
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, json.loads(raw) if raw else {}, exc.headers.get("Set-Cookie", "")

    def login(self) -> tuple[str, str]:
        status, data, cookie = self.call("/api/login", method="POST", body={"secret": "owner-secret"})
        self.assertEqual(status, 200)
        return cookie.split(";", 1)[0], data["csrf_token"]

    def seed_state(self) -> str:
        store = ScheduleStore(self.config.swarm.catalog_path)
        store.create({
            "schedule_id": "FS-20260728-000001",
            "name": "Night Owl",
            "description": "test",
            "task_type": "night_owl",
            "agent_id": "night_owl",
            "enabled": True,
            "trigger_type": "cron",
            "trigger_configuration": {"expression": "0 */4 * * *"},
            "timezone": "UTC",
            "payload": {
                "operation": "run_nightly",
                "mode": "dry_run",
                "dry_run": True,
                "script_path": str(forge_script_root() / "run_nightly.sh"),
                "timeout_seconds": 300,
                "run_hours": 4,
            },
            "misfire_policy": "skip",
            "overlap_policy": "skip",
        })
        forge_task_id = self.app.journal.next_task_id()
        self.app.journal.append_event(
            forge_task_id,
            JournalEventType.TASK_CREATED,
            metadata={"personal_task_id": "task-one", "task_type": "night_owl", "schedule_id": "FS-20260728-000001"},
        )
        self.app.journal.append_event(forge_task_id, JournalEventType.TASK_COMPLETED, agent_id="manager")
        task_dir = self.root / "personal-tasks" / "task-one"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(
            json.dumps({"task_id": "task-one", "forge_task_id": forge_task_id, "status": "completed", "task_type": "night_owl", "agent_id": "night_owl"}),
            encoding="utf-8",
        )
        notifications = NotificationStore(self.config.swarm.catalog_path)
        item = notification_from_store(
            notifications,
            event_type="night_owl.report",
            severity="warning",
            title="Needs review",
            message="Delivery state unknown",
            task_id="task-one",
            forge_task_id=forge_task_id,
            schedule_id="FS-20260728-000001",
            occurrence_id="FO-test",
            agent_id="night_owl",
            metadata={},
            deduplication_key="unknown-delivery",
        )
        row, _inserted = notifications.propose(item)
        notifications.finish(row["notification_id"], status="unknown", side_effect_state="unknown", http_classification="ambiguous_transport_error")
        ModelCatalog(self.config.swarm.catalog_path).reconcile_inventory(
            "nvidia",
            provider_items([ProviderModel("nvidia", "nvidia/model", "Model", {"capabilities": ["reasoning.fast"], "supports_streaming": True})]),
            mode="live",
        )
        return forge_task_id

    def test_auth_session_csrf_host_headers_logout_and_request_limit(self) -> None:
        self.assertEqual(self.call("/api/overview")[0], 401)
        self.assertEqual(self.call("/api/login", method="POST", body={"secret": "wrong"})[0], 401)
        cookie, csrf = self.login()
        status, data, _ = self.call("/api/session", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data["authenticated"])
        self.assertEqual(self.call("/api/overview", cookie=cookie, host="evil.example")[0], 421)
        self.assertEqual(self.call("/api/schedules/FS-20260728-000001/disable", method="POST", cookie=cookie, body={})[0], 403)
        too_large = request.Request(
            self.base + "/api/dispatch",
            data=b'{"x":"' + (b"a" * 70000) + b'"}',
            method="POST",
            headers={"Host": f"127.0.0.1:{self.server.server_port}", "Cookie": cookie, "X-CSRF-Token": csrf, "Content-Type": "application/json"},
        )
        with self.assertRaises(error.HTTPError) as raised:
            request.urlopen(too_large, timeout=3)
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.call("/api/logout", method="POST", cookie=cookie, csrf=csrf)[0], 204)

    def test_overview_tasks_schedules_notifications_agents_and_providers(self) -> None:
        forge_task_id = self.seed_state()
        cookie, _csrf = self.login()
        with patch("swarm_router.dashboard.subprocess.run", return_value=type("R", (), {"stdout": "active\n", "stderr": "", "returncode": 0})()):
            status, overview, _ = self.call("/api/overview", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(overview["forge_version"], "0.12-dev")
            self.assertEqual(overview["night_owl"]["schedule_id"], "FS-20260728-000001")
        for path in ("/api/tasks", f"/api/tasks/{forge_task_id}", "/api/schedules", "/api/night-owl", "/api/notifications", "/api/agents", "/api/providers"):
            status, data, _ = self.call(path, cookie=cookie)
            self.assertEqual(status, 200, path)
            self.assertIsInstance(data, dict)
        status, data, _ = self.call("/api/images", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["preset"]["preset_id"], "flux-schnell-768-daily")
        self.assertIn("viewport", request.urlopen(self.base + "/", timeout=3).read().decode("utf-8"))

    def test_schedule_actions_and_night_owl_dispatch_controls(self) -> None:
        self.seed_state()
        cookie, csrf = self.login()
        submitted: list[dict[str, Any]] = []

        def fake_submit(body: dict[str, Any]) -> dict[str, Any]:
            self.assertNotIn("command", body.get("task_payload", {}))
            submitted.append(body)
            task_id = f"task-{len(submitted)}"
            forge_task_id = self.app.journal.next_task_id()
            self.app.journal.append_event(forge_task_id, JournalEventType.TASK_CREATED, metadata={"personal_task_id": task_id, "task_type": "night_owl"})
            return {"task_id": task_id, "forge_task_id": forge_task_id, "status": "queued", "task_payload": body.get("task_payload", {})}

        self.app._submit_personal_task = fake_submit  # type: ignore[method-assign]
        self.assertEqual(self.call("/api/schedules/FS-20260728-000001/disable", method="POST", cookie=cookie, csrf=csrf, body={"confirm": "disable FS-20260728-000001"})[0], 200)
        self.assertEqual(self.call("/api/schedules/FS-20260728-000001/enable", method="POST", cookie=cookie, csrf=csrf, body={"confirm": "enable FS-20260728-000001"})[0], 200)
        status, run_now, _ = self.call("/api/schedules/FS-20260728-000001/run-now", method="POST", cookie=cookie, csrf=csrf, body={"confirm": "run now FS-20260728-000001"})
        self.assertEqual(status, 202)
        self.assertEqual(run_now["status"], "created")
        status, dry_run, _ = self.call("/api/night-owl/dry-run", method="POST", cookie=cookie, csrf=csrf, body={"confirm": "run night owl dry-run"})
        self.assertEqual(status, 202)
        self.assertTrue(dry_run["task_payload"]["dry_run"])
        status, image_task, _ = self.call(
            "/api/images/generate",
            method="POST",
            cookie=cookie,
            csrf=csrf,
            body={
                "preset_id": "flux-schnell-768-daily",
                "prompt": "daily validation image",
                "seed": "11",
                "notification_requested": False,
                "confirm": "generate image",
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(image_task["task_payload"]["seed"], 11)
        self.assertEqual(self.call("/api/images/generate", method="POST", cookie=cookie, body={})[0], 403)
        self.assertEqual(self.call("/api/night-owl/live", method="POST", cookie=cookie, csrf=csrf, body={"confirm": "run night owl live"})[0], 400)
        self.assertEqual(self.call("/api/dispatch", method="POST", cookie=cookie, csrf=csrf, body={"task_type": "night_owl", "mode": "dry_run", "confirm": "run night owl dry-run", "command": "bad"})[0], 400)


if __name__ == "__main__":
    unittest.main()
