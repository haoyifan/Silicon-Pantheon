"""Load game scenarios from games/<name>/config.yaml into a GameState."""

from __future__ import annotations

import uuid
from pathlib import Path

import yaml

from .state import (
    Board,
    GameState,
    Pos,
    Team,
    TerrainType,
    Tile,
    Unit,
    UnitClass,
    UnitStats,
    UnitStatus,
)
from .units import CLASS_STATS, make_stats


def _copy_stats(src: UnitStats) -> UnitStats:
    """Shallow-deep clone so per-unit mutations (future inventory, MP
    drain, etc.) don't leak back into the scenario's class table."""
    return UnitStats(
        hp_max=src.hp_max,
        atk=src.atk,
        defense=src.defense,
        res=src.res,
        spd=src.spd,
        rng_min=src.rng_min,
        rng_max=src.rng_max,
        move=src.move,
        is_magic=src.is_magic,
        can_enter_forest=src.can_enter_forest,
        can_enter_mountain=src.can_enter_mountain,
        can_heal=src.can_heal,
        heal_amount=src.heal_amount,
        sight=src.sight,
        tags=list(src.tags),
        mp_max=src.mp_max,
        mp_per_turn=src.mp_per_turn,
        abilities=list(src.abilities),
        default_inventory=list(src.default_inventory),
        damage_profile=dict(src.damage_profile),
        defense_profile=dict(src.defense_profile),
        bonus_vs_tags=[dict(b) for b in src.bonus_vs_tags],
        vulnerability_to_tags=[dict(v) for v in src.vulnerability_to_tags],
    )


def _build_unit_stats(name: str, spec: dict) -> UnitStats:
    """Construct a UnitStats from a scenario YAML unit_classes entry.

    All fields optional. Core combat stats (hp_max / atk / defense /
    res / spd / rng_min / rng_max / move) default to sensible baseline
    values if omitted. Reserved v2 fields default to empty / zero.
    """
    s = spec or {}
    return UnitStats(
        hp_max=int(s.get("hp_max", 20)),
        atk=int(s.get("atk", 5)),
        defense=int(s.get("defense", 3)),
        res=int(s.get("res", 3)),
        spd=int(s.get("spd", 4)),
        rng_min=int(s.get("rng_min", 1)),
        rng_max=int(s.get("rng_max", 1)),
        move=int(s.get("move", 4)),
        is_magic=bool(s.get("is_magic", False)),
        can_enter_forest=bool(s.get("can_enter_forest", True)),
        can_enter_mountain=bool(s.get("can_enter_mountain", False)),
        can_heal=bool(s.get("can_heal", False)),
        heal_amount=int(s.get("heal_amount", 0)),
        sight=int(s.get("sight", 3)),
        tags=list(s.get("tags") or []),
        mp_max=int(s.get("mp_max", 0)),
        mp_per_turn=int(s.get("mp_per_turn", 0)),
        abilities=list(s.get("abilities") or []),
        default_inventory=list(s.get("default_inventory") or []),
        damage_profile=dict(s.get("damage_profile") or {}),
        defense_profile=dict(s.get("defense_profile") or {}),
        bonus_vs_tags=[dict(b) for b in (s.get("bonus_vs_tags") or [])],
        vulnerability_to_tags=[
            dict(v) for v in (s.get("vulnerability_to_tags") or [])
        ],
    )


def _games_root() -> Path:
    """Find the repo-level games/ directory.

    Walks up from this file until it finds a sibling `games/` folder.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "games"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("Could not locate games/ directory")


def load_scenario(name: str) -> GameState:
    """Load a scenario by folder name (e.g. '01_tiny_skirmish').

    If the scenario directory contains a `rules.py`, load it as a
    plugin module and expose its public callables on the resulting
    state's `_plugin_namespace`. This runs at scenario-load time so
    import errors surface immediately, not mid-match.
    """
    scenario_dir = _games_root() / name
    config_path = scenario_dir / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"No scenario at {config_path}")
    with config_path.open() as f:
        cfg = yaml.safe_load(f)
    state = build_state(cfg)
    plugin_path = scenario_dir / "rules.py"
    if plugin_path.exists():
        state._plugin_namespace = _load_plugin(plugin_path, name)
    else:
        state._plugin_namespace = {}
    return state


def _load_plugin(path: Path, scenario_name: str) -> dict:
    """Load a scenario's rules.py as an isolated module.

    Returns its public namespace (names not starting with underscore).
    Operator-trusted: no sandbox, full Python. Failures here bubble up
    to the caller so broken scenarios never enter play.
    """
    import importlib.util

    mod_name = f"clash_of_odin._scenario_plugins.{scenario_name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load scenario plugin at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        name: getattr(module, name)
        for name in dir(module)
        if not name.startswith("_")
    }


SUPPORTED_SCHEMA_VERSION = 1


class UnsupportedSchemaVersion(ValueError):
    """Raised when a scenario YAML declares a schema_version the engine
    doesn't understand."""


