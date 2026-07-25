from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
import json


class RunEventLog:
    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / "events.jsonl"
        self._lock = Lock()

    def emit(self, event: str, **payload: Any) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            self.path.chmod(0o600)
