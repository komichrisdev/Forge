from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import json
import subprocess
import unittest

from swarm_router.solo_autopilot import (
    OpenWebUIGateway,
    PROCESS_TOOL_SCHEMAS,
    Runner,
    Store,
    Task,
    final_marker,
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
    def __init__(self) -> None:
        self.active_processes: list[dict[str, Any] | None] = []

    def validate(
        self,
        tool_call: dict[str, Any],
        *,
        active_process: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.active_processes.append(
            dict(active_process)
            if active_process
            else None
        )
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


class FakeClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return response("CONTINUE")


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

    def test_process_tools_are_mapped_for_developer_policy(self) -> None:
        self.assertEqual(
            set(PROCESS_TOOL_SCHEMAS),
            {
                "run_command",
                "get_process_status",
                "kill_process",
            },
        )
        self.assertIn(
            "command",
            PROCESS_TOOL_SCHEMAS["run_command"]["properties"],
        )
        self.assertIn(
            "process_id",
            PROCESS_TOOL_SCHEMAS["get_process_status"]["properties"],
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

        gateway = FakeGateway([response("Finished.\nREADY_FOR_REVIEW")])
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
        self.assertEqual(second["status"], "ready_for_review")
        prompt = gateway.messages[0][1]["content"]
        self.assertIn('"context_epoch": 1', prompt)
        self.assertIn("Inspected repository", prompt)

    def test_running_process_is_polled_on_next_tick(self) -> None:
        store = self.store()
        policy = AllowPolicy()
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
            policy,
            self.repo,
            2,
            20,
        ).tick()
        self.assertEqual(first["active_process"]["process_id"], "p1")

        second = Runner(
            self.task,
            store,
            FakeGateway([response("Tests passed.\nREADY_FOR_REVIEW")]),
            terminal,
            policy,
            self.repo,
            2,
            20,
        ).tick()
        self.assertEqual(second["status"], "ready_for_review")
        self.assertEqual(terminal.calls[1]["function"]["name"], "get_process_status")
        self.assertEqual(
            policy.active_processes[-1]["process_id"],
            "p1",
        )

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

    def test_dirty_start_blocks(self) -> None:
        (self.repo / "README.md").write_text("dirty\n", encoding="utf-8")
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
        self.assertEqual(result["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
