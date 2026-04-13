"""Room and RoomRegistry: server-authoritative match containers.

Phase 1a shape is intentionally minimal — enough to host one match
with two slots. 1b extends Room with ready flags, team-assignment
config, auto-start countdown, and scenario preview.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock

from clash_of_robots.shared.player_metadata import PlayerMetadata


class Slot(str, Enum):
    A = "a"
    B = "b"


class RoomStatus(str, Enum):
    WAITING = "waiting"  # seats still open
    READY = "ready"  # both seats filled, waiting on readiness / game start
    IN_GAME = "in_game"
    FINISHED = "finished"


@dataclass
class Seat:
    slot: Slot
    player: PlayerMetadata | None = None
    ready: bool = False


@dataclass
class Room:
    """One match container. Mutations go through RoomRegistry for locking."""

    id: str
    scenario: str
    host_name: str
    seats: dict[Slot, Seat] = field(default_factory=dict)
    status: RoomStatus = RoomStatus.WAITING
    created_at: float = field(default_factory=time.time)

    def occupied_slots(self) -> list[Slot]:
        return [s for s, seat in self.seats.items() if seat.player is not None]

    def is_full(self) -> bool:
        return all(seat.player is not None for seat in self.seats.values())


class RoomRegistry:
    """Thread-safe registry of in-memory rooms. One RoomRegistry per server."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._rooms: dict[str, Room] = {}

    @staticmethod
    def _new_id() -> str:
        return secrets.token_hex(8)

    def create(self, *, scenario: str, host: PlayerMetadata) -> tuple[Room, Slot]:
        """Create an empty two-slot room, seat the host in slot A."""
        room_id = self._new_id()
        room = Room(
            id=room_id,
            scenario=scenario,
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
                    if room.is_full():
                        room.status = RoomStatus.READY
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
            # Back to waiting if someone left a READY room.
            if room.status == RoomStatus.READY:
                room.status = RoomStatus.WAITING
            # Drop entirely if now empty and not mid-game.
            if not room.occupied_slots() and room.status != RoomStatus.IN_GAME:
                self._rooms.pop(room_id, None)
            return True

    def delete(self, room_id: str) -> bool:
        with self._lock:
            return self._rooms.pop(room_id, None) is not None
