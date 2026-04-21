"""Single bot worker — the room lifecycle loop.

Each worker is an independent async task that:
  1. Connects to the server and declares metadata.
  2. Creates a room with the configured scenario + settings.
  3. Readies up and waits for a visitor to join + ready.
  4. Plays the game via NetworkedAgent.
  5. Summarizes the match (lesson).
  6. Leaves the room and loops back to step 2.

On provider errors (rate limit, auth), the worker waits and retries.
On transport errors, it reconnects.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import random
from pathlib import Path
from typing import Any

from silicon_pantheon.client.providers.errors import ProviderError
from silicon_pantheon.host.config import WorkerConfig

log = logging.getLogger("silicon.host.worker")

# Retry delays.
PROVIDER_RETRY_S = 30.0
TRANSPORT_RETRY_S = 10.0
POLL_INTERVAL_S = 1.0
# Number of transient ProviderError retries (TIMEOUT / RATE_LIMIT /
# OVERLOADED) before we give up on the current turn and concede.
# Backoff is 30s, 60s, 120s → total ~3.5 min of waiting, well under
# the 30-min server-side turn_time_limit default. Terminal errors
# (AUTH / BILLING / MODEL_NOT_FOUND) bypass this loop entirely.
MAX_TRANSIENT_RETRIES = 3


class BotWorker:
    """One bot that perpetually hosts games."""

    def __init__(self, worker_id: int, config: WorkerConfig, server_url: str):
        self.id = worker_id
        self.config = config
        self.server_url = server_url
        self.status = "starting"
        self.opponent: str | None = None
        self.turn_info: str = ""
        self._client = None
        self._transport_ctx = None
        self._scenario: str = ""
        # Currently-running NetworkedAgent, if a match is in progress.
        # The runner reads ``agent.adapter_elapsed_s()`` off this to
        # surface the "llm Xs" elapsed-timer in the terminal status
        # line. Cleared between matches.
        self.agent = None

    # ---- public interface ----

    async def run_forever(self) -> None:
        """Main loop — never returns unless cancelled."""
        try:
            while True:
                try:
                    await self._ensure_connected()
                    await self._game_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("worker %s crashed: %s", self.config.name, e)
                    self.status = f"error: {e}"
                    await self._disconnect()
                    await asyncio.sleep(TRANSPORT_RETRY_S)
        finally:
            await self._disconnect()

    # ---- connection ----

    async def _disconnect(self) -> None:
        """Clean up transport context and client."""
        if self._client is not None:
            # Leave room so the server cleans up immediately.
            try:
                await asyncio.wait_for(
                    self._client.call("leave_room"), timeout=3.0,
                )
            except Exception:
                pass
            try:
                await self._client.stop_heartbeat()
            except Exception:
                pass
        if self._transport_ctx is not None:
            try:
                await self._transport_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._transport_ctx = None
        self._client = None

    async def _ensure_connected(self) -> None:
        if self._client is not None:
            return
        self.status = "connecting"
        from silicon_pantheon.client.transport import ServerClient

        ctx = ServerClient.connect(self.server_url)
        client = await ctx.__aenter__()
        self._client = client
        # Store the context for cleanup.
        self._transport_ctx = ctx

        from silicon_pantheon.shared.protocol import PROTOCOL_VERSION

        r = await client.call(
            "set_player_metadata",
            display_name=self.config.name,
            kind=self.config.kind,
            provider=self.config.provider,
            model=self.config.model,
            client_protocol_version=PROTOCOL_VERSION,
        )
        if not r.get("ok"):
            raise RuntimeError(f"set_player_metadata failed: {r}")
        await client.start_heartbeat()
        log.info("worker %s connected cid=%s", self.config.name, client.connection_id)

    # ---- game loop ----

    async def _game_loop(self) -> None:
        """Create room → ready → play → leave → repeat."""
        while True:
            try:
                await self._one_game()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if isinstance(e, ProviderError):
                    # At this point the error is terminal (AUTH /
                    # BILLING / MODEL_NOT_FOUND) — transient errors
                    # (TIMEOUT / RATE_LIMIT / OVERLOADED) are retried
                    # in-place inside _play_game. Terminal errors mean
                    # the provider credentials are bad / out of credit /
                    # the model is gone — no retry will help. We still
                    # sleep PROVIDER_RETRY_S so an operator has a
                    # breather to notice / re-auth before we spin the
                    # whole connection again.
                    #
                    # leave_room (now auto-conceding on in_game) has
                    # already cleanly resolved any mid-match state by
                    # the time we reach here — no zombie room.
                    log.warning(
                        "worker %s terminal provider error: %s — "
                        "reconnecting in %.0fs (human may need to re-auth)",
                        self.config.name, e, PROVIDER_RETRY_S,
                    )
                    self.status = f"provider error, reconnect in {PROVIDER_RETRY_S:.0f}s"
                    await asyncio.sleep(PROVIDER_RETRY_S)
                    raise
                raise

    async def _one_game(self) -> None:
        # Any exception below skips the cleanup at the bottom; reset
        # per-game transient state upfront so the status line doesn't
        # leak stale opponent/turn_info from the previous iteration.
        self.opponent = None
        self.turn_info = ""

        client = self._client
        assert client is not None
        cfg = self.config

        # ---- pick scenario ----
        scenario = self._pick_scenario()
        self._scenario = scenario

        # ---- create room ----
        self.status = f"creating room ({scenario})"
        log.info("worker %s creating room scenario=%s", cfg.name, scenario)
        r = await client.call(
            "create_room",
            scenario=scenario,
            team_assignment=cfg.team_assignment,
            host_team=cfg.host_team,
            fog_of_war=cfg.fog_of_war,
            turn_time_limit_s=cfg.turn_time_limit_s,
        )
        if not r.get("ok"):
            raise RuntimeError(f"create_room failed: {r}")
        room_id = r["room_id"]

        # ---- ready up ----
        await client.call("set_ready", ready=True)
        self.status = "waiting for opponent"
        log.info("worker %s room=%s waiting", cfg.name, room_id[:8])

        # ---- wait for game start ----
        _wait_polls = 0
        while True:
            r = await client.call("get_room_state")
            if not r.get("ok"):
                err = (r.get("error") or {}).get("message", str(r))
                log.warning(
                    "worker %s get_room_state failed (poll #%d): %s",
                    cfg.name, _wait_polls, err,
                )
                raise RuntimeError(f"get_room_state failed: {err}")
            room = r.get("room", {})
            status = room.get("status")
            if status == "in_game":
                break
            # Track opponent name for status display.
            seats = room.get("seats", {})
            opp_seat = seats.get("b", {})
            opp_player = opp_seat.get("player") or {}
            if opp_player.get("display_name"):
                self.opponent = opp_player["display_name"]
                self.status = f"waiting for {self.opponent} to ready"
            _wait_polls += 1
            if _wait_polls % 60 == 0:
                log.info(
                    "worker %s still waiting in room=%s (poll #%d, status=%s)",
                    cfg.name, room_id[:8], _wait_polls, status,
                )
            await asyncio.sleep(POLL_INTERVAL_S)

        # ---- play ----
        log.info("worker %s game started scenario=%s", cfg.name, scenario)
        self.status = "playing"
        try:
            await self._play_game(room_id, scenario)
        finally:
            # Clear display state no matter how _play_game exits so a
            # subsequent provider-error / state-desync reconnect
            # doesn't show stale "vs X (turn Y)" on the status line.
            self.opponent = None
            self.turn_info = ""

        # ---- leave room ----
        self.status = "leaving room"
        try:
            await client.call("leave_room")
        except Exception:
            log.exception("worker %s leave_room failed", cfg.name)
        log.info("worker %s game finished, looping", cfg.name)

    # ---- gameplay ----

    async def _play_game(self, room_id: str, scenario: str) -> None:
        from silicon_pantheon.client.agent_bridge import NetworkedAgent
        from silicon_pantheon.server.engine.state import Team

        client = self._client
        assert client is not None
        cfg = self.config

        my_team_str = cfg.host_team if cfg.team_assignment == "fixed" else "blue"
        viewer = Team.BLUE if my_team_str == "blue" else Team.RED

        # Load strategy.
        strategy_text = None
        if cfg.strategy:
            try:
                strategy_text = Path(cfg.strategy).read_text(encoding="utf-8")
            except OSError:
                log.warning("worker %s strategy file not found: %s", cfg.name, cfg.strategy)

        # Resolve lesson files from glob patterns.
        selected_lessons = self._resolve_lessons()

        # Determine lessons_dir for saving.
        project_root = Path(__file__).resolve().parents[3]
        lessons_dir = (project_root / "lessons") if cfg.save_lessons else None

        # CRITICAL: pass the list as-is (including empty). The old
        # ``selected_lessons or None`` turned an empty list into None,
        # which NetworkedAgent._load_lessons treats as "legacy
        # auto-load up to 5 saved lessons for this scenario". That
        # silently injected saved lessons from prior runs into bot
        # prompts even when the operator had deliberately left
        # ``lessons = []`` in their TOML. Empty list here means
        # empty list in the prompt.
        agent = NetworkedAgent(
            client=client,
            model=cfg.model,
            scenario=scenario,
            strategy=strategy_text,
            lessons_dir=lessons_dir,
            selected_lessons=selected_lessons,
            time_budget_s=float(cfg.turn_time_limit_s),
            locale=cfg.locale,
        )
        # Expose so the runner's status line can read the current
        # LLM-wait elapsed time. Cleared in the finally below when
        # the match ends.
        self.agent = agent

        # Fetch max_turns from room state.
        r = await client.call("get_room_state")
        room = r.get("room", {}) if r.get("ok") else {}
        max_turns = int(room.get("max_turns", 20))

        try:
            while True:
                state = await agent._fetch_state()
                game_status = state.get("status")
                if game_status == "game_over":
                    break
                active = state.get("active_player")
                turn = state.get("turn", "?")
                self.turn_info = f"turn {turn}/{max_turns}"
                if active == viewer.value:
                    self.status = f"thinking ({self.turn_info})"
                    # Hard cap on a single turn so a hung provider /
                    # stuck MCP response can't turn into a silent
                    # multi-hour zombie. 1.5x the server-side turn
                    # limit gives the in-agent budget room to shut
                    # down gracefully before we abort.
                    turn_deadline = float(cfg.turn_time_limit_s) * 1.5
                    # Transient ProviderError retry loop. TIMEOUT /
                    # RATE_LIMIT / OVERLOADED are "try again" errors —
                    # the SDK has usually already retried 2-3x by the
                    # time it surfaces, but a bigger backoff sometimes
                    # works. Retrying here preserves the in-progress
                    # match (MCP session, agent conversation buffer,
                    # scenario cache) instead of letting the error
                    # bubble up and tear down the whole worker.
                    # Terminal errors (AUTH / BILLING / MODEL_NOT_FOUND)
                    # skip retry and bubble up immediately — leave_room
                    # will auto-concede on the way out.
                    transient_attempts = 0
                    while True:
                        try:
                            await asyncio.wait_for(
                                agent.play_turn(viewer, max_turns=max_turns),
                                timeout=turn_deadline,
                            )
                            break
                        except asyncio.TimeoutError:
                            log.error(
                                "worker %s turn timed out after %.0fs at "
                                "turn %s — conceding to unblock the room",
                                cfg.name, turn_deadline, turn,
                            )
                            try:
                                await asyncio.wait_for(
                                    client.call("concede"), timeout=5.0,
                                )
                            except Exception:
                                log.exception(
                                    "worker %s concede failed after turn timeout",
                                    cfg.name,
                                )
                            return
                        except ProviderError as e:
                            if e.is_terminal:
                                # AUTH / BILLING / MODEL_NOT_FOUND: no
                                # amount of waiting helps. Bubble up;
                                # leave_room auto-concede finishes the
                                # match, run_forever reconnects.
                                raise
                            transient_attempts += 1
                            if transient_attempts > MAX_TRANSIENT_RETRIES:
                                log.error(
                                    "worker %s exhausted %d transient "
                                    "retries on %s — conceding",
                                    cfg.name, MAX_TRANSIENT_RETRIES,
                                    e.reason.value,
                                )
                                try:
                                    await asyncio.wait_for(
                                        client.call("concede"), timeout=5.0,
                                    )
                                except Exception:
                                    log.exception(
                                        "worker %s concede after retries failed",
                                        cfg.name,
                                    )
                                return
                            # Exponential backoff: 30s, 60s, 120s.
                            backoff = 30.0 * (2 ** (transient_attempts - 1))
                            log.warning(
                                "worker %s transient provider error %s — "
                                "retry %d/%d in %.0fs (match preserved)",
                                cfg.name, e.reason.value,
                                transient_attempts, MAX_TRANSIENT_RETRIES,
                                backoff,
                            )
                            self.status = (
                                f"retry {transient_attempts}/"
                                f"{MAX_TRANSIENT_RETRIES} after "
                                f"{e.reason.value} ({self.turn_info})"
                            )
                            await asyncio.sleep(backoff)
                else:
                    self.status = f"opponent's turn ({self.turn_info})"
                    await asyncio.sleep(POLL_INTERVAL_S)
        finally:
            # Summarize and close.
            try:
                await agent.summarize_match(viewer)
            except Exception:
                log.exception("worker %s summarize_match failed", cfg.name)
            try:
                await agent.close()
            except Exception:
                pass
            # Drop the agent reference so the runner's status line
            # stops probing a finished match for LLM-wait elapsed.
            self.agent = None

    # ---- helpers ----

    _scenario_bag: list[str] = []  # shuffle bag for uniform coverage

    def _pick_scenario(self) -> str:
        scenarios = self.config.scenarios
        if scenarios == ["random"] or not scenarios:
            # Use all available scenarios. List them via the
            # games/ directory, excluding the tiny test scenario.
            project_root = Path(__file__).resolve().parents[3]
            games_dir = project_root / "games"
            all_scenarios = sorted(
                d.name for d in games_dir.iterdir()
                if d.is_dir()
                and not d.name.startswith("_")
                and d.name != "01_tiny_skirmish"
                and (d / "config.yaml").exists()
            )
            if not all_scenarios:
                return "01_tiny_skirmish"
            # Shuffle-bag: play through all scenarios before repeating
            # any. This guarantees every scenario gets picked before
            # the bag refills, giving uniform long-run coverage.
            if not self._scenario_bag:
                self._scenario_bag = list(all_scenarios)
                random.shuffle(self._scenario_bag)
            return self._scenario_bag.pop()
        return random.choice(scenarios)

    def _resolve_lessons(self) -> list[Path]:
        """Resolve glob patterns from config.lessons into file paths."""
        paths: list[Path] = []
        for pattern in self.config.lessons:
            matches = glob.glob(pattern, recursive=True)
            paths.extend(Path(m) for m in sorted(matches))
        return paths
