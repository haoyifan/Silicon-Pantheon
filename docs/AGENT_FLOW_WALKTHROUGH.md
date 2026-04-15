# Agent flow walkthrough

End-to-end walkthrough of what the LLM-driven player sees, what it can
do, and how the server's session state machine reacts. Read this if you
want a single-page mental model of "what happens during a match" without
having to assemble it from `agent_bridge.py` + `prompts.py` +
`server/tools/__init__.py` + `server/heartbeat.py`.

The examples are all real — they come from running the code with a
dummy adapter and inspecting the messages list and server state. Field
names match what an agent author would actually see at runtime.

---

## 1. The big picture

```
                       ╔═══════════════════╗
                       ║  ServerSession    ║
                       ║  (engine.GameState)║
                       ╚═══════╤═══════════╝
                               │ tools (MCP)
                               │
   ┌─────────────────┐   call  │  result   ┌──────────────────┐
   │  NetworkedAgent │◀────────┴──────────▶│  Server tools/   │
   │  (agent_bridge) │                     │  game_tools.py   │
   └────────┬────────┘                     └──────────────────┘
            │
            │ play_turn(viewer)
            ▼
   ┌─────────────────────┐
   │  ProviderAdapter    │
   │  (openai / anthropic)│
   │                      │
   │  loops:              │
   │    chat.completions  │
   │    dispatch tools    │
   │    until end_turn or │
   │    iteration cap     │
   └──────────┬──────────┘
              │
              ▼
   ┌─────────────────────┐
   │   LLM provider      │
   │   (Anthropic /      │
   │    OpenAI / xAI)    │
   └─────────────────────┘
```

- **`NetworkedAgent`** — orchestrator. Lives **client-side** (same
  process as the TUI). The "Networked" prefix means it talks to a
  remote silicon-serve over MCP, not that it runs on the server.
  Each player's local client pays for / authenticates its own LLM
  provider; the server just adjudicates rules. Holds the persistent
  provider session, builds turn prompts, dispatches tool calls back
  to the server, tracks delta cursors.
- **`ProviderAdapter`** — provider-specific (Anthropic CLI session vs.
  OpenAI Chat Completions). Owns the conversation transcript with the
  LLM.
- **Server tools** — single source of truth for game state. Every
  action the agent takes goes through one of these tool calls.

---

## 2. What the LLM sees

Two distinct prompt shapes:

### 2a. The system prompt (sent ONCE at session open)

Built by `harness.prompts.build_system_prompt`. Roughly 200-400 lines
depending on the scenario. Includes:

