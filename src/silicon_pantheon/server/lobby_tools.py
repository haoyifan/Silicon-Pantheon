"""MCP lobby tool surface: list / create / preview / join / leave / ready.

Clients in state IN_LOBBY discover rooms with `list_rooms` + `preview_room`,
claim seats with `create_room` / `join_room`, and toggle readiness with
`set_ready`. The server runs a 10s auto-start countdown when both seats
are filled and both players are ready. The countdown is cancelable: any
player unreadying, leaving, or disconnecting resets it.

The countdown is implemented as an asyncio Task owned by the Room; it's
created synchronously in the lobby tool handler but runs on the event
loop FastMCP is using to serve requests, which is the same loop the
game runner will later use for per-turn scheduling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from silicon_pantheon.server.app import App, _error, _ok
from silicon_pantheon.server.engine.scenarios import load_scenario
from silicon_pantheon.server.rooms import (
    FogMode,
    Room,
    RoomConfig,
    RoomStatus,
    Slot,
    TeamAssignment,
)
from silicon_pantheon.shared.protocol import ConnectionState, ErrorCode

log = logging.getLogger("silicon.lobby")

AUTOSTART_DELAY_S = 10.0


# ---- serialization helpers ----


def _serialize_room_summary(room: Room) -> dict[str, Any]:
    """Compact row used by list_rooms / get_room_state."""
    return {
        "room_id": room.id,
        "scenario": room.config.scenario,
        "host_name": room.host_name,
        "status": room.status.value,
        "team_assignment": room.config.team_assignment,
        "host_team": room.config.host_team,
        "fog_of_war": room.config.fog_of_war,
        "max_turns": room.config.max_turns,
        "turn_time_limit_s": room.config.turn_time_limit_s,
        "seats": {
            slot.value: {
                "occupied": seat.player is not None,
                "player": seat.player.to_dict() if seat.player else None,
                "ready": seat.ready,
            }
            for slot, seat in room.seats.items()
        },
        "created_at": room.created_at,
    }


def _serialize_room_preview(room: Room) -> dict[str, Any]:
    """Room summary + scenario map for the preview screen."""
    summary = _serialize_room_summary(room)
    try:
        state = load_scenario(room.config.scenario)
    except Exception as e:  # pragma: no cover - defensive
        summary["scenario_preview"] = {"error": str(e)}
        return summary
    units = [
        {
            "id": u.id,
            "owner": u.owner.value,
            "class": u.class_,
            "pos": u.pos.to_dict(),
            "glyph": u.stats.glyph,
            "color": u.stats.color,
        }
        for u in state.units.values()
    ]
    forts = [
        {"pos": t.pos.to_dict(), "owner": t.fort_owner.value if t.fort_owner else None}
        for t in state.board.tiles.values()
        if t.is_fort
    ]
    # Enumerate every non-default tile (forts + any explicit terrain)
    # so the room MapPanel can render forests / mountains / etc. and
    # the cursor tooltip can describe what's underneath. Without this
    # the preview only shows units + fort glyphs and falls back to
    # "plain" everywhere else, even on tiles the scenario painted.
    tiles: list[dict[str, Any]] = []
    for tile in state.board.tiles.values():
        entry: dict[str, Any] = {
            "x": tile.pos.x,
            "y": tile.pos.y,
            "type": tile.type,
        }
        if tile.fort_owner is not None:
            entry["fort_owner"] = tile.fort_owner.value
        tiles.append(entry)
    summary["scenario_preview"] = {
        "width": state.board.width,
        "height": state.board.height,
        "units": units,
        "forts": forts,
        "tiles": tiles,
        "max_turns": state.max_turns,
    }
    return summary


def _enrich_win_conditions(
    win_conditions: list[dict], scenario_name: str
) -> list[dict]:
    """Attach a human description to each plugin win-rule.

    For `type: plugin` entries, resolve the function's
    scenario-supplied description (an explicit `.description`
    attribute on the function, or the first line of its docstring
    as fallback) and inject it as a `description` field on the
    returned dict. Built-in rule types (protect_unit, reach_tile,
    …) already render meaningfully from their type + kwargs alone,
    so we leave them untouched.

    Done server-side so the client never needs to import scenario
    plugin code. If the YAML already supplies a `description`
    explicitly, respect it and don't overwrite.
    """
    from silicon_pantheon.server.engine.scenarios import (
        resolve_plugin_description,
    )

    out: list[dict] = []
    for wc in win_conditions:
        if not isinstance(wc, dict):
            out.append(wc)
            continue
        enriched = dict(wc)
        if enriched.get("type") == "plugin" and "description" not in enriched:
            module = enriched.get("module", "rules")
            check_fn = enriched.get("check_fn", "")
            if module and check_fn:
                desc = resolve_plugin_description(
                    scenario_name, str(module), str(check_fn)
                )
                if desc:
                    enriched["description"] = desc
        out.append(enriched)
    return out


_BUILTIN_DESCRIPTIONS = {
    "knight": "Armored melee fighter. High HP and DEF, slow, short range.",
    "archer": "Ranged attacker. Weaker in close combat but hits from 2–3 tiles.",
    "cavalry": "Fast horse unit. Long move range; no forest or mountain.",
    "mage": "Magical unit. Uses RES instead of DEF; can heal adjacent allies.",
}


# ---- registration ----


def register_lobby_tools(mcp: FastMCP, app: App) -> None:
    """Attach the lobby tool set to a FastMCP instance."""

    @mcp.tool()
    def list_rooms(connection_id: str) -> dict:
        """List rooms currently open. Available in any post-anonymous state.

        FINISHED rooms are excluded — they're rubble waiting to be
        vacated and have no relevance to someone picking a match.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state == ConnectionState.ANONYMOUS:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_player_metadata first",
            )
        rooms = [
            _serialize_room_summary(r)
            for r in app.rooms.list()
            if r.status != RoomStatus.FINISHED
        ]
        return _ok({"rooms": rooms})

    @mcp.tool()
    def list_scenarios(connection_id: str) -> dict:
        """Enumerate scenarios available on this server.

        Walks the packaged `games/` directory and returns the
        sub-directory names that have a readable `config.yaml`. The
        client uses this to populate the 'change scenario' dropdown in
        the room screen.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state == ConnectionState.ANONYMOUS:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_player_metadata first",
            )
        from silicon_pantheon.server.engine.scenarios import _games_root

        # Use the engine's locator so server CWD doesn't matter.
        candidates: list[str] = []
        try:
            games_root = _games_root()
        except FileNotFoundError:
            return _ok({"scenarios": []})
        for sub in sorted(games_root.iterdir()):
            if sub.is_dir() and (sub / "config.yaml").is_file():
                candidates.append(sub.name)
        return _ok({"scenarios": candidates})

    @mcp.tool()
    def describe_scenario(connection_id: str, name: str) -> dict:
        """Return the full scenario bundle for UI preview.

        Includes name/description, unit class table, terrain type
        table, win conditions (as declared, not serialized rules),
        armies, board dimensions, and narrative block. The client
        caches this on room enter so the room preview and the game
        screen can show unit/terrain legends without refetching.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state == ConnectionState.ANONYMOUS:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_player_metadata first",
            )
        import yaml

        from silicon_pantheon.server.engine.scenarios import (
            _games_root,
            _is_safe_scenario_name,
        )

        if not _is_safe_scenario_name(name):
            return _error(ErrorCode.BAD_INPUT, f"unsafe scenario name: {name!r}")
        try:
            path = _games_root() / name / "config.yaml"
        except FileNotFoundError as e:
            return _error(ErrorCode.INTERNAL, str(e))
        if not path.is_file():
            return _error(ErrorCode.BAD_INPUT, f"unknown scenario: {name}")
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            return _error(ErrorCode.INTERNAL, f"scenario yaml invalid: {e}")

        # Start from built-in unit classes so the client sees the full
        # roster, not just scenario-declared overrides.
        from silicon_pantheon.server.engine.state import UnitClass
        from silicon_pantheon.server.engine.units import make_stats

        unit_classes: dict[str, dict] = {}
        for cls in UnitClass:
            s = make_stats(cls)
            unit_classes[cls.value] = {
                "hp_max": s.hp_max, "atk": s.atk, "defense": s.defense,
                "res": s.res, "spd": s.spd, "move": s.move,
                "rng_min": s.rng_min, "rng_max": s.rng_max,
                "is_magic": s.is_magic, "can_heal": s.can_heal,
                # heal_amount matters only if can_heal; included
                # unconditionally because 0 on non-healers is a fine
                # canonical answer and saves a special-case lookup.
                "heal_amount": s.heal_amount,
                # Terrain restrictions — matter for agent planning
                # (Cavalry can't enter forest, some classes can enter
                # mountain). Without these the built-in class entries
                # in describe_scenario dropped the flag entirely and
                # the agent's class catalog omitted the restriction.
                "can_enter_forest": s.can_enter_forest,
                "can_enter_mountain": s.can_enter_mountain,
                "tags": list(s.tags),
                "display_name": s.display_name or cls.value.title(),
                "description": _BUILTIN_DESCRIPTIONS.get(cls.value, ""),
            }
        for cname, spec in (cfg.get("unit_classes") or {}).items():
            unit_classes[cname] = dict(spec or {})

        # Discover ASCII art per class — same convention as
        # load_scenario uses at runtime. Done here too so the room
        # preview / scenario picker can show portraits before any
        # match starts.
        art_root = path.parent / "art"
        if art_root.is_dir():
            for entry in sorted(art_root.iterdir()):
                if entry.is_dir():
                    # Subdirectory layout: art/<class_slug>/frame1.txt
                    frames: list[str] = []
                    for f in sorted(entry.glob("*.txt")):
                        try:
                            frames.append(
                                f.read_text(encoding="utf-8").rstrip("\n")
                            )
                        except OSError:
                            continue
                    if frames:
                        unit_classes.setdefault(entry.name, {})[
                            "art_frames"
                        ] = frames
                elif entry.suffix == ".txt":
                    # Flat layout: art/<class_slug>.txt (single frame)
                    try:
                        frame = entry.read_text(encoding="utf-8").rstrip("\n")
                    except OSError:
                        continue
                    if frame:
                        unit_classes.setdefault(entry.stem, {})[
                            "art_frames"
                        ] = [frame]

        # Built-ins carry their baked-in effects so the client can show
        # what "forest" means without having to know engine internals.
        # Scenario overrides win — scenarios may redefine a built-in.
        terrain_types: dict[str, dict] = {
            "plain": {"display_name": "Plain", "move_cost": 1, "defense_bonus": 0, "res_bonus": 0,
                      "description": "Open ground. No modifiers."},
            "forest": {"display_name": "Forest", "move_cost": 2, "defense_bonus": 2, "res_bonus": 0,
                       "description": "Dense woods. +2 DEF for the occupant; costs 2 movement to enter."},
            "mountain": {"display_name": "Mountain", "move_cost": 2, "defense_bonus": 3, "res_bonus": 1,
                         "description": "Steep terrain. +3 DEF / +1 RES; most classes cannot enter."},
            "fort": {"display_name": "Fort", "move_cost": 1, "defense_bonus": 3, "res_bonus": 3, "heals": 3,
                     "description": "Fortification. +3 DEF / +3 RES; heals 3 HP to its owning team at turn start; seizing an enemy fort wins the match under default rules."},
        }
        for tname, spec in (cfg.get("terrain_types") or {}).items():
            terrain_types[tname] = dict(spec or {})

        return _ok({
            "name": cfg.get("name", name),
            "difficulty": cfg.get("difficulty", 3),
            "description": cfg.get("description", ""),
            "board": cfg.get("board", {}),
            "armies": cfg.get("armies", {}),
            "rules": cfg.get("rules", {}),
            "unit_classes": unit_classes,
            "terrain_types": terrain_types,
            "win_conditions": _enrich_win_conditions(
                cfg.get("win_conditions") or [], name
            ),
            "narrative": cfg.get("narrative") or {},
        })

    # Server-side scenario bundle cache. Built once on first call,
    # held in memory for the process lifetime. Scenarios don't change
    # at runtime (they're config files on disk), so recomputing on
    # every request is wasteful. Restart the server to pick up changes.
    _bundle_cache: dict[str, Any] = {}

    @mcp.tool()
    def get_scenario_bundle(
        connection_id: str,
        cached_hash: str | None = None,
    ) -> dict:
        """Return ALL scenario descriptions in a single response.

        The bundle includes every scenario's full describe_scenario
        output plus a content hash. The client caches the bundle
        locally; on the next login it sends `cached_hash` — if it
        matches, the server returns {ok, match: true} (no data
        transfer). If it doesn't match (scenarios changed), the
        full bundle is returned.

        This replaces 30+ sequential describe_scenario calls with
        one round-trip (~200ms vs ~7s).
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state == ConnectionState.ANONYMOUS:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_player_metadata first",
            )

        # Return from server-side cache if available. The bundle is
        # built once and held in memory — scenarios are static config
        # files that don't change at runtime.
        if _bundle_cache:
            if cached_hash and cached_hash == _bundle_cache.get("hash"):
                return _ok({"match": True, "hash": _bundle_cache["hash"]})
            return _ok({
                "match": False,
                "hash": _bundle_cache["hash"],
                "scenarios": _bundle_cache["scenarios"],
            })

        import hashlib
        import json as _json

        from silicon_pantheon.server.engine.scenarios import (
            _games_root,
            _is_safe_scenario_name,
        )

        try:
            games_root = _games_root()
        except FileNotFoundError:
            return _ok({"scenarios": {}, "hash": ""})

        import yaml

        scenarios: dict[str, dict] = {}
        for sub in sorted(games_root.iterdir()):
            if not sub.is_dir() or not (sub / "config.yaml").is_file():
                continue
            name = sub.name
            # Reuse describe_scenario's logic inline.
            try:
                cfg = yaml.safe_load(
                    (sub / "config.yaml").read_text(encoding="utf-8")
                ) or {}
            except Exception:
                continue

            from silicon_pantheon.server.engine.state import UnitClass
            from silicon_pantheon.server.engine.units import make_stats

            unit_classes: dict[str, dict] = {}
            for cls in UnitClass:
                s = make_stats(cls)
                unit_classes[cls.value] = {
                    "hp_max": s.hp_max, "atk": s.atk, "defense": s.defense,
                    "res": s.res, "spd": s.spd, "move": s.move,
                    "rng_min": s.rng_min, "rng_max": s.rng_max,
                    "is_magic": s.is_magic, "can_heal": s.can_heal,
                    "heal_amount": s.heal_amount,
                    "can_enter_forest": s.can_enter_forest,
                    "can_enter_mountain": s.can_enter_mountain,
                    "tags": list(s.tags),
                    "glyph": s.glyph, "color": s.color,
                    "display_name": s.display_name,
                    "description": s.description,
                }
            for cls_name, cls_data in (cfg.get("unit_classes") or {}).items():
                if isinstance(cls_data, dict):
                    unit_classes[cls_name] = {
                        **unit_classes.get(cls_name, {}),
                        **cls_data,
                    }

            terrain_types: dict[str, dict] = {
                "plain": {"display_name": "Plain", "move_cost": 1},
                "forest": {"display_name": "Forest", "move_cost": 2, "defense_bonus": 2},
                "mountain": {"display_name": "Mountain", "move_cost": 2, "defense_bonus": 3, "res_bonus": 1},
                "fort": {"display_name": "Fort", "move_cost": 1, "defense_bonus": 3, "res_bonus": 3, "heals": 3},
            }
            for tname, spec in (cfg.get("terrain_types") or {}).items():
                terrain_types[tname] = dict(spec or {})

            scenarios[name] = {
                "name": cfg.get("name", name),
                "difficulty": cfg.get("difficulty", 3),
                "description": cfg.get("description", ""),
                "board": cfg.get("board", {}),
                "armies": cfg.get("armies", {}),
                "rules": cfg.get("rules", {}),
                "unit_classes": unit_classes,
                "terrain_types": terrain_types,
                "win_conditions": _enrich_win_conditions(
                    cfg.get("win_conditions") or [], name
                ),
                "scenario_slug": name,
            }

        # Compute content hash from sorted JSON and cache for future calls.
        content = _json.dumps(scenarios, sort_keys=True, default=str)
        bundle_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        _bundle_cache["hash"] = bundle_hash
        _bundle_cache["scenarios"] = scenarios
        log.info(
            "scenario bundle built: %d scenarios, hash=%s, size=%dKB",
            len(scenarios), bundle_hash, len(content) // 1024,
        )

        if cached_hash and cached_hash == bundle_hash:
            return _ok({"match": True, "hash": bundle_hash})

        return _ok({"match": False, "hash": bundle_hash, "scenarios": scenarios})

    @mcp.tool()
    def get_leaderboard(connection_id: str) -> dict:
        """Return aggregated leaderboard stats per model.

        Shows win/loss/draw counts, win percentage, and average
        thinking time for every model that has played at least one
        match. Sorted by win rate descending.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state == ConnectionState.ANONYMOUS:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_player_metadata first",
            )
        from silicon_pantheon.server.leaderboard import query_leaderboard

        return _ok({"leaderboard": query_leaderboard()})

    @mcp.tool()
    async def update_room_config(
        connection_id: str,
        scenario: str | None = None,
        team_assignment: str | None = None,
        host_team: str | None = None,
        fog_of_war: str | None = None,
        max_turns: int | None = None,
        turn_time_limit_s: int | None = None,
    ) -> dict:
        """Host-only: tweak room config while still in the lobby.

        Only fields passed (non-None) are updated. Any change resets
        both seats' ready flags — if readiness was previously agreed
        upon, the config shift might change the deal. Fails outside
        the pre-game states (COUNTING_DOWN, IN_GAME, FINISHED).
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_ROOM:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "update_room_config requires state=in_room",
            )
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, slot = info
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"room {room_id} vanished")
        if slot != Slot.A:
            return _error(
                ErrorCode.BAD_INPUT, "only the host (slot A) can update room config"
            )
        if room.status not in (
            RoomStatus.WAITING_FOR_PLAYERS,
            RoomStatus.WAITING_READY,
        ):
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                f"room is in {room.status.value}; config locked",
            )

        # Validate every proposed field before mutating so we don't
        # end up with a partial update on reject.
        scenario_state = None
        if scenario is not None:
            try:
                scenario_state = load_scenario(scenario)
            except Exception as e:
                return _error(ErrorCode.BAD_INPUT, f"scenario load failed: {e}")
        if team_assignment is not None and team_assignment not in ("fixed", "random"):
            return _error(
                ErrorCode.BAD_INPUT, "team_assignment must be 'fixed' or 'random'"
            )
        if host_team is not None and host_team not in ("blue", "red"):
            return _error(ErrorCode.BAD_INPUT, "host_team must be 'blue' or 'red'")
        if fog_of_war is not None and fog_of_war not in (
            "none",
            "classic",
            "line_of_sight",
        ):
            return _error(
                ErrorCode.BAD_INPUT,
                "fog_of_war must be 'none' | 'classic' | 'line_of_sight'",
            )

        if scenario is not None:
            room.config.scenario = scenario
            # Switching scenario implicitly resets max_turns to the new
            # scenario's declared cap unless the host overrides it in
            # the same call. Otherwise a 10-turn scenario inherits the
            # 20-turn cap from the previous selection and the win
            # plugin's "turn budget" check fires too late.
            if max_turns is None and scenario_state is not None:
                room.config.max_turns = scenario_state.max_turns
        if team_assignment is not None:
            room.config.team_assignment = team_assignment  # type: ignore[assignment]
        if host_team is not None:
            room.config.host_team = host_team  # type: ignore[assignment]
        if fog_of_war is not None:
            room.config.fog_of_war = fog_of_war  # type: ignore[assignment]
        if max_turns is not None:
            if max_turns < 1 or max_turns > 200:
                return _error(
                    ErrorCode.BAD_INPUT, "max_turns must be between 1 and 200"
                )
            room.config.max_turns = max_turns
        if turn_time_limit_s is not None:
            if turn_time_limit_s < 10 or turn_time_limit_s > 3600:
                return _error(
                    ErrorCode.BAD_INPUT,
                    "turn_time_limit_s must be between 10 and 3600",
                )
            room.config.turn_time_limit_s = turn_time_limit_s

        # Config change resets readiness so both sides explicitly
        # re-agree on the new terms.
        for seat in room.seats.values():
            seat.ready = False
        room.recompute_status()
        log.info(
            "update_room_config: room=%s scenario=%s fog=%s teams=%s host_team=%s",
            room_id,
            room.config.scenario,
            room.config.fog_of_war,
            room.config.team_assignment,
            room.config.host_team,
        )
        return _ok({})

    @mcp.tool()
    def preview_room(connection_id: str, room_id: str) -> dict:
        """Show scenario map + seat occupancy for a room."""
        conn = app.get_connection(connection_id)
        if conn is None or conn.state == ConnectionState.ANONYMOUS:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_player_metadata first",
            )
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"no such room: {room_id}")
        return _ok({"room": _serialize_room_preview(room)})

    @mcp.tool()
    def create_room(
        connection_id: str,
        scenario: str,
        max_turns: int | None = None,
        team_assignment: str = "fixed",
        host_team: str = "blue",
        fog_of_war: str = "none",
        turn_time_limit_s: int = 1800,
    ) -> dict:
        """Create a new room, seating the caller in slot A as the host.

        Transitions the caller from IN_LOBBY to IN_ROOM. Fails if the
        caller isn't IN_LOBBY or already has a room, if the scenario
        doesn't load, or if the config fields don't validate.

        If `max_turns` is not provided, defaults to whatever the
        scenario declares in its YAML rules block. Hormuz declares
        max_turns: 10 (its win plugin red-wins the moment turn > 10
        without blue meeting the compound objective). Ignoring that
        and forcing 20 — the previous default — meant the timeout
        forfeit fired at turn 21 instead of 11, leaving matches
        running far past the scenario author's intent.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_LOBBY:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "create_room requires state=in_lobby",
            )
        if conn.player is None:
            return _error(ErrorCode.BAD_INPUT, "set_player_metadata first")
        if team_assignment not in ("fixed", "random"):
            return _error(
                ErrorCode.BAD_INPUT,
                "team_assignment must be 'fixed' or 'random'",
            )
        if host_team not in ("blue", "red"):
            return _error(ErrorCode.BAD_INPUT, "host_team must be 'blue' or 'red'")
        if fog_of_war not in ("none", "classic", "line_of_sight"):
            return _error(
                ErrorCode.BAD_INPUT,
                "fog_of_war must be 'none' | 'classic' | 'line_of_sight'",
            )
        # Match the range update_room_config accepts so an attacker
        # can't create a room with turn_time_limit_s=-1 or an absurd
        # 10h value. Same bounds in both places so a client that
        # validates-before-calling uses one set of rules.
        if turn_time_limit_s < 10 or turn_time_limit_s > 3600:
            return _error(
                ErrorCode.BAD_INPUT,
                "turn_time_limit_s must be between 10 and 3600",
            )
        try:
            scenario_state = load_scenario(scenario)
        except Exception as e:
            return _error(ErrorCode.BAD_INPUT, f"scenario load failed: {e}")
        if max_turns is None:
            # Honor the scenario's declared cap; the scenario author
            # tuned win conditions / pacing around this number.
            max_turns = scenario_state.max_turns
        config = RoomConfig(
            scenario=scenario,
            max_turns=max_turns,
            team_assignment=team_assignment,  # type: ignore[arg-type]
            host_team=host_team,  # type: ignore[arg-type]
            fog_of_war=fog_of_war,  # type: ignore[arg-type]
            turn_time_limit_s=turn_time_limit_s,
        )
        room, slot = app.rooms.create(config=config, host=conn.player)
        app.conn_to_room[connection_id] = (room.id, slot)
        conn.state = ConnectionState.IN_ROOM
        # Track when this player joined for unready timeout.
        import time as _time
        from silicon_pantheon.server.heartbeat import HeartbeatState
        app.heartbeat_state[connection_id] = HeartbeatState(joined_room_at=_time.time())
        return _ok({"room_id": room.id, "slot": slot.value})

    @mcp.tool()
    def join_room(connection_id: str, room_id: str) -> dict:
        """Take the open seat in an existing room."""
        if not room_id or len(room_id) > 128:
            return _error(ErrorCode.BAD_INPUT, "invalid room_id")
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_LOBBY:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "join_room requires state=in_lobby",
            )
        if conn.player is None:
            return _error(ErrorCode.BAD_INPUT, "set_player_metadata first")
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"no such room: {room_id}")
        result = app.rooms.join(room_id, conn.player)
        if result is None:
            return _error(ErrorCode.ROOM_FULL, "room is full")
        _, slot = result
        app.conn_to_room[connection_id] = (room_id, slot)
        conn.state = ConnectionState.IN_ROOM
        import time as _time
        from silicon_pantheon.server.heartbeat import HeartbeatState
        app.heartbeat_state[connection_id] = HeartbeatState(joined_room_at=_time.time())
        return _ok({"room_id": room_id, "slot": slot.value})

    @mcp.tool()
    def kick_player(connection_id: str) -> dict:
        """Host-only: kick the joiner (slot B) from the room.

        Only works pre-game (WAITING_FOR_PLAYERS, WAITING_READY).
        The kicked player's connection returns to IN_LOBBY.
        Cannot be used during gameplay.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_ROOM:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "kick_player requires state=in_room",
            )
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, slot = info
        if slot != Slot.A:
            return _error(ErrorCode.BAD_INPUT, "only the host (slot A) can kick")
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"room {room_id} vanished")
        from silicon_pantheon.server.rooms import RoomStatus
        if room.status in (RoomStatus.IN_GAME, RoomStatus.FINISHED):
            return _error(ErrorCode.BAD_INPUT, "cannot kick during gameplay")
        # Find the joiner's connection.
        joiner_cid = None
        for cid, (rid, s) in app.conn_to_room.items():
            if rid == room_id and s == Slot.B:
                joiner_cid = cid
                break
        if joiner_cid is None:
            return _error(ErrorCode.BAD_INPUT, "no player to kick (seat B is empty)")
        # Remove joiner from room, cancel countdown, revert state.
        _cancel_countdown(app, room_id)
        app.rooms.leave(room_id, Slot.B)
        app.conn_to_room.pop(joiner_cid, None)
        joiner_conn = app.get_connection(joiner_cid)
        if joiner_conn is not None:
            joiner_conn.state = ConnectionState.IN_LOBBY
        app.heartbeat_state.pop(joiner_cid, None)
        room.recompute_status()
        log.info("kick_player: host=%s kicked=%s room=%s", connection_id[:8], joiner_cid[:8] if joiner_cid else "?", room_id)
        return _ok({"kicked": joiner_cid[:8] if joiner_cid else None})

    @mcp.tool()
    async def leave_room(connection_id: str) -> dict:
        """Vacate this connection's seat and return the caller to the lobby.

        Accepts from IN_ROOM (pre-game) OR IN_GAME (mid-match or
        post-match). Mid-match departures are treated as a hard exit —
        the opponent will auto-concede via the heartbeat sweeper if
        they don't press anything. Post-match departures are the
        normal 'back to lobby' flow.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state not in (
            ConnectionState.IN_ROOM,
            ConnectionState.IN_GAME,
        ):
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "leave_room requires state=in_room or in_game",
            )
        info = app.conn_to_room.pop(connection_id, None)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, slot = info
        _cancel_countdown(app, room_id)

        # If the room is pre-game, evict the OTHER player too so they
        # don't sit in a dead room. Their next get_room_state poll will
        # fail and transition them to lobby.
        room = app.rooms.get(room_id)
        if room is not None and room.status in (
            RoomStatus.WAITING_FOR_PLAYERS,
            RoomStatus.WAITING_READY,
            RoomStatus.COUNTING_DOWN,
        ):
            other_slot = Slot.B if slot == Slot.A else Slot.A
            for cid, (rid, s) in list(app.conn_to_room.items()):
                if rid == room_id and s == other_slot:
                    other_conn = app.get_connection(cid)
                    if other_conn is not None:
                        other_conn.state = ConnectionState.IN_LOBBY
                    app.conn_to_room.pop(cid, None)
                    log.info(
                        "leave_room: evicted other player %s from room %s",
                        cid[:8], room_id,
                    )
                    break

        app.rooms.leave(room_id, slot)
        conn.state = ConnectionState.IN_LOBBY
        # If the leave deleted the room (post-match + last player
        # out, or pre-game after evicting the other player), clean up.
        if app.rooms.get(room_id) is None:
            app.sessions.pop(room_id, None)
            app.slot_to_team.pop(room_id, None)
            log.info("room %s fully cleaned up", room_id)
        else:
            # Room still exists (other player's seat was already vacated
            # above but room deletion needs both seats empty). Force
            # delete for pre-game rooms.
            if room is not None and room.status in (
                RoomStatus.WAITING_FOR_PLAYERS,
                RoomStatus.WAITING_READY,
                RoomStatus.COUNTING_DOWN,
            ):
                app.rooms.delete(room_id)
                app.sessions.pop(room_id, None)
                app.slot_to_team.pop(room_id, None)
                log.info("room %s deleted (player left pre-game)", room_id)
        return _ok({})

    @mcp.tool()
    async def get_room_state(connection_id: str) -> dict:
        """Show the caller's current room, seats, readiness, and countdown."""
        import time as _time
        _t0 = _time.monotonic()
        log.debug(
            "get_room_state ENTER cid=%s", connection_id[:8],
        )
        conn = app.get_connection(connection_id)
        if conn is None or conn.state not in (
            ConnectionState.IN_ROOM,
            ConnectionState.IN_GAME,
        ):
            log.debug(
                "get_room_state REJECT cid=%s state=%s dt=%.3fs",
                connection_id[:8],
                conn.state.value if conn else "none",
                _time.monotonic() - _t0,
            )
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "get_room_state requires state=in_room or in_game",
            )
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, _slot = info
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"room {room_id} vanished")

        # Belt-and-suspenders: if we're polled after the deadline has
        # passed but the background _run_countdown task hasn't fired
        # (FastMCP dispatch / event-loop context quirks have been known
        # to drop the task), promote the room inline. At worst a client
        # observes the transition one poll later than it would have.
        _maybe_promote_on_deadline(app, room_id)

        # Re-read the room after potential inline promotion.
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"room {room_id} vanished")
        summary = _serialize_room_summary(room)
        countdown = app.autostart_deadlines.get(room_id)
        if countdown is not None:
            summary["autostart_in_s"] = max(0.0, countdown - _time.time())
        dt = _time.monotonic() - _t0
        if dt > 1.0:
            log.warning(
                "get_room_state SLOW cid=%s room=%s dt=%.2fs status=%s",
                connection_id[:8], room_id[:8], dt, summary.get("status"),
            )
        log.debug(
            "get_room_state OK cid=%s room=%s dt=%.3fs status=%s",
            connection_id[:8], room_id[:8], dt, summary.get("status"),
        )
        return _ok({"room": summary})

    @mcp.tool()
    async def set_ready(connection_id: str, ready: bool) -> dict:
        """Toggle this connection's readiness.

        When both seats are filled and both ready, the server starts a
        10s countdown after which the match begins. The countdown is
        cancelled if either player unreadies, leaves, or disconnects.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_ROOM:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_ready requires state=in_room",
            )
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, slot = info
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"room {room_id} vanished")
        seat = room.seats[slot]
        seat.ready = ready

        log.info(
            "set_ready: room=%s cid=%s slot=%s ready=%s all_ready=%s seats=%s",
            room_id,
            connection_id[:8],
            slot.value,
            ready,
            room.all_ready(),
            {s.value: (room.seats[s].ready, room.seats[s].player is not None) for s in room.seats},
        )

        response: dict[str, Any] = {}
        if room.all_ready():
            _start_countdown(app, room_id)
            deadline = app.autostart_deadlines.get(room_id)
            if deadline is not None:
                import time

                response["autostart_in_s"] = max(0.0, deadline - time.time())
            room.status = RoomStatus.COUNTING_DOWN
        else:
            _cancel_countdown(app, room_id)
            room.recompute_status()
        return _ok(response)


# ---- countdown machinery ----


def _start_countdown(app: App, room_id: str) -> None:
    """(Re)start a 10s autostart countdown for this room."""
    import time

    _cancel_countdown(app, room_id)
    deadline = time.time() + AUTOSTART_DELAY_S
    app.autostart_deadlines[room_id] = deadline
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    log.info(
        "start_countdown: room=%s deadline=%.2f delay=%.2f loop=%s",
        room_id,
        deadline,
        AUTOSTART_DELAY_S,
        loop,
    )
    try:
        task = asyncio.create_task(_run_countdown(app, room_id))
    except Exception as e:
        log.exception("start_countdown: create_task failed: %s", e)
        return
    app.autostart_tasks[room_id] = task


def _cancel_countdown(app: App, room_id: str) -> None:
    task = app.autostart_tasks.pop(room_id, None)
    if task is not None and not task.done():
        log.info("cancel_countdown: room=%s (task was running)", room_id)
        task.cancel()
    elif room_id in app.autostart_deadlines:
        log.info("cancel_countdown: room=%s (no live task)", room_id)
    app.autostart_deadlines.pop(room_id, None)


async def _run_countdown(app: App, room_id: str) -> None:
    log.info("run_countdown: room=%s sleeping %.2fs", room_id, AUTOSTART_DELAY_S)
    try:
        await asyncio.sleep(AUTOSTART_DELAY_S)
    except asyncio.CancelledError:
        log.info("run_countdown: room=%s cancelled", room_id)
        return
    log.info("run_countdown: room=%s slept through; checking room", room_id)
    room = app.rooms.get(room_id)
    if room is None or not room.all_ready():
        log.info(
            "run_countdown: room=%s not promoting (room=%s all_ready=%s)",
            room_id,
            room,
            room and room.all_ready(),
        )
        return
    if app.on_countdown_complete is not None:
        log.info("run_countdown: room=%s firing on_countdown_complete", room_id)
        app.on_countdown_complete(room_id)
    else:
        log.warning("run_countdown: room=%s on_countdown_complete is None", room_id)


def _maybe_promote_on_deadline(app: App, room_id: str) -> None:
    """Synchronous safety net: if the autostart deadline has passed,
    promote the room even if the background _run_countdown task didn't
    fire. Called from the polling-read path so every client's next poll
    (≤1s cadence) will observe the promotion.
    """
    import time

    deadline = app.autostart_deadlines.get(room_id)
    if deadline is None:
        return
    now = time.time()
    if now < deadline:
        return
    room = app.rooms.get(room_id)
    if room is None or not room.all_ready():
        log.info(
            "maybe_promote: room=%s deadline passed but not all ready; clearing",
            room_id,
        )
        app.autostart_deadlines.pop(room_id, None)
        return
    log.info(
        "maybe_promote: room=%s fallback firing (deadline %.2fs ago)",
        room_id,
        now - deadline,
    )
    # Clear deadline so we don't double-promote.
    app.autostart_deadlines.pop(room_id, None)
    task = app.autostart_tasks.pop(room_id, None)
    if task is not None and not task.done():
        task.cancel()
    if app.on_countdown_complete is not None:
        app.on_countdown_complete(room_id)
