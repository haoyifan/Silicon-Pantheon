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
        # Pre-select current locale if set.
        for i, lc in enumerate(self._locales):
            if lc == app.state.locale:
                self._selected = i
                break

    def render(self) -> RenderableType:
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
        if key in ("down", "j"):
            self._selected = (self._selected + 1) % len(self._locales)
            return None
        if key in ("up", "k"):
            self._selected = (self._selected - 1) % len(self._locales)
            return None
        if key == "enter":
            chosen = self._locales[self._selected]
            self.app.state.locale = chosen
            # Proceed to the provider/auth screen.
            from silicon_pantheon.client.tui.screens.provider_auth import (
                ProviderAuthScreen,
            )

            return ProviderAuthScreen(self.app)
        if key == "q":
            self.app.exit()
            return None
        return None
