from __future__ import annotations

from unittest.mock import Mock
import unittest

from swarm_router.autopilot_adapter import (
    SwarmAdapterError,
    SwarmAutopilotAdapter,
)


class AutopilotIntegrationTest(unittest.TestCase):

    def test_rejects_forge_task_id_change_during_callback(self) -> None:
        adapter = object.__new__(SwarmAutopilotAdapter)
        adapter.max_rounds = 5
        adapter.terminal = Mock()
        adapter.terminal.execute_tool_call.return_value = (
            '{"id":"process-test","status":"done",'
            '"exit_code":0,"next_offset":1,'
            '"output":[],"truncated":false}'
        )
        adapter._developer_completion = Mock(
            side_effect=[
                {
                    "model": "swarm-developer",
                    "forge_task_id": "FT-ONE",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-one",
                                        "type": "function",
                                        "function": {
                                            "name": "run_command",
                                            "arguments": (
                                                '{"command":"pwd"}'
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                },
                {
                    "model": "swarm-developer",
                    "forge_task_id": "FT-TWO",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "Finished.",
                            },
                        }
                    ],
                },
            ]
        )

        with self.assertRaisesRegex(
            SwarmAdapterError,
            "changed Forge task IDs",
        ):
            adapter.run_task(
                task_id="FG-010",
                title="Sticky headers",
                objective="Implement sticky headers.",
            )


if __name__ == "__main__":
    unittest.main()
