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
etc. — see TODO.md for those).
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
from silicon_pantheon.lessons import Lesson, slugify
from silicon_pantheon.server.engine.state import Team

log = logging.getLogger("silicon.provider.openai")


def _extract_reasoning(msg: Any) -> str | None:
    """Dig chain-of-thought text out of a chat-completions message.

    Providers disagree on where reasoning lives:
      - xAI Grok 3/4:       `reasoning_content`  (str)
      - OpenAI o-series:    `reasoning_content`  (str)
      - xAI (some builds):  `reasoning`          (str)
      - DeepSeek R1 etc.:   `reasoning`          (str)
      - Anthropic via OAI-compat: `thinking`     (str)

    Pydantic models in newer openai SDKs stash unknown fields in
    `model_extra`; older versions expose them as attributes directly.
    Walk both.
    """
    for name in ("reasoning_content", "reasoning", "thinking"):
        val = getattr(msg, name, None)
        if isinstance(val, str) and val.strip():
            return val
        # List-of-blocks form (rare but used by some OAI-compat proxies).
        if isinstance(val, list):
            parts = [
                b.get("text") if isinstance(b, dict) else str(b)
                for b in val
            ]
            joined = "\n".join(p for p in parts if p)
            if joined.strip():
                return joined
    extra = getattr(msg, "model_extra", None) or {}
    for name in ("reasoning_content", "reasoning", "thinking"):
        val = extra.get(name)
        if isinstance(val, str) and val.strip():
            return val
    return None


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
        max_iterations_per_turn: int = 40,
    ):
        self.model = model
        self.max_iterations = max_iterations_per_turn
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._messages: list[dict] = []
        self._system_prompt: str | None = None
        self._corrections_this_turn: int = 0

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

    def _compact_prior_turns(self) -> None:
        """Shrink completed turns down to their reasoning so the
        context window doesn't grow without bound.

        Called at the START of each new turn's play_turn — every
        message already in self._messages is from a completed turn
        and safe to rewrite. Strategy:

          - keep the system message verbatim
          - keep user (turn-prompt) messages — they're small and
            carry useful "here's what turn N looked like" context
          - keep assistant messages' `content` text (the reasoning
            the LLM emitted in prose) but drop their `tool_calls`
            field; cross-turn reasoning needs the prose, not the
            per-tool payload
          - drop `tool` role messages entirely — the 10-20 KB
            get_state dumps are the main context-window offender
            and the model doesn't need them to remember what
            happened (the next turn's fresh user prompt carries
            the current state)
          - drop assistants whose content was empty after stripping
            (they were tool_calls-only — nothing left to contribute).

        Problem this solves: a Grok match at turn 5-6 hit
        'maximum prompt length is 131072 but request contains 351186'
        because self._messages grew linearly with every tool round-
        trip. Compacting at turn boundaries keeps growth flat."""
        if len(self._messages) <= 1:
            return  # just the system prompt; nothing to compact

        compacted: list[dict] = []
        for m in self._messages:
            role = m.get("role")
            if role == "system" or role == "user":
                compacted.append(m)
                continue
            if role == "tool":
                continue
            if role == "assistant":
                content = m.get("content") or ""
                if not content.strip():
                    continue
                compacted.append({"role": "assistant", "content": content})
        self._messages = compacted

    async def play_turn(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpec],
        tool_dispatcher: ToolDispatcher,
        on_thought: ThoughtCallback | None = None,
        time_budget_s: float = 90.0,
    ) -> None:
        # First-turn init: seed the transcript with the system prompt.
        if not self._messages:
            self._messages.append({"role": "system", "content": system_prompt})
            self._system_prompt = system_prompt
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

        self._messages.append({"role": "user", "content": user_prompt})
        # Reset per-turn corrective-reminder counter — we cap how many
        # times we inject "use proper tool_calls" reminders so a
        # stubbornly mis-formatting model can't loop forever.
        self._corrections_this_turn = 0
        # Log token estimate each turn so operators can see growth
        # trajectory even without hitting the limit.
        log.info(
            "turn start: messages=%d est_tokens=%d",
            len(self._messages), self._estimate_tokens(self._messages),
        )

        openai_tools = [_as_openai_tool(s) for s in tools]
        start = time.time()

        for _iter in range(self.max_iterations):
            if time.time() - start > time_budget_s:
                log.info("OpenAI adapter: turn time budget exhausted")
                break
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=self._messages,
                    tools=openai_tools,
                    tool_choice="auto",
                )
            except Exception as e:
                # log.exception already captures the traceback, but the
                # SDK's response `body` (the JSON the server sent back)
                # is the actually useful part for 400s — dump it
                # separately so it's grep-able even when the traceback
                # is many lines deep.
                sdk_body = getattr(e, "body", None)
                sdk_status = getattr(e, "status_code", None)
                log.exception(
                    "OpenAI completion raised "
                    "(model=%s status=%s body=%s) — last message role=%s "
                    "messages_count=%d",
                    self.model,
                    sdk_status,
                    sdk_body,
                    self._messages[-1].get("role") if self._messages else "?",
                    len(self._messages),
                )
                raise classify(e) from e

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
            self._messages.append(assistant_entry)

            # Surface any text reasoning before we dispatch tools.
            # xAI Grok reasoning models (grok-4, grok-3-mini) put
            # chain-of-thought in `reasoning_content`, leaving
            # `content` empty when the turn is pure reasoning + tool
            # calls. OpenAI's o-series uses the same split. Emit
            # whichever is non-empty so the thoughts panel actually
            # populates for reasoning-capable models.
            if on_thought is not None:
                reasoning = _extract_reasoning(msg)
                for piece in (reasoning, msg.content):
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
                if hallucinated_xml and self._corrections_this_turn < 2:
                    self._corrections_this_turn += 1
                    log.warning(
                        "model emitted XML-style function-call text "
                        "instead of using the API tool_calls field; "
                        "injecting corrective reminder (attempt %d/2)",
                        self._corrections_this_turn,
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
                break

            # Dispatch each tool call and append results.
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = await tool_dispatcher(tc.function.name, args)
                    result_text = json.dumps(result, default=str)
                except Exception as e:
                    result_text = json.dumps({"error": str(e)})
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    }
                )

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
        text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_lesson_json(text)
        if parsed is None:
            return None
        title = parsed.get("title", "Untitled").strip() or "Untitled lesson"
        slug = slugify(parsed.get("slug", "").strip() or title)
        body = parsed.get("body", "").strip()
        if not body:
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
