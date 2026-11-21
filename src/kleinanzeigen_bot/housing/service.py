import time

from .models import SourceBlocked, SourceRateLimited
from .policy import evaluate


async def collect_sources(store, sources, config, listing_filter, *, force=False):
    totals = {}
    for source in sources:
        if store.get_meta("paused", False):
            break
        old = store.source(source.name)
        if old["paused"] or (
            (not force or old.get("last_status") == "rate_limited")
            and old["next_poll"] > time.time()
        ):
            continue
        try:
            result = await source.collect(old["checkpoint"])
            changed = 0
            outcomes = {"eligible": 0, "review": 0, "excluded": 0}
            for candidate in result.candidates:
                decision = evaluate(candidate, config, listing_filter)
                outcomes[decision.outcome] += 1
                changed += int(store.upsert(candidate, decision, config.notify_review))
            details = {**result.details, **outcomes, "changed": changed}
            mail_issues = result.details.get("parse_failed_uids", []) + result.details.get(
                "oversized_skipped", []
            )
            if mail_issues:
                checkpoint = result.checkpoint or {}
                store.queue_alert(
                    f"mail-parse:{checkpoint.get('uidvalidity')}:{','.join(map(str, mail_issues))}",
                    "Gmail: some messages need manual review; UID: "
                    + ", ".join(map(str, mail_issues)),
                )
            # Advance source cursor only AFTER candidate+notification transactions committed.
            store.update_source(
                source.name,
                status="ok",
                next_poll=time.time() + source.interval,
                details=details,
                checkpoint=result.checkpoint,
            )
            if old.get("last_status") in {"error", "blocked", "rate_limited"}:
                store.queue_alert(
                    f"recovered:{source.name}:{old.get('last_success')}",
                    f"{source.name}: source available again",
                )
            totals[source.name] = details
        except Exception as exc:
            blocked = isinstance(exc, SourceBlocked)
            delay = exc.retry_after if isinstance(exc, SourceRateLimited) else source.interval
            status = (
                "blocked"
                if blocked
                else "rate_limited"
                if isinstance(exc, SourceRateLimited)
                else "error"
            )
            error = type(exc).__name__
            store.update_source(
                source.name,
                status=status,
                next_poll=time.time() + delay,
                paused=blocked,
                error=error,
            )
            if old.get("last_error") != error or old.get("last_status") != status:
                store.queue_alert(
                    f"source:{source.name}:{status}:{old.get('last_success')}",
                    f"{source.name}: {error}. "
                    + (
                        "Source paused; manual review required."
                        if blocked
                        else "Retry scheduled; other sources continue normally."
                    ),
                )
            totals[source.name] = {"error": error, "paused": blocked}
    return totals
