"""Room and RoomRegistry: server-authoritative match containers.

A Room holds the configuration for one match (scenario, team
assignment rules, fog mode, turn time limit) plus the two seats,
each player's readiness, and the room's lifecycle status. Mutations
go through RoomRegistry for locking.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Literal

from clash_of_robots.shared.player_metadata import PlayerMetadata


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


@dataclass(frozen=True)
class RoomConfig:
    """Configuration pinned when a room is created; immutable afterward.

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
    turn_time_limit_s: int = 180


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

    # Convenience passthrough for legacy callers; remove after full migration.
    @property
    def scenario(self) -> str:
        return self.config.scenario

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
    """Thread-safe registry of in-memory rooms. One RoomRegistry per server."""

    def __init__(self) -> None:
        self._lock = Lock()
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
        """Create an empty two-slot room, seat the host in slot A."""
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
        with self._lock:
            self._rooms[room_id] = room
        return room, Slot.A

    def get(self, room_id: str) -> Room | None:
        with self._lock:
            return self._rooms.get(room_id)

    def list(self) -> list[Room]:
        with self._lock:
            return list(self._rooms.values())

    def join(self, room_id: str, player: PlayerMetadata) -> tuple[Room, Slot] | None:
        """Seat player in the first empty slot. Returns None if room missing
        or full."""
        with self._lock:
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
        """Vacate a slot. Returns True if the seat was occupied."""
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                return False
            seat = room.seats.get(slot)
            if seat is None or seat.player is None:
                return False
            seat.player = None
            seat.ready = False
            room.recompute_status()
            # Drop entirely if now empty and not mid-game.
            if not room.occupied_slots() and room.status != RoomStatus.IN_GAME:
                self._rooms.pop(room_id, None)
            return True

    def delete(self, room_id: str) -> bool:
        with self._lock:
            return self._rooms.pop(room_id, None) is not None
