"""Append-only JSONL replay writer."""

from __future__ import annotations

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

    def write(self, event: dict[str, Any]) -> None:
        self._f.write(_dumps(event) + "\n")

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass

    def __enter__(self) -> ReplayWriter:
        return self

    def __exit__(self, *a) -> None:
        self.close()
