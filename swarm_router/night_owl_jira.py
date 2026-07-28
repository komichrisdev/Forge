from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, BinaryIO
import base64
import json
import re
import shlex
import urllib.error
import urllib.request


CONFIG_FILE = Path.home() / ".config/night-owl/env"
PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
STATUSES = ("In Progress", "To Do")
FIELDS = ("summary", "status", "issuetype", "priority", "assignee", "reporter", "updated", "created")


class JiraError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class JiraConfig:
    site_url: str
    email: str
    token: str


@dataclass(frozen=True)
class JiraPreflight:
    ok: bool
    project: str
    account: dict[str, str]
    statuses: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    error_category: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project": self.project,
            "account": self.account,
            "statuses": self.statuses,
            "issue_count": sum(len(items) for items in self.statuses.values()),
            "error_category": self.error_category,
            "message": self.message,
        }


def load_config(path: Path = CONFIG_FILE) -> JiraConfig:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise JiraError("auth", f"Missing Jira configuration file: {path}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        parsed = shlex.split(raw, comments=True)
        if len(parsed) == 1:
            values[key.strip()] = parsed[0]
    missing = [key for key in ("ATLASSIAN_SITE_URL", "ATLASSIAN_EMAIL", "ATLASSIAN_API_TOKEN") if not values.get(key)]
    if missing:
        raise JiraError("auth", "Missing Jira configuration: " + ", ".join(missing))
    site = values["ATLASSIAN_SITE_URL"].rstrip("/")
    if not site.startswith("https://"):
        raise JiraError("auth", "ATLASSIAN_SITE_URL must use HTTPS")
    return JiraConfig(site, values["ATLASSIAN_EMAIL"], values["ATLASSIAN_API_TOKEN"])


def mask_email(email: str) -> str:
    if "@" not in email:
        return "<unknown>"
    name, domain = email.split("@", 1)
    return f"{name[:2]}***@{domain}"


def validate_project(project: str) -> None:
    if not PROJECT_RE.fullmatch(project):
        raise JiraError("query_validation", "project key is invalid")


def queue_jql(project: str, status: str) -> str:
    validate_project(project)
    if status not in STATUSES:
        raise JiraError("query_validation", "status is not supported for Night Owl queue")
    return f'project = {project} AND issuetype != Epic AND status = "{status}" ORDER BY Rank ASC'


def classify_http_error(status: int) -> str:
    if status in {401, 403}:
        return "auth" if status == 401 else "permission"
    if status == 400:
        return "query_validation"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    return "http"


def _request(
    config: JiraConfig,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 10,
    open_url: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> dict[str, Any]:
    credentials = base64.b64encode(f"{config.email}:{config.token}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        config.site_url + path,
        data=data,
        method="POST" if body is not None else "GET",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {credentials}",
        },
    )
    try:
        with open_url(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        category = classify_http_error(exc.code)
        detail = exc.read(800).decode("utf-8", "replace")
        raise JiraError(category, f"Jira returned HTTP {exc.code}: {detail[:300]}") from None
    except TimeoutError as exc:
        raise JiraError("timeout", "Jira request timed out") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        category = "timeout" if isinstance(reason, TimeoutError) else "network"
        raise JiraError(category, f"Jira request failed: {reason}") from exc


def preflight(
    *,
    project: str = "KAN",
    config_path: Path = CONFIG_FILE,
    timeout: float = 10,
    open_url: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> JiraPreflight:
    validate_project(project)
    try:
        config = load_config(config_path)
        myself = _request(config, "/rest/api/3/myself", timeout=timeout, open_url=open_url)
        statuses: dict[str, list[dict[str, Any]]] = {}
        for status in STATUSES:
            payload = _request(
                config,
                "/rest/api/3/search/jql",
                body={"jql": queue_jql(project, status), "maxResults": 50, "fields": list(FIELDS)},
                timeout=timeout,
                open_url=open_url,
            )
            statuses[status] = [{"key": item.get("key", ""), "summary": item.get("fields", {}).get("summary", "")} for item in payload.get("issues", [])]
        return JiraPreflight(
            ok=True,
            project=project,
            account={
                "account_id": str(myself.get("accountId", "")),
                "email": mask_email(str(myself.get("emailAddress", config.email))),
            },
            statuses=statuses,
        )
    except JiraError as exc:
        return JiraPreflight(False, project, account={}, error_category=exc.category, message=str(exc))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Night Owl Jira REST preflight.")
    parser.add_argument("command", choices=("preflight",))
    parser.add_argument("--project", default="KAN")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    result = preflight(project=args.project, config_path=args.config)
    payload = result.to_dict()
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