def build_state(cfg: dict) -> GameState:
    # Schema version gate. Missing = v1 (legacy). Anything newer refuses
    # to load so we don't silently misinterpret future fields.
    schema_version = int(cfg.get("schema_version", 1))
    if schema_version > SUPPORTED_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"scenario declares schema_version={schema_version}; "
            f"this engine supports up to {SUPPORTED_SCHEMA_VERSION}. "
            "Upgrade the server."
        )

    board_cfg = cfg["board"]
    width = int(board_cfg["width"])
    height = int(board_cfg["height"])

    # Per-scenario terrain type table. Built-ins are always present
    # with legacy effects; scenario YAML can override a built-in or
    # introduce a new name.
    terrain_types: dict[str, dict] = {
        "plain": {},
        "forest": {},
        "mountain": {},
        "fort": {},
    }
    for name, spec in (cfg.get("terrain_types") or {}).items():
        terrain_types[name] = dict(spec or {})

    def _make_tile(pos: Pos, type_name: str, fort_owner: Team | None = None) -> Tile:
        spec = terrain_types.get(type_name, {})
        return Tile(
            pos=pos,
            type=type_name,
            fort_owner=fort_owner,
            _move_cost=spec.get("move_cost"),
            _defense_bonus=spec.get("defense_bonus"),
            _magic_bonus=spec.get("magic_bonus"),
            heals=int(spec.get("heals", 0)),
            blocks_sight=bool(spec.get("blocks_sight", False)),
            passable=bool(spec.get("passable", True)),
            class_overrides=dict(spec.get("class_overrides") or {}),
            glyph=spec.get("glyph"),
            color=spec.get("color"),
            effects_plugin=spec.get("effects_plugin"),
        )

    tiles: dict[Pos, Tile] = {}
    for t in board_cfg.get("terrain", []) or []:
        pos = Pos(int(t["x"]), int(t["y"]))
        tiles[pos] = _make_tile(pos, str(t["type"]))

    # Forts overlay: they become FORT tiles with a fort_owner.
    for f in board_cfg.get("forts", []) or []:
        pos = Pos(int(f["x"]), int(f["y"]))
        tiles[pos] = _make_tile(
            pos, TerrainType.FORT.value, fort_owner=Team(f["owner"])
        )

    board = Board(width=width, height=height, tiles=tiles)

    # Resolve the per-scenario class table: start from built-ins, then
    # layer on any `unit_classes:` block from the YAML. Custom classes
    # can override a built-in or introduce a brand-new name.
    class_table: dict[str, UnitStats] = {
        cls.value: make_stats(cls) for cls in UnitClass
    }
    for name, spec in (cfg.get("unit_classes") or {}).items():
        class_table[name] = _build_unit_stats(name, spec)

    units: dict[str, Unit] = {}
    rules = cfg.get("rules", {})
    max_turns = int(rules.get("max_turns", 30))
    first_player = Team(rules.get("first_player", "blue"))

    for team_name in ("blue", "red"):
        team = Team(team_name)
        # Non-first-player units start DONE so the table's
        # ready/moved/done column consistently means "can this unit act
        # right now?". The first player's next end_turn resets them to
        # READY as the normal turn-transition rule. Without this, both
        # teams look interchangeable at turn 0 even though only one
        # actually has agency.
        initial_status = (
            UnitStatus.READY if team is first_player else UnitStatus.DONE
        )
        per_class: dict[str, int] = {}
        for u in cfg["armies"].get(team_name, []):
            class_name = str(u["class"])
            if class_name not in class_table:
                raise ValueError(
                    f"army references unknown unit class {class_name!r}; "
                    f"add it to unit_classes or use a built-in"
                )
            stats = _copy_stats(class_table[class_name])
            pos = Pos(int(u["pos"]["x"]), int(u["pos"]["y"]))
            per_class[class_name] = per_class.get(class_name, 0) + 1
            uid = f"u_{team.value[0]}_{class_name}_{per_class[class_name]}"
            units[uid] = Unit(
                id=uid,
                owner=team,
                class_=class_name,
                pos=pos,
                hp=stats.hp_max,
                status=initial_status,
                stats=stats,
            )

    # Win conditions: explicit list from scenario OR default (seize /
    # elimination / max_turns). Rules stored on the state so
    # _apply_end_turn's dispatcher can walk them.
    from clash_of_odin.server.engine.win_conditions import (
        build_conditions,
        default_conditions,
    )

    if "win_conditions" in cfg:
        win_rules = build_conditions(cfg.get("win_conditions") or [])
    else:
        win_rules = default_conditions()

    state = GameState(
        game_id=f"g_{uuid.uuid4().hex[:8]}",
        turn=1,
        max_turns=max_turns,
        active_player=first_player,
        first_player=first_player,
        board=board,
        units=units,
    )
    # Runtime-attached: not a declared dataclass field because it holds
    # live rule instances (with per-game counters like HoldTile._count).
    state._win_conditions = win_rules
    return state
