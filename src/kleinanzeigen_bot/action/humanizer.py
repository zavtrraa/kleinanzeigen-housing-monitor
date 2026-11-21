import asyncio
import random


async def between_messages(minimum: float, maximum: float, *, progress=None) -> None:
    """Pacing only; not an automation-detection guarantee."""
    remaining = random.uniform(minimum, maximum)
    if progress is None:
        await asyncio.sleep(remaining)
        return
    while remaining > 0:
        progress(f"Waiting before request: {remaining:.0f} s. Scheduled delay.")
        step = min(10, remaining)
        await asyncio.sleep(step)
        remaining -= step
    progress("Delay complete. Opening listing.")
