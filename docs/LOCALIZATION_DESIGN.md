# Localization Design — Multi-language Support

## Goals
1. User selects language at login (English / Chinese initially)
2. All TUI text renders in the selected language
3. Scenario content (descriptions, unit names, objectives, narrative) in selected language
4. AI agent system prompts + turn prompts in selected language
5. Clean, pluggable — adding a 3rd language (Japanese, Korean, etc.) should be:
   - Add one TUI locale file
   - Add one `locale/<lang>.yaml` per scenario
   - Add one prompt template set
   - No code changes

## Architecture

### Layer 1: Client UI strings

```
src/silicon_pantheon/client/locale/
  en.yaml    # panel titles, status messages, key hints, error text
  zh.yaml
```

Example `zh.yaml`:
```yaml
panel:
  player: "玩家"
  map: "地图"
  reasoning: "推理"
  coach: "教练"
status:
  your_turn: "你的回合"
  opponent_turn: "对手回合"
  game_over: "游戏结束"
  agent_thinking: "AI思考中…"
  agent_idle: "AI空闲"
keys:
  tab_next: "Tab 下一面板"
  quit: "q 退出"
  range: "r 范围"
  scenario: "F3 剧情"
  help: "F2 帮助"
```

**Lookup function:**
```python
# client/locale/__init__.py
_cache: dict[str, dict] = {}

def t(key: str, locale: str = "en") -> str:
    """Lookup a UI string by dot-separated key.
    t("panel.map", "zh") → "地图"
    Falls back to English if the key is missing in the locale.
    """
    ...
```

**Where locale is stored:**
```python
# app.py SharedState
class SharedState:
    locale: str = "en"  # set at login
```

### Layer 2: Scenario localization

Each scenario has an optional `locale/` directory:

```
games/14_battle_of_bastards/
  config.yaml              # English (default, always the source of truth)
  locale/
    zh.yaml                # Chinese overrides
    ja.yaml                # Japanese (future)
  rules.py
  art/
```

The locale file mirrors the config structure but only contains
translatable string fields:

```yaml
# locale/zh.yaml
name: 私生子之战
description: |
  临冬城，303年。琼恩·雪诺从死中复活...

unit_classes:
  jon_snow:
    display_name: 琼恩·雪诺
    description: |
      白狼，由红袍女巫从死亡中唤醒。手持瓦雷利亚钢剑长爪...
  tormund:
    display_name: 托蒙德·裂骨
    description: |
      野人族长。像熊一样战斗...
  # ... each unit class gets display_name + description

narrative:
  title: 私生子之战
  description: |
    临冬城，303年。白狼对阵疯犬...
  intro: |
    黎明。两军在战场上对峙...
  events:
    - trigger: on_turn_start
      turn: 3
      text: |
        波顿盾墙开始收拢。长矛兵从两翼推进...
    # ... each narrative event

# Win condition descriptions (used by _describe_win_condition)
win_descriptions:
  - "蓝方在琼恩·雪诺死亡时失败（受蓝方保护）。"
  - "红方在拉姆塞·波顿死亡时失败（受红方保护）。"
  - "蓝方在琼恩·雪诺存活至回合上限时获胜（坚持到最后）。"
  - "任一方消灭所有敌方单位即可获胜。"
  - "回合达到上限时平局。"

terrain_types:
  corpse_pile:
    description: |
      初次骑兵冲锋中倒下的尸堆。难以穿越但提供掩护...
```

**Loader:**
```python
def load_scenario(name: str, locale: str = "en") -> GameState:
    """Load scenario, merging locale overrides if available."""
    config = _load_yaml(f"games/{name}/config.yaml")
    if locale != "en":
        locale_path = f"games/{name}/locale/{locale}.yaml"
        if exists(locale_path):
            overrides = _load_yaml(locale_path)
            config = _deep_merge(config, overrides)
    return _build_state(config)
```

The merge is **shallow-per-key**: if a locale file provides
`unit_classes.jon_snow.display_name`, it overrides just that field.
The stats (hp_max, atk, etc.) come from the base config and are
never in the locale file.

### Layer 3: System prompt localization

Prompt templates live alongside the English ones:

