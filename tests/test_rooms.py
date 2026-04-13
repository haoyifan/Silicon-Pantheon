"""Tests for Room / RoomRegistry."""

from __future__ import annotations

from clash_of_robots.server.rooms import RoomRegistry, RoomStatus, Slot
from clash_of_robots.shared.player_metadata import PlayerMetadata


def _player(name: str) -> PlayerMetadata:
    return PlayerMetadata(display_name=name, kind="ai", provider="anthropic")


def test_create_seats_host_in_slot_a() -> None:
    reg = RoomRegistry()
    room, slot = reg.create(scenario="01_tiny_skirmish", host=_player("alice"))
    assert slot == Slot.A
    assert room.seats[Slot.A].player is not None
    assert room.seats[Slot.A].player.display_name == "alice"
    assert room.seats[Slot.B].player is None
    assert room.status == RoomStatus.WAITING


def test_join_second_slot_flips_status() -> None:
    reg = RoomRegistry()
    room, _ = reg.create(scenario="01_tiny_skirmish", host=_player("alice"))
    result = reg.join(room.id, _player("bob"))
    assert result is not None
    _, slot = result
    assert slot == Slot.B
    assert reg.get(room.id).status == RoomStatus.READY  # type: ignore[union-attr]


def test_join_full_room_fails() -> None:
    reg = RoomRegistry()
    room, _ = reg.create(scenario="01_tiny_skirmish", host=_player("alice"))
    reg.join(room.id, _player("bob"))
    assert reg.join(room.id, _player("carol")) is None


def test_join_missing_room_fails() -> None:
    reg = RoomRegistry()
    assert reg.join("nope", _player("alice")) is None


def test_leave_vacates_seat_and_reverts_status() -> None:
    reg = RoomRegistry()
    room, _ = reg.create(scenario="01_tiny_skirmish", host=_player("alice"))
    reg.join(room.id, _player("bob"))
    assert reg.leave(room.id, Slot.B) is True
    r = reg.get(room.id)
    assert r is not None
    assert r.status == RoomStatus.WAITING
    assert r.seats[Slot.B].player is None


def test_leave_empty_room_is_deleted() -> None:
    reg = RoomRegistry()
    room, _ = reg.create(scenario="01_tiny_skirmish", host=_player("alice"))
    reg.leave(room.id, Slot.A)
    assert reg.get(room.id) is None


def test_list_returns_all_rooms() -> None:
    reg = RoomRegistry()
    reg.create(scenario="01_tiny_skirmish", host=_player("alice"))
    reg.create(scenario="02_basic_mirror", host=_player("carol"))
    assert len(reg.list()) == 2
