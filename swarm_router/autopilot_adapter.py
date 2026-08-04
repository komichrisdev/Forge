"""Authoritative Swarm autopilot adapter.

This adapter uses the live Forge personal API rather than constructing another
DeveloperCoordinator process. It drives the existing native tool-call protocol
and executes only the terminal calls already validated and authorized by the
DeveloperCoordinator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
import json
import os
import subprocess
import urllib.error
import urllib.request


DEVELOPER_MODEL_ID = "swarm-developer"

PROCESS_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a command in the isolated Forge terminal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "wait": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 300,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_process_status",
            "description": "Poll an Open Terminal process.",
            "parameters": {
                "type": "object",
                "properties": {
                    "process_id": {"type": "string"},
                    "wait": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 300,
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                    },
                },
                "required": ["process_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_process",
            "description": "Terminate an Open Terminal process.",
            "parameters": {
                "type": "object",
                "properties": {
                    "process_id": {"type": "string"},
                    "force": {"type": "boolean"},
                },
                "required": ["process_id"],
                "additionalProperties": False,
            },
        },
    },
]


class SwarmAdapterError(RuntimeError):
    """Raised when the adapter or one of its remote APIs fails."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}

    if not path.is_file():
        return values

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    return values


def _json_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    body = None

    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **headers,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")

        raise SwarmAdapterError(
            f"{method} {url} returned HTTP {exc.code}: "
            f"{error_body[:2000]}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SwarmAdapterError(
            f"{method} {url} failed: {type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(result, dict):
        raise SwarmAdapterError(
            f"{method} {url} returned a non-object JSON response."
        )

    return result


class OpenTerminalClient:
    """Authenticated client for the hardened Forge Open Terminal container."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        env_path: Path | None = None,
        timeout: int = 330,
    ) -> None:
        self.env_path = (
            env_path
            or Path.home()
            / ".config/forge-open-terminal/open-terminal.env"
        )

        env_values = _read_env_file(self.env_path)

        self.api_key = (
            api_key
            or os.environ.get("OPEN_TERMINAL_API_KEY", "")
            or env_values.get("OPEN_TERMINAL_API_KEY", "")
        )

        if not self.api_key:
            raise SwarmAdapterError(
                "OPEN_TERMINAL_API_KEY is unavailable."
            )

        self.base_url = (
            base_url
            or os.environ.get("OPEN_TERMINAL_BASE_URL", "")
            or self._discover_base_url()
        ).rstrip("/")

        self.timeout = timeout

    @staticmethod
    def _discover_base_url() -> str:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "open-terminal-forge",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

        address = result.stdout.strip()

        if result.returncode != 0 or not address:
            raise SwarmAdapterError(
                "Could not discover the open-terminal-forge container address: "
                + result.stderr.strip()[:1000]
            )

        return f"http://{address}:8000"

    def _request(
        self,
        *,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.base_url + path

        if query:
            filtered = {
                key: value
                for key, value in query.items()
                if value is not None
            }

            if filtered:
                url += "?" + urlencode(filtered)

        return _json_request(
            method=method,
            url=url,
            headers={
                "Authorization": "Bearer " + self.api_key,
            },
            payload=payload,
            timeout=self.timeout,
        )

    def execute_tool_call(self, call: dict[str, Any]) -> str:
        if not isinstance(call, dict):
            raise SwarmAdapterError("Malformed developer tool call.")

        call_id = str(call.get("id", "")).strip()
        function = call.get("function")

        if (
            not call_id
            or call.get("type") != "function"
            or not isinstance(function, dict)
        ):
            raise SwarmAdapterError("Malformed developer tool call.")

        name = str(function.get("name", "")).strip()
        raw_arguments = function.get("arguments")

        if not isinstance(raw_arguments, str):
            raise SwarmAdapterError(
                f"Tool call {call_id} has non-string arguments."
            )

        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise SwarmAdapterError(
                f"Tool call {call_id} contains invalid JSON arguments."
            ) from exc

        if not isinstance(arguments, dict):
            raise SwarmAdapterError(
                f"Tool call {call_id} arguments are not an object."
            )

        if name == "run_command":
            command = arguments.get("command", arguments.get("cmd"))

            if not isinstance(command, str) or not command.strip():
                raise SwarmAdapterError(
                    f"Tool call {call_id} has no command."
                )

            payload: dict[str, Any] = {"command": command}

            cwd = arguments.get("cwd")

            if cwd is not None:
                payload["cwd"] = cwd

            result = self._request(
                method="POST",
                path="/execute",
                query={
                    "wait": arguments.get("wait"),
                },
                payload=payload,
            )

        elif name == "get_process_status":
            process_id = arguments.get("process_id")

            if not isinstance(process_id, str) or not process_id.strip():
                raise SwarmAdapterError(
                    f"Tool call {call_id} has no process_id."
                )

            result = self._request(
                method="GET",
                path=f"/execute/{process_id}/status",
                query={
                    "wait": arguments.get("wait"),
                    "offset": arguments.get("offset", 0),
                },
            )

        elif name == "kill_process":
            process_id = arguments.get("process_id")

            if not isinstance(process_id, str) or not process_id.strip():
                raise SwarmAdapterError(
                    f"Tool call {call_id} has no process_id."
                )

            result = self._request(
                method="DELETE",
                path=f"/execute/{process_id}",
                query={
                    "force": str(
                        bool(arguments.get("force", False))
                    ).lower(),
                },
            )

        else:
            raise SwarmAdapterError(
                f"Unsupported developer tool: {name}"
            )

        return json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
        )


class SwarmAutopilotAdapter:
    """Drive the live Forge DeveloperCoordinator tool-call lifecycle."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8788/v1",
        api_key: str | None = None,
        api_key_env: str = "SWARM_PERSONAL_API_KEY",
        timeout: int = 2700,
        max_rounds: int = 100,
        terminal: OpenTerminalClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.timeout = timeout
        self.max_rounds = max_rounds

        if not self.api_key:
            raise SwarmAdapterError(
                f"Environment variable {api_key_env} is unavailable."
            )

        self.terminal = terminal or OpenTerminalClient()

    def _developer_completion(
        self,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return _json_request(
            method="POST",
            url=self.base_url + "/chat/completions",
            headers={
                "Authorization": "Bearer " + self.api_key,
            },
            payload={
                "model": DEVELOPER_MODEL_ID,
                "messages": messages,
                "tools": PROCESS_TOOLS,
                "tool_choice": "auto",
                # The live developer process contract is serial:
                # one Open Terminal process start per callback round.
                "parallel_tool_calls": False,
                "stream": False,
                "max_tokens": 2048,
            },
            timeout=self.timeout,
        )

    def run_task(
        self,
        *,
        task_id: str,
        title: str,
        objective: str,
        acceptance_criteria: list[str] | None = None,
        requirements: list[str] | None = None,
    ) -> dict[str, Any]:
        specification = {
            "autopilot_task_id": task_id,
            "title": title,
            "objective": objective,
            "acceptance_criteria": acceptance_criteria or [],
            "requirements": requirements or [],
        }

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    "Execute this Forge autopilot task using the complete "
                    "planner -> implementer -> reviewer -> verifier lifecycle. "
                    "Use the supplied terminal tools for all repository "
                    "inspection, edits, review, and verification. Do not push "
                    "or deploy.\n\n"
                    + json.dumps(
                        specification,
                        indent=2,
                        ensure_ascii=False,
                    )
                ),
            }
        ]

        forge_task_id = ""
        tool_calls_executed = 0

        for round_number in range(1, self.max_rounds + 1):
            response = self._developer_completion(messages)

            returned_task_id = response.get("forge_task_id")

            if isinstance(returned_task_id, str) and returned_task_id:
                if forge_task_id and returned_task_id != forge_task_id:
                    raise SwarmAdapterError(
                        "DeveloperCoordinator changed Forge task IDs "
                        "during one callback lifecycle."
                    )

                forge_task_id = returned_task_id

            choices = response.get("choices")

            if not isinstance(choices, list) or not choices:
                raise SwarmAdapterError(
                    "DeveloperCoordinator returned no completion choice."
                )

            choice = choices[0]

            if not isinstance(choice, dict):
                raise SwarmAdapterError(
                    "DeveloperCoordinator returned an invalid choice."
                )

            message = choice.get("message")

            if not isinstance(message, dict):
                raise SwarmAdapterError(
                    "DeveloperCoordinator returned no assistant message."
                )

            calls = message.get("tool_calls")

            if not calls:
                return {
                    "autopilot_task_id": task_id,
                    "forge_task_id": forge_task_id,
                    "model": str(
                        response.get("model", DEVELOPER_MODEL_ID)
                    ),
                    "completed_at": utc_now(),
                    "rounds": round_number,
                    "tool_calls_executed": tool_calls_executed,
                    "forge_result": response,
                }

            if not isinstance(calls, list):
                raise SwarmAdapterError(
                    "DeveloperCoordinator returned invalid tool_calls."
                )

            assistant_message = {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": calls,
            }

            messages.append(assistant_message)

            for call in calls:
                if not isinstance(call, dict):
                    raise SwarmAdapterError(
                        "DeveloperCoordinator returned a malformed tool call."
                    )

                call_id = str(call.get("id", "")).strip()

                if not call_id:
                    raise SwarmAdapterError(
                        "DeveloperCoordinator returned a tool call without ID."
                    )

                result_content = self.terminal.execute_tool_call(call)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_content,
                    }
                )

                tool_calls_executed += 1

        raise SwarmAdapterError(
            f"DeveloperCoordinator exceeded {self.max_rounds} callback rounds."
        )
