from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import json
import sqlite3
import subprocess
import unittest

from swarm_router.developer import (
    DeveloperCoordinator,
    DeveloperError,
)
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
    normalized_solo_tool_call,
    policy_recovery_instruction,
    repository_state_digest,
    safe_git_log_replacement,
    safe_inspection_replacement,
    select_serial_tool_call,
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
    def validate(
        self,
        tool_call: dict[str, Any],
        *,
        active_process: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return tool_call


class RejectSed:
    def validate(
        self,
        tool_call: dict[str, Any],
        *,
        active_process: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
        self.active_process: dict[str, Any] | None = None

    def _validate_tool_calls(
        self,
        calls: list[dict[str, Any]],
        tool_schemas: dict[str, dict[str, Any]],
        role: str,
        tool_choice: Any = "auto",
        *,
        active_process: dict[str, Any] | None = None,
        cancellation_requested: bool = False,
    ) -> list[dict[str, Any]]:
        self.tool_schemas = tool_schemas
        self.role = role
        self.active_process = active_process
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

        validated = policy.validate(tool_call)

        self.assertEqual(
            validated["id"],
            tool_call["id"],
        )
        self.assertEqual(
            validated["type"],
            tool_call["type"],
        )
        self.assertEqual(
            validated["function"]["name"],
            tool_call["function"]["name"],
        )
        self.assertEqual(
            json.loads(
                validated["function"]["arguments"]
            ),
            json.loads(
                tool_call["function"]["arguments"]
            ),
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

    def test_multi_call_skips_current_status_for_next_operation(
        self,
    ) -> None:
        first_status = call(
            "initial-status",
            "run_command",
            {
                "command": "git status --short",
                "cwd": "/workspace/forge",
            },
        )
        repeated_status = call(
            "repeated-status",
            "run_command",
            {
                "command": "git status --short",
                "cwd": "/workspace/forge",
            },
        )
        inventory = call(
            "inventory-call",
            "run_command",
            {
                "command": "git ls-files --cached --others --exclude-standard -- '*.py'",
                "cwd": "/workspace/forge",
            },
        )
        gateway = FakeGateway(
            [
                response(None, first_status),
                {
                    "model": MODEL,
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    repeated_status,
                                    inventory,
                                ],
                            }
                        }
                    ],
                },
                response(
                    "Continue after useful inventory.\nCONTINUE"
                ),
            ]
        )
        terminal = FakeTerminal(
            [
                {
                    "status": "done",
                    "exit_code": 0,
                    "output": "",
                },
                {
                    "status": "done",
                    "exit_code": 0,
                    "output": "swarm_router/solo_autopilot.py",
                },
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
            3,
            20,
        ).tick()

        self.assertEqual(result["status"], "continue")
        self.assertEqual(
            [item["id"] for item in terminal.calls],
            ["initial-status", "inventory-call"],
        )

        transcript = gateway.messages[2]
        assistant_messages = [
            item
            for item in transcript
            if item.get("role") == "assistant"
            and item.get("tool_calls")
        ]
        self.assertEqual(
            assistant_messages[-1]["tool_calls"][0]["id"],
            "inventory-call",
        )
        self.assertTrue(
            any(
                item.get("role") == "user"
                and "skipped a redundant leading"
                in str(item.get("content", ""))
                for item in transcript
            )
        )

        events = [
            json.loads(line)
            for line in store.events.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        selected = [
            event
            for event in events
            if event.get("event")
            == "serial_tool_calls_trimmed"
            and event.get("selected_index") == 1
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(
            selected[0]["selection_reason"],
            "skipped_redundant_status",
        )

    def test_serial_selection_keeps_status_without_current_evidence(
        self,
    ) -> None:
        status = call(
            "status",
            "run_command",
            {"command": "git status --short"},
        )
        inventory = call(
            "inventory",
            "run_command",
            {"command": "git ls-files --cached --others --exclude-standard -- '*'"},
        )

        selected, index, reason = select_serial_tool_call(
            [status, inventory],
            {"evidence": []},
            self.repo,
        )

        self.assertEqual(selected["id"], "status")
        self.assertEqual(index, 0)
        self.assertEqual(reason, "first_call")

    def test_serial_selection_keeps_status_when_repository_changed(
        self,
    ) -> None:
        state = {
            "evidence": [
                {
                    "kind": "status",
                    "success": True,
                    "repository_state_digest": (
                        repository_state_digest(
                            self.repo
                        )
                    ),
                }
            ]
        }
        (self.repo / "README.md").write_text(
            "changed\n",
            encoding="utf-8",
        )
        status = call(
            "status",
            "run_command",
            {"command": "git status --short"},
        )
        inventory = call(
            "inventory",
            "run_command",
            {"command": "git ls-files --cached --others --exclude-standard -- '*'"},
        )

        selected, index, reason = select_serial_tool_call(
            [status, inventory],
            state,
            self.repo,
        )

        self.assertEqual(selected["id"], "status")
        self.assertEqual(index, 0)
        self.assertEqual(reason, "first_call")

    def test_selected_later_call_still_passes_through_policy(
        self,
    ) -> None:
        first_status = call(
            "initial-status",
            "run_command",
            {"command": "git status --short"},
        )
        repeated_status = call(
            "repeated-status",
            "run_command",
            {"command": "git status --short"},
        )
        rejected = call(
            "rejected-sed",
            "run_command",
            {"command": "sed -n '1,20p' README.md"},
        )
        gateway = FakeGateway(
            [
                response(None, first_status),
                {
                    "model": MODEL,
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    repeated_status,
                                    rejected,
                                ],
                            }
                        }
                    ],
                },
                response(
                    "Continue after policy recovery.\nCONTINUE"
                ),
            ]
        )
        terminal = FakeTerminal(
            [
                {
                    "status": "done",
                    "exit_code": 0,
                    "output": "",
                },
            ]
        )
        store = self.store()

        result = Runner(
            self.task,
            store,
            gateway,
            terminal,
            RejectSed(),
            self.repo,
            3,
            20,
        ).tick()

        self.assertEqual(result["status"], "continue")
        self.assertEqual(len(terminal.calls), 1)
        self.assertEqual(
            terminal.calls[0]["id"],
            "initial-status",
        )

        events = [
            json.loads(line)
            for line in store.events.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertTrue(
            any(
                event.get("event") == "policy_rejected"
                and event.get("command", "").startswith("sed ")
                for event in events
            )
        )

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

    def test_forge_policy_passes_active_process_to_real_poll_validation(
        self,
    ) -> None:
        policy = ForgePolicy.__new__(ForgePolicy)
        policy.coordinator = DeveloperCoordinator.__new__(
            DeveloperCoordinator
        )
        policy.tool_schemas = PROCESS_TOOL_SCHEMAS

        active = {
            "process_id": "process-123",
            "status": "running",
            "next_offset": 7,
        }
        poll_call = call(
            "poll-call",
            "get_process_status",
            {
                "process_id": "process-123",
                "wait": 60,
                "offset": 7,
            },
        )

        self.assertEqual(
            policy.validate(
                poll_call,
                active_process=active,
            ),
            poll_call,
        )

        mismatched = call(
            "wrong-poll",
            "get_process_status",
            {
                "process_id": "other-process",
                "wait": 60,
                "offset": 7,
            },
        )

        with self.assertRaisesRegex(
            DeveloperError,
            "does not match the active process",
        ):
            policy.validate(
                mismatched,
                active_process=active,
            )

    def test_runner_poll_forwards_exact_active_process_context(
        self,
    ) -> None:
        class RecordingPolicy:
            def __init__(self) -> None:
                self.active_process = None

            def validate(
                self,
                tool_call,
                *,
                active_process=None,
            ):
                self.active_process = json.loads(
                    json.dumps(active_process)
                )
                return tool_call

        store = self.store()
        state = Runner(
            self.task,
            store,
            FakeGateway([]),
            FakeTerminal([]),
            AllowPolicy(),
            self.repo,
            2,
            20,
        ).initial_state(snapshot(self.repo))

        state["active_process"] = {
            "process_id": "process-456",
            "status": "running",
            "next_offset": 3,
            "command": "ls -la /workspace/forge/",
            "evidence_kind": "",
        }

        policy = RecordingPolicy()
        terminal = FakeTerminal(
            [
                {
                    "id": "process-456",
                    "status": "done",
                    "exit_code": 0,
                    "output": "listing",
                    "next_offset": 4,
                }
            ]
        )
        runner = Runner(
            self.task,
            store,
            FakeGateway([]),
            terminal,
            policy,
            self.repo,
            2,
            20,
        )

        self.assertFalse(
            runner.poll(state)
        )
        self.assertEqual(
            policy.active_process["process_id"],
            "process-456",
        )
        self.assertEqual(
            policy.active_process["next_offset"],
            3,
        )
        arguments = json.loads(
            terminal.calls[0]["function"]["arguments"]
        )
        self.assertEqual(
            arguments["process_id"],
            "process-456",
        )
        self.assertEqual(
            arguments["offset"],
            3,
        )

    def test_safe_bounded_grep_head_normalizes_observed_v13_form(
        self,
    ) -> None:
        observed = (
            'grep -n -m 100 "class DashboardApp" '
            '-A 200 swarm_router/dashboard.py | head -250'
        )

        self.assertEqual(
            safe_inspection_replacement(
                observed
            ),
            (
                "grep -n -m 1 -A 200 "
                "'class DashboardApp' "
                "swarm_router/dashboard.py"
            ),
        )

    def test_safe_bounded_grep_head_preserves_output_cap(
        self,
    ) -> None:
        variants = (
            (
                (
                    "grep --line-number --max-count=30 "
                    "--after-context=10 agent "
                    "swarm_router/developer.py | head -50"
                ),
                (
                    "grep -n -m 4 -A 10 agent "
                    "swarm_router/developer.py"
                ),
            ),
            (
                (
                    'grep -n -m 30 "class DashboardApp" '
                    '-A 200 '
                    '/workspace/forge/swarm_router/dashboard.py '
                    '| head -n 100'
                ),
                (
                    "grep -n -m 1 -A 99 "
                    "'class DashboardApp' "
                    "swarm_router/dashboard.py"
                ),
            ),
        )

        for command, expected in variants:
            with self.subTest(command=command):
                self.assertEqual(
                    safe_inspection_replacement(
                        command
                    ),
                    expected,
                )

    def test_safe_bounded_grep_head_rejects_unsafe_forms(
        self,
    ) -> None:
        rejected = (
            (
                'grep -n "DashboardApp" '
                'swarm_router/dashboard.py | head -250'
            ),
            (
                'grep -n -m 30 "DashboardApp" '
                'swarm_router/dashboard.py | head -250'
            ),
            (
                'grep -rn -m 30 "DashboardApp" '
                '-A 20 swarm_router/dashboard.py | head -250'
            ),
            (
                'grep -n -m 30 "DashboardApp" '
                '-A 20 swarm_router/dashboard.py '
                'swarm_router/agents.py | head -250'
            ),
            (
                'grep -n -m 30 "DashboardApp" '
                '-A 20 ../outside.py | head -250'
            ),
            (
                'grep -n -m 30 "DashboardApp" '
                '-A 20 swarm_router/*.py | head -250'
            ),
            (
                'grep -n -m 30 "DashboardApp" '
                '-A 20 swarm_router/dashboard.py | tail -250'
            ),
            (
                'grep -n -m 30 "DashboardApp" '
                '-A 20 swarm_router/dashboard.py '
                '| head -250 | cat'
            ),
            (
                'grep -n -m 30 "DashboardApp" '
                '-A 20 swarm_router/dashboard.py | head -501'
            ),
        )

        for command in rejected:
            with self.subTest(command=command):
                self.assertEqual(
                    safe_inspection_replacement(
                        command
                    ),
                    "",
                )

    def test_forge_policy_normalizes_observed_bounded_grep_head(
        self,
    ) -> None:
        policy = ForgePolicy.__new__(
            ForgePolicy
        )
        policy.coordinator = (
            DeveloperCoordinator.__new__(
                DeveloperCoordinator
            )
        )
        policy.tool_schemas = (
            PROCESS_TOOL_SCHEMAS
        )

        original = call(
            "bounded-grep-call",
            "run_command",
            {
                "command": (
                    'grep -n -m 100 '
                    '"class DashboardApp" '
                    '-A 200 '
                    'swarm_router/dashboard.py '
                    '| head -250'
                ),
                "cwd": "/tmp",
                "wait": 30,
            },
        )

        valid = policy.validate(
            original
        )
        arguments = json.loads(
            valid["function"]["arguments"]
        )

        self.assertEqual(
            arguments["command"],
            (
                "grep -n -m 1 -A 200 "
                "'class DashboardApp' "
                "swarm_router/dashboard.py"
            ),
        )
        self.assertEqual(
            arguments["cwd"],
            "/workspace/forge",
        )
        self.assertEqual(
            arguments["wait"],
            30,
        )

    def test_policy_recovery_names_bounded_grep_equivalent(
        self,
    ) -> None:
        rejected = call(
            "bounded-grep-call",
            "run_command",
            {
                "command": (
                    'grep -n -m 100 '
                    '"class DashboardApp" '
                    '-A 200 '
                    'swarm_router/dashboard.py '
                    '| head -250'
                ),
                "cwd": "/tmp",
            },
        )

        instruction = (
            policy_recovery_instruction(
                rejected,
                RuntimeError(
                    "policy rejected"
                ),
            )
        )

        self.assertIn(
            (
                "grep -n -m 1 -A 200 "
                "'class DashboardApp' "
                "swarm_router/dashboard.py"
            ),
            instruction,
        )
        self.assertIn(
            "cwd='/workspace/forge'",
            instruction,
        )

    def test_safe_find_pipeline_has_exact_git_replacement(self) -> None:
        command = (
            'find /workspace/forge/swarm_router '
            '-type f -name "*.py" | head -30'
        )
        self.assertEqual(
            safe_inspection_replacement(command),
            "git ls-files --cached --others --exclude-standard -- 'swarm_router/*.py'",
        )

    def test_forge_policy_forces_workspace_and_normalizes_safe_find(self) -> None:
        policy = ForgePolicy.__new__(ForgePolicy)
        policy.coordinator = DeveloperCoordinator.__new__(DeveloperCoordinator)
        policy.tool_schemas = PROCESS_TOOL_SCHEMAS
        original = call(
            "inventory-call",
            "run_command",
            {
                "command": (
                    'find /workspace/forge/swarm_router '
                    '-type f -name "*.py" | head -30'
                ),
                "cwd": "/tmp",
                "wait": 30,
            },
        )
        valid = policy.validate(original)
        arguments = json.loads(valid["function"]["arguments"])
        self.assertEqual(arguments["command"], "git ls-files --cached --others --exclude-standard -- 'swarm_router/*.py'")
        self.assertEqual(arguments["cwd"], "/workspace/forge")
        self.assertEqual(arguments["wait"], 30)
        self.assertEqual(json.loads(original["function"]["arguments"])["cwd"], "/tmp")

    def test_forge_policy_forces_workspace_for_normal_command(self) -> None:
        policy = ForgePolicy.__new__(ForgePolicy)
        policy.coordinator = DeveloperCoordinator.__new__(DeveloperCoordinator)
        policy.tool_schemas = PROCESS_TOOL_SCHEMAS
        valid = policy.validate(
            call(
                "status-call",
                "run_command",
                {"command": "git status --short", "cwd": "/", "wait": 30},
            )
        )
        arguments = json.loads(valid["function"]["arguments"])
        self.assertEqual(arguments["command"], "git status --short")
        self.assertEqual(arguments["cwd"], "/workspace/forge")

    def test_unsafe_find_command_is_not_rewritten_or_allowed(self) -> None:
        unsafe = call(
            "unsafe-find",
            "run_command",
            {
                "command": "find /workspace/forge -type f -delete",
                "cwd": "/tmp",
            },
        )
        normalized = normalized_solo_tool_call(unsafe)
        arguments = json.loads(normalized["function"]["arguments"])
        self.assertEqual(arguments["command"], "find /workspace/forge -type f -delete")
        self.assertEqual(arguments["cwd"], "/workspace/forge")
        policy = ForgePolicy.__new__(ForgePolicy)
        policy.coordinator = DeveloperCoordinator.__new__(DeveloperCoordinator)
        policy.tool_schemas = PROCESS_TOOL_SCHEMAS
        with self.assertRaises(DeveloperError):
            policy.validate(unsafe)

    def test_runner_executes_and_replays_normalized_tool_call(self) -> None:
        requested = call(
            "inventory-call",
            "run_command",
            {
                "command": (
                    'find /workspace/forge/swarm_router '
                    '-type f -name "*.py" | head -30'
                ),
                "cwd": "/tmp",
                "wait": 30,
            },
        )
        gateway = FakeGateway([
            response(call=requested),
            response("Inventory inspected.\nCONTINUE"),
        ])
        terminal = FakeTerminal([
            {"status": "done", "exit_code": 0, "output": "swarm_router/solo_autopilot.py"}
        ])
        policy = ForgePolicy.__new__(ForgePolicy)
        policy.coordinator = DeveloperCoordinator.__new__(DeveloperCoordinator)
        policy.tool_schemas = PROCESS_TOOL_SCHEMAS
        store = self.store()
        result = Runner(
            self.task,
            store,
            gateway,
            terminal,
            policy,
            self.repo,
            2,
            20,
        ).tick()
        self.assertEqual(result["status"], "continue")
        self.assertEqual(result["total_tool_calls"], 1)
        executed = json.loads(terminal.calls[0]["function"]["arguments"])
        replayed = json.loads(
            gateway.messages[1][2]["tool_calls"][0]["function"]["arguments"]
        )
        expected = {
            "command": "git ls-files --cached --others --exclude-standard -- 'swarm_router/*.py'",
            "cwd": "/workspace/forge",
            "wait": 30,
        }
        self.assertEqual(executed, expected)
        self.assertEqual(replayed, expected)
        events = [
            json.loads(line)
            for line in store.events.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        normalized_events = [
            event for event in events
            if event.get("event") == "tool_call_normalized"
        ]
        self.assertEqual(len(normalized_events), 1)
        self.assertEqual(
            normalized_events[0]["normalized_command"],
            "git ls-files --cached --others --exclude-standard -- 'swarm_router/*.py'",
        )

    def test_policy_recovery_names_exact_supported_equivalent(self) -> None:
        rejected = call(
            "inventory-call",
            "run_command",
            {
                "command": (
                    'find /workspace/forge/swarm_router '
                    '-type f -name "*.py" | head -30'
                ),
                "cwd": "/tmp",
            },
        )
        instruction = policy_recovery_instruction(
            rejected,
            RuntimeError("policy rejected"),
        )
        self.assertIn("Do not repeat the rejected command", instruction)
        self.assertIn("git ls-files --cached --others --exclude-standard -- 'swarm_router/*.py'", instruction)
        self.assertIn("cwd='/workspace/forge'", instruction)

    def test_safe_find_pipeline_accepts_maxdepth_variant(
        self,
    ) -> None:
        command = (
            'find /workspace/forge -maxdepth 3 '
            '-type f -name "*.py" | head -60'
        )

        self.assertEqual(
            safe_inspection_replacement(command),
            "git ls-files --cached --others --exclude-standard -- '*.py'",
        )

    def test_safe_find_pipeline_accepts_multiple_glob_variant(
        self,
    ) -> None:
        command = (
            'find /workspace/forge -type f '
            '-name "*.py" -o -name "*.js" '
            '-o -name "*.ts" -o -name "*.tsx" '
            '-o -name "*.vue" -o -name "*.html" '
            '| head -80'
        )

        self.assertEqual(
            safe_inspection_replacement(command),
            (
                "git ls-files --cached --others "
                "--exclude-standard -- '*.py' '*.js' "
                "'*.ts' '*.tsx' '*.vue' "
                "'*.html'"
            ),
        )

    def test_forge_policy_accepts_observed_v8_inventory_variants(
        self,
    ) -> None:
        policy = ForgePolicy.__new__(
            ForgePolicy
        )
        policy.coordinator = (
            DeveloperCoordinator.__new__(
                DeveloperCoordinator
            )
        )
        policy.tool_schemas = (
            PROCESS_TOOL_SCHEMAS
        )

        commands = (
            (
                'find /workspace/forge '
                '-maxdepth 3 -type f '
                '-name "*.py" | head -60'
            ),
            (
                'find /workspace/forge -type f '
                '-name "*.py" -o -name "*.js" '
                '-o -name "*.ts" -o -name "*.tsx" '
                '-o -name "*.vue" -o -name "*.html" '
                '| head -80'
            ),
            (
                'find /workspace/forge -type f '
                '-name "*.py" -o -name "*.js" '
                '-o -name "*.ts" -o -name "*.vue" '
                '-o -name "*.svelte" -o -name "*.html" '
                '| head -80'
            ),
        )

        for index, command in enumerate(
            commands,
            start=1,
        ):
            with self.subTest(command=command):
                valid = policy.validate(
                    call(
                        f"inventory-{index}",
                        "run_command",
                        {
                            "command": command,
                            "cwd": "/tmp",
                            "wait": 30,
                        },
                    )
                )
                arguments = json.loads(
                    valid["function"][
                        "arguments"
                    ]
                )

                self.assertTrue(
                    arguments["command"].startswith(
                        "git ls-files "
                    )
                )
                self.assertNotIn(
                    "|",
                    arguments["command"],
                )
                self.assertEqual(
                    arguments["cwd"],
                    "/workspace/forge",
                )

    def test_safe_find_pipeline_rejects_unrecognized_predicates(
        self,
    ) -> None:
        rejected = (
            "find /workspace/forge "
            "-maxdepth 3 -type f "
            '-name "*.py" -delete | head -60'
        )

        self.assertEqual(
            safe_inspection_replacement(
                rejected
            ),
            "",
        )

        policy = ForgePolicy.__new__(
            ForgePolicy
        )
        policy.coordinator = (
            DeveloperCoordinator.__new__(
                DeveloperCoordinator
            )
        )
        policy.tool_schemas = (
            PROCESS_TOOL_SCHEMAS
        )

        with self.assertRaises(
            DeveloperError
        ):
            policy.validate(
                call(
                    "unsafe-inventory",
                    "run_command",
                    {
                        "command": rejected,
                        "cwd": "/tmp",
                    },
                )
            )

    def test_safe_find_pipeline_accepts_relative_root(
        self,
    ) -> None:
        command = (
            'find . -type f -name "*.py" | head -50'
        )

        self.assertEqual(
            safe_inspection_replacement(command),
            "git ls-files --cached --others --exclude-standard -- '*.py'",
        )

    def test_safe_find_pipeline_accepts_relative_json_inventory(
        self,
    ) -> None:
        command = (
            'find . -type f '
            '-name "*.py" -o -name "*.js" '
            '-o -name "*.ts" -o -name "*.tsx" '
            '-o -name "*.vue" -o -name "*.html" '
            '-o -name "*.json" | head -80'
        )

        self.assertEqual(
            safe_inspection_replacement(command),
            (
                "git ls-files --cached --others "
                "--exclude-standard -- '*.py' '*.js' "
                "'*.ts' '*.tsx' '*.vue' "
                "'*.html' '*.json'"
            ),
        )

    def test_safe_find_pipeline_accepts_observed_metadata_globs(
        self,
    ) -> None:
        command = (
            'find /workspace/forge -maxdepth 1 -type f '
            '-name "*.py" -o -name "*.json" '
            '-o -name "*.toml" -o -name "*.md" '
            '| head -30'
        )

        self.assertEqual(
            safe_inspection_replacement(command),
            (
                "git ls-files --cached --others "
                "--exclude-standard -- '*.py' '*.json' "
                "'*.toml' '*.md'"
            ),
        )

    def test_inventory_replacement_uses_git_without_rg(
        self,
    ) -> None:
        replacement = safe_inspection_replacement(
            (
                'find . -type f -name "*.py" '
                '| head -50'
            )
        )

        self.assertTrue(
            replacement.startswith(
                "git ls-files "
            )
        )
        self.assertNotIn(
            "rg",
            replacement.split(),
        )

    def test_forge_policy_normalizes_observed_v10_relative_variants(
        self,
    ) -> None:
        policy = ForgePolicy.__new__(
            ForgePolicy
        )
        policy.coordinator = (
            DeveloperCoordinator.__new__(
                DeveloperCoordinator
            )
        )
        policy.tool_schemas = (
            PROCESS_TOOL_SCHEMAS
        )

        commands = (
            (
                'find . -type f '
                '-name "*.py" | head -50'
            ),
            (
                'find . -type f '
                '-name "*.py" -o -name "*.js" '
                '-o -name "*.ts" -o -name "*.tsx" '
                '-o -name "*.vue" -o -name "*.html" '
                '-o -name "*.json" | head -80'
            ),
        )

        for index, command in enumerate(
            commands,
            start=1,
        ):
            with self.subTest(command=command):
                valid = policy.validate(
                    call(
                        f"relative-inventory-{index}",
                        "run_command",
                        {
                            "command": command,
                            "cwd": "/tmp",
                            "wait": 30,
                        },
                    )
                )
                arguments = json.loads(
                    valid["function"]["arguments"]
                )

                self.assertTrue(
                    arguments["command"].startswith(
                        "git ls-files "
                    )
                )
                self.assertNotIn(
                    "|",
                    arguments["command"],
                )
                self.assertEqual(
                    arguments["cwd"],
                    "/workspace/forge",
                )

    def test_safe_find_pipeline_rejects_relative_escape(
        self,
    ) -> None:
        command = (
            'find ../outside -type f '
            '-name "*.py" | head -50'
        )

        self.assertEqual(
            safe_inspection_replacement(command),
            "",
        )

    def test_safe_git_log_normalizes_observed_v12_form(
        self,
    ) -> None:
        self.assertEqual(
            safe_git_log_replacement(
                "git log --oneline -5"
            ),
            "git log -n 5 --oneline",
        )

    def test_safe_git_log_accepts_only_bounded_equivalent_forms(
        self,
    ) -> None:
        variants = (
            "git log -5 --oneline",
            "git log --oneline -n 5",
            "git log -n5 --oneline",
            "git log --oneline --max-count 5",
            "git log --max-count=5 --oneline",
            "git --no-pager log --oneline -5",
            "git log -n 5 --oneline",
        )

        for command in variants:
            with self.subTest(command=command):
                self.assertEqual(
                    safe_git_log_replacement(command),
                    "git log -n 5 --oneline",
                )

    def test_safe_git_log_rejects_unbounded_or_scoped_forms(
        self,
    ) -> None:
        rejected = (
            "git log --oneline",
            "git log --oneline -0",
            "git log --oneline -101",
            "git log --oneline -5 README.md",
            "git log --format=%H -5",
            "git log --oneline -5 -- README.md",
            "git -C /workspace/forge log --oneline -5",
        )

        for command in rejected:
            with self.subTest(command=command):
                self.assertEqual(
                    safe_git_log_replacement(command),
                    "",
                )

    def test_forge_policy_normalizes_observed_v12_git_log(
        self,
    ) -> None:
        policy = ForgePolicy.__new__(ForgePolicy)
        policy.coordinator = DeveloperCoordinator.__new__(
            DeveloperCoordinator
        )
        policy.tool_schemas = PROCESS_TOOL_SCHEMAS

        valid = policy.validate(
            call(
                "observed-v12-log",
                "run_command",
                {
                    "command": "git log --oneline -5",
                    "cwd": "/tmp",
                    "wait": 30,
                },
            )
        )
        arguments = json.loads(
            valid["function"]["arguments"]
        )

        self.assertEqual(
            arguments["command"],
            "git log -n 5 --oneline",
        )
        self.assertEqual(
            arguments["cwd"],
            "/workspace/forge",
        )
        self.assertEqual(arguments["wait"], 30)

    def test_serial_selection_executes_normalized_v12_git_log(
        self,
    ) -> None:
        first_status = call(
            "initial-status",
            "run_command",
            {
                "command": "git status --short",
                "cwd": "/workspace/forge",
            },
        )
        repeated_status = call(
            "repeated-status",
            "run_command",
            {
                "command": "git status --short",
                "cwd": "/workspace/forge",
            },
        )
        observed_log = call(
            "observed-v12-log",
            "run_command",
            {
                "command": "git log --oneline -5",
                "cwd": "/workspace/forge",
                "wait": 30,
            },
        )
        gateway = FakeGateway(
            [
                response(None, first_status),
                {
                    "model": MODEL,
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    repeated_status,
                                    observed_log,
                                ],
                            }
                        }
                    ],
                },
                response(
                    "Continue after history inspection.\nCONTINUE"
                ),
            ]
        )
        terminal = FakeTerminal(
            [
                {
                    "status": "done",
                    "exit_code": 0,
                    "output": "",
                },
                {
                    "status": "done",
                    "exit_code": 0,
                    "output": "abc1234 prior commit",
                },
            ]
        )
        store = self.store()
        policy = ForgePolicy.__new__(ForgePolicy)
        policy.coordinator = DeveloperCoordinator.__new__(
            DeveloperCoordinator
        )
        policy.tool_schemas = PROCESS_TOOL_SCHEMAS

        result = Runner(
            self.task,
            store,
            gateway,
            terminal,
            policy,
            self.repo,
            3,
            20,
        ).tick()

        self.assertEqual(result["status"], "continue")
        self.assertEqual(
            [item["id"] for item in terminal.calls],
            ["initial-status", "observed-v12-log"],
        )

        executed = json.loads(
            terminal.calls[1]["function"]["arguments"]
        )
        self.assertEqual(
            executed["command"],
            "git log -n 5 --oneline",
        )

        events = [
            json.loads(line)
            for line in store.events.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertTrue(
            any(
                event.get("event")
                == "serial_tool_calls_trimmed"
                and event.get("selection_reason")
                == "skipped_redundant_status"
                for event in events
            )
        )
        self.assertTrue(
            any(
                event.get("event")
                == "tool_call_normalized"
                and event.get("normalized_command")
                == "git log -n 5 --oneline"
                for event in events
            )
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
