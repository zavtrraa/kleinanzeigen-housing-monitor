import pytest

from kleinanzeigen_bot.demo.session import DemoSession


@pytest.mark.asyncio
async def test_demo_configure_run_and_repeat_uses_real_controls_and_monitor(tmp_path):
    session = DemoSession(tmp_path)
    try:
        await session.start()
        for command in ("/city Hannover", "/search balcony", "/budget 600", "/area 30", "/run"):
            await session.command(command)
        state = session.snapshot()
        report = state["reports"][-1]
        assert state["preferences"]["max_rent"] == "600"
        assert report["monitoring"]
        assert report["sent"] == 0
        assert report["notified"] == 2
        assert any("Balcony apartment" in item.get("caption", "") for item in state["messages"])
        await session.command("/run")
        assert session.snapshot()["reports"][-1]["notified"] == 0
        assert session.repo.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        assert not (tmp_path / "state.sqlite3").exists()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_demo_pause_resume_and_template_are_functional(tmp_path):
    session = DemoSession(tmp_path)
    try:
        await session.start()
        await session.command("/pause")
        await session.command("/run")
        assert session.queue.status()["jobs"]["queued"] == 1
        assert not session.snapshot()["reports"]
        await session.command("/template")
        assert any("Guten Tag" in item.get("text", "") for item in session.snapshot()["messages"])
        await session.command("/resume")
        assert len(session.snapshot()["reports"]) == 1
        await session.command("/replies on")
        await session.command("/run")
        assert session.snapshot()["reports"][-1]["sent"] == 0
        assert len(session.snapshot()["reports"][-1]["previews"]) == 1
        assert session.repo.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    finally:
        session.close()


@pytest.mark.asyncio
async def test_demo_restart_preserves_filters_and_receipts(tmp_path):
    session = DemoSession(tmp_path)
    await session.start()
    await session.command("/budget 600")
    await session.command("/run")
    session.close()
    restarted = DemoSession(tmp_path)
    try:
        await restarted.start()
        assert restarted.preferences.load().max_rent == 600
        await restarted.command("/run")
        assert restarted.snapshot()["reports"][-1]["notified"] == 0
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_demo_status_does_not_claim_to_run_a_scheduler(tmp_path):
    session = DemoSession(tmp_path)
    try:
        await session.start()
        text = session.snapshot()["messages"][-1]["text"]
        assert "Manual runs only" in text
        assert "50-70" not in text
    finally:
        session.close()
