from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
from unittest.mock import patch
import json
import os
import unittest

from swarm_router.catalog import ModelCatalog
from swarm_router.cli import main
from swarm_router.config import AppConfig, load_config
from swarm_router.journal import TaskJournal
from swarm_router.personal import PersonalTaskManager
from swarm_router.scheduler import Schedule, Scheduler, ScheduleStore, next_due, validate_schedule, validate_schedule_id


class FakeDashboard:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.catalog = ModelCatalog(config.swarm.catalog_path)

    def sync_models(self) -> list[dict[str, object]]:
        return self.list_models()

    def list_models(self) -> list[dict[str, object]]:
        return [self.catalog.as_dict(record, self.config.reliability) for record in self.catalog.list()]

    def list_runs(self) -> list[dict[str, object]]:
        return []


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def write_config(root: Path) -> Path:
    path = root / "config.toml"
    path.write_text(
        f'''[openwebui]
base_url = "http://127.0.0.1:9"
api_key_env = "OPEN_WEBUI_API_KEY"

[swarm]
run_directory = "{root / 'runs'}"
catalog_path = "{root / 'catalog.db'}"
worker_timeout_seconds = 1
judge_timeout_seconds = 1

[dashboard]
metadata_directory = "{root / 'dashboard'}"

[personal]
task_directory = "{root / 'personal'}"
task_timeout_seconds = 5
worker_timeout_seconds = 1

[scheduler]
poll_interval_seconds = 1
lease_seconds = 30
timezone = "UTC"

[authority]
supervisor_name = "Codex"

[judge]
model = "fake/judge"

[[workers]]
name = "planner"
model = "fake/planner"
modes = ["auto", "general"]
''',
        encoding="utf-8",
    )
    return path


def load_test_config(root: Path) -> AppConfig:
    os.environ["OPEN_WEBUI_API_KEY"] = "test-openwebui-key"
    os.environ["SWARM_PERSONAL_API_KEY"] = "test-personal-key"
    return load_config(write_config(root))


def base_schedule(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": "Night Owl triage",
        "description": "Queue Night Owl work.",
        "task_type": "planning",
        "agent_id": "planner",
        "trigger_type": "interval",
        "trigger_configuration": {"every_minutes": 5},
        "timezone": "UTC",
        "payload": {"prompt": "Prepare a compact plan."},
    }
    data.update(overrides)
    return data