- Team identity ("You are playing as **blue**.")
- Scenario name + description (the YAML's `description` block)
- Win conditions (one bullet per rule, in plain English — see
  `_describe_win_condition`; plugin rules surface their own
  `.description` attribute)
- Class catalog: every unit class fielded, with HP/ATK/DEF/RES/SPD/
  MOVE/RNG, flags (magic / can_heal / no forest / can enter mountain),
  description, and the slug agents use with `describe_class`.
- Terrain catalog (move_cost, defense_bonus, res_bonus, heals,
  description per type — including the scenario's custom terrain)
- Starting map grid (ASCII board with unit glyphs at turn-1 positions)
- Universal combat rules (damage formula, doubling, counter,
  determinism, max_turns, fog mode, fort heal)
- Per-turn unit-status lifecycle (`ready` / `moved` / `done` and what
  each allows / requires)
- "How to play" recipe (call `get_coach_messages` → assess → act →
  end_turn; reminders that `simulate_attack` is read-only)
- Strategy playbook (if `STRATEGY.md` configured)
- Prior lessons (post-match reflections from past games — if the
  Lessons toggle is on)

### 2b. The per-turn user prompt

Two shapes depending on whether it's the first turn or not.

**Bootstrap (turn 1 only)** — full state snapshot so the model has a
starting mental map:

```
It is turn 1 and it is your (blue) turn to play.

This is your first turn, so here is the full state snapshot.
Subsequent turns will only include what changed (opponent actions,
your unit status) — call `get_state` any time you need the full
picture.

```json
{
  "turn": 1,
  "active_player": "blue",
  "you": "blue",
  "board": {"width": 18, "height": 10, "forts": [...]},
  "units": [
    {"id": "u_b_trump_1", "owner": "blue", "class": "trump",
     "pos": {"x": 0, "y": 4}, "hp": 22, "status": "ready", "alive": true},
    ...
  ],
  "last_action": null
}
```

Play your turn. Remember to call end_turn at the end.
```

**Delta (turn 2+)** — only what changed:

```
It is turn 3 and it is your (blue) turn to play.

Opponent actions since your last turn:
- u_r_speedboat_1 moved to (5, 3)
- u_r_speedboat_2 attacked u_b_destroyer_1: damage=8, counter=3
- red ended turn

Your units:
- u_b_destroyer_1 (destroyer)  hp 32  pos (2, 3)  status ready
- u_b_trump_1 (trump)  hp 22  pos (0, 4)  status ready
- u_b_f35_1 (f35)  hp 24  pos (2, 2)  status ready
- ...

Call `get_state` if you need the full board / enemy positions /
fog-of-war map. Remember to call `end_turn` at the end.
```

The delta is ~500 bytes; the bootstrap is 5-10 KB.

---

## 3. What the LLM can do (the tool surface)

Defined in `agent_bridge.GAME_TOOLS`. Every tool comes with a JSON
schema the LLM consults. There are 14 of them, in three groups:

### Read-only (safe to call any time)

| Tool | Returns | Notes |
|---|---|---|
| `get_state` | `{turn, active_player, you, board(w/h/forts), units, last_action}` | Slimmed: no per-tile terrain, no `_visible_tiles` annotation, units carry only dynamic fields. Fog-filtered when room runs in classic / line_of_sight. |
| `get_unit(unit_id)` | flat `{id, owner, class, pos, hp, hp_max, atk, def, res, spd, rng, move, ...}` | Errors on dead units. |
| `get_legal_actions(unit_id)` | `{moves: [...], attacks: [...], heals: [...], wait: bool}` | Requires turn-active + own unit. |
| `simulate_attack(att, tgt, from_tile?)` | `{kind: "prediction", note, predicted_damage_to_defender, predicted_defender_dies, predicted_counter_damage, predicted_attacker_dies, ...}` | READ-ONLY. `kind`/`predicted_*` make conflation with `attack` impossible. |
| `get_threat_map` | `{threats: {"x,y": [unit_id, ...]}}` | Only visible enemies. |
| `get_history(last_n=10)` | `{history: [...], last_action, turn, active_player}` | Fog-filtered. |
| `get_coach_messages(since_turn=0)` | `{messages: [{turn, text}, ...]}` | Drains the coach queue. |
| `describe_class(class)` | `{class, spec}` | Cached scenario bundle — no server round-trip. |
| `describe_scenario` | full scenario bundle | Cached. |

### Mutating (require it to be your turn AND your unit)

| Tool | What it does |
|---|---|
| `move(unit_id, dest)` | Unit must be `READY`. Status flips to `MOVED`. |
| `attack(unit_id, target_id)` | Attacker must not be `DONE`. Sets `DONE`. |
| `heal(healer_id, target_id)` | Healer must have `can_heal`, target adjacent friendly. Sets `DONE`. |
| `wait(unit_id)` | Unit was `MOVED` or `READY`. Sets `DONE`. |
| `end_turn` | Hands control to the opponent. Rejected if any of YOUR units is currently in `MOVED` status. |

### Important precondition the model often forgets

`end_turn` rejects with `"unit X moved but has not acted; call
attack/heal/wait before end_turn"` if any of the agent's units is in
the `MOVED` state. The system prompt + the tool description both call
this out, and `wait(unit_id)` is the standard way to clear a moved
unit that has nothing useful to do.

---

## 4. Server state machine

### 4a. Per-unit status

```
   ┌───────┐   move   ┌───────┐  attack/heal/wait  ┌──────┐
   │ READY ├─────────▶│ MOVED ├───────────────────▶│ DONE │
   └───┬───┘          └───────┘                    └──┬───┘
       │                                               │
       │  attack/heal/wait                             │
       └──────────────────────────────────────────────▶│
                                                       │
                                       end_turn        │
                          (incoming team only)         ▼
                                                  ┌───────┐
                                                  │ READY │  (next turn)
                                                  └───────┘
```

### 4b. Per-half-turn flow

A "turn N" is two half-turns (blue then red, or whichever is
`first_player`). At each half-turn end:

1. `_apply_end_turn` runs in `engine/rules.py`:
   - terrain damage / heal applies to OUTGOING team
   - `active_player` flips to the enemy
   - if we wrapped back to `first_player`, `state.turn += 1`
   - INCOMING team's units reset to `READY`, fort heal applies (+3 HP
     for any incoming-team unit standing on its own fort)
   - plugin `on_turn_start` hooks fire
2. Win conditions evaluate. First match returns a `WinResult`.
3. If a win fires, `state.status = GAME_OVER`, `state.winner` set.

### 4c. Match-level state machine (server session)

```
         create_room   ─►  WAITING_FOR_PLAYERS
                               │
                  both join    ▼
                          WAITING_READY
                               │
              both ready'd     ▼
                          COUNTING_DOWN  (5s grace)
                               │
                               ▼
                            IN_GAME
                       ┌────────┴───────┐
                       │                │
              win fires│         disconnect forfeit
                       ▼                ▼
                    FINISHED         FINISHED
```

In `IN_GAME`:
- The active half-turn's player calls action tools.
- Heartbeat runs every few seconds; 30s silent → `soft_disconnect`,
  120s soft → auto-concede via `end_turn`-equivalent flag.

---

## 5. Worked example: happy-path full match

Setup: `02_basic_mirror`, fog `none`, blue first, max_turns 10. Blue
is a NetworkedAgent on Grok-4; red is a NetworkedAgent on Sonnet.

### t=0  Both clients enter the room

- Server: `WAITING_FOR_PLAYERS` → `WAITING_READY` → `COUNTING_DOWN`
  → `IN_GAME` once both ready and the 5s grace passes.
- Server creates the `Session`, loads scenario YAML, builds initial
  GameState. `state.turn = 1`, `state.active_player = blue`.

### Turn 1 — blue plays

**Client side (blue's NetworkedAgent):**

1. TUI poll: `get_state` returns `active_player=blue`, `you=blue`.
2. `_maybe_trigger_agent` fires because turn ownership matches.
3. `play_turn(viewer=Team.BLUE, max_turns=10)` enters:
   ```
   play_turn ENTER team=blue turns_played=0 no_progress_retries=0
   ```
4. Re-fetches state to defend against stale-poll race. Confirms
   active_player=blue.
5. Builds the bootstrap user prompt (full state snapshot — turn 1 only).
6. Lazy-fetches `describe_scenario` for the system-prompt scenario
   bundle. Builds the system prompt (~12 KB) and logs it.
7. Calls `adapter.play_turn(system, user, tools, dispatcher)`.

**Adapter side (OpenAI/Grok flavor):**

1. First call: appends system + user1 to the empty `_messages` list.
2. Loop iteration 0:
   ```
   iter 0: messages=2 est_tokens=3041
   ```
3. POST to `chat.completions.create`. Model returns:
   ```
   reasoning_content: "Blue starts at (0,4). I should..."
   tool_calls: [{name: "get_legal_actions", args: {"unit_id":"u_b_x_1"}}]
   ```
4. Surfaces reasoning via `on_thought` → renders in the Reasoning panel.
5. Dispatches the tool. Server runs `get_legal_actions`, returns
   `{moves: [...], attacks: [...], wait: true}` — slimmed if it goes
   through `_slim_tool_response` (it doesn't here — only `get_state`
   gets slimmed).
   ```
   tool dispatch: name=get_legal_actions args_keys=['unit_id'] result_bytes=412 (capped=412)
   ```
6. Tool result appended as `{role: "tool", tool_call_id, content: ...}`.
7. Iteration 1: model picks a move:
   ```
   tool_calls: [{name: "move", args: {"unit_id":"u_b_x_1", "dest":{"x":1,"y":4}}}]
   ```
   Server runs `_apply_move` → unit's `pos` updates, `status` becomes
   `MOVED`. Returns `{type: "move", unit_id: "u_b_x_1", dest: {...}}`.
8. Iteration 2: model attacks from new position:
   ```
   tool_calls: [{name: "attack", args: {"unit_id":"u_b_x_1", "target_id":"u_r_y_1"}}]
   ```
   Server runs `_apply_attack` → damage applied, counter happens if
   the defender survives + can range, attacker.status becomes `DONE`.
9. Iterations 3-N repeat for each ready unit.
10. Eventually the model calls `end_turn`. Server runs `_apply_end_turn`:
    - active_player flips: `blue` → `red`
    - `state.turn` does NOT increment yet (still wrapping)
    - red's units reset to `READY`
    - on_turn_start plugin hooks fire
    - returns `{type: "end_turn", by: "blue"}`
11. Adapter loop: model returns again with no tool_calls (it's done).
    Loop exits with `loop exit: no tool_calls (iter=12, ...)`.
12. Adapter returns from `play_turn`.

**Back in NetworkedAgent.play_turn:**

13. `_fetch_state` → `active_player=red` ≠ viewer (blue). Turn ended.
14. `_turns_played = 1`, `_no_progress_retries = 0`.
15. `get_history(last_n=0)` to advance `_history_cursor` past blue's
    actions.
16. Logs:
    ```
    play_turn EXIT OK turn_ended=True turns_played=1 history_cursor=8
    ```

### Turn 1 — red plays (same flow on red's TUI process)

Identical structure. After red ends turn:
- active_player flips: `red` → `blue`
- `state.turn` increments to **2** (wrapped back to first_player)
- blue's units reset to `READY`

### Turn 2 — blue plays

**Client side:**

1. TUI poll detects `active_player=blue`. Trigger.
2. `play_turn ENTER team=blue turns_played=1` — turns_played > 0, so
   we'll build a delta prompt.
3. `get_history(last_n=0)` → fetch full history; slice from
   `_history_cursor` (8) onward to get actions since blue last played.
   Result = red's turn-1 actions.
4. `build_turn_prompt_from_state_dict(state, viewer, is_first_turn=False,
   new_history=red_actions)` returns the delta prompt.
5. `adapter.play_turn(system, user, ...)`:

**Adapter side:**

1. `_messages` already has system + user1 + (turn-1 assistant +
   tool-call/result pairs). Compaction runs:
   - First system kept verbatim.
   - user1 (bootstrap snapshot) kept.
   - All assistant messages: keep `content` (capped 1500 chars), keep
     `tool_calls` field (lets Grok see the proper protocol pattern).
   - All tool messages: kept structurally, content replaced with
     `"[result trimmed for context bound]"`.
   ```
   compacted transcript: 18421→6803 est_tokens (24 messages)
   ```
2. Append user2 (the delta).
3. Loop runs. Tool calls happen. Model calls `end_turn`.
4. Returns.

**NetworkedAgent:**

5. Turn ended. `_turns_played = 2`. Cursor advanced.

### Turns 3-10

Same delta-prompt pattern. Compaction keeps growth flat — typical
mid-game `est_tokens` should hover around 8-15 KB regardless of how
many turns have elapsed.

### Turn N — match ends

Either:
- **Win condition fires**. `_apply_end_turn` evaluates conditions
  after the half-turn ended. First condition that returns a WinResult
  wins. `state.status = GAME_OVER`, `state.winner` set.
- **`state.turn > max_turns`**. `max_turns_draw` returns `winner=None`.

NetworkedAgent's `_fetch_state` sees `status=game_over`. play_turn
returns early without invoking the adapter:
```
play_turn: match already game_over; skipping
```

TUI transitions to PostMatchScreen. If lessons toggle is on,
`summarize_match` runs (separate one-shot LLM call) and writes a
Lesson markdown file.

---

## 6. Edge cases

### 6a. Stale-poll race — TUI triggers play_turn but server flipped

TUI's poll says `active_player=blue` (1s stale). `_maybe_trigger_agent`
spawns play_turn. Inside play_turn, fresh `get_state` says
`active_player=red` (opponent forfeited / heartbeat auto-conceded).

Defense: `play_turn` re-checks ownership on fresh state and bails:
```
play_turn: fresh state says active=red but we are blue; skipping turn
```
Returns without calling the adapter — no wasted tokens, no false "your
turn" prompt to the LLM.

### 6b. Mid-turn re-entry — adapter exited without end_turn

Possible causes: max_iterations hit, time budget exhausted, model
hallucinated XML tool calls (and the corrective reminder didn't help).

Flow:
- Adapter returns. NetworkedAgent: `post_state.active_player == blue`
  still. Bookkeeping does NOT advance: `_turns_played` stays put,
  `_history_cursor` stays put, `_no_progress_retries += 1`.
- TUI poll re-triggers play_turn next tick.
- After 3 stuck retries, the watchdog at agent_bridge:
  ```
  agent stuck (no end_turn after 3 retries); forcing end_turn
  ```
  Calls `end_turn` server-side directly. Active flips. Game progresses.

### 6c. xAI / Grok hallucinates XML function-call tags

Older xAI models trained on `<function_call>tool(args)</function_call>`
demos sometimes emit those as plain content text instead of using the
API's `tool_calls` field. Server never sees a tool call.

Defense:
1. Adapter detects the pattern in `msg.content`. Logs:
   ```
   model emitted XML-style function-call text instead of using API tool_calls
   ```
2. Injects ONE corrective system message reminding it of the right
   protocol and continues the loop.
3. If the model still emits XML, the loop exits with no tool_calls.
4. Mid-turn re-entry watchdog (6b) catches the persistent failure
   after 3 retries.

The corrective system message is dropped at the next turn-boundary
compaction so it doesn't pollute the transcript permanently.

### 6d. Context window blow-up

Was the original "maximum prompt length is 131072 but the request
contains 351186" symptom. Two roots, both fixed:

1. **`get_state` was returning all 180 board tiles every call.** Slimmer
   now drops `board.tiles` (the terrain map is in the system prompt)
   and `_visible_tiles` annotation. Per-call payload drops ~70%.
2. **Compaction stripped `tool_calls` from prior assistants.** That
   destroyed the format-pattern Grok needed to see in its own history,
   leading to the XML hallucination loop in 6c. Compaction now keeps
   `tool_calls` and stubs the paired tool result content instead.

Mid-turn safety net: SOFT 90k → inject "wrap up, call end_turn" nudge;
HARD 120k → force-break the loop. Both below Grok's 131k.

### 6e. Coach sends a message mid-turn

Coach (a human-typed message via the room screen's chat panel) appends
to `session.coach_queues[team]`. Agent next reads it via
`get_coach_messages` (called once at turn start per the system prompt
recipe). Message is removed from the queue on read.

If the agent never calls `get_coach_messages` they miss the message
that turn. Coach can resend.

### 6f. Opponent disconnects mid-match

Heartbeat:
- 30s silent → `soft_disconnect`. Opponent's TUI shows "opponent
  disconnected" banner.
- 120s soft → `_auto_concede`: server sets `state.status = GAME_OVER`,
  `state.winner = the surviving team`. Logged.

Surviving client's next poll sees `status=game_over`, transitions to
PostMatchScreen. If the agent was mid-turn when the concede landed,
play_turn's game_over check at start short-circuits cleanly.

### 6g. Unit dies mid-attack

`_apply_attack` mutates HP, then calls `_remove_dead_units` which
moves the dead unit from `state.units` to `state.fallen_units` (a
parallel dict). Engine invariants (`units_of`, `unit_at`, occupancy)
operate on `state.units` only. `state_to_dict` serializes BOTH so the
agent and the TUI roster still see the unit with `alive: false, hp: 0`.

Win conditions like `protect_unit` check `state.dead_unit_ids` so they
fire even after the unit's record has been moved out of `state.units`.

### 6h. Plugin scenarios that mutate the board

Some scenarios mutate terrain mid-match (e.g. Helm's Deep's culvert
explosion, or a sea-mine detonation). Engine writes to
`state.board.tiles[pos]`. The system prompt's static `map_grid` is
stale after that point.

Agent recourse: call `get_state` to refetch — the response no longer
includes `board.tiles` (we slim it), so this case isn't fully covered.
**Known gap** — if a scenario does dynamic terrain mutation, the agent
won't see it without us either un-slimming `board.tiles` for that
scenario or surfacing the change via a coach-message-style notification.
Not currently used by any built-in scenario but worth flagging.

---

## 7. What you'll see in the logs

Two log streams to correlate when something goes wrong: the client log
written by `silicon-join`, and the server log written by
`silicon-serve`.

### Client log: `~/.silicon-pantheon/logs/client-<name>-*.log`

Healthy turn:

```
play_turn ENTER team=blue turns_played=4 no_progress_retries=0 history_cursor=23
session init: system_prompt_bytes=12044 (~3011 est_tokens)        # only on turn 1
turn start: messages=21 est_tokens=8042 user_prompt_bytes=487
turn start breakdown: by_role: assistant=2440B/9msg, system=12033B/1msg, tool=320B/8msg, user=10810B/3msg | top5: #0 system 12033B; #1 user 10048B; ...
iter 0: messages=21 est_tokens=8042
openai/xai response [model=grok-4 iter=0]: keys=[...] content_len=287 ...
iter 0 response: hash=ab12cd34ef89 repeats=0 content_preview='I will check legal actions for u_b_x_1' tool_call_names=[get_legal_actions]
tool dispatch: name=get_legal_actions args_keys=['unit_id'] result_bytes=412 (capped=412)
iter 1: messages=23 est_tokens=8398
iter 1 response: hash=ff34ac9b1244 repeats=0 content_preview='Now I will move' tool_call_names=[move]
tool dispatch: name=move args_keys=['unit_id', 'dest'] result_bytes=89 (capped=89)
...
loop exit: no tool_calls (iter=11, hallucinated_xml=False, corrections=0, content_len=42)
play_turn EXIT OK turn_ended=True turns_played=5 history_cursor=29
```

Stuck-loop signature ("agent reprinting same paragraph"):

```
iter 4 response: hash=ab12cd34ef89 repeats=0 ...
iter 5 response: hash=ab12cd34ef89 repeats=1 ...
iter 6 response: hash=ab12cd34ef89 repeats=2 ...
iter 7 response: hash=ab12cd34ef89 repeats=3 ...
loop exit: model emitting identical responses (hash=ab12cd34ef89, 3 repeats in a row at iter=7) — breaking to prevent spew loop
play_turn EXIT WITHOUT end_turn (active still blue); no_progress_retries=1/3
```

Context-overflow signature:

```
soft token limit 90000 crossed at iter=12 ... breakdown: by_role: tool=78000B/14msg, ...
loop exit: HARD token limit 120000 reached at iter=14 ... breakdown: by_role: tool=104000B/18msg ...
```

If a 400 actually lands (provider rejected before our cap):

```
OpenAI completion raised (model=grok-4 status=400 body={'error': {'message': 'maximum prompt length...'}}) messages_count=37 est_tokens=87901 last_role=tool | breakdown: by_role: assistant=12044B/14msg, system=12033B/1msg, tool=287122B/15msg, user=10810B/4msg | top5: #21 tool[tc-abc12345] 38214B; #15 tool[tc-9f...] 28100B; ...
```

The `breakdown` line tells you exactly which message types and which
INDIVIDUAL messages are bloating the transcript. `top5` names the worst
offenders by index — cross-reference with the `tool dispatch` lines
to figure out which specific call ballooned.

### Server log: `silicon-serve` stdout (or wherever you redirected it)

Every game-tool call is logged with the connection id (truncated) for
correlation:

```
tool dispatch: cid=a1b2c3d4 tool=get_state viewer=blue active=blue turn=4 args={}
tool dispatch: cid=a1b2c3d4 tool=move viewer=blue active=blue turn=4 args={"unit_id": "u_b_x_1", "dest": {"x": 5, "y": 3}}
tool rejected: cid=a1b2c3d4 tool=end_turn viewer=blue err=unit u_b_y_2 moved but has not acted
post-end_turn viewer=blue active=red turn=4 units=u_b_x_1=ready,u_b_y_2=ready,...
```

Match the client's `tool dispatch: name=move` to the server's
`tool dispatch: cid=... tool=move` to confirm a call landed (and
how the server interpreted it).

### Recommended grep one-liner

```
grep -E "play_turn|iter |response: hash|tool dispatch|loop exit|breakdown|token limit|stuck|forcing end_turn" \
  ~/.silicon-pantheon/logs/client-*.log
```

For the server side just `tail -F` the silicon-serve output.

---

## 8. Where things live in the code

| Concept | File | Key symbol |
|---|---|---|
| System prompt | `harness/prompts.py` | `build_system_prompt` |
| Per-turn user prompt | `harness/prompts.py` | `build_turn_prompt_from_state_dict` |
| Tool catalog (what LLM sees) | `client/agent_bridge.py` | `GAME_TOOLS` |
| Tool dispatch + slimming | `client/agent_bridge.py` | `_dispatch_tool`, `_slim_tool_response` |
| Provider session loop | `client/providers/openai.py` | `OpenAIAdapter.play_turn` |
| Compaction | `client/providers/openai.py` | `_compact_prior_turns` |
| Watchdog / cursor advance | `client/agent_bridge.py` | `NetworkedAgent.play_turn` (post-adapter section) |
| Server tool handlers | `server/tools/__init__.py` | `get_state`, `move`, `attack`, ... |
| Rules / state transitions | `server/engine/rules.py` | `apply`, `_apply_attack`, `_apply_end_turn` |
| Win conditions | `server/engine/win_conditions/rules.py` | `seize_enemy_fort`, `protect_unit`, ... |
| Fog filter | `shared/viewer_filter.py` | `filter_state`, `filter_history` |
| Heartbeat / disconnect | `server/heartbeat.py` | `run_sweep_once` |
