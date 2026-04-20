"""Room and RoomRegistry: server-authoritative match containers.

A Room holds the configuration for one match (scenario, team
assignment rules, fog mode, turn time limit) plus the two seats,
each player's readiness, and the room's lifecycle status.

Locking
-------

RoomRegistry is **lockless**. Callers MUST hold ``App._state_lock``
for every access, including reads. See docs/THREADING.md. The old
per-registry lock was removed to collapse three app-level locks
down to two — every RoomRegistry operation already happens inside
a broader lobby/game-tool critical section that holds state_lock,
so a second lock added no safety and created ordering questions.

Same applies to direct field mutations on ``Room`` (``status``,
``config.*``, ``seats[…].ready``). The registry does not police
them — callers must hold ``app._state_lock``.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from silicon_pantheon.shared.player_metadata import PlayerMetadata

MAX_ROOMS = 100


class Slot(str, Enum):
    A = "a"
    B = "b"


class RoomStatus(str, Enum):
    WAITING_FOR_PLAYERS = "waiting_for_players"  # at least one empty seat
    WAITING_READY = "waiting_ready"  # both seats filled, not both ready
    COUNTING_DOWN = "counting_down"  # both ready; auto-start timer running
    IN_GAME = "in_game"
    FINISHED = "finished"


TeamAssignment = Literal["fixed", "random"]
HostTeam = Literal["blue", "red"]
FogMode = Literal["none", "classic", "line_of_sight"]


@dataclass
class RoomConfig:
    """Per-room configuration. Mutable pre-game so the host can tweak it.

    Frozen semantics would be nicer for safety, but the UX is that the
    host can flip scenario / fog / team mode in the lobby before both
    players press ready. `update_room_config` is the single write path
    (host-only, refuses once COUNTING_DOWN/IN_GAME/FINISHED).

    - team_assignment="fixed":  host gets host_team; joiner gets the other.
    - team_assignment="random": coin-flipped at game-start time.
    - fog_of_war / max_turns / turn_time_limit_s drive the engine and
      filter behavior during the match.
    """

    scenario: str
    max_turns: int = 20
    team_assignment: TeamAssignment = "fixed"
    host_team: HostTeam = "blue"
    fog_of_war: FogMode = "none"  # easier onboarding; bump to "classic" per-room
    # Default is deliberately huge so neither the client-side agent
    # loop nor the server-side forfeit interferes with reasoning-model
    # debugging. Hosts can dial it down to blitz-game values from the
    # room Actions panel; 1800s (30 min) is a reasonable upper ceiling
    # for a single turn where a weak model with many units needs the
    # full observe-act-observe cycle plus ample reasoning tokens.
    turn_time_limit_s: int = 1800


@dataclass
class Seat:
    slot: Slot
    player: PlayerMetadata | None = None
    ready: bool = False


@dataclass
class Room:
    """One match container. Mutations go through RoomRegistry for locking."""

    id: str
    config: RoomConfig
    host_name: str
    seats: dict[Slot, Seat] = field(default_factory=dict)
    status: RoomStatus = RoomStatus.WAITING_FOR_PLAYERS
    created_at: float = field(default_factory=time.time)

    def occupied_slots(self) -> list[Slot]:
        return [s for s, seat in self.seats.items() if seat.player is not None]

    def is_full(self) -> bool:
        return all(seat.player is not None for seat in self.seats.values())

    def all_ready(self) -> bool:
        return self.is_full() and all(seat.ready for seat in self.seats.values())

    def recompute_status(self) -> None:
        """Reconcile status based on current occupancy + readiness.

        Leaves IN_GAME / FINISHED alone (terminal from this module's view;
        only the game runner flips in / out of those). For pre-game
        states: empty seat -> WAITING_FOR_PLAYERS; full but not all
        ready -> WAITING_READY; full and all ready -> WAITING_READY
        (the countdown is set explicitly by the caller, not here).
        """
        if self.status in (RoomStatus.IN_GAME, RoomStatus.FINISHED):
            return
        if not self.is_full():
            self.status = RoomStatus.WAITING_FOR_PLAYERS
        else:
            # Full but readiness is the caller's business; drop out of
            # COUNTING_DOWN if we got here via someone unreadying.
            if self.status == RoomStatus.COUNTING_DOWN and not self.all_ready():
                self.status = RoomStatus.WAITING_READY
            elif self.status != RoomStatus.COUNTING_DOWN:
                self.status = RoomStatus.WAITING_READY


class RoomRegistry:
    """Lockless registry of in-memory rooms. One RoomRegistry per server.

    ── Locking ──
    All methods on this class assume the caller holds
    ``App._state_lock``. No internal lock is used (see module
    docstring). Calling these from outside a state_lock critical
    section is a bug.
    """

    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}

    @staticmethod
    def _new_id() -> str:
        return secrets.token_hex(8)

    def create(
        self,
        *,
        config: RoomConfig,
        host: PlayerMetadata,
    ) -> tuple[Room, Slot]:
        """Create an empty two-slot room, seat the host in slot A.

        Caller must hold ``App._state_lock``. Raises ValueError if
        the server-wide room limit has been reached.
        """
        if len(self._rooms) >= MAX_ROOMS:
            raise ValueError(
                f"server room limit ({MAX_ROOMS}) reached"
            )
        room_id = self._new_id()
        room = Room(
            id=room_id,
            config=config,
            host_name=host.display_name,
            seats={
                Slot.A: Seat(slot=Slot.A, player=host),
                Slot.B: Seat(slot=Slot.B, player=None),
            },
        )
        self._rooms[room_id] = room
        return room, Slot.A

    def get(self, room_id: str) -> Room | None:
        """Caller must hold ``App._state_lock``."""
        return self._rooms.get(room_id)

    def list(self) -> list[Room]:
        """Caller must hold ``App._state_lock``."""
        return list(self._rooms.values())

    def join(self, room_id: str, player: PlayerMetadata) -> tuple[Room, Slot] | None:
        """Seat player in the first empty slot. Returns None if room
        missing or full. Caller must hold ``App._state_lock``."""
        room = self._rooms.get(room_id)
        if room is None:
            return None
        for slot_id in (Slot.A, Slot.B):
            seat = room.seats[slot_id]
            if seat.player is None:
                seat.player = player
                room.recompute_status()
                return room, slot_id
        return None

    def leave(self, room_id: str, slot: Slot) -> bool:
        """Vacate a slot. Returns True if the seat was occupied.
        Caller must hold ``App._state_lock``."""
        room = self._rooms.get(room_id)
        if room is None:
            return False
        seat = room.seats.get(slot)
        if seat is None or seat.player is None:
            return False
        seat.player = None
        seat.ready = False
        room.recompute_status()
        # Drop entirely if now empty and no live game is running. A
        # FINISHED room with nobody seated is rubble — the downloads
        # are cached client-side already.
        if not room.occupied_slots() and room.status in (
            RoomStatus.WAITING_FOR_PLAYERS,
            RoomStatus.WAITING_READY,
            RoomStatus.COUNTING_DOWN,
            RoomStatus.FINISHED,
        ):
            self._rooms.pop(room_id, None)
        return True

    def delete(self, room_id: str) -> bool:
        """Caller must hold ``App._state_lock``."""
        return self._rooms.pop(room_id, None) is not None
