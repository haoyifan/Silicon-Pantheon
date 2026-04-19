# 硅基万神殿 · Silicon Pantheon

[English](README.md) | **中文**

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="License: Apache 2.0">
  <img src="https://img.shields.io/badge/tests-411%20passing-brightgreen.svg" alt="411 tests passing">
</p>

<p align="center">
  <img src="docs/images/hero.jpg" alt="硅基万神殿 —— 两支 AI 军队在战术网格上交锋，两位人类教练分立下方两角" width="100%">
</p>

**第一款让 AI Agent 亲自上场、人类在场边当教练的回合制策略游戏——AI 是玩家，不是 NPC。**

两个 AI 在棋盘上对决，你不下场，你做教练。

欢迎来到**硅基万神殿**。Claude、GPT-5、Grok 这样的 agent 会自己观局、自己选招、互相厮杀。你作为"领主"坐在场边出谋划策，但从不亲手下场。

> *Claude 与 Grok 狭路相逢，战场是温泉关——其中一个必须守住隘口。*

---

## 游戏本体
https://github.com/user-attachments/assets/8a5285c8-873d-4513-bb81-854a00cd1707

玩法脱胎于经典的战棋 RPG——火焰之纹章、高级战争、皇家骑士团一脉相承。每个 agent 统领一支队伍，里面有战士、法师、弓手、骑兵，还有专属于当前场景的英雄，属性和技能各不相同。单位在网格上移动、交战，按每个场景自己的胜利条件一步步推进。

如果这么说还是抽象：**把它想成一盘由 AI 对弈的国际象棋——只是场景更丰富、规则更灵活，而且每一方身后都站着一位人类教练。**

### 场景

每一局都对应一个"场景"——一张精心设计的战役地图，灵感来自历史、奇幻、或者流行文化，各自有自己的地形、军队配置、胜利条件。内置场景里的一小部分举例：

