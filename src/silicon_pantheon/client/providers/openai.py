"""OpenAI adapter — Chat Completions API + function calling.

Persistent session model: we keep a single `messages: list[dict]`
transcript per match. `play_turn` appends the system (on turn 1)
+ user prompt, then loops: send → receive → dispatch any tool
calls → append results → send again, until the model stops
emitting tool calls or we hit the time budget. The transcript
continues across turns so chain-of-thought persists.

Manual transcript maintenance (rather than the Responses API's
server-side conversation storage) keeps the adapter simple and
portable to any OpenAI-compatible endpoint (xAI, Together, Groq,
etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from openai import AsyncOpenAI

from silicon_pantheon.client.providers.base import (
    ThoughtCallback,
    ToolDispatcher,
    ToolSpec,
)
from silicon_pantheon.client.providers.anthropic import _parse_lesson_json
from silicon_pantheon.client.providers.errors import classify
from silicon_pantheon.client.providers.reasoning import _extract_reasoning
from silicon_pantheon.lessons import Lesson, slugify
from silicon_pantheon.server.engine.state import Team

log = logging.getLogger("silicon.provider.openai")


def _as_openai_tool(spec: ToolSpec) -> dict:
    """Convert our generic ToolSpec to OpenAI's function-tool format."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema,
        },
    }


