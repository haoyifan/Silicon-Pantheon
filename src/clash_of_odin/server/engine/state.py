"""Core data model for Clash of Odin.

Pure Python, no MCP or I/O. Engine modules import from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Team(str, Enum):
    BLUE = "blue"
    RED = "red"

    def other(self) -> Team:
        return Team.RED if self is Team.BLUE else Team.BLUE


class UnitClass(str, Enum):
    KNIGHT = "knight"
    ARCHER = "archer"
    CAVALRY = "cavalry"
    MAGE = "mage"


class TerrainType(str, Enum):
    PLAIN = "plain"
    FOREST = "forest"
    MOUNTAIN = "mountain"
    FORT = "fort"


class UnitStatus(str, Enum):
    READY = "ready"  # can move and/or act
    MOVED = "moved"  # moved this turn; can still act
    DONE = "done"  # acted this turn


class GameStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    GAME_OVER = "game_over"


@dataclass(frozen=True)
class Pos:
    x: int
    y: int

    def manhattan(self, other: Pos) -> int:
        return abs(self.x - other.x) + abs(self.y - other.y)

    def neighbors4(self) -> list[Pos]:
        return [
            Pos(self.x + 1, self.y),
            Pos(self.x - 1, self.y),
            Pos(self.x, self.y + 1),
            Pos(self.x, self.y - 1),
        ]

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y}

    @staticmethod
    def from_dict(d: dict) -> Pos:
        return Pos(int(d["x"]), int(d["y"]))


@dataclass
class UnitStats:
    hp_max: int
    atk: int
    defense: int
    res: int
    spd: int
    rng_min: int
    rng_max: int
    move: int
    is_magic: bool  # damage uses defender's RES instead of DEF
    can_enter_forest: bool
    can_enter_mountain: bool
    can_heal: bool  # may take `heal` action instead of `attack`
    heal_amount: int = 0
    # Fog-of-war sight range. Chebyshev distance from the unit within
    # which tiles are considered visible before terrain line-of-sight
    # is applied. 0 means no intrinsic sight (unit contributes nothing
    # to team vision).
    sight: int = 3

    # ---- v1 schema, v2+ behavior (reserved fields) ----
    # All of these are accepted by the scenario loader, serialized in
    # state_to_dict, and kept on the Unit, but the v1 engine ignores
    # them for combat / movement purposes. v2 engines flip the switches
    # without a schema change.
    tags: list[str] = field(default_factory=list)
    # MP pool for ability use. mp_per_turn is the recharge per end_turn
    # (default 0 = no recharge; burn what you have across the match).
    mp_max: int = 0
    mp_per_turn: int = 0
    # Named abilities this class can invoke. Ability catalog lives on
    # the scenario.
    abilities: list[str] = field(default_factory=list)
    # Starting inventory (item ids from the scenario's item catalog).
    default_inventory: list[str] = field(default_factory=list)
    # Damage-type aware combat (v2). Keys are damage-type strings:
    #   physical, magic, fire, wind, lightning, holy, dark, ...
    # Empty dict = use legacy ATK vs DEF/RES formula.
    damage_profile: dict[str, int] = field(default_factory=dict)
    defense_profile: dict[str, int] = field(default_factory=dict)
    # Tag-aware bonus / vulnerability multipliers for combat (v2).
    # Each entry: {"tag": "flying", "mult": 2.0}.
    bonus_vs_tags: list[dict] = field(default_factory=list)
    vulnerability_to_tags: list[dict] = field(default_factory=list)


@dataclass
class Tile:
    pos: Pos
    # Type is now a free-form string so scenarios can introduce custom
    # terrain types. Built-in names ('plain', 'forest', 'mountain',
    # 'fort') produce the legacy behavior unless overridden.
    type: str
    fort_owner: Team | None = None  # None unless `type == 'fort'`
    # Configurable effects. If a scenario's terrain_types: block
    # supplies these they override the built-in defaults. None = derive
    # from type at accessor time (backward compatible).
    _move_cost: int | None = None
    _defense_bonus: int | None = None
    _magic_bonus: int | None = None
    heals: int = 0  # positive = per-turn heal; negative = damage
    blocks_sight: bool = False
    passable: bool = True
    class_overrides: dict[str, dict] = field(default_factory=dict)
    glyph: str | None = None
    color: str | None = None

    @property
    def is_fort(self) -> bool:
        return self.type == TerrainType.FORT.value or self.type == "fort"

    def move_cost(self, unit_class: str | None = None) -> int:
        """Move cost honoring per-class overrides (e.g. cavalry pays
        more on sand)."""
        if unit_class is not None:
            override = self.class_overrides.get(unit_class) or {}
            if "move_cost" in override:
                return int(override["move_cost"])
        if self._move_cost is not None:
            return self._move_cost
        # Legacy built-in defaults.
        if self.type == "forest" or self.type == "mountain":
            return 2
        return 1

    def def_bonus(self) -> int:
        if self._defense_bonus is not None:
            return self._defense_bonus
        if self.type == "forest":
            return 2
        if self.type == "mountain" or self.type == "fort":
            return 3
        return 0

    def res_bonus(self) -> int:
        if self._magic_bonus is not None:
            return self._magic_bonus
        if self.type == "mountain":
            return 1
        if self.type == "fort":
            return 3
        return 0


@dataclass
class Board:
    width: int
    height: int
    tiles: dict[Pos, Tile] = field(default_factory=dict)

    def in_bounds(self, p: Pos) -> bool:
        return 0 <= p.x < self.width and 0 <= p.y < self.height

    def tile(self, p: Pos) -> Tile:
        """Return tile at p; if not explicitly set, default to plain."""
        t = self.tiles.get(p)
        if t is not None:
            return t
        return Tile(pos=p, type=TerrainType.PLAIN.value)

    def all_positions(self) -> list[Pos]:
        return [Pos(x, y) for y in range(self.height) for x in range(self.width)]


@dataclass
class Unit:
    id: str
    owner: Team
    # Was UnitClass (enum); now a plain str so scenarios can introduce
    # custom class names beyond the built-in four. The built-in enum
    # still exists for the default scenarios' convenience, but custom
    # classes just pass a string through.
    class_: str
    pos: Pos
    hp: int
    status: UnitStatus
    stats: UnitStats

    @property
    def alive(self) -> bool:
        return self.hp > 0


@dataclass
class GameState:
    game_id: str
    turn: int  # 1-indexed turn number (increments when active_player wraps back to first_player)
    max_turns: int
    active_player: Team
    first_player: Team
    board: Board
    units: dict[str, Unit]  # id -> Unit; dead units removed
    status: GameStatus = GameStatus.IN_PROGRESS
    winner: Team | None = None
    last_action: dict | None = None
    history: list[dict] = field(default_factory=list)

    # ---- lookups ----
    def units_of(self, team: Team) -> list[Unit]:
        return [u for u in self.units.values() if u.owner is team and u.alive]

    def unit_at(self, p: Pos) -> Unit | None:
        for u in self.units.values():
            if u.alive and u.pos == p:
                return u
        return None

    def occupied(self, p: Pos) -> bool:
        return self.unit_at(p) is not None
