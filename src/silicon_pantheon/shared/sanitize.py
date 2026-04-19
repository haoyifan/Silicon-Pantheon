"""Input sanitization utilities for untrusted client data."""

from __future__ import annotations

import re

# Matches ANSI escape sequences:
#   - CSI 7-bit:  ESC [ <params> <final-byte>     e.g. \x1b[0m, \x1b[38;2;255;0;0m
#   - CSI 8-bit:  0x9B <params> <final-byte>      e.g. \x9b0m
#   - OSC:        ESC ] <payload> (BEL | ST)       e.g. \x1b]0;title\x07
#   - Other ESC:  ESC <intermediate> <final>       e.g. \x1b(B
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]"       # CSI 7-bit
    r"|\x9b[0-9;]*[A-Za-z]"        # CSI 8-bit
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (terminated by BEL or ST)
    r"|\x1b[^[\]()[0-9;]"          # other two-byte ESC sequences
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _strip_control_chars(text: str, *, allow_newline: bool = False) -> str:
    """Remove C0 control chars (< 0x20 except space) and C1 control chars (0x80-0x9F)."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x20:  # space — always keep
            out.append(ch)
        elif allow_newline and ch == "\n":
            out.append(ch)
        elif code < 0x20:
            continue  # strip C0 control char
        elif 0x80 <= code <= 0x9F:
            continue  # strip C1 control char (includes 0x9B CSI)
        else:
            out.append(ch)
    return "".join(out)


def sanitize_display_text(text: str, max_length: int = 64) -> str:
    """Sanitize a short display string (name, model, provider).

    Strips ANSI escapes, control characters (< 0x20 except space),
    leading/trailing whitespace, and truncates to *max_length*.
    """
    text = _strip_ansi(text)
    text = _strip_control_chars(text, allow_newline=False)
    text = text.strip()
    return text[:max_length]


def sanitize_freetext(text: str, max_length: int = 10_000) -> str:
    """Sanitize longer free-form text (thoughts, coach messages).

    Strips ANSI escapes and control characters (except newlines),
    then truncates to *max_length*.
    """
    text = _strip_ansi(text)
    text = _strip_control_chars(text, allow_newline=True)
    return text[:max_length]
