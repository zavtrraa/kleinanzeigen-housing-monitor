"""Local-only failure artifacts; data/ is excluded from version control and archives."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

log = logging.getLogger(__name__)


async def capture_failure(
    page, directory: Path | None, error: Exception, *, stage: str, extra=None
):
    if directory is None:
        return None
    name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    target = directory / name
    try:
        target.mkdir(parents=True, exist_ok=True)
        metadata = {
            "stage": stage,
            "exception": type(error).__name__,
            "url": page.url,
            **(extra or {}),
        }
        if type(error).__name__ == "SelectorNotFound":
            metadata["reason"] = str(error)
        for name, operation in [
            ("title", lambda: page.title()),
            ("html", lambda: page.content()),
            (
                "screenshot",
                lambda: page.screenshot(
                    path=str(target / "page.png"), full_page=False, timeout=3000
                ),
            ),
        ]:
            try:
                value = await asyncio.wait_for(operation(), timeout=4)
                if name == "title":
                    metadata["title"] = value
                elif name == "html":
                    (target / "page.html").write_text(value, encoding="utf-8")
            except Exception as exc:
                metadata[name + "_error"] = type(exc).__name__
        (target / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(target)
    except OSError as exc:
        log.warning("diagnostic_capture_failed type=%s", type(exc).__name__)
        return None
