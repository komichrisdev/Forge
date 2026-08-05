from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib import error, request
import json
import logging
import os
import socket

from .context_budget import ContextBudgetExceeded, preflight_check

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatResult:
    model: str
    content: str
    raw: dict[str, Any]


class RequestFailure(RuntimeError):
    def __init__(self, message: str, category: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code


class OpenWebUIClient:
    COMPLETION_FIELDS = {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "temperature",
        "max_tokens",
    }

    def __init__(
        self,
        base_url: str,
        endpoint: str,
        api_key_env: str,
        timeout_seconds: int,
        health_endpoint: str = "/health",
        models_endpoint: str = "/api/models",
        model_id: str | None = None,
        catalog_context: int | None = None,
        budget_enabled: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint
        self.api_key = os.environ[api_key_env].strip()
        self.timeout_seconds = timeout_seconds
        self.health_endpoint = health_endpoint
        self.models_endpoint = models_endpoint
        self._budget_enabled = budget_enabled
        self._model_id = model_id
        self._catalog_context = catalog_context

    def _redact(self, value: str) -> str:
        return value.replace(self.api_key, "[REDACTED]") if self.api_key else value

    def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
        timeout_seconds: int | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if authenticated and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if body is not None:
            headers["Content-Type"] = "application/json"

        req = request.Request(url, data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=timeout_seconds or self.timeout_seconds) as response:
                data = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = self._redact(exc.read().decode("utf-8", errors="replace"))
            lower = detail.lower()
            if exc.code in {401, 403}:
                category = "authentication"
            elif exc.code == 413 or any(token in lower for token in (
                "context_length_exceeded", "context length exceeded",
                "context size", "context window", "maximum context",
                "prompt is too long", "too many tokens", "context length",
            )):
                category = "context_overflow"
            elif exc.code in {429, 500, 502, 503, 504} or any(
                token in lower
                for token in (
                    "resourceexhausted",
                    "resource exhausted",
                    "quota",
                    "capacity",
                    "rate limit",
                    "ratelimit",
                    "too many requests",
                    "service temporarily overloaded",
                    "temporarily overloaded",
                    "internal server error",
                    "upstream server error",
                    "provider unavailable",
                )
            ):
                # Open WebUI can wrap an upstream provider 429/5xx
                # response in HTTP 400. Preserve the provider failure
                # classification from the bounded error detail.
                category = "capacity"
            else:
                category = "http"
            raise RequestFailure(
                f"Open WebUI returned HTTP {exc.code} for {path}: {detail[:1000]}",
                category,
                exc.code,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RequestFailure(
                f"Open WebUI request timed out for {path}.", "timeout"
            ) from exc
        except (error.URLError, ValueError, OSError) as exc:
            detail = self._redact(str(exc))
            category = "timeout" if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)) else "transport"
            raise RequestFailure(f"Unable to reach Open WebUI at {url}: {detail}", category) from exc

        try:
            return json.loads(data)
        except json.JSONDecodeError as exc:
            raise RequestFailure(
                f"Open WebUI returned non-JSON data for {path}: {self._redact(data[:500])}",
                "protocol",
            ) from exc

    def health(self) -> Any:
        return self._json_request("GET", self.health_endpoint, authenticated=False)

    def list_model_entries(self) -> list[dict[str, Any]]:
        data = self._json_request("GET", self.models_endpoint)
        candidates = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        entries: list[dict[str, Any]] = []
        if isinstance(candidates, list):
            for item in candidates:
                if isinstance(item, str):
                    entries.append({"id": item})
                elif isinstance(item, dict) and (item.get("id") or item.get("name")):
                    entries.append(item)
        return entries

    def list_models(self) -> list[str]:
        return sorted({str(item.get("id") or item.get("name")) for item in self.list_model_entries()})

    def completion(
        self,
        payload: dict[str, Any],
        timeout_seconds: int | None = None,
        catalog_context: int | None = None,
    ) -> dict[str, Any]:
        request_payload = {
            key: value for key, value in payload.items() if key in self.COMPLETION_FIELDS
        }
        request_payload["stream"] = False
        if self._budget_enabled:
            try:
                preflight_check(
                    request_payload,
                    model_id=str(request_payload.get("model") or self._model_id or ""),
                    catalog_context=(
                        catalog_context
                        if catalog_context is not None
                        else self._catalog_context
                    ),
                )
            except ContextBudgetExceeded as exc:
                raise RequestFailure(str(exc), "context_overflow", 413) from exc
        data = self._json_request(
            "POST", self.endpoint, request_payload, timeout_seconds=timeout_seconds
        )
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError
        except (KeyError, IndexError, TypeError) as exc:
            model = request_payload.get("model", "")
            raise RequestFailure(
                f"Unexpected completion response from model {model}: "
                f"{self._redact(json.dumps(data)[:1000])}",
                "protocol",
            ) from exc
        return data

    def chat(
        self,
        model: str,
        system: str,
        user: str | list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        timeout_seconds: int | None = None,
        catalog_context: int | None = None,
    ) -> ChatResult:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self._budget_enabled:
            try:
                preflight_check(
                    payload,
                    model_id=model or self._model_id,
                    catalog_context=(
                        catalog_context
                        if catalog_context is not None
                        else self._catalog_context
                    ),
                )
            except ContextBudgetExceeded as exc:
                raise RequestFailure(str(exc), "context_overflow", 413) from exc
        data = self._json_request(
            "POST", self.endpoint, payload, timeout_seconds=timeout_seconds
        )
        try:
            message = data["choices"][0]["message"]
            content = message.get("content", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise RequestFailure(
                f"Unexpected completion response from model {model}: "
                f"{self._redact(json.dumps(data)[:1000])}",
                "protocol",
            ) from exc

        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"text", "output_text"}:
                    text_parts.append(str(part.get("text", "")))
                elif isinstance(part, str):
                    text_parts.append(part)
            content = "\n".join(text_parts)

        return ChatResult(model=model, content=str(content).strip(), raw=data)
