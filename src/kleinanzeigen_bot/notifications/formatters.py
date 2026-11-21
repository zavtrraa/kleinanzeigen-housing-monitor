from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from ..core.models import CycleReport

LABELS = {
    "notified": "Notification delivered",
    "already_notified": "Already notified",
    "notification_failed": "Notification failed; retry pending",
    "sent": "Reply sent",
    "excluded": "Excluded",
    "review": "Review manually",
    "uncertain": "Delivery unconfirmed",
    "preview": "Reply preview",
    "deferred": "Deferred",
    "not_checked": "Not checked",
    "already_processed": "Already processed",
    "cached": "Unchanged; recheck later",
    "prepare_failed": "Form unavailable; no message sent",
    "inspection_failed": "Listing could not be inspected; retry pending",
    "blocked": "Review login or access",
    "cancelled": "Check cancelled",
    "error": "Error before sending",
}


def local_time(value):
    if not value:
        return "Not finished"
    return (
        datetime.fromisoformat(value)
        .astimezone(ZoneInfo("Europe/Berlin"))
        .strftime("%d.%m.%Y %H:%M:%S %Z")
    )


def summary_blocks(report: CycleReport, *, html=False):
    mode = "MONITORING" if report.monitoring else "DRY RUN" if report.dry_run else "LIVE"
    lines = [
        f"Housing monitor · {mode} · {report.id[:8]}",
        f"Started: {local_time(report.started_at)}",
        f"Finished: {local_time(report.finished_at)}",
        f"Trigger: {report.trigger}"
        + (f" | request #{report.request_id}" if report.request_id else ""),
        (
            f"Found: {report.found}; new: {report.new}; sent: {report.sent}"
            if report.search_completed
            else "Search incomplete; result count is unknown."
        ),
        f"Unconfirmed: {report.uncertain}; recovered after interruption: {report.recovered}",
    ]
    lines.append(
        f"Changed: {report.changed}; unchanged: {report.unchanged}; "
        f"cached checks: {report.cached}; form failures: {report.preparation_failed}"
    )
    if report.monitoring:
        lines.append(f"Matched: {report.matched}; notifications delivered: {report.notified}")
    waiting = sum(
        item.change_kind == "unchanged" and item.status in {"deferred", "not_checked"}
        for item in report.items
    )
    if waiting:
        lines.append(f"Known pending listings, omitted from repeated links: {waiting}.")
    lines.append("New means first seen locally, not necessarily newly published.")
    for source in report.sources:
        note = " | collection limit reached" if source.get("limited") else ""
        lines.append(f"{source['profile']}: {source['found']} | pages {source['pages']}{note}")
    if report.halted:
        lines.append("Run stopped.")
    if report.excluded or report.review:
        lines.append(f"Excluded: {report.excluded}; manual review: {report.review}")
    blocks = [escape("\n".join(lines)) if html else "\n".join(lines)]
    for error in report.errors:
        text = "Error: " + error[:500]
        blocks.append(escape(text) if html else text)
    visible_items = [
        item
        for item in report.items
        if item.status not in {"cached", "already_processed", "already_notified"}
        and not (item.change_kind == "unchanged" and item.status in {"deferred", "not_checked"})
    ]
    for index, item in enumerate(visible_items, 1):
        title = item.title[:240]
        link = (
            f'<a href="{escape(item.url, quote=True)}">{escape(title)}</a>'
            if html
            else title + "\n" + item.url
        )
        status = LABELS.get(item.status, item.status)
        notes = [item.note] if item.note else []
        notes.extend("! " + note for note in item.warnings)
        detail = "\n".join([status, *notes])
        blocks.append(f"{index}. {link}\n" + (escape(detail) if html else detail))
    # Legacy reports and tests without the new item inventory remain readable.
    if not report.items:
        for index, item in enumerate(report.skipped, 1):
            text = f"{index}. {LABELS.get(item.outcome, item.outcome)}: {item.reason}\n{item.url}"
            blocks.append(escape(text) if html else text)
        for preview in report.previews:
            text = f"Preview {preview.listing_id}\n{preview.url}\n{preview.text}"
            blocks.append(escape(text) if html else text)
    return blocks


def format_summary(report: CycleReport) -> str:
    return "\n\n".join(summary_blocks(report))


def summary_parts(report: CycleReport) -> list[str]:
    parts, current = [], ""
    for block in summary_blocks(report, html=True):
        candidate = current + ("\n\n" if current else "") + block
        if len(candidate.encode("utf-16-le")) // 2 > 3600 and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        while len(current.encode("utf-16-le")) // 2 > 3600:
            cut = 3600
            while len(current[:cut].encode("utf-16-le")) // 2 > 3600:
                cut -= 1
            parts.append(current[:cut])
            current = current[cut:]
        parts.append(current)
    return parts
