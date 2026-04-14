"""Full-screen scenario picker — list on the right, preview on the left.

When the room host picks Change Scenario, instead of a blind one-line
dropdown, they get:

    ┌─────────────────────────────────────┬─────────────────┐
    │ Map preview (highlighted scenario)  │ Scenarios       │
    ├─────────────────────────────────────┤                 │
    │ Description + win conditions +      │ ● 01_tiny_..    │
    │ team composition for the current    │   02_basic_..   │
    │ highlight                           │   journey_..    │
    └─────────────────────────────────────┴─────────────────┘

Tab toggles focus between the list (up/down cycles scenarios) and the
map panel (cursor + Enter opens a UnitCard on a unit). Enter inside
the list confirms the selection and invokes on_confirm.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel as RichPanel
from rich.text import Text

from clash_of_odin.client.tui.panels import border_style
from clash_of_odin.client.tui.screens.room import (
    UnitCard,
    _describe_win_condition,
    _terrain_effect_summary,
    _unit_cell_style,
)


class ScenarioPicker:
    def __init__(
        self,
        *,
        scenarios: list[str],
        current: str,
        client,  # ServerClient | None
        on_confirm: Callable[[str], Awaitable[None]],
        on_cancel: Callable[[], None] = lambda: None,
    ) -> None:
        self.scenarios = scenarios or [current]
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel
        self.client = client
        self.selected_idx = (
            self.scenarios.index(current) if current in self.scenarios else 0
        )
        # Cache of describe_scenario results, keyed by scenario name.
        self._cache: dict[str, dict[str, Any]] = {}
        # Pending fetches so we don't hammer the server while renders tick.
        self._in_flight: set[str] = set()
        # Focus state: True = list panel focused (arrows cycle
        # selection); False = map focused (arrows move cursor).
        self._list_focused = True
        self.cursor = (0, 0)
        self.unit_card: UnitCard | None = None

    # ---- async bridge ----

    async def prefetch_current(self) -> None:
        """Called once by the owning screen when the picker is first
        opened — ensures the displayed scenario has data available."""
        await self._ensure_loaded(self.scenarios[self.selected_idx])

    async def _ensure_loaded(self, name: str) -> None:
        if name in self._cache or name in self._in_flight or self.client is None:
            return
        self._in_flight.add(name)
        try:
            r = await self.client.call("describe_scenario", name=name)
            if r.get("ok"):
                self._cache[name] = r
        except Exception:
            pass
        finally:
            self._in_flight.discard(name)

    def _current_name(self) -> str:
        return self.scenarios[self.selected_idx]

    def _current_desc(self) -> dict[str, Any] | None:
        return self._cache.get(self._current_name())

    # ---- render ----

    def render(self) -> RenderableType:
        root = Layout()
        root.split_column(
            Layout(name="hdr", size=1),
            Layout(name="body"),
            Layout(name="ftr", size=1),
        )
        root["hdr"].update(
            Text("Change Scenario — Tab switch panel · Enter select · Esc cancel",
                 style="bold cyan")
        )
        body = Layout()
        body.split_row(
            Layout(name="left", ratio=3),
            Layout(name="right", ratio=1),
        )
        body["left"].split_column(
            Layout(name="map", ratio=3),
            Layout(name="desc", ratio=2),
        )
        body["left"]["map"].update(self._render_map())
        body["left"]["desc"].update(self._render_description())
        body["right"].update(self._render_list())
        root["body"].update(body)
        root["ftr"].update(self._footer_hint())
        return root

    def _footer_hint(self) -> RenderableType:
        if self._list_focused:
            return Text(
                "[Scenarios] ↑/↓ browse   Enter select   "
                "Tab → preview map   Esc cancel",
                style="dim",
            )
        return Text(
            "[Map preview] ←↑↓→ / h j k l move cursor   Enter unit stats   "
            "Tab → scenario list   Esc cancel",
            style="dim",
        )

    def _render_list(self) -> RenderableType:
        lines: list[Text] = []
        for i, name in enumerate(self.scenarios):
            is_selected = i == self.selected_idx
            marker = "●" if is_selected else "○"
            # Show the scenario's display name if we've loaded it.
            desc = self._cache.get(name)
            display = desc.get("name") if desc else name
            style = "bold cyan" if is_selected else "white"
            lines.append(Text(f"  {marker} {display}", style=style))
        return RichPanel(
            Group(*lines),
            title="Scenarios",
            border_style=border_style(self._list_focused),
            padding=(0, 1),
        )

    def _render_map(self) -> RenderableType:
        desc = self._current_desc()
        focused = not self._list_focused
        if desc is None:
            return RichPanel(
                Text("(loading scenario preview…)", style="dim italic"),
                title="Map",
                border_style=border_style(focused),
                padding=(0, 1),
            )
        board = desc.get("board") or {}
        w = int(board.get("width", 0))
        h = int(board.get("height", 0))
        terrain_entries = board.get("terrain") or []
        armies = desc.get("armies") or {}

        tile_type: dict[tuple[int, int], str] = {}
        for t in terrain_entries:
            tile_type[(int(t["x"]), int(t["y"]))] = str(t["type"])
        for f in board.get("forts") or []:
            tile_type[(int(f["x"]), int(f["y"]))] = "fort"

        unit_at: dict[tuple[int, int], dict] = {}
        unit_classes = desc.get("unit_classes") or {}
        for owner in ("blue", "red"):
            for u in armies.get(owner, []):
                pos = u.get("pos") or {}
                cls = str(u.get("class", ""))
                spec = unit_classes.get(cls) or {}
                unit_at[(int(pos["x"]), int(pos["y"]))] = {
                    "owner": owner,
                    "class": cls,
                    "glyph": spec.get("glyph"),
                    "color": spec.get("color"),
                    "pos": pos,
                }

        cx, cy = self.cursor
        if w > 0 and h > 0:
            cx = max(0, min(cx, w - 1))
            cy = max(0, min(cy, h - 1))
            self.cursor = (cx, cy)

        text = Text()
        text.append(
            "   " + " ".join(f"{x:>2}" for x in range(w)) + "\n", style="dim"
        )
        for y in range(h):
            text.append(f"{y:>2} ", style="dim")
            for x in range(w):
                u = unit_at.get((x, y))
                if u is not None:
                    g, st = _unit_cell_style(u)
                else:
                    ttype = tile_type.get((x, y), "plain")
                    g, st = {
                        "plain": (".", "dim"),
                        "forest": ("f", "green"),
                        "mountain": ("^", "bright_black"),
                        "fort": ("*", "yellow"),
                    }.get(ttype, (ttype[:1] or "?", "dim"))
                if focused and x == cx and y == cy:
                    text.append(f"[{g}]", style=f"reverse {st}")
                else:
                    text.append(f" {g} ", style=st)
            text.append("\n")
        # Tooltip below the board.
        if self.unit_card is not None:
            footer: RenderableType = self.unit_card.render()
        else:
            footer = self._cursor_tooltip(tile_type, unit_at)
        return RichPanel(
            Group(text, Text(""), footer),
            title="Map",
            border_style=border_style(focused),
            padding=(0, 1),
        )

    def _cursor_tooltip(
        self,
        tile_type: dict[tuple[int, int], str],
        unit_at: dict[tuple[int, int], dict],
    ) -> RenderableType:
        cx, cy = self.cursor
        terrain = tile_type.get((cx, cy), "plain")
        line = Text()
        line.append(f"({cx}, {cy}) ", style="dim")
        line.append(f"terrain: {terrain}", style="yellow")
        summary = _terrain_effect_summary(self._current_desc(), terrain)
        if summary:
            line.append(f" — {summary}", style="dim")
        u = unit_at.get((cx, cy))
        if u:
            owner = u.get("owner", "?")
            color = "cyan" if owner == "blue" else "red"
            line.append("   ")
            line.append(
                f"{u.get('class', '?')} ({owner})", style=f"bold {color}"
            )
            line.append("   Enter for details", style="dim italic")
        return line

    def _render_description(self) -> RenderableType:
        desc = self._current_desc()
        name = self._current_name()
        if desc is None:
            return RichPanel(
                Text("(loading…)", style="dim italic"),
                title=name,
                border_style="dim",
                padding=(0, 1),
            )
        title = desc.get("name", name)
        story = (desc.get("description") or "").strip()
        wcs = desc.get("win_conditions") or []
        armies = desc.get("armies") or {}
        unit_classes = desc.get("unit_classes") or {}
        rules = desc.get("rules") or {}

        rows: list[RenderableType] = []
        if story:
            rows.append(Text(story))
        if wcs:
            rows.append(Text(""))
            rows.append(Text("How to win:", style="bold"))
            for wc in wcs:
                rows.append(Text(f"  • {_describe_win_condition(wc)}", style="dim"))
        rows.append(Text(""))
        rows.append(Text("Armies:", style="bold"))
        for owner in ("blue", "red"):
            units = armies.get(owner, [])
            if not units:
                continue
            cls_counts: dict[str, int] = {}
            for u in units:
                cls_counts[u.get("class", "?")] = cls_counts.get(u.get("class", "?"), 0) + 1
            summary = ", ".join(
                f"{n}×{c}" if n > 1 else c for c, n in cls_counts.items()
            )
            color = "cyan" if owner == "blue" else "red"
            rows.append(Text(f"  {owner}: {summary}", style=color))
        max_turns = rules.get("max_turns")
        if max_turns:
            rows.append(Text(""))
            rows.append(Text(f"Max turns: {max_turns}", style="dim"))
        return RichPanel(
            Group(*rows),
            title=title,
            border_style="dim",
            padding=(0, 1),
        )

    # ---- input ----

    async def handle_key(self, key: str) -> bool:
        """Return True when the picker should close."""
        # Unit card consumes its own Esc/Enter first.
        if self.unit_card is not None and key in ("esc", "enter", "q"):
            self.unit_card = None
            return False

        if key == "esc":
            self.on_cancel()
            return True
        if key == "\t":
            self._list_focused = not self._list_focused
            return False

        if self._list_focused:
            return await self._handle_list_key(key)
        return self._handle_map_key(key)

    async def _handle_list_key(self, key: str) -> bool:
        n = len(self.scenarios)
        if n == 0:
            return False
        if key in ("up", "k"):
            self.selected_idx = (self.selected_idx - 1) % n
            await self._ensure_loaded(self._current_name())
            return False
        if key in ("down", "j"):
            self.selected_idx = (self.selected_idx + 1) % n
            await self._ensure_loaded(self._current_name())
            return False
        if key == "enter":
            await self.on_confirm(self._current_name())
            return True
        return False

    def _handle_map_key(self, key: str) -> bool:
        desc = self._current_desc()
        if desc is None:
            return False
        board = desc.get("board") or {}
        w = int(board.get("width", 0))
        h = int(board.get("height", 0))
        if w == 0 or h == 0:
            return False
        cx, cy = self.cursor
        if key in ("up", "k"):
            cy = (cy - 1) % h
        elif key in ("down", "j"):
            cy = (cy + 1) % h
        elif key in ("left", "h"):
            cx = (cx - 1) % w
        elif key in ("right", "l"):
            cx = (cx + 1) % w
        elif key == "enter":
            # Find a unit at the cursor.
            unit_classes = desc.get("unit_classes") or {}
            for owner in ("blue", "red"):
                for u in (desc.get("armies") or {}).get(owner, []):
                    pos = u.get("pos") or {}
                    if int(pos["x"]) == cx and int(pos["y"]) == cy:
                        enriched = {
                            "id": f"preview_{owner}_{u.get('class', '?')}",
                            "owner": owner,
                            "class": u.get("class"),
                            "pos": pos,
                        }
                        self.unit_card = UnitCard(
                            unit=enriched,
                            class_spec=unit_classes.get(u.get("class")),
                        )
                        break
        self.cursor = (cx, cy)
        return False

    # ---- tick hook (owning screen calls this each poll) ----

    async def tick(self) -> None:
        """Opportunistic prefetch for the currently-highlighted scenario,
        so browsing feels responsive."""
        await self._ensure_loaded(self._current_name())
