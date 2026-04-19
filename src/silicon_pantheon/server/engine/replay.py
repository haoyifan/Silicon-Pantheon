"""Append-only JSONL replay writer.

Thread-safe: `write()` and `close()` are serialized via an internal
lock. The lock is a leaf in the server's lock hierarchy — see
docs/THREADING.md. Critical sections are a single `fh.write + \\n`
so contention is negligible, and the lock never nests with any
other app/session lock.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

try:
    import orjson

    def _dumps(obj: Any) -> str:
        return orjson.dumps(obj).decode()
except ImportError:
    import json

    def _dumps(obj: Any) -> str:
        return json.dumps(obj)


class ReplayWriter:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("a", buffering=1)
        # Serialises concurrent write()/close() calls. Leaf in the
        # server's lock hierarchy — callers must not acquire other
        # locks while holding this one. See docs/THREADING.md.
        self._lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> None:
        line = _dumps(event) + "\n"
        with self._lock:
            self._f.write(line)

    def close(self) -> None:
        with self._lock:
            try:
                self._f.close()
            except Exception:
                pass

    def __enter__(self) -> ReplayWriter:
        return self

    def __exit__(self, *a) -> None:
        self.close()
