from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from swarm_router.client import OpenWebUIClient, RequestFailure


class OpenWebUIClientCompletionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {"TEST_OPENWEBUI_KEY": "test-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = OpenWebUIClient(
            "http://open-webui", "/v1/chat/completions", "TEST_OPENWEBUI_KEY", 10
        )

    def test_forwards_tools_and_preserves_tool_calls(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {"name": "terminal", "parameters": {"type": "object"}},
            }
        ]
        response = {
            "id": "chat-1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "terminal",
                                    "arguments": '{"command":"pwd"}',
                                },
                            }
                        ],
                    }
                }
            ],
        }

        with patch.object(
            self.client, "_json_request", return_value=response
        ) as request:
            result = self.client.completion(
                {
                    "model": "provider/model",
                    "messages": [{"role": "user", "content": "Inspect the repository"}],
                    "tools": tools,
                    "tool_choice": "auto",
                    "ignored": "not forwarded",
                }
            )

        sent = request.call_args.args[2]
        self.assertEqual(sent["tools"], tools)
        self.assertEqual(sent["tool_choice"], "auto")
        self.assertFalse(sent["stream"])
        self.assertNotIn("ignored", sent)
        self.assertIs(result, response)
        self.assertEqual(
            result["choices"][0]["message"]["tool_calls"][0]["id"], "call-1"
        )

    def test_rejects_malformed_response(self) -> None:
        with patch.object(
            self.client, "_json_request", return_value={"choices": []}
        ):
            with self.assertRaisesRegex(RequestFailure, "Unexpected completion response"):
                self.client.completion(
                    {
                        "model": "provider/model",
                        "messages": [{"role": "user", "content": "Hello"}],
                    }
                )


if __name__ == "__main__":
    unittest.main()
