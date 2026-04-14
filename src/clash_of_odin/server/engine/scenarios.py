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
        glyph=src.glyph,
        color=src.color,
        display_name=src.display_name,
        description=src.description,
        art_frames=list(src.art_frames),
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
        glyph=(str(s["glyph"]) if s.get("glyph") is not None else None),
        color=(str(s["color"]) if s.get("color") is not None else None),
        display_name=str(s.get("display_name", "") or ""),
        description=str(s.get("description", "") or ""),
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


def _is_safe_scenario_name(name: str) -> bool:
    """Names must be a single path component — no slashes, no dot-only,
    no leading dot. Prevents path traversal when name comes from a
    network request (e.g. describe_scenario)."""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    if name.startswith("."):
        return False
    return True


def load_scenario(name: str) -> GameState:
    """Load a scenario by folder name (e.g. '01_tiny_skirmish').

    If the scenario directory contains a `rules.py`, load it as a
    plugin module and expose its public callables on the resulting
    state's `_plugin_namespace`. This runs at scenario-load time so
    import errors surface immediately, not mid-match.
    """
    if not _is_safe_scenario_name(name):
        raise ValueError(f"unsafe scenario name: {name!r}")
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
    # ASCII-art portrait frames per unit class, auto-discovered from
    # <scenario_dir>/art/<class_slug>/*.txt (sorted lexically). Each
    # file is a single frame; multiple files animate. Files larger
    # than the configured limits are rejected so a typo doesn't blow
    # up rendering with a wall-of-text.
    art_limits = cfg.get("art_limits") or {}
    max_cols = int(art_limits.get("max_cols", DEFAULT_ART_MAX_COLS))
    max_rows = int(art_limits.get("max_rows", DEFAULT_ART_MAX_ROWS))
    art_root = scenario_dir / "art"
    if art_root.is_dir():
        # Walk every alive unit and try to fill in its art_frames from
        # disk if its class has an art directory.
        loaded: dict[str, list[str]] = {}
        for class_slug_dir in sorted(art_root.iterdir()):
            if not class_slug_dir.is_dir():
                continue
            frames: list[str] = []
            for f in sorted(class_slug_dir.glob("*.txt")):
                text = f.read_text(encoding="utf-8").rstrip("\n")
                _validate_art_frame(text, str(f), max_cols, max_rows)
                frames.append(text)
            if frames:
                loaded[class_slug_dir.name] = frames
        if loaded:
            for u in state.units.values():
                if u.class_ in loaded and not u.stats.art_frames:
                    u.stats.art_frames = list(loaded[u.class_])
            state._art_frames_by_class = loaded
    return state


# Default size caps. Generous so authors can do detailed pieces, but
# not so generous a misplaced screenshot blows out the layout. Authors
# can override at scenario level via art_limits.
DEFAULT_ART_MAX_COLS = 80
DEFAULT_ART_MAX_ROWS = 30


def _validate_art_frame(
    text: str, source: str, max_cols: int, max_rows: int
) -> None:
    rows = text.split("\n")
    if len(rows) > max_rows:
        raise ValueError(
            f"ASCII art {source} has {len(rows)} rows, exceeds max_rows={max_rows}"
        )
    for i, row in enumerate(rows):
        if len(row) > max_cols:
            raise ValueError(
                f"ASCII art {source} row {i + 1} is {len(row)} cols, "
                f"exceeds max_cols={max_cols}"
            )


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
    # Expose only names defined in this module — drop imported helpers
    # (Pos, Team, etc.) and dunder/private names. Without this, every
    # `from ... import` in rules.py would land in the plugin namespace
    # and could shadow a real plugin function with the same name.
    out: dict = {}
    for name in dir(module):
        if name.startswith("_"):
            continue
        obj = getattr(module, name)
        defined_here = getattr(obj, "__module__", None) == mod_name
        if defined_here:
            out[name] = obj
    return out


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
    occupied_positions: dict[Pos, str] = {}
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
            if not (0 <= pos.x < width and 0 <= pos.y < height):
                raise ValueError(
                    f"unit {class_name} placed off-board at {pos} "
                    f"(board is {width}x{height})"
                )
            if pos in occupied_positions:
                raise ValueError(
                    f"two units share starting position {pos}: "
                    f"{occupied_positions[pos]} and a {team.value} {class_name}"
                )
            per_class[class_name] = per_class.get(class_name, 0) + 1
            uid = f"u_{team.value[0]}_{class_name}_{per_class[class_name]}"
            occupied_positions[pos] = uid
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
    # Narrative block (title/description/intro + events). Absent → empty.
    from clash_of_odin.server.engine.narrative import parse_narrative

    state._narrative = parse_narrative(cfg)
    state._narrative_log = []
    # Plugin hook registration: scenarios list plugin callable names
    # under plugin_hooks: { on_turn_start: [fn1, fn2] }.
    hooks = cfg.get("plugin_hooks") or {}
    state._turn_start_hooks = list(hooks.get("on_turn_start") or [])
    return state
