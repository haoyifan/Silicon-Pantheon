"""End-to-end smoke test for the network random-action agent.

Two RandomNetworkAgent instances play a full match against each
other over the MCP+SSE transport. Proves:
  - Agent interface matches what BotWorker._play_game expects
  - Legal-action selection + tool-calling works over the wire
  - A match actually reaches game_over in a bounded number of turns
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn

from silicon_pantheon.client.random_agent import RandomNetworkAgent
from silicon_pantheon.client.transport import ServerClient
from silicon_pantheon.server.app import App, build_mcp_server
from silicon_pantheon.server.engine.state import Team


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
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
    if not srv.started:
        raise RuntimeError("uvicorn failed to start")
    try:
        yield f"http://127.0.0.1:{port}/mcp/", app
    finally:
        srv.should_exit = True
        thread.join(timeout=5.0)


def test_two_random_agents_play_full_match(server) -> None:
    """Two RandomNetworkAgents play a full match to game_over."""
    url, _app = server

    async def go() -> None:
        async with (
            ServerClient.connect(url) as blue,
            ServerClient.connect(url) as red,
        ):
            await blue.call(
                "set_player_metadata", display_name="rb", kind="ai"
            )
            await red.call(
                "set_player_metadata", display_name="rr", kind="ai"
            )

            r = await blue.call(
                "create_dev_game", scenario="01_tiny_skirmish"
            )
            assert r["ok"], r
            r = await red.call("join_dev_game")
            assert r["ok"], r

            blue_agent = RandomNetworkAgent(client=blue, seed=42)
            red_agent = RandomNetworkAgent(client=red, seed=43)

            # Play up to 60 turn-exchanges. 01_tiny_skirmish usually
            # resolves in 5-15 turns under random play; 60 is a
            # generous ceiling so a pathological sequence doesn't hang
            # the test forever.
            for _ in range(60):
                state = await blue_agent._fetch_state()
                status = state.get("status")
                if status == "game_over":
                    return
                active = state.get("active_player")
                if active == "blue":
                    await blue_agent.play_turn(Team.BLUE, max_turns=20)
                elif active == "red":
                    await red_agent.play_turn(Team.RED, max_turns=20)
                else:
                    raise RuntimeError(
                        f"unexpected active_player={active}"
                    )

            # If we get here, the match didn't end within 60 turns.
            # That's a red flag — something about action selection
            # isn't producing progress.
            state = await blue_agent._fetch_state()
            raise AssertionError(
                f"random-vs-random match did not end in 60 turns; "
                f"final state status={state.get('status')} "
                f"turn={state.get('turn')}"
            )

    asyncio.run(go())
