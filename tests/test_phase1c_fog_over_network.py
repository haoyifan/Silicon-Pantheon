"""Phase 1c integration: fog-of-war correctness over the wire."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn

from clash_of_robots.client.transport import ServerClient
from clash_of_robots.server.app import App, build_mcp_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fast_countdown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clash_of_robots.server.lobby_tools.AUTOSTART_DELAY_S", 0.2)


@pytest.fixture
def server(fast_countdown):
    app = App()
    mcp = build_mcp_server(app)
    port = _free_port()
    starlette_app = mcp.streamable_http_app()
    config = uvicorn.Config(
        app=starlette_app, host="127.0.0.1", port=port, log_level="warning"
    )
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while time.time() < deadline and not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp/", app
    srv.should_exit = True
    thread.join(timeout=5.0)


def test_classic_fog_hides_enemy_from_get_state(server) -> None:
    url, _app = server

    async def go() -> None:
        async with (
            ServerClient.connect(url) as blue,
            ServerClient.connect(url) as red,
        ):
            await blue.call("set_player_metadata", display_name="a", kind="ai")
            await red.call("set_player_metadata", display_name="b", kind="ai")
            r = await blue.call(
                "create_room",
                scenario="01_tiny_skirmish",
                fog_of_war="classic",
            )
            room_id = r["room_id"]
            await red.call("join_room", room_id=room_id)
            await blue.call("set_ready", ready=True)
            await red.call("set_ready", ready=True)
            await asyncio.sleep(0.4)

            # With the default scenario's unit placement, blue can partially
            # see red (archer sight 4 reaches red knight). Assert that at
            # least one red unit IS hidden under classic fog (the one
            # outside blue's sight cone), while blue's own units are visible.
            r = await blue.call("get_state")
            assert r["ok"] is True
            gs = r["result"]
            owners = [u["owner"] for u in gs["units"]]
            assert "blue" in owners
            # At least one red unit out of the initial 2 should be hidden.
            assert owners.count("red") < 2
            # Fog annotation present.
            assert "_visible_tiles" in gs

    asyncio.run(go())


def test_none_mode_shows_full_state(server) -> None:
    url, _app = server

    async def go() -> None:
        async with (
            ServerClient.connect(url) as blue,
            ServerClient.connect(url) as red,
        ):
            await blue.call("set_player_metadata", display_name="a", kind="ai")
            await red.call("set_player_metadata", display_name="b", kind="ai")
            r = await blue.call(
                "create_room", scenario="01_tiny_skirmish", fog_of_war="none"
            )
            room_id = r["room_id"]
            await red.call("join_room", room_id=room_id)
            await blue.call("set_ready", ready=True)
            await red.call("set_ready", ready=True)
            await asyncio.sleep(0.4)

            r = await blue.call("get_state")
            assert r["ok"] is True
            gs = r["result"]
            owners = {u["owner"] for u in gs["units"]}
            assert owners == {"blue", "red"}
            # No fog annotation in none mode.
            assert "_visible_tiles" not in gs

    asyncio.run(go())
