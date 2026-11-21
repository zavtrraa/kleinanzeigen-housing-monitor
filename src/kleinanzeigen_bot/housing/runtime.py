import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


def new_runtime():
    now = datetime.now(UTC).isoformat()
    return {"pid": os.getpid(), "state": "running", "started_at": now, "updated_at": now}


def save_runtime(directory: Path, report: dict):
    report["updated_at"] = datetime.now(UTC).isoformat()
    target = directory / "hub-runtime.json"
    try:
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    except OSError as exc:
        log.warning("hub_runtime_write_failed type=%s", type(exc).__name__)


def read_runtime(directory: Path):
    target = directory / "hub-runtime.json"
    if not target.exists():
        return {"state": "not_recorded"}
    try:
        report = json.loads(target.read_text(encoding="utf-8"))
        age = (
            datetime.now(UTC).timestamp() - datetime.fromisoformat(report["updated_at"]).timestamp()
        )
        report["heartbeat_age_seconds"] = round(max(0, age), 1)
        report["heartbeat_stale"] = report["state"] == "running" and age > 120
        return report
    except (ValueError, KeyError, OSError):
        return {"state": "unreadable"}
