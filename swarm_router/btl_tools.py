"""Structured repository tools confined to one BTL task worktree."""

from __future__ import annotations

from fnmatch import fnmatch
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any


MAX_BYTES = 500_000
MAX_RESULTS = 100
MAX_FILES = 2_000
READ_TOOLS = frozenset({"list_files", "read_file", "search_text", "git_status", "git_diff"})
WRITE_TOOLS = frozenset({"write_file", "replace_text"})
SECRET_PARTS = frozenset({
    ".git", ".env", ".ssh", ".aws", ".gnupg", ".credentials",
    "credentials", "credentials.json", "auth.json", "secrets.json", "id_rsa", "id_ed25519",
})
SECRET_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|nvapi)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
)


def _secret_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in SECRET_PARTS or lowered.startswith(".env.")
        or Path(lowered).suffix in SECRET_SUFFIXES
    )


def _contains_secret(content: str) -> bool:
    return any(pattern.search(content) for pattern in SECRET_PATTERNS)


def _schema(name: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Structured repository {name.replace('_', ' ')} operation.",
            "parameters": {
                "type": "object", "properties": properties,
                "required": required, "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS = {
    "list_files": _schema("list_files", {"path": {"type": "string"}}, []),
    "read_file": _schema("read_file", {"path": {"type": "string"}}, ["path"]),
    "search_text": _schema("search_text", {
        "query": {"type": "string"}, "path": {"type": "string"},
        "include": {"type": "string"},
    }, ["query"]),
    "git_status": _schema("git_status", {}, []),
    "git_diff": _schema("git_diff", {"path": {"type": "string"}}, []),
    "write_file": _schema("write_file", {
        "path": {"type": "string"}, "content": {"type": "string"},
    }, ["path", "content"]),
    "replace_text": _schema("replace_text", {
        "path": {"type": "string"}, "old": {"type": "string"},
        "new": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 1000},
    }, ["path", "old", "new"]),
}


