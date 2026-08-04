from __future__ import annotations

from unittest.mock import patch

from unittest.mock import Mock
import json
import unittest

from swarm_router.autopilot_adapter import SwarmAutopilotAdapter


def completion_with_tool() -> dict:
    return {
        "id": "chatcmpl-FT-TEST-1",
        "object": "chat.completion",
        "model": "swarm-developer",
        "forge_task_id": "FT-TEST-1",
        "forge_role": "planner",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-planner",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps({
                                    "command": "pwd",
                                    "cwd": "/workspace/forge",
                                    "wait": 10,
                                }),
                            },
                        }
                    ],
                },
            }
        ],
    }


def completion_finished() -> dict:
    return {
        "id": "chatcmpl-FT-TEST-1",
        "object": "chat.completion",
        "model": "swarm-developer",
        "forge_task_id": "FT-TEST-1",
        "forge_role": "verifier",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Verification passed.",
                },
            }
        ],
    }


class AutopilotAdapterTest(unittest.TestCase):

    def test_executes_tool_callback_and_returns_final_result(self) -> None:
        adapter = object.__new__(SwarmAutopilotAdapter)
        adapter.max_rounds = 5
        adapter.terminal = Mock()
        adapter.terminal.execute_tool_call.return_value = json.dumps({
            "id": "process-test",
            "status": "done",
            "exit_code": 0,
            "next_offset": 1,
            "output": [
                {
                    "type": "output",
                    "data": "/workspace/forge\n",
                }
            ],
            "truncated": False,
        })
        adapter._developer_completion = Mock(
            side_effect=[
                completion_with_tool(),
                completion_finished(),
            ]
        )

        result = adapter.run_task(
            task_id="FG-010",
            title="Sticky headers",
            objective="Implement sticky headers.",
            acceptance_criteria=["Tests pass."],
        )

        self.assertEqual(result["autopilot_task_id"], "FG-010")
        self.assertEqual(result["forge_task_id"], "FT-TEST-1")
        self.assertEqual(result["model"], "swarm-developer")
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["tool_calls_executed"], 1)
        self.assertEqual(
            result["forge_result"]["choices"][0]["message"]["content"],
            "Verification passed.",
        )

        adapter.terminal.execute_tool_call.assert_called_once()
        self.assertEqual(adapter._developer_completion.call_count, 2)

        callback_messages = (
            adapter._developer_completion.call_args_list[1].args[0]
        )

        self.assertEqual(
            [message["role"] for message in callback_messages],
            ["user", "assistant", "tool"],
        )
        self.assertEqual(
            callback_messages[-1]["tool_call_id"],
            "call-planner",
        )



class AutopilotSerialTerminalContractTest(
    unittest.TestCase
):
    def test_adapter_disables_parallel_tool_calls(
        self,
    ) -> None:
        adapter = SwarmAutopilotAdapter(
            api_key="test-key",
        )

        with patch(
            (
                "swarm_router."
                "autopilot_adapter."
                "_json_request"
            ),
            return_value={
                "choices": [],
            },
        ) as request:
            adapter._developer_completion(
                [
                    {
                        "role": "user",
                        "content": "test",
                    }
                ]
            )

        payload = request.call_args.kwargs[
            "payload"
        ]

        self.assertIs(
            payload[
                "parallel_tool_calls"
            ],
            False,
        )


if __name__ == "__main__":
    unittest.main()
