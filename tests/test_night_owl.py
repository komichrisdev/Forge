from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import Mock, patch
import json
import os
import unittest

from swarm_router.config import AppConfig, load_config
from swarm_router.journal import TaskJournal
from swarm_router.night_owl import NightOwlResult, run_night_owl, validate_night_owl_payload
from swarm_router.personal import PersonalTaskManager
from swarm_router.scheduler import Scheduler, ScheduleStore


class FakeDashboard:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def sync_models(self) -> list[dict[str, object]]:
        return []

    def list_models(self) -> list[dict[str, object]]:
        return []

    def list_runs(self) -> list[dict[str, object]]:
        return []


def write_config(root: Path) -> AppConfig:
    os.environ["OPEN_WEBUI_API_KEY"] = "test-openwebui-key"
    os.environ["SWARM_PERSONAL_API_KEY"] = "test-personal-key"
    config_path = root / "config.toml"
    config_path.write_text(
        f'''[openwebui]
base_url = "http://127.0.0.1:9"
api_key_env = "OPEN_WEBUI_API_KEY"

[swarm]
run_directory = "{root / 'runs'}"
catalog_path = "{root / 'catalog.db'}"

[personal]
task_directory = "{root / 'personal'}"
task_timeout_seconds = 5
worker_timeout_seconds = 1
max_retries = 1

[scheduler]
poll_interval_seconds = 1
lease_seconds = 30
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
    return load_config(config_path)


def fake_script(root: Path, body: str) -> Path:
    scripts = root / "skill" / "scripts"
    scripts.mkdir(parents=True)
    path = scripts / "run_nightly.sh"
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


class NightOwlTest(unittest.TestCase):
    def test_payload_validation_rejects_unknown_fields_and_unsafe_paths(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            script = fake_script(root, "echo ready\n")
            good = {
                "operation": "run_nightly",
                "mode": "dry_run",
                "dry_run": True,
                "script_path": str(script),
                "state_dir": str(root / "state"),
            }
            self.assertEqual(validate_night_owl_payload(
                good,
                allowed_script_roots=(script.parent,),
                allowed_state_roots=(root,),
            ), [])
            self.assertIn("unknown Night Owl payload fields: command", validate_night_owl_payload({**good, "command": "rm"}))
            self.assertIn("script_path must be an approved Night Owl run_nightly.sh", validate_night_owl_payload({**good, "script_path": "/bin/sh"}))
            self.assertIn("state_dir must be under the approved Night Owl state directory", validate_night_owl_payload(
                {**good, "state_dir": "/tmp/not-night-owl"},
                allowed_script_roots=(script.parent,),
                allowed_state_roots=(root / "state",),
            ))

    def test_dry_run_success_captures_output_and_redacts_secrets(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            script = fake_script(root, "echo 'ready https://discord.com/api/webhooks/123/abcdef'; echo err >&2\n")
            result = run_night_owl(
                {"script_path": str(script), "state_dir": str(root / "state"), "dry_run": True},
                allowed_script_roots=(script.parent,),
                allowed_state_roots=(root,),
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.side_effect_state, "none")
            self.assertIn("<redacted>", result.stdout)
            self.assertIn("err", result.stderr)
            self.assertEqual(result.command, [str(script), "--dry-run"])

    def test_success_failure_timeout_and_output_limit(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            success = fake_script(root / "success", "python3 - <<'PY'\nprint('x' * 7000)\nPY\n")
            failed = fake_script(root / "failed", "echo bad >&2\nexit 7\n")
            timeout = fake_script(root / "timeout", "sleep 10\n")
            ok = run_night_owl({"script_path": str(success), "state_dir": str(root / "state1")}, allowed_script_roots=(success.parent,), allowed_state_roots=(root,))
            self.assertEqual(ok.status, "completed")
            self.assertLessEqual(len(ok.stdout), 6000)
            bad = run_night_owl({"script_path": str(failed), "state_dir": str(root / "state2")}, allowed_script_roots=(failed.parent,), allowed_state_roots=(root,))
            self.assertEqual(bad.status, "failed")
            self.assertEqual(bad.returncode, 7)
            slow = run_night_owl(
                {"script_path": str(timeout), "state_dir": str(root / "state3"), "timeout_seconds": 1},
                allowed_script_roots=(timeout.parent,),
                allowed_state_roots=(root,),
                grace_seconds=0.1,
            )
            self.assertEqual(slow.status, "timeout")
            self.assertTrue(slow.timed_out)

    def test_personal_task_journal_and_schedule_metadata_integration(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = write_config(root)
            store = ScheduleStore(config.swarm.catalog_path, clock=lambda: datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc))
            schedule = store.create({
                "name": "Night Owl",
                "task_type": "night_owl",
                "agent_id": "night_owl",
                "trigger_type": "one_time",
                "trigger_configuration": {"run_at": "2026-07-28T12:00:00Z"},
                "payload": {"operation": "run_nightly", "mode": "dry_run", "dry_run": True},
                "timezone": "America/New_York",
            })
            result = NightOwlResult(
                status="completed",
                command=["run_nightly.sh", "--dry-run"],
                returncode=0,
                duration_ms=1,
                stdout="Night Owl ready",
                stderr="",
                timed_out=False,
                side_effect_state="none",
                checkpoint_reference="night-owl/test",
            )
            runner = Mock(return_value=result)
            fixed_now = lambda: datetime(2026, 7, 28, 12, 5, tzinfo=timezone.utc)
            with patch("swarm_router.personal.DashboardApp", FakeDashboard), patch("swarm_router.personal.run_night_owl", runner):
                manager = PersonalTaskManager(config)
                scheduler = Scheduler(config, store=store, submit_task=lambda _s, _o: manager.create_task(scheduler._task_body(_s, _o)), clock=fixed_now)
                occurrence = scheduler.run_once(schedule.schedule_id)
                task = self._wait_task(manager, occurrence["task_id"])
                scheduler.run_once(schedule.schedule_id)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["selected_workers"], ["night_owl"])
            self.assertEqual(task["metadata"]["schedule_id"], schedule.schedule_id)
            events = TaskJournal(config.swarm.catalog_path).events(str(task["forge_task_id"]))
            self.assertIn("TASK_ASSIGNED", [event.event_type for event in events])
            created = next(event for event in events if event.event_type == "TASK_CREATED")
            self.assertEqual(created.metadata["occurrence_id"], occurrence["occurrence_id"])
            journal = TaskJournal(config.swarm.catalog_path)
            self.assertEqual(journal.checkpoints(str(task["forge_task_id"]))[0].checkpoint_reference, "night-owl/test")
            self.assertEqual(journal.recovery_status(str(task["forge_task_id"]))["replay_safety"], "safe")

    def test_uncertain_live_failure_is_not_replay_safe(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = replace(write_config(root), personal=replace(write_config(root).personal, max_retries=1))
            manager = PersonalTaskManager(config)
            result = NightOwlResult(
                status="failed",
                command=["run_nightly.sh"],
                returncode=2,
                duration_ms=1,
                stdout="",
                stderr="failed",
                timed_out=False,
                side_effect_state="unknown",
                checkpoint_reference="night-owl/test",
            )
            with patch("swarm_router.personal.run_night_owl", return_value=result):
                task = manager.create_task({
                    "model": config.personal.model_id,
                    "messages": [{"role": "user", "content": "Forge Night Owl automation occurrence."}],
                    "task_type": "night_owl",
                    "agent_id": "night_owl",
                    "task_payload": {"operation": "run_nightly", "mode": "live", "dry_run": False},
                    "metadata": {"schedule_id": "FS-20260728-000001", "occurrence_id": "FO-test"},
                })
                task = self._wait_task(manager, str(task["task_id"]))
            self.assertEqual(task["status"], "failed")
            recovery = TaskJournal(config.swarm.catalog_path).recovery_status(str(task["forge_task_id"]))
            self.assertEqual(recovery["replay_safety"], "unsafe")
            self.assertFalse(recovery["recovery_allowed"])

    def _wait_task(self, manager: PersonalTaskManager | None, task_id: str) -> dict[str, object]:
        self.assertIsNotNone(manager)
        deadline = monotonic() + 5
        while monotonic() < deadline:
            task = manager.task_view(task_id)  # type: ignore[union-attr]
            if task["status"] in {"completed", "failed", "cancelled"}:
                return task
            sleep(0.05)
        self.fail(f"Timed out waiting for {task_id}")


if __name__ == "__main__":
    unittest.main()
