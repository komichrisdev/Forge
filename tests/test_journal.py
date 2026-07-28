from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest

from swarm_router.catalog import ModelCatalog
from swarm_router.cli import main
from swarm_router.journal import (
    CheckpointRecord,
    JournalEventType,
    SideEffectState,
    TaskJournal,
    validate_task_id,
)


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

[swarm]
run_directory = "{root / 'runs'}"
catalog_path = "{root / 'catalog.db'}"

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


class TaskJournalTest(unittest.TestCase):
    def test_task_id_generation_validation_and_restart_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            clock = Clock()
            path = Path(temp) / "catalog.db"
            journal = TaskJournal(path, clock=clock)
            first = journal.next_task_id()
            second = journal.next_task_id()
            self.assertEqual(first, "FT-20260728-000001")
            self.assertEqual(second, "FT-20260728-000002")
            self.assertTrue(validate_task_id(first))
            self.assertFalse(validate_task_id("task-abc"))

            restarted = TaskJournal(path, clock=clock)
            self.assertEqual(restarted.next_task_id(), "FT-20260728-000003")

    def test_append_only_ordering_duplicate_protection_and_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            journal = TaskJournal(Path(temp) / "catalog.db", clock=Clock())
            task_id = journal.next_task_id()
            created = journal.append_event(
                task_id,
                JournalEventType.TASK_CREATED,
                message="created",
                transition_key="create",
            )
            duplicate = journal.append_event(
                task_id,
                JournalEventType.TASK_CREATED,
                message="created again",
                transition_key="create",
            )
            journal.append_event(task_id, JournalEventType.TASK_STARTED, agent_id="manager")
            journal.append_event(task_id, JournalEventType.TASK_COMPLETED, agent_id="manager")

            self.assertEqual(created.event_id, duplicate.event_id)
            events = journal.events(task_id)
            self.assertEqual([event.event_type for event in events], [
                "TASK_CREATED", "TASK_STARTED", "TASK_COMPLETED",
            ])
            self.assertEqual([event.sequence for event in events], sorted(event.sequence for event in events))
            self.assertEqual(journal.reconstruct(task_id)["status"], "completed")
            self.assertEqual(journal.recovery_status(task_id)["recovery_allowed"], False)

    def test_malformed_events_unknown_agents_and_checkpoint_validation_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            journal = TaskJournal(Path(temp) / "catalog.db", clock=Clock())
            task_id = journal.next_task_id()
            with self.assertRaisesRegex(ValueError, "event_type is invalid"):
                journal.append_event(task_id, "NOPE")
            with self.assertRaisesRegex(ValueError, "agent_id is not registered"):
                journal.append_event(task_id, JournalEventType.TASK_ASSIGNED, agent_id="ghost")
            with self.assertRaisesRegex(ValueError, "checkpoint_reference is invalid"):
                journal.add_checkpoint(CheckpointRecord(
                    task_id=task_id,
                    stage="draft",
                    agent_id="planner",
                    timestamp=journal.now_iso(),
                    checkpoint_reference="../secret",
                    summary="bad",
                ))
            checkpoint = CheckpointRecord(
                task_id=task_id,
                stage="draft",
                agent_id="planner",
                timestamp=journal.now_iso(),
                checkpoint_reference="runs/task/checkpoint.json",
                summary="draft saved",
                metadata={"bytes": 10},
            )
            journal.add_checkpoint(checkpoint)
            self.assertEqual(journal.checkpoints(task_id)[0].to_dict(), checkpoint.to_dict())

    def test_leases_heartbeats_renewal_and_orphan_detection_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            clock = Clock()
            journal = TaskJournal(Path(temp) / "catalog.db", clock=clock)
            task_id = journal.next_task_id()
            journal.append_event(task_id, JournalEventType.TASK_CREATED)
            journal.append_event(task_id, JournalEventType.TASK_STARTED, agent_id="manager")
            journal.grant_lease(task_id, "manager", 10)
            journal.record_heartbeat(task_id, "manager")
            clock.advance(11)
            self.assertEqual(journal.orphan_candidates()[0]["orphan_status"], "suspected_orphan")
            self.assertEqual(journal.reconstruct(task_id)["status"], "running")

            journal.renew_lease(task_id, "manager", 30)
            self.assertEqual(journal.orphan_candidates(), [])
            clock.advance(31)
            self.assertEqual(journal.orphan_candidates()[0]["orphan_status"], "suspected_orphan")
            journal.append_event(task_id, JournalEventType.TASK_ORPHANED, agent_id="manager")
            self.assertEqual(journal.orphan_candidates()[0]["orphan_status"], "confirmed_orphan")

    def test_side_effect_replay_safety(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            journal = TaskJournal(Path(temp) / "catalog.db", clock=Clock())
            safe = journal.next_task_id()
            started = journal.next_task_id()
            confirmed = journal.next_task_id()
            unknown = journal.next_task_id()
            for task_id, state in (
                (safe, SideEffectState.PROPOSED),
                (started, SideEffectState.STARTED),
                (confirmed, SideEffectState.CONFIRMED),
                (unknown, SideEffectState.UNKNOWN),
            ):
                journal.append_event(task_id, JournalEventType.TASK_CREATED)
                journal.append_event(task_id, JournalEventType.TASK_FAILED, side_effect_state=state)

            self.assertEqual(journal.recovery_status(safe)["replay_safety"], "safe")
            self.assertEqual(journal.recovery_status(started)["replay_safety"], "unsafe")
            self.assertEqual(journal.recovery_status(confirmed)["replay_safety"], "unsafe")
            self.assertEqual(journal.recovery_status(unknown)["replay_safety"], "requires_review")

    def test_provider_inventory_failure_does_not_affect_journal_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "catalog.db"
            catalog = ModelCatalog(path)
            catalog.record_inventory_failure("nvidia", "temporary outage")
            journal = TaskJournal(path, clock=Clock())
            task_id = journal.next_task_id()
            journal.append_event(task_id, JournalEventType.TASK_CREATED)
            self.assertEqual(journal.reconstruct(task_id)["status"], "created")

    def test_cli_list_show_events_orphans_and_recovery_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = write_config(root)
            clock = Clock()
            journal = TaskJournal(root / "catalog.db", clock=clock)
            task_id = journal.next_task_id()
            journal.append_event(task_id, JournalEventType.TASK_CREATED)
            journal.append_event(task_id, JournalEventType.TASK_STARTED, agent_id="manager")
            journal.grant_lease(task_id, "manager", 1)
            clock.advance(2)

            for args in (
                ["--config", str(config), "journal", "list", "--json"],
                ["--config", str(config), "journal", "show", task_id, "--json"],
                ["--config", str(config), "journal", "events", task_id, "--json"],
                ["--config", str(config), "journal", "checkpoints", task_id, "--json"],
                ["--config", str(config), "journal", "orphans", "--json"],
                ["--config", str(config), "journal", "recovery-status", task_id, "--json"],
            ):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(args), 0)
                self.assertIsNotNone(json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
