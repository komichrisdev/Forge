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
import shutil
import subprocess
import tempfile
import unittest

from swarm_router.catalog import ModelCatalog
from swarm_router.client import ChatResult, OpenWebUIClient, RequestFailure
from swarm_router.config import load_config
from swarm_router.dashboard import FORGE_HTML, DashboardApp, Handler, _toronto_time
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
        for path in ("/api/tasks", f"/api/tasks/{forge_task_id}", "/api/developer-runs", "/api/schedules", "/api/night-owl", "/api/notifications", "/api/agents", "/api/providers"):
            status, data, _ = self.call(path, cookie=cookie)
            self.assertEqual(status, 200, path)
            self.assertIsInstance(data, dict)
        status, data, _ = self.call("/api/images", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["preset"]["preset_id"], "flux-schnell-768-daily")
        self.assertEqual(data["presets"], [data["preset"]])
        self.assertIn("viewport", request.urlopen(self.base + "/", timeout=3).read().decode("utf-8"))
        self.assertIn('id="imagePreset"', FORGE_HTML)
        self.assertNotIn('id="imageConfirm"', FORGE_HTML)
        self.assertIn("confirm:'generate image'", FORGE_HTML)
        self.assertIn("/api/tasks/", FORGE_HTML)
        self.assertIn("Developer Runs", FORGE_HTML)

    def test_probe_forwards_catalog_context(self) -> None:
        self.app.catalog.sync([{"id": "worker/model", "context_length": 28672}])
        with patch.object(
            self.app.client,
            "chat",
            return_value=ChatResult("worker/model", "HEALTHY", {}),
        ) as chat:
            self.app.probe_models(["worker/model"])
        self.assertEqual(chat.call_args.kwargs["catalog_context"], 28672)

        with patch.object(
            self.app.client,
            "chat",
            side_effect=RequestFailure("prompt exceeds context", "context_overflow", 413),
        ):
            self.app.probe_models(["worker/model"])
        self.assertEqual(self.app.catalog.get("worker/model").probe_status, "healthy")  # type: ignore[union-attr]
        self.assertEqual(self.app.catalog.probe_history(1)[0]["status"], "context_overflow")

    def test_dispatch_run_uses_catalog_context(self) -> None:
        contexts: list[int | None] = []
        self.app.catalog.sync([
            {"id": "worker/model", "context_length": 24576},
            {"id": "judge/model", "context_length": 32768},
        ])

        def chat(_client, model, system, *args, **kwargs):
            contexts.append(kwargs.get("catalog_context"))
            content = (
                json.dumps({
                    "answer": "Integrated proposal",
                    "confidence": 0.8,
                    "agreements": [],
                    "disagreements": [],
                    "verification": [],
                    "selected_candidates": ["planner"],
                    "stale_or_uncertain_claims": [],
                    "confidence_reasons": ["test"],
                })
                if "integration clerk" in system
                else "Candidate"
            )
            return ChatResult(model, content, {})

        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self) -> None:
                self.target()

        with (
            patch.object(OpenWebUIClient, "chat", chat),
            patch("swarm_router.dashboard.Thread", ImmediateThread),
        ):
            self.app.start_run({"objective": "Task", "mode": "auto"})
        self.assertEqual(contexts, [24576, 32768])

    def test_developer_runs_requires_auth_and_redacts_secrets(self) -> None:
        with self.app.developer._connect() as db:
            db.execute(
                """
                INSERT INTO forge_developer_runs(
                    task_id, status, phase, instruction, instruction_digest,
                    role_models, created_at, updated_at
                ) VALUES (?, 'running', 'planner', ?, 'digest', ?, ?, ?)
                """,
                (
                    "FT-20260729-999999",
                    "Inspect sk-1234567890 safely; password is hunter2",
                    json.dumps({
                        "planner": {
                            "provider": "fake",
                            "model": "fake/planner",
                            "health": "healthy",
                        }
                    }),
                    "2026-07-29T12:00:00+00:00",
                    "2026-07-29T12:01:00+00:00",
                ),
            )
        lock = self.app.developer.acquire_writer("FT-20260729-999999")
        self.assertEqual(self.call("/api/developer-runs")[0], 401)
        cookie, _csrf = self.login()
        status, data, _ = self.call("/api/developer-runs", cookie=cookie)
        self.assertEqual(status, 200)
        encoded = json.dumps(data)
        self.assertNotIn("sk-1234567890", encoded)
        self.assertNotIn("hunter2", encoded)
        self.assertNotIn(lock["lease_id"], encoded)
        self.assertNotIn("lease_id", encoded)
        self.assertEqual(data["writer_lock"]["state"], "locked")
        self.assertEqual(data["runs"][0]["created_at"], "2026-07-29 08:00:00 EDT")
        self.assertEqual(data["runs"][0]["roles"]["planner"]["model"], "fake/planner")

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
        status, image_detail, _ = self.call(
            f"/api/tasks/{image_task['forge_task_id']}", cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(image_detail["task"]["status"], "created")
        invalid_image = {
            "preset_id": "flux-schnell-768-daily",
            "prompt": "must remain rejected",
            "confirm": "Generate image",
        }
        self.assertEqual(self.call("/api/images/generate", method="POST", cookie=cookie, csrf=csrf, body=invalid_image)[0], 400)
        self.assertEqual(self.call("/api/images/generate", method="POST", cookie=cookie, body={})[0], 403)
        self.assertEqual(self.call("/api/night-owl/live", method="POST", cookie=cookie, csrf=csrf, body={"confirm": "run night owl live"})[0], 400)
        self.assertEqual(self.call("/api/dispatch", method="POST", cookie=cookie, csrf=csrf, body={"task_type": "night_owl", "mode": "dry_run", "confirm": "run night owl dry-run", "command": "bad"})[0], 400)
        self.assertEqual(self.call("/api/dispatch")[0], 401)
        status, dispatch_config, _ = self.call("/api/dispatch", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(dispatch_config["task_types"][0]["confirmations"]["dry_run"], "run night owl dry-run")
        self.assertEqual(self.call("/api/dispatch", method="POST", cookie=cookie, body={})[0], 403)
        status, dispatched, _ = self.call(
            "/api/dispatch",
            method="POST",
            cookie=cookie,
            csrf=csrf,
            body={"task_type": "night_owl", "mode": "dry_run", "confirm": "run night owl dry-run"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.app.journal.reconstruct(dispatched["forge_task_id"])["status"], "created")

    def test_toronto_time_est_and_edt(self) -> None:
        self.assertEqual(_toronto_time("2026-01-15T12:00:00Z"), "2026-01-15 07:00:00 EST")
        self.assertEqual(_toronto_time("2026-07-15T12:00:00+00:00"), "2026-07-15 08:00:00 EDT")
        self.assertEqual(_toronto_time("invalid"), "invalid")

    def test_agent_fixed_dynamic_and_fallback_routing(self) -> None:
        catalog = ModelCatalog(self.config.swarm.catalog_path)
        catalog.reconcile_inventory(
            "nvidia",
            provider_items([ProviderModel("nvidia", "nvidia/fallback", "Fallback", {"capabilities": ["reasoning"], "supports_streaming": True})]),
            mode="live",
        )
        catalog.record_probe("nvidia/fallback", "healthy", 10, "")
        task_id = self.app.journal.next_task_id()
        self.app.journal.append_event(task_id, JournalEventType.TASK_CREATED)
        self.app.journal.append_event(
            task_id,
            JournalEventType.TASK_ASSIGNED,
            agent_id="planner",
            metadata={"model_id": "nvidia/old", "provider": "NVIDIA"},
            timestamp="2026-01-16T01:00:00+14:00",
        )
        self.app.journal.append_event(
            task_id,
            JournalEventType.TASK_ASSIGNED,
            agent_id="planner",
            metadata={"model_id": "nvidia/dynamic", "provider": "NVIDIA"},
            timestamp="2026-01-15T23:30:00-12:00",
        )
        agents = {row["agent_id"]: row for row in self.app.agents_status()["agents"]}
        self.assertEqual(agents["judge"]["routing"], "fixed")
        self.assertEqual(agents["planner"]["model_id"], "nvidia/dynamic")
        self.assertEqual(agents["planner"]["provider"], "NVIDIA")
        self.assertEqual(agents["planner"]["routing"], "dynamic")
        self.assertEqual(agents["crypto_keeper"]["model_id"], "nvidia/fallback")
        self.assertEqual(agents["crypto_keeper"]["routing"], "fallback")
        with patch.object(self.app.catalog, "recommend", return_value=[]):
            unassigned = {
                row["agent_id"]: row for row in self.app.agents_status()["agents"]
            }
        self.assertEqual(unassigned["crypto_keeper"]["routing"], "unassigned")
        self.assertEqual(unassigned["crypto_keeper"]["model_id"], "")
        self.assertIn("['Model',", FORGE_HTML)
        self.assertIn("['Provider',", FORGE_HTML)
        self.assertIn("['Routing',", FORGE_HTML)

    def test_image_status_uses_approved_preset_source(self) -> None:
        approved = {"preset_id": "registry-approved", "name": "Approved Registry Preset"}
        status = type("Status", (), {"state": "offline", "queue_depth": 0, "detail": "", "system": None})()
        with (
            patch("swarm_router.dashboard.preset_summary", return_value=approved),
            patch("swarm_router.dashboard.ComfyUIClient.status", return_value=status),
            patch("swarm_router.dashboard.image_gallery", return_value=[]),
        ):
            result = self.app.image_status()
        self.assertEqual(result["preset"], approved)
        self.assertEqual(result["presets"], [approved])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard JavaScript validation")
    def test_image_selector_submit_and_poll_javascript(self) -> None:
        submit_start = FORGE_HTML.index("async function imageSubmit()")
        render_start = FORGE_HTML.index("function renderImages", submit_start)
        render_end = FORGE_HTML.index("async function dispatch", render_start)
        functions = FORGE_HTML[submit_start:render_start] + FORGE_HTML[render_start:render_end]
        script = f"""
const assert=require('assert');
const elements={{content:{{innerHTML:''}},imagePreset:{{value:'approved-two'}},
 imagePrompt:{{value:'test prompt'}},imageNegative:{{value:''}},
 imageSeed:{{value:'7'}},imageDiscord:{{checked:false}},imageResult:{{textContent:''}}}};
const $=id=>elements[id];
const esc=value=>String(value??'');
const card=()=>''; const table=()=>''; const cls=()=>''; let loaded=0;
let releasePoll; const pollGate=new Promise(resolve=>releasePoll=resolve);
const calls=[];
async function api(path,opts={{}}){{
 calls.push({{path,body:opts.body?JSON.parse(opts.body):null}});
 if(opts.method==='POST')return {{forge_task_id:'FT-20260729-060000'}};
 await pollGate; return {{task:{{status:'completed'}}}};
}}
async function load(){{loaded++}}
{functions}
(async()=>{{
 renderImages({{connection:{{}},presets:[
  {{preset_id:'approved-one',name:'Approved One'}},
  {{preset_id:'approved-two',name:'Approved Two'}}
 ],tasks:[],gallery:[]}});
 assert(elements.content.innerHTML.includes('value="approved-one"'));
 assert(elements.content.innerHTML.includes('Approved Two'));
 elements.imagePreset.value='approved-two';
 await imageSubmit();
 assert(elements.imageResult.textContent.includes('FT-20260729-060000'));
 assert.deepStrictEqual(calls[0].body.preset_id,'approved-two');
 assert.deepStrictEqual(calls[0].body.confirm,'generate image');
 assert.deepStrictEqual(calls[1].path,'/api/tasks/FT-20260729-060000');
 releasePoll(); await new Promise(resolve=>setImmediate(resolve));
 assert.strictEqual(loaded,1);
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
        result = subprocess.run(
            ["node", "-"], input=script, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disconnect_write_suppresses_only_client_disconnects(self) -> None:
        handler = object.__new__(Handler)

        class Broken:
            def __init__(self, error_type: type[Exception]) -> None:
                self.error_type = error_type

            def write(self, _payload: bytes) -> None:
                raise self.error_type()

        handler.wfile = Broken(BrokenPipeError)  # type: ignore[assignment]
        handler._write(b"test")
        handler.wfile = Broken(ConnectionResetError)  # type: ignore[assignment]
        handler._write(b"test")
        handler.wfile = Broken(ValueError)  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            handler._write(b"test")


if __name__ == "__main__":
    unittest.main()
