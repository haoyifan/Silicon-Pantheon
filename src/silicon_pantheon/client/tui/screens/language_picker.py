"""Language picker — first screen on startup.

Sets SharedState.locale before any other screen renders, so every
subsequent panel, prompt, and scenario description uses the selected
language. Adding a new language = adding a YAML file to client/locale/
and it appears here automatically.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from silicon_pantheon.client.locale import available_locales, t
from silicon_pantheon.client.tui.app import Screen, TUIApp

# Display names for each locale code. If a locale isn't listed here
# it shows its code as-is. Extend this when adding new languages.
_DISPLAY_NAMES = {
    "en": "English",
    "zh": "中文（简体）",
    "ja": "日本語",
    "ko": "한국어",
}


class LanguagePickerScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self._locales = available_locales()
        self._selected = 0
        self._confirm = None
        # Pre-select current locale if set.
        for i, lc in enumerate(self._locales):
            if lc == app.state.locale:
                self._selected = i
                break

    def render(self) -> RenderableType:
        if self._confirm is not None:
            return self._confirm.render()
        lines: list[RenderableType] = []
        lines.append(Text("Pick Language / 选择语言", style="bold yellow"))
        lines.append(Text(""))
        for i, lc in enumerate(self._locales):
            name = _DISPLAY_NAMES.get(lc, lc)
            marker = "►" if i == self._selected else " "
            style = "bold white reverse" if i == self._selected else "white"
            lines.append(Text(f"  {marker} {name}", style=style))
        lines.append(Text(""))
        # Note about language impact — shown in both English and Chinese.
        lines.append(
            Text(
                "Language affects all game text AND how the AI agent thinks.\n"
                "语言设置会影响所有游戏文字以及AI的思考方式。",
                style="dim italic",
            )
        )
        lines.append(Text(""))
        lines.append(
            Text("Enter to confirm / 按 Enter 确认", style="dim")
        )
        return Align.center(
            Panel(
                Group(*lines),
                title="Language / 语言",
                border_style="bright_yellow",
                padding=(1, 3),
            ),
            vertical="middle",
        )

    async def handle_key(self, key: str) -> Screen | None:
        if self._confirm is not None:
            close = await self._confirm.handle_key(key)
            if close:
                self._confirm = None
            return None
        if key in ("down", "j"):
            self._selected = (self._selected + 1) % len(self._locales)
            return None
        if key in ("up", "k"):
            self._selected = (self._selected - 1) % len(self._locales)
            return None
        if key == "enter":
            chosen = self._locales[self._selected]
            self.app.state.locale = chosen
            # Proceed to the LOGIN screen (not directly to provider
            # auth). LoginScreen connects to the server and calls
            # set_player_metadata, then transitions to ProviderAuthScreen.
            # Skipping it means app.client is None and the lobby can't
            # create/join rooms.
            from silicon_pantheon.client.tui.screens.login import LoginScreen

            return LoginScreen(self.app)
        if key == "q":
            from silicon_pantheon.client.tui.widgets import ConfirmModal
            from silicon_pantheon.client.locale import t
            async def _quit(yes: bool) -> None:
                if yes:
                    self.app.exit()
            self._confirm = ConfirmModal(
                prompt=t("lobby_quit.confirm", self.app.state.locale),
                on_confirm=_quit,
                locale=self.app.state.locale,
            )
            return None
        return None
