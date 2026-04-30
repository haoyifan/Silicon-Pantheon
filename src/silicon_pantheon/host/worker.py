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
        # Preserved across reconnects so the server can rebind us to
        # existing state (seated room, in-progress match, etc.) via
        # set_player_metadata with the same cid. Populated on the
        # first _disconnect, reused by every subsequent
        # _ensure_connected.
        self._last_cid: str | None = None
        self._scenario: str = ""
        # Currently-running NetworkedAgent, if a match is in progress.
        # The runner reads ``agent.adapter_elapsed_s()`` off this to
        # surface the "llm Xs" elapsed-timer in the terminal status
        # line. Cleared between matches.
        self.agent = None
        # Set by the runner before task.cancel() to distinguish genuine
        # shutdown from spurious anyio cancel-scope CancelledErrors.
        self._shutting_down = False

    # ---- public interface ----

    async def run_forever(self) -> None:
        """Main loop — never returns unless cancelled.

        Layer 3 of transport-resilience (see
        ~/dev/transport-resilience-plan.md): the game loop races
        against the transport-dead event the client sets on
        Layer 1/2 detection. Whichever completes first triggers
        reconnect. This replaces the previous behaviour where a
        wedged MCP call could leave the worker permanently stuck
        because the un-cancellable await never returned and no
        exception path ever fired.
        """
        try:
            while True:
                try:
                    await self._ensure_connected()
                    await self._run_game_loop_with_transport_watch()
                    # One-shot workers: _game_loop returned cleanly
                    # after one completed match. Exit run_forever so
                    # the task completes and the system-test
                    # orchestrator can observe this process as "done"
                    # rather than still-running. For long-running
                    # auto-host workers (default), _game_loop is an
                    # infinite loop and never reaches this point.
                    if self.config.one_shot:
                        return
                except asyncio.CancelledError:
                    if self._shutting_down:
                        raise
                    # Spurious CancelledError from anyio cancel scope
                    # leaked through the MCP SDK (e.g. httpcore
                    # receive_response_body.failed). The cancel scope
                    # may deliver MULTIPLE CancelledErrors across
                    # successive awaits, so we must drain + shield
                    # through the entire recovery path, not just once.
                    self._drain_cancellations()
                    log.warning(
                        "worker %s caught spurious CancelledError "
                        "(anyio cancel scope) — reconnecting",
                        self.config.name,
                    )
                    self.status = "cancel-scope crash, reconnecting"
                    await self._shielded_recovery()
                except Exception as e:
                    log.exception("worker %s crashed: %s", self.config.name, e)
                    self.status = f"error: {e}"
                    await self._disconnect()
                    await asyncio.sleep(TRANSPORT_RETRY_S)
        except asyncio.CancelledError:
            log.info("worker %s shutting down (cancelled)", self.config.name)
        finally:
            await self._disconnect()

    def _drain_cancellations(self) -> None:
        """Call task.uncancel() until the cancellation counter reaches zero."""
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling() > 0:
                task.uncancel()

    async def _shielded_recovery(self) -> None:
        """Disconnect and sleep, absorbing any further spurious cancellations.

        The anyio cancel scope may deliver additional CancelledErrors
        on subsequent awaits even after the first one was drained.
        Each step re-checks _shutting_down (a genuine shutdown may
        arrive mid-recovery) and re-drains before continuing.
        """
        for step in ("disconnect", "sleep"):
            try:
                if step == "disconnect":
                    await self._disconnect()
                else:
                    await asyncio.sleep(TRANSPORT_RETRY_S)
            except asyncio.CancelledError:
                if self._shutting_down:
                    raise
                self._drain_cancellations()
                log.debug(
                    "worker %s: extra CancelledError during %s, drained",
                    self.config.name, step,
                )

    async def _run_game_loop_with_transport_watch(self) -> None:
        """Run _game_loop but bail early if the transport dies.

        The game loop can park indefinitely inside
        ``session.call_tool`` when the MCP SDK's anyio streams go
        closed silently (zombie-worker bug). The transport-dead
        event, set by Layer 1 stream monitor OR Layer 2 heartbeat
        escalation, is our one signal that we need to tear down
        and reconnect rather than keep waiting.
        """
        assert self._client is not None
        dead_event = self._client.transport_dead

        game_task = asyncio.create_task(self._game_loop())
        dead_task = asyncio.create_task(dead_event.wait())
        done: set[asyncio.Task] = set()
        try:
            done, _ = await asyncio.wait(
                {game_task, dead_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # Cancel whichever task(s) are still pending. Crucially
            # we do NOT `await t` unbounded here: if the game task
            # is parked inside ``session.call_tool`` on a dead MCP
            # stream, cancellation may never propagate (observed
            # 2026-04-22 20:49:21: Layer 1 fired, dead_task returned,
            # but `await game_task` then hung forever in the finally,
            # preventing RuntimeError("transport dead") from ever
            # being raised and the worker from reconnecting). Give
            # each task 2s to acknowledge the cancel; anything still
            # stuck gets abandoned — the outer run_forever will
            # _disconnect() the whole client next, which tears down
            # the underlying transport and GCs the orphaned task.
            CANCEL_TIMEOUT_S = 2.0
            for t in (game_task, dead_task):
                if not t.done():
                    t.cancel()
            for t in (game_task, dead_task):
                if t.done():
                    continue
                try:
                    await asyncio.wait_for(t, timeout=CANCEL_TIMEOUT_S)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    log.warning(
                        "worker %s: task %r did not cancel within %.0fs "
                        "after transport-dead; abandoning to let "
                        "_disconnect tear down the transport",
                        self.config.name, t, CANCEL_TIMEOUT_S,
                    )
                except Exception:
                    pass

        if dead_task in done:
            log.warning(
                "worker %s transport-dead event fired — "
                "forcing reconnect", self.config.name,
            )
            self.status = "transport dead, reconnecting"
            # Raise so the outer try/except in run_forever routes us
            # through _disconnect + sleep + loop (same path as any
            # other fatal error).
            raise RuntimeError("transport dead")
        # Otherwise game_task finished on its own — re-raise any
        # exception it stored.
        if game_task in done:
            exc = game_task.exception()
            if exc is not None:
                raise exc

    # ---- connection ----

    async def _disconnect(self) -> None:
        """Clean up transport context and client.

        IMPORTANT: every await in this method can raise CancelledError
        (BaseException, not Exception) when the transport is already
        dead.  We must guarantee that ``self._client`` and
        ``self._transport_ctx`` are cleared regardless, otherwise
        ``_ensure_connected`` will skip reconnection and the worker
        spins in an infinite loop on the dead socket.
        """
        # Remember our cid so the NEXT _ensure_connected can rebind
        # to the same server-side state (room seat, in-flight
        # session, etc.) instead of starting fresh. The server keys
        # every Connection by this cid; calling set_player_metadata
        # with the SAME cid re-attaches to existing state.
        if self._client is not None and self._last_cid is None:
            self._last_cid = self._client.connection_id
        try:
            if self._client is not None:
                # Leave room so the server cleans up immediately.
                try:
                    await asyncio.wait_for(
                        self._client.call("leave_room"), timeout=3.0,
                    )
                except BaseException:
                    pass
                try:
                    await self._client.stop_heartbeat()
                except BaseException:
                    pass
            if self._transport_ctx is not None:
                try:
                    await asyncio.wait_for(
                        self._transport_ctx.__aexit__(None, None, None),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "worker %s: transport __aexit__ timed out; "
                        "abandoning context (orphaned stream will be GCd)",
                        self.config.name,
                    )
                except BaseException:
                    pass
        finally:
            self._transport_ctx = None
            self._client = None

    async def _ensure_connected(self) -> None:
        if self._client is not None:
            return
        self.status = "connecting"
        from silicon_pantheon.client.transport import ServerClient

        # Pass our previous cid (if any) so the server rebinds to
        # existing state — see __init__ comment for ``_last_cid``.
        ctx = ServerClient.connect(
            self.server_url, connection_id=self._last_cid,
        )
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
        # If the connection is stuck in a stale state (IN_GAME/IN_ROOM
        # from a prior run where leave_room failed on a dead transport),
        # force leave_room to reset to IN_LOBBY. The server-side fix
        # in set_player_metadata handles the orphaned-mapping case, but
        # a belt-and-suspenders leave_room covers the case where the
        # room mapping still exists (e.g. sweeper hasn't run yet).
        returned_state = r.get("state", "")
        if returned_state not in ("in_lobby", "anonymous"):
            log.warning(
                "worker %s reconnected in stale state=%s — forcing leave_room",
                self.config.name, returned_state,
            )
            try:
                await client.call("leave_room")
            except Exception:
                pass
        await client.start_heartbeat()
        log.info("worker %s connected cid=%s", self.config.name, client.connection_id)

    # ---- game loop ----

    async def _game_loop(self) -> None:
        """Create room → ready → play → leave → repeat."""
        while True:
            try:
                await self._one_game()
                # One-shot mode: the system-test framework uses this to
                # run a bounded workload. Return cleanly after a single
                # successful match so run_forever's loop exits, the
                # worker disconnects, and the orchestrator can count
                # this process as "done" instead of treating it as a
                # still-running service. Crashes go through the normal
                # exception path below and don't terminate — the
                # retry-on-crash semantics stay useful even in one-shot.
                if self.config.one_shot:
                    log.info(
                        "worker %s one_shot completed — exiting",
                        self.config.name,
                    )
                    return
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

        # ---- pick room: create (default) or join existing ----
        # joined_team is the "blue"/"red" team the server assigned us
        # on join. For hosts it's derived from cfg.host_team as today;
        # for join_only workers it's the opposite of the host's
        # host_team read from list_rooms. _play_game reads
        # self._joined_team below.
        self._joined_team: str | None = None
        if cfg.join_only:
            # System-test joiner path: list rooms, pick any that's
            # waiting for a second player, join it. No scenario pick
            # — the host chose. Retry briefly if no match is open
            # yet (hosts are spawned concurrently by the orchestrator
            # and there can be a small race before they're listed).
            room_id, scenario, self._joined_team = await self._find_and_join_room()
            self._scenario = scenario
        else:
            scenario = self._pick_scenario()
            self._scenario = scenario
            self.status = f"creating room ({scenario})"
            log.info(
                "worker %s creating room scenario=%s", cfg.name, scenario
            )
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

        # Team resolution:
        #   - join_only workers learn their team from the server's
        #     seat assignment (see _find_and_join_room); _joined_team
        #     is set before this function is called.
        #   - Fixed-assignment hosts use their configured host_team.
        #   - Random-assignment hosts default to blue (existing behaviour).
        if self._joined_team is not None:
            my_team_str = self._joined_team
        elif cfg.team_assignment == "fixed":
            my_team_str = cfg.host_team
        else:
            my_team_str = "blue"
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
        # Fetch max_turns + effective fog mode from room state. The
        # room's fog_of_war may differ from cfg.fog_of_war if the
        # opponent reconfigured the room at join time, so we read
        # the server's view of truth rather than trusting the local
        # config. Used to override the scenario's declared fog
        # in the system prompt.
        r = await client.call("get_room_state")
        room = r.get("room", {}) if r.get("ok") else {}
        max_turns = int(room.get("max_turns", 20))
        effective_fog = room.get("fog_of_war") or cfg.fog_of_war

        if cfg.mode == "random":
            # System-test mode: no LLM, random legal action each move.
            # Skip prompt/lessons/strategy machinery entirely — none of
            # it applies. Cheap both in wall-clock and in API-credit cost.
            from silicon_pantheon.client.random_agent import RandomNetworkAgent
            agent = RandomNetworkAgent(client=client, seed=cfg.seed)
            log.info(
                "worker %s agent=random seed=%s", cfg.name, cfg.seed,
            )
        else:
            agent = NetworkedAgent(
                client=client,
                model=cfg.model,
                scenario=scenario,
                strategy=strategy_text,
                lessons_dir=lessons_dir,
                selected_lessons=selected_lessons,
                time_budget_s=float(cfg.turn_time_limit_s),
                locale=cfg.locale,
                fog_of_war=effective_fog,
            )
        # Expose so the runner's status line can read the current
        # LLM-wait elapsed time. Cleared in the finally below when
        # the match ends.
        self.agent = agent

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

    async def _find_and_join_room(self) -> tuple[str, str, str]:
        """Join an existing waiting_for_players room.

        Returns (room_id, scenario, my_team_str). ``my_team_str`` is
        "blue" or "red" — computed from the host's ``host_team`` field
        as the opposite team (since the host took seat A and we're
        taking seat B). ``_play_game`` needs this to set ``viewer``
        correctly; without it the joiner would try to play as the
        host's team, never see its own turn, and poll forever.

        Used by ``join_only`` workers in the system-test framework.
        Polls ``list_rooms`` up to ~60 s (hosts spawned in parallel by
        the orchestrator may take a beat to register). Picks the first
        room with status == waiting_for_players whose slot B is open.
        Tries ``join_room`` on it; if the server rejects (another
        joiner beat us to it — expected race), continues the scan.
        Raises ``RuntimeError`` if no joinable room appears in time.
        """
        import time as _time
        client = self._client
        assert client is not None
        self.status = "looking for room to join"
        deadline = _time.monotonic() + 60.0
        poll = 0
        while _time.monotonic() < deadline:
            poll += 1
            r = await client.call("list_rooms")
            if not r.get("ok"):
                await asyncio.sleep(1.0)
                continue
            open_rooms = [
                room for room in r.get("rooms", [])
                if room.get("status") == "waiting_for_players"
                and not room.get("seats", {}).get("b", {}).get("occupied", True)
            ]
            for room in open_rooms:
                room_id = room.get("room_id")
                if not room_id:
                    continue
                scenario = room.get("scenario", "unknown")
                host_team = room.get("host_team", "blue")
                my_team = "red" if host_team == "blue" else "blue"
                jr = await client.call("join_room", room_id=room_id)
                if jr.get("ok"):
                    log.info(
                        "worker %s joined room=%s scenario=%s team=%s "
                        "(host_team=%s, poll #%d)",
                        self.config.name, room_id[:8], scenario,
                        my_team, host_team, poll,
                    )
                    return room_id, scenario, my_team
                # Race lost (probably); try the next room.
            await asyncio.sleep(1.0)
        raise RuntimeError(
            "join_only: no joinable room appeared within 60s"
        )

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