class SchedulerTest(unittest.TestCase):
    def test_schedule_id_generation_validation_and_schema_restart(self) -> None:
        with TemporaryDirectory() as temp:
            clock = Clock()
            store = ScheduleStore(Path(temp) / "catalog.db", clock=clock)
            first = store.next_schedule_id()
            self.assertEqual(first, "FS-20260728-000001")
            self.assertTrue(validate_schedule_id(first))
            self.assertFalse(validate_schedule_id("schedule-1"))
            restarted = ScheduleStore(Path(temp) / "catalog.db", clock=clock)
            self.assertEqual(restarted.next_schedule_id(), "FS-20260728-000002")

    def test_one_time_execution_repeated_ticks_and_restart_do_not_duplicate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            clock = Clock()
            config = load_test_config(root)
            store = ScheduleStore(config.swarm.catalog_path, clock=clock)
            schedule = store.create(base_schedule(
                trigger_type="one_time",
                trigger_configuration={"run_at": "2026-07-28T12:01:00Z"},
            ))
            submitted: list[dict[str, object]] = []

            def submit(_schedule: Schedule, occurrence: dict[str, object]) -> dict[str, object]:
                submitted.append(occurrence)
                return {"task_id": "task-one", "forge_task_id": "FT-20260728-999999"}

            clock.advance(60)
            scheduler = Scheduler(config, store=store, submit_task=submit, clock=clock)
            self.assertEqual(len(scheduler.tick()["processed"]), 1)
            self.assertEqual(scheduler.tick()["processed"], [])
            restarted = Scheduler(config, store=ScheduleStore(config.swarm.catalog_path, clock=clock), submit_task=submit, clock=clock)
            self.assertEqual(restarted.tick()["processed"], [])
            self.assertEqual(len(submitted), 1)
            self.assertFalse(store.get(schedule.schedule_id).enabled)

    def test_restart_after_claim_without_task_does_not_resubmit(self) -> None:
        with TemporaryDirectory() as temp:
            clock = Clock()
            config = load_test_config(Path(temp))
            store = ScheduleStore(config.swarm.catalog_path, clock=clock)
            schedule = store.create(base_schedule(trigger_configuration={"every_minutes": 1}))
            store.claim_occurrence(schedule, "2026-07-28T12:01:00Z", "claimed", {})
            clock.advance(60)
            submitted: list[dict[str, object]] = []
            result = Scheduler(
                config,
                store=ScheduleStore(config.swarm.catalog_path, clock=clock),
                submit_task=lambda _s, item: submitted.append(item) or {"task_id": "task-duplicate"},
                clock=clock,
            ).tick()
            self.assertEqual(submitted, [])
            self.assertEqual(result["processed"][0]["status"], "failed")
            self.assertIn("manual review", result["processed"][0]["metadata"]["error"])

    def test_interval_calculation_and_misfire_run_once_no_backlog(self) -> None:
        with TemporaryDirectory() as temp:
            clock = Clock()
            config = load_test_config(Path(temp))
            store = ScheduleStore(config.swarm.catalog_path, clock=clock)
            schedule = store.create(base_schedule(trigger_configuration={"every_minutes": 1}))
            self.assertEqual(schedule.next_run_at, "2026-07-28T12:01:00Z")
            clock.advance(600)
            scheduler = Scheduler(config, store=store, submit_task=lambda _s, _o: {"task_id": "task-catchup"}, clock=clock)
            self.assertEqual(len(scheduler.tick()["processed"]), 1)
            occurrences = store.occurrences(schedule.schedule_id)
            self.assertEqual(len(occurrences), 1)
            self.assertEqual(occurrences[0]["scheduled_for"], "2026-07-28T12:10:00Z")

    def test_misfire_skip_records_missed_without_task(self) -> None:
        with TemporaryDirectory() as temp:
            clock = Clock()
            config = load_test_config(Path(temp))
            store = ScheduleStore(config.swarm.catalog_path, clock=clock)
            schedule = store.create(base_schedule(trigger_configuration={"every_minutes": 1}, misfire_policy="skip"))
            clock.advance(600)
            result = Scheduler(config, store=store, submit_task=lambda _s, _o: {"task_id": "nope"}, clock=clock).tick()
            self.assertEqual(result["processed"][0]["status"], "missed")
            self.assertEqual(result["processed"][0]["task_id"], "")
            self.assertEqual(len(store.occurrences(schedule.schedule_id)), 1)

    def test_cron_calculation_and_dst_fold_skip(self) -> None:
        created = "2026-10-31T00:00:00Z"
        schedule = Schedule.from_dict(base_schedule(
            schedule_id="FS-20261031-000001",
            trigger_type="cron",
            trigger_configuration={"expression": "30 1 * * *"},
            timezone="America/New_York",
            created_at=created,
        ))
        first = next_due(schedule, datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc))
        second = next_due(schedule, datetime.fromisoformat(first.replace("Z", "+00:00")))
        self.assertEqual(first, "2026-11-01T05:30:00Z")
        self.assertEqual(second, "2026-11-02T06:30:00Z")

    def test_cron_nonexistent_spring_time_skips_deterministically(self) -> None:
        schedule = Schedule.from_dict(base_schedule(
            schedule_id="FS-20260308-000001",
            trigger_type="cron",
            trigger_configuration={"expression": "30 2 * * *"},
            timezone="America/New_York",
            created_at="2026-03-08T00:00:00Z",
        ))
        self.assertEqual(
            next_due(schedule, datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc)),
            "2026-03-09T06:30:00Z",
        )

    def test_disabled_schedules_are_not_processed(self) -> None:
        with TemporaryDirectory() as temp:
            clock = Clock()
            config = load_test_config(Path(temp))
            store = ScheduleStore(config.swarm.catalog_path, clock=clock)
            store.create(base_schedule(enabled=False, trigger_configuration={"every_minutes": 1}))
            clock.advance(60)
            result = Scheduler(config, store=store, submit_task=lambda _s, _o: {"task_id": "task"}, clock=clock).tick()
            self.assertEqual(result["processed"], [])

    def test_overlap_skip_and_wait_policies(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            clock = Clock()
            config = load_test_config(root)
            store = ScheduleStore(config.swarm.catalog_path, clock=clock)
            skip_schedule = store.create(base_schedule(trigger_configuration={"every_minutes": 1}, overlap_policy="skip"))
            wait_schedule = store.create(base_schedule(name="wait", trigger_configuration={"every_minutes": 1}, overlap_policy="wait"))

            def submit(_schedule: Schedule, occurrence: dict[str, object]) -> dict[str, object]:
                task_id = f"task-{occurrence['schedule_id'][-6:]}-{occurrence['occurrence_id'][-8:].lower()}"
                task_dir = root / "personal" / task_id
                task_dir.mkdir(parents=True)
                (task_dir / "task.json").write_text(json.dumps({"status": "queued"}), encoding="utf-8")
                return {"task_id": task_id}

            scheduler = Scheduler(config, store=store, submit_task=submit, clock=clock)
            clock.advance(60)
            scheduler.tick()
            clock.advance(60)
            result = scheduler.tick()["processed"]
            by_schedule = {item["schedule_id"]: item["status"] for item in result}
            self.assertEqual(by_schedule[skip_schedule.schedule_id], "skipped")
            self.assertEqual(by_schedule[wait_schedule.schedule_id], "waiting")
            self.assertEqual(store.get(wait_schedule.schedule_id).next_run_at, "2026-07-28T12:02:00Z")
            self.assertEqual(len(store.occurrences(wait_schedule.schedule_id)), 1)

    def test_scheduler_lease_blocks_second_instance(self) -> None:
        with TemporaryDirectory() as temp:
            clock = Clock()
            store = ScheduleStore(Path(temp) / "catalog.db", clock=clock)
            self.assertTrue(store.acquire_lease("one", lease_seconds=30))
            self.assertFalse(store.acquire_lease("two", lease_seconds=30))
            clock.advance(31)
            self.assertTrue(store.acquire_lease("two", lease_seconds=30))

    def test_run_now_creates_personal_task_and_journal_metadata(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = load_test_config(root)
            catalog = ModelCatalog(config.swarm.catalog_path)
            catalog.sync([{"id": "fake/planner", "provider": "fake"}, {"id": "fake/judge", "provider": "fake"}])
            catalog.record_probe("fake/planner", "healthy", 1)
            catalog.record_probe("fake/judge", "healthy", 1)
            store = ScheduleStore(config.swarm.catalog_path, clock=Clock())
            schedule = store.create(base_schedule())
            with patch("swarm_router.personal.DashboardApp", FakeDashboard), patch(
                "swarm_router.personal.SwarmOrchestrator.run",
                return_value=("done", root, {"answer": "done"}),
            ):
                scheduler = Scheduler(config, store=store)
                occurrence = scheduler.run_once(schedule.schedule_id)
                deadline = monotonic() + 5
                task = {}
                while monotonic() < deadline:
                    task = scheduler._manager.task_view(occurrence["task_id"])  # type: ignore[union-attr]
                    if task["status"] == "completed":
                        break
                    sleep(0.05)
            self.assertEqual(task["metadata"]["schedule_id"], schedule.schedule_id)
            events = TaskJournal(config.swarm.catalog_path).events(str(task["forge_task_id"]))
            created = next(event for event in events if event.event_type == "TASK_CREATED")
            self.assertEqual(created.metadata["schedule_id"], schedule.schedule_id)
            self.assertEqual(created.metadata["occurrence_id"], occurrence["occurrence_id"])

    def test_malformed_unknown_task_and_unknown_agent_are_rejected(self) -> None:
        bad = Schedule.from_dict(base_schedule(agent_id="ghost"))
        self.assertIn("agent_id is not registered", validate_schedule(bad, allow_empty_id=True))
        bad_type = Schedule.from_dict(base_schedule(task_type="teleport"))
        self.assertIn("task_type is not supported by agent_id", validate_schedule(bad_type, allow_empty_id=True))
        bad_payload = Schedule.from_dict(base_schedule(payload={"command": "rm -rf /"}))
        self.assertIn("payload must not contain shell command fields", validate_schedule(bad_payload, allow_empty_id=True))

    def test_cli_commands_json_output_and_empty_tick(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = write_config(root)
            os.environ["OPEN_WEBUI_API_KEY"] = "test-openwebui-key"
            os.environ["SWARM_PERSONAL_API_KEY"] = "test-personal-key"
            schedule_path = root / "schedule.json"
            schedule_path.write_text(json.dumps(base_schedule(trigger_configuration={"every_hours": 1})), encoding="utf-8")

            commands = [
                ["--config", str(config_path), "schedule", "validate", str(schedule_path), "--json"],
                ["--config", str(config_path), "schedule", "create", str(schedule_path), "--json"],
                ["--config", str(config_path), "schedule", "list", "--json"],
                ["--config", str(config_path), "scheduler", "status", "--json"],
                ["--config", str(config_path), "scheduler", "tick", "--json"],
            ]
            schedule_id = ""
            for command in commands:
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(command), 0)
                payload = json.loads(output.getvalue())
                if command[3] == "create":
                    schedule_id = payload["schedule_id"]
            self.assertTrue(schedule_id)
            for command in (
                ["--config", str(config_path), "schedule", "show", schedule_id, "--json"],
                ["--config", str(config_path), "schedule", "disable", schedule_id, "--json"],
                ["--config", str(config_path), "schedule", "enable", schedule_id, "--json"],
                ["--config", str(config_path), "schedule", "occurrences", schedule_id, "--json"],
            ):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(command), 0)
                self.assertIsNotNone(json.loads(output.getvalue()))

    def test_graceful_scheduler_shutdown(self) -> None:
        with TemporaryDirectory() as temp:
            config = load_test_config(Path(temp))
            config = replace(config, scheduler=replace(config.scheduler, poll_interval_seconds=1))
            stop = Event()
            stop.set()
            thread = Thread(target=Scheduler(config).run_forever, args=(stop,), daemon=True)
            thread.start()
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())

    def test_systemd_unit_has_required_scheduler_contract(self) -> None:
        text = Path("systemd/forge-scheduler.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=%h/openwebui-codex-swarm/.venv/bin/owui-swarm --config %h/.config/owui-swarm/config.toml scheduler run", text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("ReadWritePaths=%h/.local/share/owui-swarm", text)


if __name__ == "__main__":
    unittest.main()
