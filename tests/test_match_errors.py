"""Tests for shared.match_errors — terminal-match detection."""

from __future__ import annotations

from silicon_pantheon.shared.match_errors import is_terminal_tool_error


def test_server_native_game_already_over() -> None:
    """Raw server envelope shape {"ok": false, "error": {...}}."""
    assert is_terminal_tool_error({
        "ok": False,
        "error": {"code": "bad_input", "message": "game is already over"},
    })


def test_unwrapped_bridge_shape_game_already_over() -> None:
    """Bridge-unwrapped shape {"error": {...}} — observed in grok hang."""
    assert is_terminal_tool_error({
        "error": {"code": "bad_input", "message": "game is already over"},
    })


def test_case_insensitive_marker() -> None:
    """Match regardless of message casing."""
    assert is_terminal_tool_error({
        "error": {"message": "GAME IS ALREADY OVER"},
    })
    assert is_terminal_tool_error({
        "error": {"message": "The Game Is Over, winner=blue"},
    })


def test_substring_match() -> None:
    """Marker can appear anywhere in the message."""
    assert is_terminal_tool_error({
        "error": {"message": "cannot end_turn: game is already over"},
    })


def test_normal_error_not_terminal() -> None:
    """Other error shapes must NOT trip the detector."""
    assert not is_terminal_tool_error({
        "error": {"code": "bad_input", "message": "not your turn"},
    })
    assert not is_terminal_tool_error({
        "error": {"code": "invalid_argument", "message": "move out of range"},
    })
    assert not is_terminal_tool_error({
        "error": {"code": "dropped_parallel_mutation", "message": "dropped"},
    })


def test_success_not_terminal() -> None:
    """ok=True responses and non-error shapes never count as terminal."""
    assert not is_terminal_tool_error({"ok": True, "result": {"turn": 3}})
    assert not is_terminal_tool_error({"result": {"status": "in_progress"}})
    assert not is_terminal_tool_error({})


def test_non_dict_inputs() -> None:
    """Tolerant of garbage shapes — False, never raising."""
    assert not is_terminal_tool_error(None)
    assert not is_terminal_tool_error("some string")
    assert not is_terminal_tool_error([{"error": {"message": "game is already over"}}])
    assert not is_terminal_tool_error(42)


def test_missing_message_field() -> None:
    """Error dict without message → not terminal (can't confirm)."""
    assert not is_terminal_tool_error({"error": {"code": "bad_input"}})
    assert not is_terminal_tool_error({"error": {}})
