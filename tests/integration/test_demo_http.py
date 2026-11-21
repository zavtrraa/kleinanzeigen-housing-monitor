import asyncio

import httpx
import pytest

from kleinanzeigen_bot.demo.server import create_server
from kleinanzeigen_bot.demo.session import DemoSession


@pytest.mark.asyncio
async def test_demo_http_uses_local_commands_and_rejects_foreign_origins(tmp_path):
    session = DemoSession(tmp_path)
    await session.start()
    server = create_server(session, asyncio.get_running_loop(), port=0)
    serving = asyncio.create_task(asyncio.to_thread(server.serve_forever, poll_interval=0.05))
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
            assert (await client.get("/")).status_code == 200
            response = await client.post("/api/command", json={"command": "/budget 600"})
            assert response.status_code == 200
            assert response.json()["preferences"]["max_rent"] == "600"
            denied = await client.post(
                "/api/command",
                json={"command": "/budget 999"},
                headers={"Origin": "https://foreign.example"},
            )
            assert denied.status_code == 403
            assert (await client.get("/api/state")).json()["preferences"]["max_rent"] == "600"
            assert (
                await client.get("/api/state", headers={"Host": "foreign.example"})
            ).status_code == 403
            assert (await client.get("/assets/../../.env")).status_code == 404
            assert (
                await client.post(
                    "/api/command", content="x" * 5000, headers={"Content-Type": "application/json"}
                )
            ).status_code == 413
    finally:
        await asyncio.to_thread(server.shutdown)
        await serving
        server.server_close()
        session.close()
