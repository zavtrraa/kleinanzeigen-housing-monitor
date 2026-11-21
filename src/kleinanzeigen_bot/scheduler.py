import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler


async def run_scheduled(orchestrator, settings) -> None:
    # Run once immediately. Never overlap cycles or replay missed jobs in a burst.
    report = await orchestrator.run_cycle()
    if report.halted:
        return
    stopped = asyncio.Event()
    scheduler = AsyncIOScheduler(timezone="UTC")

    async def job():
        result = await orchestrator.run_cycle()
        if result.halted:
            stopped.set()

    scheduler.add_job(
        job,
        "interval",
        seconds=settings.interval_seconds,
        jitter=settings.jitter_seconds,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.start()
    try:
        await stopped.wait()
    finally:
        scheduler.shutdown(wait=False)