class OpenAIAdapter:
    """ProviderAdapter for OpenAI Chat Completions with function calls."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str | None = None,
        max_iterations_per_turn: int = 20,
    ):
        self.model = model
        # Lowered from 40 → 20: a healthy turn needs maybe 8-12 tool
        # round-trips. 40 was a generous safety margin that turned
        # into a footgun — a stuck/looping model could fill the
        # transcript with 40 iterations of garbage in a single turn
        # before the loop exited.
        self.max_iterations = max_iterations_per_turn
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._messages: list[dict] = []
        self._system_prompt: str | None = None
        self._corrections_this_turn: int = 0
        # Repeat-detection: when the model emits the same assistant
        # content twice in a row in a single turn we're in a sterile
        # loop (no progress, just regurgitating the same prose).
        # Track last-seen content hash + repeat count for logging
        # / early-break.
        self._last_content_hash: str | None = None
        self._consecutive_repeats: int = 0
        # Accumulated telemetry for post-game stats.
        self.total_tokens: int = 0
        self.total_tool_calls: int = 0
        self.total_errors: int = 0
        # Monotonic timestamp when the currently-pending LLM API call
        # started, or None when we're not waiting on the provider.
        # Set just before chat.completions.create() and cleared as
        # soon as the response returns. NetworkedAgent reads this
        # via adapter_elapsed_s() so the TUI can render "thinking
        # (Xs)" — tells operators that a minutes-long wait (grok-4
        # reasoning) is the model thinking, not a wedged client.
        self.api_call_started_at: float | None = None

    # ---- transcript bookkeeping ----

    @staticmethod
    def _estimate_tokens(messages: list[dict]) -> int:
        """Rough token count — ~4 characters per token for English +
        code. Good enough to detect context-window blow-ups before
        the provider rejects; tiktoken would be exact but adds a
        runtime dep and matters only for a warning threshold."""
        total = 0
        for m in messages:
            # content + tool_calls.function.arguments + tool name are
            # the main bulk. Walk them generically via json.dumps so
            # a provider-specific field doesn't slip past the count.
            total += len(json.dumps(m, default=str))
        return total // 4

    @classmethod
    def _transcript_breakdown(cls, messages: list[dict]) -> str:
        """Per-role / per-message-type token breakdown for diagnostics.

        Returns a single-line string suitable for log output. Walks
        every message, bucketing bytes by role, and also dumps the
        top-5 individual messages by size so we can see WHICH
        message (e.g. "tool result for get_state at index 14, 12KB")
        is causing trouble. This is the line you'd grep for on a
        context-overflow report — it answers "what's bloating the
        prompt".
        """
        bucket_bytes: dict[str, int] = {}
        bucket_count: dict[str, int] = {}
        per_message: list[tuple[int, int, str, str]] = []
        for i, m in enumerate(messages):
            role = m.get("role", "?")
            raw = json.dumps(m, default=str)
            n = len(raw)
            bucket_bytes[role] = bucket_bytes.get(role, 0) + n
            bucket_count[role] = bucket_count.get(role, 0) + 1
            # Per-message sample for the top-5 dump.
            tag = role
            if role == "tool":
                # Show what tool_call_id this matched so we can
                # cross-reference with the dispatch log.
                tag = f"tool[{m.get('tool_call_id', '?')[:8]}]"
            elif role == "assistant" and m.get("tool_calls"):
                names = [
                    tc.get("function", {}).get("name", "?")
                    for tc in m.get("tool_calls") or []
                ]
                tag = f"assistant[tc={','.join(names)}]"
            per_message.append((n, i, tag, role))
        per_message.sort(reverse=True)
        roles_summary = ", ".join(
            f"{r}={bucket_bytes[r]}B/{bucket_count[r]}msg"
            for r in sorted(bucket_bytes)
        )
        top5 = "; ".join(
            f"#{idx} {tag} {n}B" for n, idx, tag, _r in per_message[:5]
        )
        return f"by_role: {roles_summary} | top5: {top5}"

    # Replacement string for trimmed tool results. Short on purpose
    # — long enough to be unambiguous if it surfaces in logs, short
    # enough not to add up at scale.
    _STUB_TOOL_RESULT = "[result trimmed for context bound]"
    # Cap on assistant prose length per message in compacted form.
    # Long chain-of-thought from prior turns rarely matters now;
    # a few hundred chars preserves the gist.
    _ASST_CONTENT_CAP = 1500
    # Cap on user-message content in compacted form. The bootstrap
    # (turn-1 full state snapshot) weighs 5-10 KB and stays in the
    # transcript forever; by turn 2+ the model has either absorbed
    # that info or can re-fetch via get_state. Delta turn prompts
    # are small (~1-2 KB) and fit comfortably under this cap, so
    # they pass through unchanged.
    _USER_CONTENT_CAP = 3000

    def _compact_prior_turns(self) -> None:
        """Shrink completed turns to bound the context window without
        destroying the conversation's structural cues.

        Called at the START of each new turn's play_turn — every
        message already in self._messages is from a completed turn
        and safe to rewrite. Earlier strategy was "drop tool_calls
        and tool messages, keep prose only" but that broke xAI /
        Grok: with no `tool_calls` examples in their own conversation
        history, those models fell back to emitting
        `<function_call>...</function_call>` in plain text.

        Current strategy preserves the SHAPE of the conversation:

          - system / user — kept verbatim.
          - assistant — keep content (capped to _ASST_CONTENT_CAP)
            AND keep tool_calls field as-is. The model needs to
            see "I called these tools last turn" so it knows the
            native tool_calls protocol is the right way.
          - tool — keep the message structure (role + tool_call_id)
            but replace .content with a small stub. This keeps the
            transcript valid (every assistant.tool_calls has its
            matching tool result) AND drops the heavy payload that
            was the actual problem (get_state dumps were the main
            offender).

        The token cost per "stubbed" tool round-trip drops from
        ~5KB to ~80 bytes while the model still sees both halves
        of the conversation, including the proper tool-call shape.
        """
        if len(self._messages) <= 1:
            return  # just the system prompt; nothing to compact

        compacted: list[dict] = []
        # Track how many "system" messages we've kept so far. The
        # FIRST one is the canonical system prompt and always stays.
        # Every subsequent system message is one of OUR per-turn
        # corrective injections (e.g. "your previous message used
        # XML function-call tags — use the native protocol"). Those
        # were turn-scoped nudges; carrying them forever pollutes
        # the transcript and inflates token count linearly with the
        # number of stuck turns we've recovered from.
        system_kept = 0
        for m in self._messages:
            role = m.get("role")
            if role == "system":
                system_kept += 1
                if system_kept == 1:
                    compacted.append(m)
                # Drop subsequent systems (corrective reminders from
                # prior turns).
                continue
            if role == "user":
                # Truncate oversize user messages (specifically the
                # turn-1 bootstrap snapshot). Delta prompts fit under
                # the cap and pass through unchanged.
                content = m.get("content") or ""
                if len(content) > self._USER_CONTENT_CAP:
                    content = (
                        content[: self._USER_CONTENT_CAP]
                        + "…[bootstrap snapshot truncated; call "
                        "`get_state` if you need fresh board data]"
                    )
                compacted.append({"role": "user", "content": content})
                continue
            if role == "tool":
                # Preserve the structural pairing (assistant.tool_calls
                # → matching tool result) but drop the heavy payload.
                # OpenAI rejects orphaned tool_calls, so we MUST keep
                # the message — just empty its content.
                compacted.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": self._STUB_TOOL_RESULT,
                })
                continue
            if role == "assistant":
                content = m.get("content") or ""
                if len(content) > self._ASST_CONTENT_CAP:
                    content = content[: self._ASST_CONTENT_CAP] + "…[truncated]"
                new_m: dict = {"role": "assistant", "content": content}
                if m.get("tool_calls"):
                    # Keep the tool_calls metadata so the model sees
                    # the right protocol from its own history. The
                    # paired tool result above is now a stub but the
                    # API still validates the pairing.
                    new_m["tool_calls"] = m["tool_calls"]
                # Preserve reasoning_content if upstream attached it
                # (DeepSeek V4 thinking-mode requires it round-tripped
                # on every assistant message; without this carry the
                # next turn's first API call 400s).
                if m.get("reasoning_content"):
                    new_m["reasoning_content"] = m["reasoning_content"]
                compacted.append(new_m)
                continue
        self._messages = compacted

    async def play_turn(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpec],
        tool_dispatcher: ToolDispatcher,
        on_thought: ThoughtCallback | None = None,
        time_budget_s: float = 1800.0,
    ) -> None:
        # First-turn init: seed the transcript with the system prompt.
        if not self._messages:
            self._messages.append({"role": "system", "content": system_prompt})
            self._system_prompt = system_prompt
            log.info(
                "session init: system_prompt_bytes=%d (~%d est_tokens)",
                len(system_prompt), len(system_prompt) // 4,
            )
        else:
            # Every turn after the first: compact completed turns so
            # the transcript doesn't grow without bound and hit the
            # provider's context limit on turn 5-6. See docstring.
            before = self._estimate_tokens(self._messages)
            self._compact_prior_turns()
            after = self._estimate_tokens(self._messages)
            if before != after:
                log.info(
                    "compacted transcript: %d→%d est_tokens (%d messages)",
                    before, after, len(self._messages),
                )

        self._messages.append(
            {"role": "user", "content": user_prompt}
        )
        # Reset per-turn corrective-reminder counter — we cap how many
        # times we inject "use proper tool_calls" reminders so a
        # stubbornly mis-formatting model can't loop forever.
        self._corrections_this_turn = 0
        # Reset spew-loop detector for the new turn — repeats only
        # count within a single turn.
        self._last_content_hash = None
        self._consecutive_repeats = 0
        # Log token estimate AND the per-role breakdown each turn so
        # the trajectory + the bloat source are both visible from the
        # log alone. user_prompt is logged separately because it can
        # be huge (turn-1 bootstrap snapshot is 5-10 KB).
        log.info(
            "turn start: messages=%d est_tokens=%d user_prompt_bytes=%d",
            len(self._messages),
            self._estimate_tokens(self._messages),
            len(user_prompt),
        )
        log.info(
            "turn start breakdown: %s",
            self._transcript_breakdown(self._messages),
        )

        openai_tools = [_as_openai_tool(s) for s in tools]
        # Name -> mutates? lookup used by Layer 2 to partition each
        # response's tool_calls into reads (execute all) vs mutations
        # (execute only the first). Unknown names default to read-only
        # — a new tool added without flagging is safest as a read.
        mutates_by_name: dict[str, bool] = {s.name: s.mutates for s in tools}
        start = time.time()

        # No mid-turn nudges: the soft-token "wrap up" system message
        # was removed because it made the model give up and call
        # end_turn immediately, hiding real strategic failures behind
        # a context-limit excuse. Now the model is free to reason as
        # long as it wants within its turn; if it runs past the
        # server-side turn_time_limit_s it forfeits naturally — which
        # is a legible signal to improve, not noise.
        #
        # The one remaining safety net is a hard break on pathological
        # transcript growth — the provider will hard-400 past its
        # context window anyway, so a cheap client-side check saves
        # a round-trip and logs a useful diagnostic.
        HARD_TOKEN_LIMIT = 180_000

        for _iter in range(self.max_iterations):
            if time.time() - start > time_budget_s:
                log.info(
                    "loop exit: time budget exhausted (iter=%d, "
                    "elapsed=%.1fs, budget=%.1fs)",
                    _iter, time.time() - start, time_budget_s,
                )
                break
            est_now = self._estimate_tokens(self._messages)
            if est_now >= HARD_TOKEN_LIMIT:
                log.warning(
                    "loop exit: HARD token limit %d reached at iter=%d "
                    "(messages=%d) — force-breaking; breakdown: %s",
                    HARD_TOKEN_LIMIT, _iter, len(self._messages),
                    self._transcript_breakdown(self._messages),
                )
                break
            log.info(
                "iter %d: messages=%d est_tokens=%d",
                _iter, len(self._messages), est_now,
            )
            try:
                self.api_call_started_at = time.monotonic()
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=self._messages,
                    tools=openai_tools,
                    tool_choice="auto",
                    # Allow parallel tool calls at the LLM level — but
                    # Layer 2 below still enforces "at most one MUTATING
                    # call per response" while executing all read-only
                    # calls in the same batch. This matches how a human
                    # plays: observe a bunch of things, then take one
                    # action, then observe again. Blanket one-per-step
                    # (parallel_tool_calls=False) was too slow for weak
                    # reasoning models — 15–20 units × 15s per iteration
                    # blew past the turn's time budget.
                    parallel_tool_calls=True,
                )
            except Exception as e:
                # log.exception already captures the traceback, but the
                # SDK's response `body` (the JSON the server sent back)
                # is the actually useful part for 400s — dump it
                # separately so it's grep-able even when the traceback
                # is many lines deep.
                #
                # On a context-overflow 400 ("maximum prompt length is
                # X but request contains Y") the per-role breakdown is
                # the diagnostic. It tells you whether the bloat is
                # the system prompt, the bootstrap user message,
                # accumulated assistant chain-of-thought, tool results,
                # or our own corrective system messages.
                sdk_body = getattr(e, "body", None)
                sdk_status = getattr(e, "status_code", None)
                log.exception(
                    "OpenAI completion raised "
                    "(model=%s status=%s body=%s) "
                    "messages_count=%d est_tokens=%d "
                    "last_role=%s | breakdown: %s",
                    self.model,
                    sdk_status,
                    sdk_body,
                    len(self._messages),
                    self._estimate_tokens(self._messages),
                    self._messages[-1].get("role") if self._messages else "?",
                    self._transcript_breakdown(self._messages),
                )
                raise classify(e) from e
            finally:
                # Response arrived (success or error). Clear the pending
                # flag so the TUI stops rendering "thinking (Xs)" until
                # the next chat.completions.create() call.
                self.api_call_started_at = None

            # Accumulate token usage from the API response.
            if hasattr(resp, "usage") and resp.usage is not None:
                self.total_tokens += getattr(resp.usage, "total_tokens", 0)

            choice = resp.choices[0]
            msg = choice.message

            # Diagnostic: dump the full message shape so we can see
            # where Grok / OpenAI actually stashed reasoning. xAI has
            # shipped at least three different field names across
            # Grok 3 / 4 releases (reasoning_content, reasoning,
            # thinking); this log makes the next field-rename
            # trivial to diagnose from a client log tail.
            try:
                msg_dump = msg.model_dump() if hasattr(msg, "model_dump") else {
                    k: getattr(msg, k, None)
                    for k in ("role", "content", "reasoning_content",
                              "reasoning", "thinking", "tool_calls")
                }
                log.info(
                    "openai/xai response [model=%s iter=%d]: keys=%s content_len=%s reasoning_keys=%s dump=%s",
                    self.model,
                    _iter,
                    list(msg_dump.keys()) if isinstance(msg_dump, dict) else type(msg_dump).__name__,
                    len(msg.content) if msg.content else 0,
                    {k: (len(v) if isinstance(v, str) else type(v).__name__)
                     for k, v in (msg_dump.items() if isinstance(msg_dump, dict) else [])
                     if k in ("reasoning_content", "reasoning", "thinking")},
                    # Truncate the dump so we don't spam megabytes per turn.
                    json.dumps(msg_dump, default=str)[:4000],
                )
            except Exception:
                log.exception("failed to dump openai response for diagnostic")

            # Repeat detection: hash the (content + tool_call names)
            # tuple. If two consecutive iterations produce the same
            # output, the model is stuck regurgitating — log it so
            # we can correlate "Reasoning panel reprinted the same
            # paragraph 12 times" with the actual loop. Also break
            # if the repeat count crosses a threshold.
            import hashlib as _hash
            tc_names = ",".join(
                tc.function.name for tc in (msg.tool_calls or [])
            )
            content_for_hash = (msg.content or "") + "|tc:" + tc_names
            content_hash = _hash.sha1(
                content_for_hash.encode("utf-8", "replace")
            ).hexdigest()[:12]
            if content_hash == self._last_content_hash:
                self._consecutive_repeats += 1
            else:
                self._consecutive_repeats = 0
            self._last_content_hash = content_hash
            log.info(
                "iter %d response: hash=%s repeats=%d "
                "content_preview=%r tool_call_names=[%s]",
                _iter, content_hash, self._consecutive_repeats,
                ((msg.content or "")[:120]).replace("\n", " "),
                tc_names,
            )
            if self._consecutive_repeats >= 3:
                log.warning(
                    "loop exit: model emitting identical responses "
                    "(hash=%s, %d repeats in a row at iter=%d) — "
                    "breaking to prevent spew loop; the no-progress "
                    "watchdog in NetworkedAgent will retry/force end_turn",
                    content_hash, self._consecutive_repeats, _iter,
                )
                break

            # Persist the assistant turn so the transcript stays valid
            # for subsequent tool-result appends.
            assistant_entry: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
            }
            if msg.tool_calls:
                assistant_entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            # DeepSeek V4 thinking-mode reasoners require their
            # `reasoning_content` to be echoed back on the assistant
            # message in subsequent requests; without it the API 400s
            # with "must be passed back to the API". Dispatch on the
            # base_url (each OpenAI-compatible provider has a unique
            # one) rather than the model id, which could collide if
            # another provider ever ships a like-named model.
            if "deepseek.com" in str(self._client.base_url):
                rc = getattr(msg, "reasoning_content", None)
                if not (isinstance(rc, str) and rc):
                    extra = getattr(msg, "model_extra", None) or {}
                    rc = extra.get("reasoning_content")
                if isinstance(rc, str) and rc:
                    assistant_entry["reasoning_content"] = rc
            self._messages.append(assistant_entry)

            # Surface text reasoning before we dispatch tools.
            # xAI Grok models (grok-4, grok-3-mini) put chain-of-
            # thought in `reasoning_content` and a brief restatement
            # in `content` — the panel was double-printing those
            # ("same timestamp, one a subset of the other"). For
            # Grok we want ONLY the reasoning_content; the content
            # is just a summary of what's already shown.
            #
            # For non-Grok models that don't expose `reasoning_content`
            # (regular GPT chat completions), `_extract_reasoning`
            # returns None and we fall back to `content`.
            if on_thought is not None:
                piece = _extract_reasoning(msg) or msg.content
                if piece:
                    try:
                        await on_thought(piece)
                    except Exception:
                        pass

            # Terminal condition: no tool calls requested.
            if not msg.tool_calls:
                # Some models (notably older Grok / xAI builds) were
                # trained with XML-style function-calling demos and
                # emit "<function_call>tool(args)</function_call>" as
                # plain content text instead of using the OpenAI
                # tool_calls field. From the API's perspective there
                # are no tool calls so the loop would exit silently
                # — except the model intended to act, didn't actually
                # do anything, and the next play_turn re-trigger loops
                # forever sending the same delta prompt.
                #
                # If we detect that pattern, append a corrective
                # system message reminding the model to use the
                # native tool-calls protocol and continue the loop.
                # Cap the corrections at 2 so we don't loop forever
                # on stubborn models.
                content = (msg.content or "")
                hallucinated_xml = (
                    "<function_call" in content
                    or "</function_call" in content
                    or "<tool_call" in content
                )
                if hallucinated_xml and self._corrections_this_turn < 1:
                    # One correction attempt only. Models trained on
                    # XML tool-call demos rarely flip protocols mid-
                    # turn; if they do, great, otherwise let the
                    # NetworkedAgent watchdog force end_turn after
                    # 3 stuck retries. Two corrections per stuck turn
                    # × multiple stuck turns was just inflating the
                    # transcript.
                    self._corrections_this_turn += 1
                    log.warning(
                        "model emitted XML-style function-call text "
                        "instead of using API tool_calls; injecting "
                        "single corrective reminder (model=%s iter=%d)",
                        self.model, _iter,
                    )
                    self._messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Your previous message contained "
                                "XML-style <function_call> tags in the "
                                "content. Those are NOT executed. Use "
                                "the native function-calling protocol: "
                                "emit a tool_call entry on your "
                                "message (the SDK exposes this via "
                                "the standard tools= argument). Do "
                                "not write <function_call> tags as "
                                "text — they are inert. Try again."
                            ),
                        }
                    )
                    continue
                log.info(
                    "loop exit: no tool_calls (iter=%d, hallucinated_xml=%s, "
                    "corrections=%d, content_len=%d)",
                    _iter, hallucinated_xml,
                    self._corrections_this_turn, len(content),
                )
                break

            # Selective Layer 2: one MUTATION per assistant message,
            # unlimited READS. We walk tool_calls in order, dispatching
            # reads normally; for mutations, dispatch the first one and
            # synthesize dropped_parallel_mutation errors for any
            # subsequent mutations. Reads that appear after a mutation
            # in the same response still run (e.g. move then get_state
            # surfaces the post-move board — that's actually useful).
            #
            # Blanket one-per-response was too slow for weak reasoning
            # models: each tool_call spun its own ~15-20s LLM round-trip,
            # and a turn with 15 units needed >180s wall-clock. With
            # this split, the model can batch reads (get_legal_actions
            # × N units, simulate_attack × K pairs, etc.) in one
            # round-trip, then commit to one mutation, observe, and
            # loop — the act-observe-decide pattern a human uses.
            executed: list[Any] = []
            dropped: list[Any] = []
            mutation_seen = False
            for tc in msg.tool_calls:
                is_mutation = mutates_by_name.get(tc.function.name, False)
                if is_mutation and mutation_seen:
                    dropped.append(tc)
                else:
                    executed.append(tc)
                    if is_mutation:
                        mutation_seen = True
            if dropped:
                log.warning(
                    "iter %d: dropping %d excess mutation tool_calls "
                    "(Layer 2 selective: one mutation per message). "
                    "executed=%s dropped=%s",
                    _iter, len(dropped),
                    [t.function.name for t in executed],
                    [t.function.name for t in dropped],
                )
            from silicon_pantheon.shared.match_errors import is_terminal_tool_error
            terminal_reached = False
            for tc in executed:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                self.total_tool_calls += 1
                try:
                    result = await tool_dispatcher(tc.function.name, args)
                    result_text = json.dumps(result, default=str)
                except Exception as e:
                    self.total_errors += 1
                    result = {"error": str(e)}
                    result_text = json.dumps(result)
                # Detect "game is already over" / similar terminal-match
                # errors. grok-3-mini has been observed in production
                # apologizing and re-calling end_turn on a loop for 30+
                # minutes while the server keeps replying "game is
                # already over"; break the iteration loop ourselves so
                # the worker's outer game-loop can re-check state and
                # exit cleanly.
                if is_terminal_tool_error(result):
                    terminal_reached = True
                    log.info(
                        "loop exit (terminal): tool %s returned a "
                        "game-over error — breaking iteration so the "
                        "outer loop can detect game_over",
                        tc.function.name,
                    )
                # Cap individual tool-result size. Tightened from
                # 8KB → 4KB. After our _slim_tool_response strips
                # board.tiles, get_state typically lands around
                # 2-3KB — anything bigger is a runaway tool that
                # we'd rather truncate than ship.
                MAX_RESULT_BYTES = 4000
                orig_len = len(result_text)
                if orig_len > MAX_RESULT_BYTES:
                    log.warning(
                        "tool result for %s is %d bytes (cap %d) — "
                        "truncating",
                        tc.function.name, orig_len, MAX_RESULT_BYTES,
                    )
                    result_text = (
                        result_text[:MAX_RESULT_BYTES]
                        + "...[truncated; full payload in client log]"
                    )
                # Log every tool dispatch so the harness flow is
                # reconstructible from the log alone.
                log.info(
                    "tool dispatch: name=%s args_keys=%s "
                    "result_bytes=%d (capped=%d)",
                    tc.function.name,
                    list(args.keys()) if isinstance(args, dict) else type(args).__name__,
                    orig_len,
                    len(result_text),
                )
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    }
                )
            # Layer 2: synthetic tool-result for each dropped mutation.
            # OpenAI requires every assistant.tool_calls[i] to have a
            # matching tool message; using that channel to surface the
            # drop is cleaner than a side-channel system message.
            for tc in dropped:
                err = {
                    "error": {
                        "code": "dropped_parallel_mutation",
                        "message": (
                            "Only ONE mutating tool call (move / attack "
                            "/ heal / wait / end_turn) is executed per "
                            f"assistant message. Your call to "
                            f"{tc.function.name!r} was DROPPED — it "
                            "did NOT run and the game state did NOT "
                            "change. Read-only calls (get_state, "
                            "get_legal_actions, simulate_attack, etc.) "
                            "CAN be batched freely in one message — so "
                            "observe as much as you want in one "
                            "response, then commit to one action, then "
                            "observe the result on your next turn-step. "
                            "Re-issue this call (or a different one "
                            "informed by the first action's result) in "
                            "your next message."
                        ),
                    }
                }
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(err),
                    }
                )
            # If a tool result this iteration was a terminal-match error,
            # exit the loop AFTER writing all tool results (executed +
            # dropped) so the OpenAI message buffer stays consistent
            # (every assistant.tool_calls[i] has a matching tool
            # message, required for any subsequent adapter use such as
            # summarize_match).
            if terminal_reached:
                break

    async def summarize_match(
        self,
        *,
        viewer: Team,
        scenario: str,
        final_state: dict[str, Any],
        action_history: list[dict[str, Any]],
    ) -> Lesson | None:
        winner = final_state.get("winner")
        outcome = "draw" if winner is None else ("win" if winner == viewer.value else "loss")
        last = final_state.get("last_action") or {}
        reason = str(last.get("reason", "")) if isinstance(last, dict) else ""

        context = {
            "scenario": scenario,
            "you": viewer.value,
            "outcome": outcome,
            "reason": reason,
            "turns_played": final_state.get("turn"),
            "max_turns": final_state.get("max_turns"),
            "action_history": action_history[-60:],
            "final_units": final_state.get("units", []),
        }
        prompt = (
            f"You just finished a SiliconPantheon match as {viewer.value} on scenario "
            f"'{scenario}'. Outcome: {outcome}"
            + (f" by {reason}" if reason else "")
            + ".\n\nReflect on ONE key decision or pattern that drove the outcome. "
            "Focus on generalizable tactical principle, not play-by-play.\n\n"
            "Respond with ONLY a JSON object (no prose, no code fences) with fields:\n"
            '  "title": short human title (<=80 chars)\n'
            '  "slug":  kebab-case phrase (<=60 chars)\n'
            '  "body":  markdown, <=400 words, with Situation and Lesson sections\n\n'
            f"Match context (JSON):\n```json\n{json.dumps(context, indent=2, default=str)}\n```\n"
        )
        try:
            self.api_call_started_at = time.monotonic()
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a tactical post-mortem writer. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )
        except Exception:
            log.exception("summarize_match raised")
            return None
        finally:
            self.api_call_started_at = None
        text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_lesson_json(text)
        if parsed is None:
            rc = getattr(resp.choices[0].message, "reasoning_content", "") or ""
            log.warning(
                "summarize_match: parse failed (model=%s content_len=%d "
                "reasoning_content_len=%d) text=%r",
                self.model, len(text), len(rc), text[:1000],
            )
            return None
        title = parsed.get("title", "Untitled").strip() or "Untitled lesson"
        slug = slugify(parsed.get("slug", "").strip() or title)
        body = parsed.get("body", "").strip()
        if not body:
            log.warning(
                "summarize_match: empty body in parsed JSON "
                "(model=%s parsed_keys=%s text=%r)",
                self.model, list(parsed.keys()), text[:500],
            )
            return None
        return Lesson(
            slug=slug,
            title=title,
            scenario=scenario,
            team=viewer.value,
            model=self.model,
            outcome=outcome,
            reason=reason,
            created_at=Lesson.now_iso(),
            body=body,
        )

    async def close(self) -> None:
        # AsyncOpenAI holds an httpx client that benefits from explicit
        # close to release the connection pool.
        try:
            await self._client.close()
        except Exception:
            pass
        self._messages = []
        self._system_prompt = None