class BTLTools:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)

    def schemas(self, writable: bool) -> list[dict[str, Any]]:
        names = READ_TOOLS | (WRITE_TOOLS if writable else frozenset())
        return [TOOL_SCHEMAS[name] for name in sorted(names)]

    def _path(self, value: str, *, allow_root: bool = False) -> Path:
        if not isinstance(value, str) or (not value and not allow_root):
            raise ValueError("path must be a non-empty relative string")
        raw = Path(value or ".")
        if raw.is_absolute() or ".." in raw.parts or "\x00" in value or "\\" in value:
            raise ValueError("path must be relative and cannot contain '..' or backslashes")
        if any(_secret_name(part) for part in raw.parts):
            raise ValueError("Git metadata and credential paths are unavailable")
        candidate = (self.root / raw).resolve(strict=False)
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("path escapes task worktree")
        current = self.root
        for part in raw.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("symlink paths are unavailable")
        return candidate

    def dispatch(self, name: str, arguments: dict[str, Any], *, writable: bool) -> str:
        allowed = READ_TOOLS | (WRITE_TOOLS if writable else frozenset())
        if name not in allowed or not isinstance(arguments, dict):
            raise ValueError(f"tool {name!r} is unavailable")
        try:
            result = getattr(self, name)(**arguments)
        except TypeError as exc:
            raise ValueError(f"invalid arguments for {name}: {exc}") from exc
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    def list_files(self, path: str = "") -> dict[str, Any]:
        directory = self._path(path, allow_root=True)
        if not directory.is_dir():
            raise ValueError("path is not a directory")
        entries = []
        for item in sorted(directory.iterdir(), key=lambda entry: entry.name)[:MAX_RESULTS]:
            if _secret_name(item.name) or item.is_symlink():
                continue
            entries.append({"name": item.name, "type": "directory" if item.is_dir() else "file"})
        return {"path": path, "entries": entries, "truncated": len(entries) == MAX_RESULTS}

    def read_file(self, path: str) -> dict[str, Any]:
        file = self._path(path)
        if not file.is_file():
            raise ValueError("path is not a file")
        with file.open("rb") as handle:
            data = handle.read(MAX_BYTES + 1)
        content = data[:MAX_BYTES].decode("utf-8", "replace")
        if _contains_secret(content):
            raise ValueError("file contains likely credential material")
        return {
            "path": path, "content": content,
            "size": file.stat().st_size, "truncated": len(data) > MAX_BYTES,
        }

    def search_text(self, query: str, path: str = "", include: str = "*") -> dict[str, Any]:
        if not isinstance(query, str) or not query or len(query) > 1000:
            raise ValueError("query must contain 1-1000 characters")
        directory = self._path(path, allow_root=True)
        if not directory.is_dir():
            raise ValueError("path is not a directory")
        matches: list[dict[str, Any]] = []
        scanned = 0
        for base, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = [d for d in dirs if not _secret_name(d) and not (Path(base) / d).is_symlink()]
            for name in files:
                if scanned >= MAX_FILES or len(matches) >= MAX_RESULTS:
                    break
                file = Path(base) / name
                if (
                    _secret_name(name) or not fnmatch(name, include) or file.is_symlink()
                    or file.stat().st_size > MAX_BYTES
                ):
                    continue
                scanned += 1
                content = file.read_text("utf-8", "replace")
                if _contains_secret(content):
                    continue
                for number, line in enumerate(content.splitlines(), 1):
                    if query in line:
                        matches.append({"path": str(file.relative_to(self.root)), "line": number, "text": line[:2000]})
                        if len(matches) >= MAX_RESULTS:
                            break
        return {"matches": matches, "files_scanned": scanned, "truncated": len(matches) >= MAX_RESULTS}

    def git_status(self) -> dict[str, Any]:
        result = self._git("status", "--short", "--branch", "--untracked-files=all")
        return {"output": result}

    def git_diff(self, path: str = "") -> dict[str, Any]:
        args = ["diff", "--no-ext-diff", "--no-textconv", "--"]
        if path:
            args.append(str(self._path(path).relative_to(self.root)))
        output = self._git(*args)[:MAX_BYTES]
        if _contains_secret(output):
            raise ValueError("diff contains likely credential material")
        return {"output": output}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        if not isinstance(content, str) or len(content.encode()) > MAX_BYTES:
            raise ValueError("content must be a string no larger than 500 KB")
        if _contains_secret(content):
            raise ValueError("content contains likely credential material")
        file = self._path(path)
        if file.exists():
            if not file.is_file() or file.stat().st_size > MAX_BYTES:
                raise ValueError("existing path is not a bounded regular file")
            if _contains_secret(file.read_text("utf-8", "replace")):
                raise ValueError("existing file contains likely credential material")
        file.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(file, content)
        return {"path": path, "bytes": len(content.encode())}

    def replace_text(self, path: str, old: str, new: str, count: int = 1) -> dict[str, Any]:
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("old must be non-empty and new must be a string")
        if not isinstance(count, int) or not 1 <= count <= 1000:
            raise ValueError("count must be between 1 and 1000")
        file = self._path(path)
        if not file.is_file() or file.stat().st_size > MAX_BYTES:
            raise ValueError("path is not a bounded regular file")
        content = file.read_text(encoding="utf-8")
        if _contains_secret(content) or _contains_secret(new):
            raise ValueError("file or replacement contains likely credential material")
        replacements = min(content.count(old), count)
        if not replacements:
            raise ValueError("old text was not found")
        updated = content.replace(old, new, count)
        if len(updated.encode()) > MAX_BYTES:
            raise ValueError("replacement exceeds 500 KB")
        self._atomic_write(file, updated)
        return {"path": path, "replacements": replacements}

    def _git(self, *args: str) -> str:
        environment = {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
        }
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args], cwd=self.root,
            capture_output=True, text=True, check=False, timeout=30, env=environment,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "git read failed")
        return result.stdout

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(temporary, mode)
            Path(temporary).replace(path)
        finally:
            Path(temporary).unlink(missing_ok=True)