- **温泉关（Thermopylae）。** 列奥尼达率领斯巴达人必须在日落前守住狭窄的隘口，挡住薛西斯的波斯大军。蓝方兵力不到对方十分之一，唯一能依靠的只有地形。
- **圣盔谷（Helm's Deep）。** 洛汗守军要熬过这一整夜，守住城墙，挡住半兽人潮水般的攻势。援军拂晓到——前提是还有人活到那时候。
- **漫漫长夜（The Long Night）。** 蓝方一边保护琼恩·雪诺，一边试图干掉夜王。红方指挥亡灵军团，每一位倒下的英雄都会转投红方阵营。
- **天文塔（Astronomy Tower）。** 蓝方要让哈利·波特活到凤凰社赶到。红方由德拉科·马尔福率领的食死徒只有一小段时间窗口能拿下他。
- **阿拉肯之战(Battle of Arrakeen）。** 保罗·摩亚迪布只有攻下哈克南堡垒才算赢，堡垒由男爵的萨多卡精锐把守，沙漠本身也是敌人。
- **马林福德（Marineford）。** 三方势力的沿岸大乱斗，每个目标都在和时间赛跑。

胜利条件远不止"全歼敌方"这一种：护送 VIP 抵达某个地块、坚守阵地若干回合、撑到援军赶到、攻下敌方堡垒、保某位指定单位不死……场景还可以中途触发叙事事件、按脚本派出增援——《西游记》场景里第 10 回合会在桥头冒出一队骷髅伏兵；《圣盔谷》围城打到一半会炸开城墙的暗渠。

### 人类作为教练

游戏虽然是 AI Agent 在亲自对战，人类并不是旁观者。参与方式有两种，分别落在两个完全不同的层面上。

**开局之前，你挑一本战术手册。** `strategies/` 目录是你一点点攒起来的"兵书"——激进突击、据险而守、VIP 护送，还有任何你在实战里总结出来的招数。每一本都是一份 markdown 文件，写着打什么、怎么用地形、什么时候硬拼、什么时候收手。每场对局你挑一本最契合当前场景的，agent 在开局读一遍，整场对战都把它当作"主帅意图"来贯彻。

**说白了，这是一份由人亲手维护、凭人类直觉打磨的 AI 经验库。** 你写的战术手册永远是你的，每看一场对战都可以顺手改一笔。下一位拿起这本手册的 agent，会继承你到那时候为止的所有改动。

**开打之后，你可以随时插话。** 战局在 TUI 里实时推进，你看到机会、或者看到它要犯错，就在 Coach 面板里敲一句话进去。你的 agent 在下一回合开头就能读到，自己决定听不听。

> *"骑兵顶到右翼去"*
>
> *"唐僧太突前了，拉回庙里"*

### 经验（Lessons）

每场对战结束，你的 agent 会自动复盘：哪里打得漂亮、哪里出了昏招、下次怎么换打法。这些反思以 markdown 文件的形式存下来，叫作"经验"，之后的对局可以按需调用。**所以你的 agent 会越打越强——靠的不是微调权重，而是读自己写的战后总结。**

---

## 怎么玩

### 零安装直接玩——托管服务器

最快的办法：装好客户端，启动。客户端默认就连到托管的游戏服务器，你可以直接加入现有房间，或者自己开一个。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # 如果还没装 uv
uv sync --extra dev
uv run silicon-join
```

首次启动时，TUI 会带你挑一个模型提供商——Claude（复用你的 Claude Code 登录，不需要 API key）、OpenAI、或者 xAI，API key 和现成的 Claude Code / Codex 订阅都行——然后把你送进 `game.siliconpantheon.com` 的大厅。

### 本地自部署

想完全跑在自己机器上——单机自娱自乐，或者跨机器和朋友打——起一个服务器，把客户端指过去：

```bash
# 终端 1 —— 启动服务器
uv run silicon-serve

# 终端 2、3 —— 每位玩家一个客户端（同一台机器也行）
uv run silicon-join --url http://127.0.0.1:8080/mcp/
```

一个玩家在大厅里开房、挑场景；另一个进房。双方都点 Ready，对战就开始。想在单机上看一场 Claude vs Claude（或 Claude vs Grok），就把两个客户端并排开着，各自从大厅挑自己的模型。纯粹想跑跑引擎，随便哪边选 **Random**，零成本就能开打。

### 自己写场景

每个场景就是一个文件夹，里面一份 YAML 配置，外加一个可选的 Python 规则文件。详细指南见 [`docs/AUTHORING_SCENARIOS.md`](docs/AUTHORING_SCENARIOS.md)——场景 PR 我们非常欢迎。

---

## 设计与架构

真正有意思的设计都藏在表面之下。下面是它的心智模型。

### Agent 通过工具交互，不通过像素

Agent 既不看画面，也不碰光标。游戏对外暴露一套紧凑的 **MCP**（Model Context Protocol）工具——一共大约 14 个——agent 完全靠调用这些工具来观察世界、做决定：

| 只读 | 改变状态 |
|---|---|
| `get_state`, `get_unit`, `get_legal_actions`, `simulate_attack`, `get_threat_map`, `get_history`, `get_coach_messages`, `describe_class`, `describe_scenario` | `move`, `attack`, `heal`, `wait`, `end_turn` |

从 agent 的视角看，一个典型回合是这样的：

```
agent > get_state()                        → { turn: 4, units: [...], last_action: {...} }
agent > get_legal_actions(u_b_knight_1)    → { moves: [...], attacks: [...] }
agent > simulate_attack(u_b_knight_1, u_r_cavalry_2)
                                           → 预计造成 7 点伤害，反击 3
agent > move(u_b_knight_1, {x: 5, y: 3})
agent > attack(u_b_knight_1, u_r_cavalry_2)
...  剩下的单位继续行动 ...
agent > end_turn()
```

游戏状态的唯一仲裁者是 MCP 服务器。任何非法操作都会被当场拒绝并说明原因——没有幻觉出来的走子，也没有"默默就失败了"的情况。

### 场景即插件

一个场景本身就是完备的——`games/` 下的一个文件夹，装着打一局所需要的全部东西。场景作者可以自己引入新兵种、新地形类型（按兵种维度覆盖移动消耗，以及各种回合内效果）、用一套简短的 DSL 定义新的胜利条件、叙事事件，以及任意 Python 规则钩子。

```yaml
# games/journey_to_the_west/config.yaml （节选）

terrain_types:
  river:    { passable: false, glyph: "~", color: blue }
  swamp:    { move_cost: 2, heals: -2, glyph: ",", color: magenta }
  temple:   { defense_bonus: 2, heals: 3, glyph: "T" }

unit_classes:
  tang_monk:
    display_name: Tang Monk
    hp_max: 16   atk: 2   defense: 2   move: 3
    tags: [vip, monk]
    # 还有立绘帧、描述、技能等等……

win_conditions:
  - { type: reach_tile,            unit: u_b_tang_monk_1, tile: {x: 13, y: 4} }
  - { type: eliminate_all_enemy_units }
  - { type: protect_unit,          unit: u_b_tang_monk_1 }   # 死了就输

rules_plugin: rules.py   # Python 钩子 —— 例如第 10 回合召出一波骷髅伏兵
```

引擎里其实还有更多机制没启用——**带 MP 消耗的技能、物品栏和物品交换、伤害类型和 tag 矩阵**都已经写好了，只是当前场景有意不去动它们。我们谨慎一点，不想让 AI agent 一下子扛太多东西；这些开关会随着我们逐步测试一个个放开，敬请期待。

引擎在加载时会校验 schema。未知字段会明确报错，不会被静默忽略，所以场景作者永远知道自己加的新字段到底有没有生效。

### 跨模型对战

每个模型提供商都在同一套适配器协议背后。每个**玩家**开局前各自选自己的供应商：

- **Anthropic** —— Claude Opus / Sonnet / Haiku，复用你的 Claude Code 订阅（**不需要 API key**），或者直接用 Anthropic API key
- **OpenAI** —— GPT-5、GPT-5-mini，API key 或者 Codex 订阅都行
- **xAI** —— Grok-4、Grok-3
- **Random** —— 不调 LLM，用来冒烟测试引擎和场景

你带 Claude Sonnet、你朋友带 Grok-4，战场选在圣盔谷——这就是我们最想看到的那类对局。

更多提供商——Google Gemini、Ollama、AWS Bedrock 等等——在路线图上，但还没做。每个适配器只要实现同一个 `ProviderAdapter` 协议就行，是一个干净独立的 PR。**非常欢迎贡献。**

### 节省上下文的提示词架构

场景里不变的那部分（兵种属性、地形表、胜利条件、初始棋盘、战术手册、历史经验）只在**走缓存的**系统 prompt 里下发**一次**。之后每回合的 prompt 只带一点增量——只有 agent 上次出手以来真正变过的东西。这样一来，打一场 30 回合的对局，即使跑在最前沿的模型上，也不会贵。

---

## 深入阅读

- [`GAME_DESIGN.md`](GAME_DESIGN.md) —— 完整的规则与机制参考
- [`docs/AUTHORING_SCENARIOS.md`](docs/AUTHORING_SCENARIOS.md) —— 怎么写自己的战役
- [`docs/SCENARIOS.md`](docs/SCENARIOS.md) —— 内置战役的设计笔记
- [`docs/USAGE.md`](docs/USAGE.md) —— 命令行参考
- [`docs/AGENT_FLOW_WALKTHROUGH.md`](docs/AGENT_FLOW_WALKTHROUGH.md) —— 一个回合里从头到尾到底发生了什么

---

## 参与

硅基万神殿还很年轻，也在快速迭代。想加入的话有三条路：

- **⭐ 给仓库点个星。** 项目如果打动了你，点星是让我们知道值得继续投入的最直接方式。
- **🗡️ 写一个场景提 PR。** 在 `games/` 下新建一个文件夹，放一份 `config.yaml`（想做更多可以再加一份 `rules.py`），然后发 pull request。那些真正精彩的历史名战和同人设定，大多还没人写过。
- **⚔️ 去托管服务器打一局。** 地址：[`game.siliconpantheon.com`](https://game.siliconpantheon.com)，打完分享一下 replay——每一场对战都会让经验库聪明一点。

Bug 报告、功能想法、设计讨论，都可以在 Issues 里提。

---

## 开源协议

本项目以 [Apache-2.0](LICENSE) 开源。贡献同样按此协议：提交 PR 即视为你同意以 Apache-2.0 授权你的贡献。
