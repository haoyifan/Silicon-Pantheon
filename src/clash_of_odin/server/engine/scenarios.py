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
    UnitStatus,
)
from .units import make_stats


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
    """Load a scenario by folder name (e.g. '01_tiny_skirmish')."""
    config_path = _games_root() / name / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"No scenario at {config_path}")
    with config_path.open() as f:
        cfg = yaml.safe_load(f)
    return build_state(cfg)


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
    tiles: dict[Pos, Tile] = {}

    for t in board_cfg.get("terrain", []) or []:
        pos = Pos(int(t["x"]), int(t["y"]))
        tiles[pos] = Tile(pos=pos, type=TerrainType(t["type"]))

    # Forts overlay: they become FORT tiles with a fort_owner.
    for f in board_cfg.get("forts", []) or []:
        pos = Pos(int(f["x"]), int(f["y"]))
        tiles[pos] = Tile(pos=pos, type=TerrainType.FORT, fort_owner=Team(f["owner"]))

    board = Board(width=width, height=height, tiles=tiles)

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
            cls = UnitClass(u["class"])
            stats = make_stats(cls)
            pos = Pos(int(u["pos"]["x"]), int(u["pos"]["y"]))
            per_class[cls.value] = per_class.get(cls.value, 0) + 1
            uid = f"u_{team.value[0]}_{cls.value}_{per_class[cls.value]}"
            units[uid] = Unit(
                id=uid,
                owner=team,
                class_=cls,
                pos=pos,
                hp=stats.hp_max,
                status=initial_status,
                stats=stats,
            )

    return GameState(
        game_id=f"g_{uuid.uuid4().hex[:8]}",
        turn=1,
        max_turns=max_turns,
        active_player=first_player,
        first_player=first_player,
        board=board,
        units=units,
    )
