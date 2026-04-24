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
    """Recoverable per-action error shapes must NOT trip the detector."""
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
    """Error dict without message → not terminal unless code is terminal."""
    assert not is_terminal_tool_error({"error": {"code": "bad_input"}})
    assert not is_terminal_tool_error({"error": {}})


def test_not_your_turn_message_is_not_terminal() -> None:
    """``not your turn`` is a routine off-turn rejection, NOT a
    terminal-match signal. Server returns it whenever a tool call
    lands while the opponent is active (very common during the
    polling phase). Treating it as terminal made the openai loop
    bail mid-iteration on every reflexive get_legal_actions —
    65 false-positive bailouts across 12 agents in the 2026-04-23
    system test. The worker's outer poll already guards on
    active_player == viewer, so this is recoverable in-place."""
    assert not is_terminal_tool_error({
        "error": {"code": "bad_input",
                  "message": "not your turn (active: red, you: blue)"},
    })
    assert not is_terminal_tool_error({
        "error": {"code": "not_your_turn", "message": "anything"},
    })


def test_terminal_error_codes() -> None:
    """State-loss codes are terminal even with empty/vague message."""
    assert is_terminal_tool_error({
        "error": {"code": "game_not_started", "message": "no active game"},
    })
    assert is_terminal_tool_error({
        "error": {"code": "tool_not_available_in_state", "message": ""},
    })
    assert is_terminal_tool_error({
        "error": {"code": "not_registered", "message": "anything"},
    })
    assert is_terminal_tool_error({
        "error": {"code": "not_in_room", "message": ""},
    })
    assert is_terminal_tool_error({
        "error": {"code": "game_already_over", "message": ""},
    })


def test_bad_input_without_marker_is_not_terminal() -> None:
    """BAD_INPUT is overloaded — only its specific messages count."""
    assert not is_terminal_tool_error({
        "error": {"code": "bad_input", "message": "invalid args: missing x"},
    })
    assert not is_terminal_tool_error({
        "error": {"code": "bad_input", "message": "unit u_b_foo not found"},
    })
    # Per-action tactical errors that the LLM CAN recover from:
    assert not is_terminal_tool_error({
        "error": {"code": "bad_input", "message": "target out of attack range"},
    })
    assert not is_terminal_tool_error({
        "error": {"code": "bad_input", "message": "heal requires adjacent ally"},
    })
