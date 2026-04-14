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

        self._messages.append({"role": "user", "content": user_prompt})

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
                log.exception("OpenAI completion raised")
                raise classify(e) from e

            choice = resp.choices[0]
            msg = choice.message

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
                reasoning = getattr(msg, "reasoning_content", None)
                # Some SDK versions expose it via model_extra instead.
                if not reasoning:
                    extra = getattr(msg, "model_extra", None) or {}
                    reasoning = extra.get("reasoning_content")
                for piece in (reasoning, msg.content):
                    if piece:
                        try:
                            await on_thought(piece)
                        except Exception:
                            pass

            # Terminal condition: no tool calls requested.
            if not msg.tool_calls:
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
