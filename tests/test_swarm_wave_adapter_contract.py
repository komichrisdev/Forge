from __future__ import annotations

from unittest.mock import Mock
import json
import unittest

from swarm_router.autopilot_adapter import (
    OpenTerminalClient,
    PROCESS_TOOLS,
)


class SwarmWaveAdapterContractTest(unittest.TestCase):

    def test_exposes_exact_developer_process_tools(self) -> None:
        names = [
            tool["function"]["name"]
            for tool in PROCESS_TOOLS
        ]

        self.assertEqual(
            names,
            [
                "run_command",
                "get_process_status",
                "kill_process",
            ],
        )

        for tool in PROCESS_TOOLS[:2]:
            properties = tool["function"]["parameters"]["properties"]
            self.assertNotIn("tail", properties)

    def test_open_terminal_maps_all_process_operations(self) -> None:
        terminal = object.__new__(OpenTerminalClient)
        terminal._request = Mock(
            return_value={
                "id": "process-test",
                "status": "done",
                "exit_code": 0,
                "next_offset": 1,
                "output": [],
                "truncated": False,
            }
        )

        calls = [
            {
                "id": "call-run",
                "type": "function",
                "function": {
                    "name": "run_command",
                    "arguments": json.dumps({
                        "command": "pwd",
                        "cwd": "/workspace/forge",
                        "wait": 10,
                    }),
                },
            },
            {
                "id": "call-poll",
                "type": "function",
                "function": {
                    "name": "get_process_status",
                    "arguments": json.dumps({
                        "process_id": "process-test",
                        "offset": 1,
                        "wait": 30,
                    }),
                },
            },
            {
                "id": "call-kill",
                "type": "function",
                "function": {
                    "name": "kill_process",
                    "arguments": json.dumps({
                        "process_id": "process-test",
                        "force": False,
                    }),
                },
            },
        ]

        for call in calls:
            result = json.loads(
                terminal.execute_tool_call(call)
            )
            self.assertEqual(result["status"], "done")

        self.assertEqual(terminal._request.call_count, 3)

        first = terminal._request.call_args_list[0].kwargs
        second = terminal._request.call_args_list[1].kwargs
        third = terminal._request.call_args_list[2].kwargs

        self.assertEqual(first["method"], "POST")
        self.assertEqual(first["path"], "/execute")
        self.assertEqual(
            first["payload"]["command"],
            "pwd",
        )

        self.assertEqual(second["method"], "GET")
        self.assertEqual(
            second["path"],
            "/execute/process-test/status",
        )
        self.assertEqual(second["query"]["offset"], 1)

        self.assertEqual(third["method"], "DELETE")
        self.assertEqual(
            third["path"],
            "/execute/process-test",
        )


if __name__ == "__main__":
    unittest.main()