```
src/silicon_pantheon/harness/
  prompts.py           # English templates (existing)
  prompts_zh.py        # Chinese templates
```

Or, cleaner: locale-keyed template registry.

```python
# prompts.py
SYSTEM_PROMPT_TEMPLATES = {
    "en": """You are an AI player in "SiliconPantheon"...""",
    "zh": """你是"SiliconPantheon"中的AI玩家...""",
}

TURN_PROMPT_TEMPLATES = {
    "en": {
        "bootstrap": """It is turn {turn}...""",
        "delta": """It is turn {turn} of {max_turns}...""",
        "retry": """You did NOT call end_turn...""",
    },
    "zh": {
        "bootstrap": """现在是第{turn}回合...""",
        "delta": """现在是第{turn}回合（共{max_turns}回合）...""",
        "retry": """你没有在回合{turn}中调用end_turn...""",
    },
}
```

The `build_system_prompt` and `build_turn_prompt_from_state_dict`
functions accept a `locale` parameter and select the right template.

**Important:** tool NAMES stay English (`move`, `attack`, `get_state`)
because the LLM API tool schema is language-agnostic. Only the
descriptions, the prose, and the system-prompt instructions are
localized. The model calls the same tools regardless of locale.

### Layer 4: Login-time language selection

Add a language picker to the provider-auth / login screen:

```
┌─────────────────────────┐
│  Pick Language / 选择语言  │
│                         │
│  ► English              │
│    中文                  │
│                         │
│  Enter to confirm       │
└─────────────────────────┘
```

This fires BEFORE the provider picker. The selection is stored in
`SharedState.locale` and threaded through to all rendering + prompt
building.

### Layer 5: describe_scenario localization

The MCP `describe_scenario` tool returns the scenario bundle used
by the system prompt builder. It needs to accept a `locale`
parameter (or the room could store the locale so the server applies
it automatically).

Option A: Client-side merge — the client loads the locale file and
merges before building the prompt. No server change needed.

Option B: Server-side merge — `describe_scenario` accepts locale,
server does the merge. Cleaner for the "true agent" path where
Claude Code connects directly.

**Recommendation: Option A for now** (simpler, no server protocol
change). The client already has the scenario_description bundle
cached; it can merge the locale file on top.

### Data flow

```
User selects "中文" at login
  → SharedState.locale = "zh"
  → TUI renders panels with t("panel.map", "zh") → "地图"
  → Scenario loads with locale/zh.yaml merged
  → System prompt uses SYSTEM_PROMPT_TEMPLATES["zh"]
  → Turn prompts use TURN_PROMPT_TEMPLATES["zh"]
  → Win-condition descriptions use zh overrides
  → Narrative events use zh text
  → Agent sees everything in Chinese
  → Tool names stay English (move, attack, etc.)
```

### File count estimate per new language

| Component | Files | Effort |
|---|---|---|
| Client UI strings | 1 YAML | Small (100 keys) |
| Prompt templates | 1 Python file | Medium (3 templates × ~200 lines) |
| Per-scenario locale | 1 YAML per scenario | Large (31 scenarios × ~100 lines each) |
| Art (ASCII) | 0 (language-independent) | None |
| Unit stats | 0 (language-independent) | None |
| Code changes | 0 (if architecture is in place) | None |

Total for Chinese: ~33 files. For a 3rd language: ~33 more files,
zero code changes.

## Implementation phases

**Phase 1 — Infrastructure (small):**
- SharedState.locale field
- t() lookup function + en.yaml + zh.yaml for UI strings
- Language picker at login
- TUI panels use t() for titles, hints, status

**Phase 2 — Scenario localization (medium):**
- Locale YAML merge in describe_scenario path
- Create zh.yaml for 3-5 pilot scenarios (e.g. 14, 18, 25, 30)
- _describe_win_condition uses locale

**Phase 3 — Prompt localization (medium):**
- Chinese system prompt template
- Chinese turn prompt templates (bootstrap, delta, retry)
- Chinese batching-rule section
- Chinese no-constraint reminder

**Phase 4 — Full scenario translation (large):**
- zh.yaml for all 31 scenarios
- This is mostly translation work, not engineering
