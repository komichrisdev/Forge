from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import os
import socket
import urllib.error
import unittest

from swarm_router.cli import main
from swarm_router.discord_notifications import (
    NotificationStore,
    deliver,
    load_config as load_discord_config,
    notify_night_owl,
    notification_from_store,
    safe_content,
    validate_webhook,
)
from swarm_router.night_owl import NightOwlResult


class Response(BytesIO):
    def __init__(self, status: int, payload: bytes = b"") -> None:
        super().__init__(payload)
        self.status = status

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def write_config(root: Path) -> Path:
    path = root / "config.toml"
    path.write_text(
        f'''[openwebui]
base_url = "http://127.0.0.1:9"
api_key_env = "OPEN_WEBUI_API_KEY"

[swarm]
run_directory = "{root / 'runs'}"
catalog_path = "{root / 'catalog.db'}"

[personal]
task_directory = "{root / 'personal'}"

[scheduler]
timezone = "UTC"

[dashboard]
metadata_directory = "{root / 'dashboard'}"

[authority]
supervisor_name = "Codex"

[judge]
model = "fake/judge"

[[workers]]
name = "planner"
model = "fake/planner"
modes = ["auto"]
''',
        encoding="utf-8",
    )
    return path


def discord_env(root: Path, *, mode: int = 0o600, url: str = "https://discord.com/api/webhooks/1234567890123456789/token_token_token") -> Path:
    path = root / "discord.env"
    path.write_text(f"FORGE_DISCORD_WEBHOOK_URL={url}\n", encoding="utf-8")
    path.chmod(mode)
    return path


