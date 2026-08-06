from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import json
import sqlite3
import subprocess
import unittest

from swarm_router.solo_autopilot import (
    ForgePolicy,
    OpenWebUIGateway,
    PROCESS_TOOL_SCHEMAS,
    Runner,
    SharedWriterLease,
    Store,
    Task,
    WriterBusy,
    WriterLeaseLost,
    evidence_kind,
    final_marker,
    repository_state_digest,
    snapshot,
)


MODEL = "deepseek-ai/deepseek-v4-pro"


def response(content: str | None = None, call: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if call is not None:
        message["tool_calls"] = [call]
    return {"model": MODEL, "choices": [{"message": message}]}


def call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class FakeGateway:
    model_id = MODEL

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.messages: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.messages.append(json.loads(json.dumps(messages)))
        return self.responses.pop(0)


class FakeTerminal:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def execute_tool_call(self, tool_call: dict[str, Any]) -> str:
        self.calls.append(json.loads(json.dumps(tool_call)))
        return json.dumps(self.results.pop(0))


class AllowPolicy:
    def validate(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        return tool_call


class RejectSed:
    def validate(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        command = json.loads(tool_call["function"]["arguments"]).get("command", "")
        if command.startswith("sed "):
            raise RuntimeError("Command sed is not allowed for implementer.")
        return tool_call


class MutatingTerminal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def execute_tool_call(self, tool_call: dict[str, Any]) -> str:
        self.path.write_text("changed\n", encoding="utf-8")
        return json.dumps(
            {"status": "passed", "exit_code": 0, "output": ""}
        )


class FakeClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return response("CONTINUE")


class CapturingCoordinator:
    def __init__(self) -> None:
        self.tool_schemas: dict[str, dict[str, Any]] | None = None
        self.role = ""

    def _validate_tool_calls(
        self,
        calls: list[dict[str, Any]],
        tool_schemas: dict[str, dict[str, Any]],
        role: str,
    ) -> list[dict[str, Any]]:
        self.tool_schemas = tool_schemas
        self.role = role
        return calls


class SoloTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "test", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        self.task = Task(
            "FG-060",
            "Unified Agents and Models view",
            "Build the view.",
            ("Identity remains stable.",),
            ("Do not deploy.",),
            ("swarm_router", "tests"),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def store(self) -> Store:
        return Store(self.root / "state", "FG-060")

    def test_marker_requires_final_line(self) -> None:
        self.assertEqual(final_marker("work\nCONTINUE"), "CONTINUE")
        self.assertIsNone(final_marker("READY_FOR_REVIEW\nextra"))

    def test_gateway_pins_model_and_serial_tools(self) -> None:
        client = FakeClient()
        OpenWebUIGateway(client, MODEL).complete([{"role": "user", "content": "x"}])
        payload = client.payloads[0]
        self.assertEqual(payload["model"], MODEL)
        self.assertFalse(payload["parallel_tool_calls"])

    def test_forge_policy_registers_terminal_tools_by_name(self) -> None:
        coordinator = CapturingCoordinator()
        policy = ForgePolicy.__new__(ForgePolicy)
        policy.coordinator = coordinator
        policy.tool_schemas = PROCESS_TOOL_SCHEMAS
        tool_call = call(
            "status-call",
            "run_command",
            {
                "command": "git status --short",
                "cwd": "/workspace/forge",
                "wait": 30,
            },
        )

        self.assertEqual(
            policy.validate(tool_call),
            tool_call,
        )
        self.assertEqual(
            coordinator.role,
            "implementer",
        )
        self.assertEqual(
            set(
                coordinator.tool_schemas
                or {}
            ),
            {
                "run_command",
                "get_process_status",
                "kill_process",
            },
        )
        self.assertEqual(
            (
                coordinator.tool_schemas
                or {}
            )["run_command"]["type"],
            "object",
        )

    def test_multiple_tool_calls_execute_only_first_and_continue_serially(
        self,
    ) -> None:
        first = call(
            "first-call",
            "run_command",
            {"command": "pwd"},
        )
        second = call(
            "second-call",
            "run_command",
            {"command": "git status --short"},
        )
        gateway = FakeGateway(
            [
                {
                    "model": MODEL,
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    first,
                                    second,
                                ],
                            }
                        }
                    ],
                },
                response(
                    "Continue after the accepted first call.\nCONTINUE"
                ),
            ]
        )
        terminal = FakeTerminal(
            [
                {
                    "status": "passed",
                    "exit_code": 0,
                    "output": "/workspace/forge",
                }
            ]
        )
        store = self.store()

        result = Runner(
            self.task,
            store,
            gateway,
            terminal,
            AllowPolicy(),
            self.repo,
            2,
            20,
        ).tick()

        self.assertEqual(result["status"], "continue")
        self.assertEqual(result["total_tool_calls"], 1)
        self.assertEqual(len(terminal.calls), 1)
        self.assertEqual(terminal.calls[0]["id"], "first-call")
        self.assertEqual(
            len(gateway.messages[1][2]["tool_calls"]),
            1,
        )
        self.assertEqual(
            gateway.messages[1][2]["tool_calls"][0]["id"],
            "first-call",
        )
        self.assertEqual(
            gateway.messages[1][3]["tool_call_id"],
            "first-call",
        )
        self.assertIn(
            "accepted and executed only the first tool call",
            gateway.messages[1][4]["content"],
        )

        events = [
            json.loads(line)
            for line in store.events.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        trimmed = [
            event
            for event in events
            if event.get("event")
            == "serial_tool_calls_trimmed"
        ]
        self.assertEqual(len(trimmed), 1)
        self.assertEqual(trimmed[0]["ignored_calls"], 1)

    def test_shared_writer_lease_records_actual_pinned_model(
        self,
    ) -> None:
        database = self.root / "model-ledger.sqlite3"

        with sqlite3.connect(database) as db:
            db.executescript(
                "CREATE TABLE forge_developer_writer_lock ("
                "workspace TEXT PRIMARY KEY,"
                "task_id TEXT NOT NULL,"
                "acquired_at TEXT NOT NULL,"
                "expires_at TEXT NOT NULL,"
                "lease_id TEXT NOT NULL DEFAULT ''"
                ");"
                "CREATE TABLE forge_developer_pending_calls ("
                "tool_call_id TEXT PRIMARY KEY,"
                "task_id TEXT NOT NULL"
                ");"
                "CREATE TABLE forge_developer_runs ("
                "task_id TEXT PRIMARY KEY,"
                "status TEXT NOT NULL,"
                "selected_model TEXT NOT NULL DEFAULT '',"
                "updated_at TEXT NOT NULL,"
                "active_process TEXT NOT NULL DEFAULT '{}',"
                "writer_lease_id TEXT NOT NULL DEFAULT ''"
                ");"
            )

        model = "deepseek-ai/deepseek-v4-flash"
        writer = SharedWriterLease(
            database,
            "solo:FG-060",
            model,
        )
        lock = writer.acquire()

        with sqlite3.connect(database) as db:
            selected_model = db.execute(
                "SELECT selected_model "
                "FROM forge_developer_runs "
                "WHERE task_id='solo:FG-060'"
            ).fetchone()[0]

        self.assertEqual(selected_model, model)

        writer.release(
            lock["lease_id"],
            status="blocked",
        )

    def test_fresh_context_resumes_from_checkpoint(self) -> None:
        store = self.store()
        first = Runner(
            self.task,
            store,
            FakeGateway([response("Inspected repository.\nCONTINUE")]),
            FakeTerminal([]),
            AllowPolicy(),
            self.repo,
            2,
            20,
        ).tick()
        self.assertEqual(first["context_epoch"], 1)

        gateway = FakeGateway([response("Continue from the checkpoint.\nCONTINUE")])
        second = Runner(
            self.task,
            store,
            gateway,
            FakeTerminal([]),
            AllowPolicy(),
            self.repo,
            2,
            20,
        ).tick()
        self.assertEqual(second["status"], "continue")
        prompt = gateway.messages[0][1]["content"]
        self.assertIn('"context_epoch": 1', prompt)
        self.assertIn("Inspected repository", prompt)

    def test_running_process_is_polled_on_next_tick(self) -> None:
        store = self.store()
        terminal = FakeTerminal(
            [
                {"id": "p1", "status": "running", "output": "started", "next_offset": 1},
                {"id": "p1", "status": "passed", "exit_code": 0, "output": "OK", "next_offset": 2},
            ]
        )
        first = Runner(
            self.task,
            store,
            FakeGateway([response(call=call("c1", "run_command", {"command": "python3 -m unittest"}))]),
            terminal,
            AllowPolicy(),
            self.repo,
            2,
            20,
        ).tick()
        self.assertEqual(first["active_process"]["process_id"], "p1")

        second = Runner(
            self.task,
            store,
            FakeGateway([response("Tests passed.\nCONTINUE")]),
            terminal,
            AllowPolicy(),
            self.repo,
            2,
            20,
        ).tick()
        self.assertEqual(second["status"], "continue")
        self.assertEqual(terminal.calls[1]["function"]["name"], "get_process_status")

    def test_same_sed_rejection_blocks_after_three(self) -> None:
        rejected = call("sed-call", "run_command", {"command": "sed -n '1,20p' README.md"})
        result = Runner(
            self.task,
            self.store(),
            FakeGateway([response(call=rejected), response(call=rejected), response(call=rejected)]),
            FakeTerminal([]),
            RejectSed(),
            self.repo,
            4,
            20,
        ).tick()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("three times", result["last_error"])

    def test_ready_gate_rejects_missing_changes_and_evidence(self) -> None:
        result = Runner(
            self.task,
            self.store(),
            FakeGateway(
                [
                    response("Claiming completion.\nREADY_FOR_REVIEW"),
                    response("More work is required.\nCONTINUE"),
                ]
            ),
            FakeTerminal([]),
            AllowPolicy(),
            self.repo,
            2,
            20,
        ).tick()
        self.assertEqual(result["status"], "continue")
        self.assertIn("No repository changes are present.", result["last_error"])

    def test_ready_gate_accepts_scoped_change_and_evidence(self) -> None:
        task = Task(
            "FG-060", "Unified Agents and Models view", "Build the view.",
            ("Identity remains stable.",), ("Do not deploy.",), ("allowed",),
        )
        store = self.store()
        runner = Runner(
            task,
            store,
            FakeGateway([
                response(
                    "Implementation, tests, diff inspection, and self-review "
                    "are complete.\nREADY_FOR_REVIEW"
                )
            ]),
            FakeTerminal([]), AllowPolicy(), self.repo, 2, 20,
        )
        state = runner.initial_state(snapshot(self.repo))
        allowed = self.repo / "allowed"
        allowed.mkdir()
        (allowed / "feature.py").write_text("value = 1\n", encoding="utf-8")
        state_digest = repository_state_digest(self.repo)
        state["evidence"] = [
            {
                "kind": kind,
                "success": True,
                "repository_state_digest": state_digest,
            }
            for kind in ("focused_test", "full_test", "diff", "status")
        ]
        store.save(state)
        result = runner.tick()
        self.assertEqual(result["status"], "ready_for_review")
        self.assertEqual(result["writer_lease_id"], "")
        self.assertTrue(
            Path(result["review_bundle"]).joinpath("review.md").is_file()
        )

    def test_out_of_scope_edit_blocks_immediately(self) -> None:
        result = Runner(
            self.task,
            self.store(),
            FakeGateway(
                [
                    response(
                        call=call(
                            "write-call",
                            "run_command",
                            {"command": "printf 'changed\\n' > README.md"},
                        )
                    )
                ]
            ),
            MutatingTerminal(self.repo / "README.md"),
            AllowPolicy(),
            self.repo,
            2,
            20,
        ).tick()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("outside task scope", result["last_error"])
        self.assertEqual(result["writer_lease_id"], "")

    def test_shared_writer_lease_fences_and_detects_loss(
        self,
    ) -> None:
        database = (
            self.root
            / "catalog.sqlite3"
        )

        with sqlite3.connect(
            database
        ) as db:
            db.executescript(
                """
                CREATE TABLE forge_developer_writer_lock (
                    workspace TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    lease_id TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE forge_developer_pending_calls (
                    tool_call_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL
                );

                CREATE TABLE forge_developer_runs (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    active_process TEXT NOT NULL DEFAULT '{}',
                    writer_lease_id TEXT NOT NULL DEFAULT ''
                );
                """
            )

        first = SharedWriterLease(
            database,
            "solo:FG-060",
        )
        second = SharedWriterLease(
            database,
            "developer:other",
        )

        first_lock = first.acquire(
            status="running",
            active_process={
                "process_id": "process-1",
                "status": "running",
            },
        )

        with sqlite3.connect(
            database
        ) as db:
            db.execute(
                """
                UPDATE forge_developer_writer_lock
                SET expires_at=?
                WHERE workspace='/workspace/forge'
                """,
                (
                    (
                        datetime.now(
                            timezone.utc
                        )
                        - timedelta(
                            seconds=1
                        )
                    ).isoformat(),
                ),
            )

        with self.assertRaises(
            WriterBusy
        ):
            second.acquire()

        with self.assertRaises(
            WriterBusy
        ):
            first.release(
                first_lock["lease_id"],
                status="blocked",
            )

        with sqlite3.connect(
            database
        ) as db:
            lock_row = db.execute(
                """
                SELECT task_id, lease_id
                FROM forge_developer_writer_lock
                WHERE workspace='/workspace/forge'
                """
            ).fetchone()

            run_row = db.execute(
                """
                SELECT
                    active_process,
                    writer_lease_id
                FROM forge_developer_runs
                WHERE task_id='solo:FG-060'
                """
            ).fetchone()

        self.assertEqual(
            lock_row,
            (
                "solo:FG-060",
                first_lock["lease_id"],
            ),
        )
        self.assertEqual(
            json.loads(run_row[0])["process_id"],
            "process-1",
        )
        self.assertEqual(
            run_row[1],
            first_lock["lease_id"],
        )

        first.acquire(
            first_lock["lease_id"],
            status="running",
            active_process={},
        )
        first.release(
            first_lock["lease_id"],
            status="blocked",
        )

        second_lock = second.acquire()

        with self.assertRaises(
            WriterLeaseLost
        ):
            first.acquire(
                first_lock["lease_id"]
            )

        second.release(
            second_lock["lease_id"]
        )

    def test_diff_check_is_not_final_diff_evidence(self) -> None:
        self.assertEqual(
            evidence_kind("git --no-pager diff --check"),
            "diff_check",
        )
        self.assertEqual(
            evidence_kind("git --no-pager diff --stat"),
            "diff",
        )


    def test_later_edit_invalidates_prior_evidence(self) -> None:
        task = Task(
            "FG-060", "Unified Agents and Models view", "Build the view.",
            ("Identity remains stable.",), ("Do not deploy.",), ("allowed",),
        )
        runner = Runner(
            task, self.store(), FakeGateway([]), FakeTerminal([]),
            AllowPolicy(), self.repo, 2, 20,
        )
        allowed = self.repo / "allowed"
        allowed.mkdir()
        feature = allowed / "feature.py"
        feature.write_text("value = 1\n", encoding="utf-8")
        state_digest = repository_state_digest(self.repo)
        state = runner.initial_state(snapshot(self.repo))
        state["evidence"] = [
            {
                "kind": kind,
                "success": True,
                "repository_state_digest": state_digest,
            }
            for kind in ("focused_test", "full_test", "diff", "status")
        ]
        self.assertEqual(runner.readiness_issues(state, snapshot(self.repo)), [])
        feature.write_text("value = 2\n", encoding="utf-8")
        issues = runner.readiness_issues(state, snapshot(self.repo))
        self.assertTrue(issues)
        self.assertTrue(
            all("current repository state" in issue for issue in issues)
        )


    def test_scope_block_retains_writer_while_process_active(
        self,
    ) -> None:
        task = Task(
            "FG-060",
            "Unified Agents and Models view",
            "Build the view.",
            ("Identity remains stable.",),
            ("Do not deploy.",),
            ("allowed",),
        )

        runner = Runner(
            task,
            self.store(),
            FakeGateway([]),
            FakeTerminal([]),
            AllowPolicy(),
            self.repo,
            2,
            20,
        )

        state = runner.initial_state(
            snapshot(self.repo)
        )
        state["writer_lease_id"] = "noop"
        state["active_process"] = {
            "process_id": "process-1",
            "status": "running",
        }

        (
            self.repo
            / "README.md"
        ).write_text(
            "out of scope\n",
            encoding="utf-8",
        )

        result = runner.block_for_scope(
            state,
            "scope_probe",
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result["status"],
            "continue",
        )
        self.assertEqual(
            result["writer_lease_id"],
            "noop",
        )
        self.assertEqual(
            result["active_process"]["process_id"],
            "process-1",
        )


    def test_release_writer_preserves_token_on_failure(
        self,
    ) -> None:
        class RejectRelease:
            def release(
                self,
                lease_id: str,
                *,
                status: str = "completed",
            ) -> None:
                raise WriterLeaseLost(
                    "simulated exact-token mismatch"
                )

        runner = Runner.__new__(Runner)
        runner.writer = RejectRelease()

        state = {
            "status": "blocked",
            "writer_lease_id": "exact-token",
        }

        with self.assertRaises(
            WriterLeaseLost
        ):
            runner.release_writer(state)

        self.assertEqual(
            state["writer_lease_id"],
            "exact-token",
        )


    def test_max_rounds_polls_active_process_before_blocking(
        self,
    ) -> None:
        store = self.store()
        terminal = FakeTerminal(
            [
                {
                    "id": "process-1",
                    "status": "passed",
                    "exit_code": 0,
                    "output": "done",
                    "next_offset": 1,
                }
            ]
        )

        runner = Runner(
            self.task,
            store,
            FakeGateway([]),
            terminal,
            AllowPolicy(),
            self.repo,
            2,
            1,
        )

        state = runner.initial_state(
            snapshot(self.repo)
        )
        state["total_rounds"] = 1
        state["active_process"] = {
            "process_id": "process-1",
            "status": "running",
            "next_offset": 0,
            "command": "python3 -m unittest",
        }
        store.save(state)

        result = runner.tick()

        self.assertEqual(
            terminal.calls[0]["function"]["name"],
            "get_process_status",
        )
        self.assertEqual(
            result["active_process"],
            {},
        )
        self.assertEqual(
            result["status"],
            "blocked",
        )
        self.assertIn(
            "Maximum total model rounds",
            result["last_error"],
        )


    def test_dirty_start_blocks(self) -> None:
        (
            self.repo
            / "README.md"
        ).write_text(
            "dirty\n",
            encoding="utf-8",
        )

        result = Runner(
            self.task,
            self.store(),
            FakeGateway([]),
            FakeTerminal([]),
            AllowPolicy(),
            self.repo,
            2,
            20,
        ).tick()

        self.assertEqual(
            result["status"],
            "blocked",
        )


if __name__ == "__main__":
    unittest.main()
