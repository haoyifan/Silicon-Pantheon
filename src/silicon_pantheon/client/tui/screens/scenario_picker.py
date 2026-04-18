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

from silicon_pantheon.client.locale import t
from silicon_pantheon.client.tui.panels import border_style, wrap_rows_to_width
from silicon_pantheon.client.tui.terrain import terrain_cell as _terrain_cell


def _slug_to_title(slug: str) -> str:
    """Cheap human-readable name for a scenario directory slug —
    `journey_to_the_west` → `Journey To The West`. Used as a
    placeholder while describe_scenario is in flight."""
    parts = slug.replace("-", "_").split("_")
    return " ".join(p.capitalize() if p else "_" for p in parts if p)
from silicon_pantheon.client.tui.widgets import UnitCard
from silicon_pantheon.client.tui.scenario_display import (
    describe_win_condition,
    filter_win_conditions,
    terrain_effect_summary,
    unit_cell_style,
    unit_display_name,
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
        locale: str = "en",
    ) -> None:
        self.scenarios = scenarios or [current]
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel
        self.client = client
        self.locale = locale
        self.selected_idx = (
            self.scenarios.index(current) if current in self.scenarios else 0
        )
        # Cache of describe_scenario results, keyed by scenario name.
        self._cache: dict[str, dict[str, Any]] = {}
        # Pending fetches so we don't hammer the server while renders tick.
        self._in_flight: set[str] = set()
        # Focus rotates Tab → "list" → "map" → "desc" → "list" — the
        # description panel can hold a long story plus all the win
        # conditions, so it needs its own scroll state.
        self.focus: str = "list"
        self.cursor = (0, 0)
        self.desc_scroll = 0
        self._desc_gg: list[bool] = [False]
        self.unit_card: UnitCard | None = None

    # ---- async bridge ----

    async def prefetch_current(self) -> None:
        """Called once by the owning screen when the picker is first
        opened — fetches descriptions for ALL scenarios in parallel
        so the right-side list can show display_name (e.g. 'Journey
        to the West') from the very first frame instead of slugs
        (e.g. 'journey_to_the_west') that flip to display_name only
        after the user moves the highlight onto them.

        The current scenario is awaited so the left-side preview is
        populated immediately; the rest run in the background."""
        await self._ensure_loaded(self.scenarios[self.selected_idx])
        import asyncio as _asyncio

        for n in self.scenarios:
            if n in self._cache or n in self._in_flight:
                continue
            _asyncio.create_task(self._ensure_loaded(n))

    async def _ensure_loaded(self, name: str) -> None:
        if name in self._cache or name in self._in_flight or self.client is None:
            return
        self._in_flight.add(name)
        try:
            r = await self.client.call("describe_scenario", name=name)
            if r.get("ok"):
                from silicon_pantheon.client.locale.scenario import localize_scenario
                r["scenario_slug"] = name
                self._cache[name] = localize_scenario(r, self.locale)
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
        lc = self.locale
        root["hdr"].update(
            Text(f"{t('scenario_pick.header', lc)} — {t('scenario_pick.tab_switch', lc)} · {t('scenario_pick.enter_select', lc)} · {t('scenario_pick.esc_cancel', lc)}",
                 style="bold yellow")
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
        lc = self.locale
        if self.focus == "list":
            return Text(
                f"[{t('scenario_picker.title', lc)}] {t('scenario_pick.browse', lc)}   "
                f"{t('scenario_pick.enter_select', lc)}   "
                f"{t('scenario_picker.tab_preview', lc)}",
                style="dim",
            )
        if self.focus == "map":
            return Text(
                f"[{t('scenario_picker.map', lc)}] {t('scenario_pick.map_cursor', lc)}   "
                f"{t('scenario_picker.enter_unit', lc)}",
                style="dim",
            )
        return Text(
            f"[{t('panel.description', lc)}] {t('scenario_pick.desc_scroll', lc)}   "
            f"{t('scenario_picker.tab_list', lc)}",
            style="dim",
        )

    def _render_list(self) -> RenderableType:
        lines: list[Text] = []
        for i, name in enumerate(self.scenarios):
            is_selected = i == self.selected_idx
            marker = "●" if is_selected else "○"
            # Prefer the loaded display name. While the fetch is
            # in flight, fall back to a title-cased slug so the list
            # reads "Journey To The West" instead of
            # "journey_to_the_west" before the network roundtrip
            # completes — no jarring flip when fetches finish.
            desc = self._cache.get(name)
            display = desc.get("name") if desc else _slug_to_title(name)
            style = "bold yellow" if is_selected else "white"
            lines.append(Text(f"  {marker} {display}", style=style))
        return RichPanel(
            Group(*lines),
            title=t("scenario_picker.title", self.locale),
            border_style=border_style(self.focus == "list"),
            padding=(0, 1),
        )

    def _render_map(self) -> RenderableType:
        desc = self._current_desc()
        focused = self.focus == "map"
        # Card takes the full Map region while it's up — same UX as
        # the room/game MapPanels.
        if self.unit_card is not None:
            return RichPanel(
                self.unit_card.render(),
                title=t("scenario_picker.map", self.locale),
                border_style=border_style(focused),
                padding=(0, 1),
            )
        if desc is None:
            return RichPanel(
                Text(t("scenario_pick.loading_preview", self.locale), style="dim italic"),
                title=t("scenario_picker.map", self.locale),
                border_style=border_style(focused),
                padding=(0, 1),
            )
        board = desc.get("board") or {}
        w = int(board.get("width", 0))
        h = int(board.get("height", 0))
        terrain_entries = board.get("terrain") or []
        armies = desc.get("armies") or {}
        # Scenario-declared terrain_types (06_agincourt's mud / Troy's
        # xanthus / etc.). Same source the in-game map and room
        # preview use, so all three views agree on glyph + color.
        scenario_terrain_types = desc.get("terrain_types") or {}

        tile_type: dict[tuple[int, int], str] = {}
        for te in terrain_entries:
            tile_type[(int(te["x"]), int(te["y"]))] = str(te["type"])
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
                    g, st = unit_cell_style(u)
                else:
                    ttype = tile_type.get((x, y), "plain")
                    g, st = _terrain_cell(ttype, scenario_terrain_types)
                if focused and x == cx and y == cy:
                    text.append(f"[{g}]", style=f"reverse {st}")
                else:
                    text.append(f" {g} ", style=st)
            text.append("\n")
        footer = self._cursor_tooltip(tile_type, unit_at)
        return RichPanel(
            Group(text, Text(""), footer),
            title=t("scenario_picker.map", self.locale),
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
        line.append(f"{t('scenario_pick.terrain_label', self.locale)}: {terrain}", style="yellow")
        summary = terrain_effect_summary(self._current_desc(), terrain, self.locale)
        if summary:
            line.append(f" — {summary}", style="dim")
        u = unit_at.get((cx, cy))
        if u:
            owner = u.get("owner", "?")
            color = "cyan" if owner == "blue" else "red"
            name = unit_display_name(u, self._current_desc())
            line.append("   ")
            line.append(f"{name} ({owner})", style=f"bold {color}")
            line.append(f"   {t('game_map.enter_details', self.locale)}", style="dim italic")
        return line

    def _render_description(self) -> RenderableType:
        desc = self._current_desc()
        name = self._current_name()
        focused = self.focus == "desc"
        if desc is None:
            return RichPanel(
                Text(t("scenario_pick.loading", self.locale), style="dim italic"),
                title=_slug_to_title(name),
                border_style=border_style(focused),
                padding=(0, 1),
            )
        title = desc.get("name") or _slug_to_title(name)
        difficulty = desc.get("difficulty", 3)
        story = (desc.get("description") or "").strip()
        wcs = desc.get("win_conditions") or []
        armies = desc.get("armies") or {}
        unit_classes = desc.get("unit_classes") or {}
        rules = desc.get("rules") or {}

        rows: list[RenderableType] = []
        if story:
            rows.append(Text(story))
        level_name = t(f"difficulty.{difficulty}", self.locale)
        diff_text = Text()
        diff_text.append(f"{t('difficulty.label', self.locale)}: ", style="bold")
        diff_text.append("★" * difficulty + "☆" * (5 - difficulty), style="yellow")
        diff_text.append(f" ({level_name})", style="dim")
        rows.append(Text(""))
        rows.append(diff_text)
        if wcs:
            rows.append(Text(""))
            rows.append(Text(t("section.how_to_win", self.locale), style="bold"))
            for wc in filter_win_conditions(wcs):
                rows.append(
                    Text(f"  • {describe_win_condition(wc, desc, self.locale)}", style="dim")
                )
        rows.append(Text(""))
        rows.append(Text(t("section.armies", self.locale), style="bold"))
        for owner in ("blue", "red"):
            units = armies.get(owner, [])
            if not units:
                continue
            cls_counts: dict[str, int] = {}
            for u in units:
                cls_counts[u.get("class", "?")] = cls_counts.get(u.get("class", "?"), 0) + 1
            # Show display_name when the class has one — "1×Tang Monk"
            # reads better than "1×tang_monk".
            def _cls_label(slug: str) -> str:
                spec = unit_classes.get(slug) or {}
                return str(spec.get("display_name") or slug)
            summary = ", ".join(
                f"{n}×{_cls_label(c)}" if n > 1 else _cls_label(c)
                for c, n in cls_counts.items()
            )
            color = "cyan" if owner == "blue" else "red"
            from silicon_pantheon.client.tui.scenario_display import localized_team
            rows.append(Text(f"  {localized_team(owner, self.locale)}: {summary}", style=color))
        # Per-class descriptions for classes that are actually fielded.
        # Color each name by the team(s) that field it so the preview
        # reads like a team roster, not a neutral yellow dump.
        class_teams: dict[str, set[str]] = {}
        for team_name, army in armies.items():
            for u in (army or []):
                class_teams.setdefault(u.get("class"), set()).add(team_name)
        in_play = set(class_teams.keys())
        described = [
            (slug, unit_classes[slug])
            for slug in sorted(in_play)
            if slug and slug in unit_classes and unit_classes[slug].get("description")
        ]
        if described:
            rows.append(Text(""))
            rows.append(Text(t("section.units", self.locale), style="bold"))
            for slug, spec in described:
                name_str = spec.get("display_name") or slug
                teams = class_teams.get(slug, set())
                if teams == {"blue"}:
                    name_style = "bold cyan"
                elif teams == {"red"}:
                    name_style = "bold red"
                else:
                    name_style = "bold yellow"
                rows.append(Text(f"  {name_str}", style=name_style))
                rows.append(
                    Text(f"    {spec['description'].strip()}", style="dim")
                )
        max_turns = rules.get("max_turns")
        if max_turns:
            rows.append(Text(""))
            rows.append(Text(f"{t('section.max_turns', self.locale)}: {max_turns}", style="dim"))
        # Pre-wrap every row to the panel's inner width so scrolling
        # advances one visible display line per step — previously a
        # single long-paragraph row was atomic and disappeared in one
        # keypress. Short styled rows (headers like "Armies:") pass
        # through unchanged; long rows get split into multiple Text
        # items preserving their original style.
        try:
            cw = self.screen_console_width()
        except Exception:
            cw = 120
        # Description panel sits in the left column (ratio=3 of 4)
        # and occupies the bottom half of that column. Wrap width
        # approximates inner width minus border + padding.
        inner_width = max(20, int(cw * 3 / 4) - 6)
        rows = wrap_rows_to_width(rows, inner_width)
        # Apply scroll offset by trimming leading rows. Clamp first
        # so scrolling past the end snaps back instead of going blank.
        if self.desc_scroll > 0 and rows:
            self.desc_scroll = min(self.desc_scroll, max(0, len(rows) - 1))
            rows = rows[self.desc_scroll :]
        return RichPanel(
            Group(*rows),
            title=title,
            border_style=border_style(focused),
            padding=(0, 1),
        )

    def screen_console_width(self) -> int:
        """Best-effort console width lookup — the picker doesn't own
        the console, but most owners forward one via .console. Falls
        back to rich's get_console() which queries the terminal."""
        from rich.console import Console

        return Console().width

    # ---- input ----

    async def handle_key(self, key: str) -> bool:
        """Return True when the picker should close."""
        # Unit card consumes its own Esc/Enter first; snap the map
        # cursor to the unit that was being inspected.
        if self.unit_card is not None and key in ("esc", "enter", "q", "\t"):
            pos = self.unit_card.unit.get("pos") or {}
            self.cursor = (int(pos.get("x", self.cursor[0])),
                           int(pos.get("y", self.cursor[1])))
            self.unit_card = None
            return False

        if key == "esc":
            self.on_cancel()
            return True
        if key == "\t":
            order = ["list", "map", "desc"]
            i = order.index(self.focus)
            self.focus = order[(i + 1) % len(order)]
            return False

        if self.focus == "list":
            return await self._handle_list_key(key)
        if self.focus == "map":
            return self._handle_map_key(key)
        return self._handle_desc_key(key)

    def _handle_desc_key(self, key: str) -> bool:
        from silicon_pantheon.client.tui.panels import apply_vim_scroll

        nxt = apply_vim_scroll(
            key, current=self.desc_scroll, gg_state=self._desc_gg
        )
        if nxt is not None:
            self.desc_scroll = nxt
        return False

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
        # Card-mode navigation while a unit card is open.
        if self.unit_card is not None:
            if key in ("left", "h"):
                self.unit_card.navigate(-1)
                return False
            if key in ("right", "l"):
                self.unit_card.navigate(1)
                return False
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
            unit_classes = desc.get("unit_classes") or {}
            navigable: list[dict] = []
            for owner in ("blue", "red"):
                for u in (desc.get("armies") or {}).get(owner, []):
                    pos = u.get("pos") or {}
                    navigable.append({
                        "id": f"preview_{owner}_{u.get('class', '?')}",
                        "owner": owner,
                        "class": u.get("class"),
                        "pos": pos,
                    })
            navigable.sort(
                key=lambda u: (
                    int((u.get("pos") or {}).get("y", 0)),
                    int((u.get("pos") or {}).get("x", 0)),
                )
            )
            target_idx = next(
                (
                    i for i, u in enumerate(navigable)
                    if int((u.get("pos") or {}).get("x", -1)) == cx
                    and int((u.get("pos") or {}).get("y", -1)) == cy
                ),
                None,
            )
            if target_idx is not None:
                self.unit_card = UnitCard(
                    units=navigable,
                    index=target_idx,
                    unit_classes=unit_classes,
                    locale=self.locale,
                )
        self.cursor = (cx, cy)
        return False

    # ---- tick hook (owning screen calls this each poll) ----

    async def tick(self) -> None:
        """Opportunistic prefetch for the currently-highlighted scenario,
        so browsing feels responsive."""
        await self._ensure_loaded(self._current_name())