class DiscordNotificationTest(unittest.TestCase):
    def test_config_validation_webhook_host_permissions_redaction_and_content_limits(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = discord_env(root, mode=0o644)
            loaded = load_discord_config(cfg)
            self.assertFalse(loaded.valid)
            self.assertIn("mode must be 0600", "; ".join(loaded.issues))
            self.assertIn("webhook_url", loaded.public())
            self.assertEqual(loaded.public()["webhook_url"], "<redacted>")
            self.assertIn("webhook host", validate_webhook("https://example.com/api/webhooks/id/token")[0])
            content = safe_content("Title @everyone", "x" * 3000 + " @here", "warning")
            self.assertLessEqual(len(content), 1900)
            self.assertNotIn("@everyone", content)
            self.assertNotIn("@here", content)

    def test_delivery_success_persistence_restart_and_duplicate_suppression(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = discord_env(root)
            store = NotificationStore(root / "catalog.db", clock=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc))
            calls = []

            def ok(req: object, timeout: float = 10) -> Response:
                calls.append(req)
                return Response(200, b'{"id":"discord-message-1"}')

            item = notification_from_store(
                store,
                event_type="test",
                severity="info",
                title="Test",
                message="Hello",
                deduplication_key="same",
            )
            first = deliver(store, item, config_path=cfg, open_url=ok)
            second = deliver(store, notification_from_store(store, event_type="test", severity="info", title="Test", message="Hello", deduplication_key="same"), config_path=cfg, open_url=ok)
            restarted = NotificationStore(root / "catalog.db").get(first["notification_id"])
            self.assertEqual(first["status"], "confirmed")
            self.assertEqual(first["external_message_id"], "discord-message-1")
            self.assertTrue(second["duplicate_suppressed"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(restarted["status"], "confirmed")

    def test_http_failures_rate_limit_and_unknown_are_classified(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = discord_env(root)
            store = NotificationStore(root / "catalog.db")

            def item(key: str):
                return notification_from_store(store, event_type="test", severity="error", title="T", message="M", deduplication_key=key)

            def http(code: int, body: bytes = b"{}"):
                def opener(_req: object, timeout: float = 10) -> Response:
                    raise urllib.error.HTTPError("https://discord.com", code, "err", {}, BytesIO(body))
                return opener

            self.assertEqual(deliver(store, item("403"), config_path=cfg, open_url=http(403))["http_classification"], "permission_denied")
            self.assertEqual(deliver(store, item("404"), config_path=cfg, open_url=http(404))["http_classification"], "invalid_webhook")
            self.assertEqual(deliver(store, item("500"), config_path=cfg, open_url=http(500))["status"], "unknown")

            calls = []

            def limited(_req: object, timeout: float = 10) -> Response:
                calls.append(1)
                if len(calls) == 1:
                    raise urllib.error.HTTPError("https://discord.com", 429, "rate", {}, BytesIO(b'{"retry_after":0.001}'))
                return Response(204)

            self.assertEqual(deliver(store, item("429"), config_path=cfg, open_url=limited, sleep=lambda _s: None)["status"], "confirmed")
            self.assertEqual(len(calls), 2)

            timeout = deliver(store, item("timeout"), config_path=cfg, open_url=lambda _r, timeout=10: (_ for _ in ()).throw(socket.timeout("late")))
            self.assertEqual(timeout["status"], "unknown")
            self.assertTrue(deliver(store, item("timeout"), config_path=cfg, open_url=lambda _r, timeout=10: Response(204))["duplicate_suppressed"])

    def test_cli_status_test_list_show_json(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = write_config(root)
            discord_path = discord_env(root)
            os.environ["OPEN_WEBUI_API_KEY"] = "test"
            os.environ["SWARM_PERSONAL_API_KEY"] = "test"

            def ok(_req: object, timeout: float = 10) -> Response:
                return Response(200, b'{"id":"discord-cli"}')

            with patch("swarm_router.discord_notifications.CONFIG_FILE", discord_path), patch("swarm_router.discord_notifications.request.urlopen", ok):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["--config", str(config_path), "discord", "test", "--deduplication-key", "cli-test", "--json"]), 0)
                notification_id = json.loads(output.getvalue())["notification_id"]
                for command in (
                    ["--config", str(config_path), "discord", "status", "--json"],
                    ["--config", str(config_path), "notification", "list", "--json"],
                    ["--config", str(config_path), "notification", "show", notification_id, "--json"],
                ):
                    output = StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(main(command), 0)
                    self.assertIsNotNone(json.loads(output.getvalue()))

    def test_night_owl_notification_policies(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = discord_env(root)
            state = root / "night-owl"
            state.mkdir()
            db = root / "catalog.db"
            task = {"metadata": {"schedule_id": "FS-20260728-000001", "occurrence_id": "FO-test"}}
            result = NightOwlResult(
                status="completed",
                command=[],
                returncode=0,
                duration_ms=1,
                stdout="empty",
                stderr="",
                timed_out=False,
                side_effect_state="confirmed",
                checkpoint_reference="night-owl/test",
                metadata={"state_dir": str(state)},
            )
            with patch("swarm_router.discord_notifications.CONFIG_FILE", cfg), patch("swarm_router.discord_notifications.request.urlopen", lambda _r, timeout=10: Response(200, b'{"id":"night-owl-message"}')):
                self.assertIsNone(notify_night_owl(db, result=result, task=task, task_id="task-1", forge_task_id="FT-20260728-000001", agent_id="night_owl"))
                (state / "report.md").write_text("Work complete", encoding="utf-8")
                sent = notify_night_owl(db, result=result, task=task, task_id="task-1", forge_task_id="FT-20260728-000001", agent_id="night_owl")
            self.assertEqual(sent["status"], "confirmed")
            self.assertFalse((state / "report.md").exists())
            self.assertTrue(list((state / "sent").glob("*.md")))

            (state / "report.md").write_text("Work queued", encoding="utf-8")
            with patch("swarm_router.discord_notifications.CONFIG_FILE", cfg), patch("swarm_router.discord_notifications.request.urlopen", lambda _r, timeout=10: (_ for _ in ()).throw(urllib.error.HTTPError("https://discord.com", 403, "no", {}, BytesIO(b"forbidden")))):
                failed = notify_night_owl(db, result=result, task=task, task_id="task-2", forge_task_id="FT-20260728-000002", agent_id="night_owl")
            self.assertEqual(failed["http_classification"], "permission_denied")
            self.assertTrue((state / "report.md").exists())

            failure = NightOwlResult(
                status="failed",
                command=[],
                returncode=1,
                duration_ms=1,
                stdout="",
                stderr="boom",
                timed_out=False,
                side_effect_state="unknown",
                checkpoint_reference="night-owl/test",
                metadata={"state_dir": str(state / "missing")},
            )
            with patch("swarm_router.discord_notifications.CONFIG_FILE", cfg), patch("swarm_router.discord_notifications.request.urlopen", lambda _r, timeout=10: Response(204)):
                row = notify_night_owl(db, result=failure, task=task, task_id="task-3", forge_task_id="FT-20260728-000003", agent_id="night_owl")
            self.assertEqual(row["event_type"], "night_owl.failure")


if __name__ == "__main__":
    unittest.main()
