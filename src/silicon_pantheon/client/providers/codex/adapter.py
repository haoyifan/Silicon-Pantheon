"""ProviderAdapter that hits OpenAI's Codex backend with a ChatGPT
subscription's OAuth bearer token.

Architecture is parallel to providers/openai.py — same outer loop
shape, same compaction strategy, same error classification — but
the wire protocol is the Responses API instead of Chat Completions
and the auth is OAuth refresh-token based instead of an API key.

Per-turn loop:
  1. Append the new user message to a persistent `_input` array
     (Responses API's stateful-conversation analogue of openai's
     `messages` list).
  2. Loop:
        POST /backend-api/codex/responses with {input, tools}
        parse the response's `output` array
        emit reasoning to on_thought
        if no function_calls → break
        else dispatch each call, append function_call_output to input
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from silicon_pantheon.client.providers.base import (
    ThoughtCallback,
    ToolDispatcher,
    ToolSpec,
)
from silicon_pantheon.client.providers.errors import classify
from silicon_pantheon.lessons import Lesson, slugify
from silicon_pantheon.server.engine.state import Team

from .oauth import (
    CodexAuthError,
    CodexCredentials,
    ensure_fresh_access_token,
    load_credentials,
)
from .responses_api import (
    assistant_text_to_input_item,
    function_call_output_to_input_item,
    function_call_to_input_item,
    parse_response_output,
    system_to_input_item,
    to_responses_tool,
    user_to_input_item,
)

log = logging.getLogger("silicon.providers.codex.adapter")

# The endpoint the codex CLI hits. Not officially documented as a
# public API; works because it's the same one our credentials are
# scoped to. See README.md for risk discussion.
RESPONSES_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"

# User-agent codex CLI sends. The backend may key on this for routing
# / quota; mimicking it keeps us in the "codex client" bucket. Bumping
# the version string is harmless.
USER_AGENT = "codex_cli_rs/0.0.0 silicon-pantheon"

# Default model when the caller doesn't override. Codex models live
# in their own namespace; we pick a reasonable default that's known
# to support tool calling.
DEFAULT_MODEL = "gpt-5-codex"


class CodexAdapter:
    """ProviderAdapter for the OpenAI Codex backend (subscription auth)."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        max_iterations_per_turn: int = 20,
        credentials: CodexCredentials | None = None,
    ):
        self.model = model
        self.max_iterations = max_iterations_per_turn
        self._credentials = credentials  # may be None; lazy-loaded on first call
        self._client: httpx.AsyncClient | None = None
        # Persistent input array — Responses API equivalent of the
        # OpenAI Chat Completions `messages` list.
        self._input: list[dict] = []
        # Once-per-session marker so we don't re-emit the system prompt.
        self._initialized = False
        # Per-turn correction counter (mirrors openai.py).
        self._corrections_this_turn: int = 0

    # ---- transport ------------------------------------------------------

    async def _get_token(self) -> str:
        """Resolve the bearer token, loading from disk + refreshing
        as needed. Raises CodexAuthError if no credentials exist."""
        creds = self._credentials
        if creds is None:
            creds = load_credentials()
            if creds is None:
                raise CodexAuthError(
                    "no Codex OAuth credentials — run `silicon-codex-login` "
                    "or pick OpenAI (subscription) in the TUI to set them up"
                )
            self._credentials = creds
        return await ensure_fresh_access_token(creds)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Chat-completion-grade timeouts: turn time budget is 90s
            # by default upstream so 120s here gives the network +
            # model ample headroom without becoming a wedge point.
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def _post_responses(self, body: dict) -> dict:
        """POST to the Responses endpoint with auto-refresh on 401."""
        client = await self._ensure_client()

        for attempt in range(2):
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                # The codex CLI sends these — see codex-rs/core/src/
                # client.rs. The originator distinguishes us from
                # browser sessions; the version is informational.
                "Originator": "codex_cli_rs",
                "Version": "0.0.0",
            }
            if self._credentials and self._credentials.account_id:
                headers["Chatgpt-Account-Id"] = self._credentials.account_id

            try:
                resp = await client.post(
                    RESPONSES_ENDPOINT, headers=headers, json=body,
                )
            except Exception as e:
                log.exception("Codex POST raised")
                raise classify(e) from e

            if resp.status_code == 401 and attempt == 0:
                # Token might have rotated mid-flight, or the cached
                # one was already past expiry. Force-refresh and retry.
                log.info("Codex 401; forcing token refresh and retrying")
                self._credentials = None  # drop cached creds → reload from disk
                continue
            if resp.status_code != 200:
                # Surface the body on errors so 4xx/5xx are diagnosable.
                body_text = resp.text[:1000]
                log.warning(
                    "Codex POST non-200: status=%d body=%s",
                    resp.status_code, body_text,
                )
                err = httpx.HTTPStatusError(
                    f"Codex {resp.status_code}: {body_text}",
                    request=resp.request, response=resp,
                )
                raise classify(err) from err

            return resp.json()

        raise classify(RuntimeError("Codex POST failed after 401-retry"))

    # ---- play_turn ------------------------------------------------------

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
        # Init: seed the persistent input with system + user.
        if not self._initialized:
            self._input.append(system_to_input_item(system_prompt))
            self._initialized = True
            log.info(
                "codex session init: system_prompt_bytes=%d (~%d est_tokens)",
                len(system_prompt), len(system_prompt) // 4,
            )
        self._input.append(user_to_input_item(user_prompt))
        self._corrections_this_turn = 0

        responses_tools = [to_responses_tool(s) for s in tools]
        start = time.time()

        # Hard ceilings (parallel to openai.py).
        SOFT_TOKEN_LIMIT = 90_000
        HARD_TOKEN_LIMIT = 120_000
        warned_soft = False

        for _iter in range(self.max_iterations):
            if time.time() - start > time_budget_s:
                log.info(
                    "loop exit: time budget exhausted (iter=%d, elapsed=%.1fs)",
                    _iter, time.time() - start,
                )
                break
            est = self._estimate_tokens(self._input)
            if est >= HARD_TOKEN_LIMIT:
                log.warning(
                    "loop exit: HARD token limit %d reached at iter=%d",
                    HARD_TOKEN_LIMIT, _iter,
                )
                break
            if est >= SOFT_TOKEN_LIMIT and not warned_soft:
                warned_soft = True
                log.warning(
                    "soft token limit %d crossed at iter=%d; nudging end_turn",
                    SOFT_TOKEN_LIMIT, _iter,
                )
                self._input.append(system_to_input_item(
                    "You are approaching the model's context limit. Wrap up "
                    "your turn now: skip non-essential planning, finish "
                    "whatever unit you're moving, and call end_turn."
                ))

            log.info(
                "codex iter %d: input_items=%d est_tokens=%d",
                _iter, len(self._input), est,
            )

            body = self._build_request_body(responses_tools)
            data = await self._post_responses(body)
            parsed = parse_response_output(data.get("output") or [])

            log.info(
                "codex response [iter=%d]: text_pieces=%d reasoning_pieces=%d "
                "tool_calls=%d",
                _iter, len(parsed["text"]), len(parsed["reasoning"]),
                len(parsed["tool_calls"]),
            )

            # Persist the assistant's output back into _input so the
            # next iteration sees its own prior moves. This is the
            # Responses-API equivalent of appending the assistant
            # message in chat completions.
            for item in parsed["raw_items"]:
                self._input.append(item)

            # Surface reasoning to the TUI panel + replay log.
            if on_thought is not None:
                # Prefer reasoning content (chain-of-thought); fall
                # back to plain text only if there's no reasoning
                # this turn (mirrors openai.py's dedup behaviour).
                pieces = parsed["reasoning"] or parsed["text"]
                for piece in pieces:
                    if piece:
                        try:
                            await on_thought(piece)
                        except Exception:
                            pass

            # No tool calls → loop ends.
            if not parsed["tool_calls"]:
                log.info(
                    "loop exit: no tool_calls (iter=%d, content_len=%d)",
                    _iter, sum(len(t) for t in parsed["text"]),
                )
                break

            # Dispatch each tool call and feed the results back in.
            for tc in parsed["tool_calls"]:
                try:
                    args = json.loads(tc.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = await tool_dispatcher(tc["name"], args)
                    result_text = json.dumps(result, default=str)
                except Exception as e:
                    result_text = json.dumps({"error": str(e)})
                MAX_RESULT_BYTES = 4000
                if len(result_text) > MAX_RESULT_BYTES:
                    result_text = (
                        result_text[:MAX_RESULT_BYTES]
                        + "...[truncated]"
                    )
                log.info(
                    "codex tool dispatch: name=%s result_bytes=%d",
                    tc["name"], len(result_text),
                )
                self._input.append(
                    function_call_output_to_input_item(
                        call_id=tc["call_id"], output=result_text,
                    )
                )

    def _build_request_body(self, tools: list[dict]) -> dict:
        return {
            "model": self.model,
            "input": self._input,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "store": False,
            "stream": False,
            # Codex models support reasoning summaries; ask for them
            # so we can surface chain-of-thought.
            "reasoning": {"summary": "auto"},
        }

    # ---- compaction (parallel to openai.py) -----------------------------

    @staticmethod
    def _estimate_tokens(items: list[dict]) -> int:
        total = 0
        for it in items:
            total += len(json.dumps(it, default=str))
        return total // 4

    # ---- summarize_match ------------------------------------------------

    async def summarize_match(
        self,
        *,
        viewer: Team,
        scenario: str,
        final_state: dict[str, Any],
        action_history: list[dict[str, Any]],
    ) -> Lesson | None:
        """One-shot reflection. Reuses the same Responses API but
        with no tools — just a prose-out request."""
        winner = final_state.get("winner")
        outcome = (
            "draw" if winner is None
            else ("win" if winner == viewer.value else "loss")
        )
        last = final_state.get("last_action") or {}
        reason = str(last.get("reason", "")) if isinstance(last, dict) else ""

        context = {
            "scenario": scenario, "you": viewer.value,
            "outcome": outcome, "reason": reason,
            "turns_played": final_state.get("turn"),
            "max_turns": final_state.get("max_turns"),
            "action_history": action_history[-60:],
            "final_units": final_state.get("units", []),
        }
        prompt = (
            f"You just finished a SiliconPantheon match as {viewer.value} on "
            f"scenario '{scenario}'. Outcome: {outcome}"
            + (f" by {reason}" if reason else "")
            + ".\n\nReflect on ONE key decision or pattern that drove the "
            "outcome. Focus on a generalizable tactical principle.\n\n"
            "Respond with ONLY a JSON object (no prose, no code fences) with "
            'fields:\n  "title": short human title (<=80 chars)\n'
            '  "slug":  kebab-case phrase (<=60 chars)\n'
            '  "body":  markdown, <=400 words, with Situation and Lesson '
            "sections\n\n"
            f"Match context (JSON):\n```json\n{json.dumps(context, indent=2, default=str)}\n```\n"
        )

        body = {
            "model": self.model,
            "input": [
                system_to_input_item(
                    "You are a tactical post-mortem writer. Return JSON only."
                ),
                user_to_input_item(prompt),
            ],
            "store": False,
            "stream": False,
        }
        try:
            data = await self._post_responses(body)
        except Exception:
            log.exception("Codex summarize_match POST raised")
            return None
        parsed = parse_response_output(data.get("output") or [])
        text = "\n".join(parsed["text"]).strip()
        if not text:
            return None

        # Reuse the same lesson-JSON parser as the openai adapter.
        from silicon_pantheon.client.providers.anthropic import _parse_lesson_json
        obj = _parse_lesson_json(text)
        if obj is None:
            return None
        title = obj.get("title", "Untitled").strip() or "Untitled lesson"
        slug = slugify(obj.get("slug", "").strip() or title)
        body_md = obj.get("body", "").strip()
        if not body_md:
            return None
        return Lesson(
            slug=slug, title=title, scenario=scenario,
            team=viewer.value, model=self.model, outcome=outcome,
            reason=reason, created_at=Lesson.now_iso(), body=body_md,
        )

    # ---- close ----------------------------------------------------------

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        self._input = []
        self._initialized = False
