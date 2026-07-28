from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import os
import re
import subprocess


ALLOWED_FIELDS = {
    "operation",
    "mode",
    "dry_run",
    "script_path",
    "state_dir",
    "timeout_seconds",
    "run_hours",
}
SECRET_PATTERNS = (
    re.compile(r"https://discord(?:app)?\.com/api/webhooks/[^\s'\"<>]+"),
    re.compile(r"\b(?:sk|nvapi)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b[A-Za-z0-9_=-]{24,}\.[A-Za-z0-9_=-]{6,}\.[A-Za-z0-9_=-]{20,}\b"),
)
OUTPUT_LIMIT = 6000


class NightOwlError(RuntimeError):
    pass


@dataclass(frozen=True)
class NightOwlPayload:
    operation: str = "run_nightly"
    mode: str = "dry_run"
    dry_run: bool = True
    script_path: str = ""
    state_dir: str = ""
    timeout_seconds: int = 300
    run_hours: int = 4

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NightOwlPayload":
        mode = str(data.get("mode", "dry_run")).strip() or "dry_run"
        dry_run = bool(data.get("dry_run", mode != "live"))
        if dry_run:
            mode = "dry_run"
        return cls(
            operation=str(data.get("operation", "run_nightly")).strip() or "run_nightly",
            mode=mode,
            dry_run=dry_run,
            script_path=str(data.get("script_path", "")).strip(),
            state_dir=str(data.get("state_dir", "")).strip(),
            timeout_seconds=int(data.get("timeout_seconds", 300)),
            run_hours=int(data.get("run_hours", 4)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "mode": self.mode,
            "dry_run": self.dry_run,
            "script_path": self.script_path,
            "state_dir": self.state_dir,
            "timeout_seconds": self.timeout_seconds,
            "run_hours": self.run_hours,
        }


@dataclass(frozen=True)
class NightOwlResult:
    status: str
    command: list[str]
    returncode: int
    duration_ms: int
    stdout: str
    stderr: str
    timed_out: bool
    side_effect_state: str
    checkpoint_reference: str
    metadata: dict[str, Any] = field(default_factory=dict)


def default_script_path() -> Path:
    return Path.home() / ".codex/skills/night-owl/scripts/run_nightly.sh"


def default_state_dir() -> Path:
    return Path.home() / ".local/share/owui-swarm/night-owl"


def forge_script_root() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts/night-owl"


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _secret_values() -> list[str]:
    values: list[str] = []
    config = Path.home() / ".config/night-owl/env"
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if any(token in key for token in ("TOKEN", "WEBHOOK", "SECRET", "KEY")):
                values.append(value.strip().strip("'\""))
    except FileNotFoundError:
        pass
    return [value for value in values if len(value) >= 8]


def redact(text: str) -> str:
    redacted = text
    for value in _secret_values():
        redacted = redacted.replace(value, "<redacted>")
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted[:OUTPUT_LIMIT]


def validate_night_owl_payload(
    data: dict[str, Any],
    *,
    allowed_script_roots: tuple[Path, ...] | None = None,
    allowed_state_roots: tuple[Path, ...] | None = None,
) -> list[str]:
    issues: list[str] = []
    unknown = sorted(set(data) - ALLOWED_FIELDS)
    if unknown:
        issues.append("unknown Night Owl payload fields: " + ", ".join(unknown))
    try:
        payload = NightOwlPayload.from_dict(data)
    except (TypeError, ValueError):
        return issues + ["Night Owl payload has invalid field types"]
    if payload.operation != "run_nightly":
        issues.append("operation must be run_nightly")
    if payload.mode not in {"dry_run", "live"}:
        issues.append("mode must be dry_run or live")
    if payload.timeout_seconds < 1 or payload.timeout_seconds > 14_400:
        issues.append("timeout_seconds must be between 1 and 14400")
    if payload.run_hours < 1 or payload.run_hours > 12:
        issues.append("run_hours must be between 1 and 12")

    script = Path(payload.script_path).expanduser().resolve() if payload.script_path else default_script_path().resolve()
    roots = allowed_script_roots or (
        forge_script_root().resolve(),
        (Path.home() / ".codex/skills/night-owl/scripts").resolve(),
        (Path.home() / "misc/skills/night-owl/scripts").resolve(),
    )
    if script.name != "run_nightly.sh" or not _within(script, roots):
        issues.append("script_path must be an approved Night Owl run_nightly.sh")
    if payload.state_dir:
        state = Path(payload.state_dir).expanduser().resolve()
        state_roots = allowed_state_roots or (default_state_dir().resolve(),)
        if not _within(state, state_roots):
            issues.append("state_dir must be under the approved Night Owl state directory")
    return issues


def run_night_owl(
    data: dict[str, Any],
    *,
    allowed_script_roots: tuple[Path, ...] | None = None,
    allowed_state_roots: tuple[Path, ...] | None = None,
    grace_seconds: float = 5.0,
) -> NightOwlResult:
    issues = validate_night_owl_payload(
        data,
        allowed_script_roots=allowed_script_roots,
        allowed_state_roots=allowed_state_roots,
    )
    if issues:
        raise NightOwlError("; ".join(issues))
    payload = NightOwlPayload.from_dict(data)
    script = Path(payload.script_path).expanduser().resolve() if payload.script_path else default_script_path().resolve()
    if not script.exists():
        raise NightOwlError(f"Night Owl script is missing: {script}")
    state_dir = Path(payload.state_dir).expanduser().resolve() if payload.state_dir else default_state_dir().resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    command = [str(script)]
    if payload.dry_run:
        command.append("--dry-run")
    env = {
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER", ""),
        "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "")),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "NIGHT_OWL_STATE_DIR": str(state_dir),
        "NIGHT_OWL_RUN_HOURS": str(payload.run_hours),
        "NIGHT_OWL_TIMEOUT": f"{payload.timeout_seconds}s",
    }
    started = datetime.now(timezone.utc)
    process = subprocess.Popen(
        command,
        cwd=str(script.parents[1]),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=payload.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
    duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    returncode = int(process.returncode or 0)
    status = "completed" if returncode == 0 and not timed_out else ("timeout" if timed_out else "failed")
    side_effect_state = "none" if payload.dry_run else ("confirmed" if status == "completed" else "unknown")
    return NightOwlResult(
        status=status,
        command=command,
        returncode=returncode,
        duration_ms=duration_ms,
        stdout=redact(stdout or ""),
        stderr=redact(stderr or ""),
        timed_out=timed_out,
        side_effect_state=side_effect_state,
        checkpoint_reference=f"night-owl/{started.strftime('%Y%m%dT%H%M%SZ')}",
        metadata={
            "operation": payload.operation,
            "mode": payload.mode,
            "dry_run": payload.dry_run,
            "state_dir": str(state_dir),
            "script_path": str(script),
        },
    )
