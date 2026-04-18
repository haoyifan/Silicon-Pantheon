"""ProviderAdapter that hits OpenAI's Codex backend with a ChatGPT
subscription's OAuth bearer token.

Architecture is parallel to providers/openai.py — same outer loop
shape, same compaction strategy (truncate old user / assistant
content, stub function_call_output, drop reasoning items), same
error classification — but the wire protocol is the Responses API
instead of Chat Completions and the auth is OAuth refresh-token
based instead of an API key.

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

# Default model when the caller doesn't override. The codex backend's
# /codex/models endpoint returns which models are available; as of
# 2026-04 that's gpt-5.2. OpenClaw's catalog lists gpt-5.4 but those
# are forward-compat entries that may not be live yet.
DEFAULT_MODEL = "gpt-5.2"


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
        self._system_prompt: str | None = None
        # Once-per-session marker so we don't re-emit the system prompt.
        self._initialized = False
        # Per-turn correction counter (mirrors openai.py).
        self._corrections_this_turn: int = 0
        # Guards against retry bloat: when agent_bridge retries
        # play_turn after a crash, we must not append the same user
        # prompt again. We snapshot len(_input) before appending so
        # a retry can roll back.
        self._pre_turn_input_len: int = 0

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
        """POST to the Responses endpoint (streaming) with auto-refresh on 401.

        The Codex API requires stream=true. We collect SSE events and
        return the final 'response.completed' event's data, which has
        the same shape as the old non-streaming JSON response.
        """
        client = await self._ensure_client()

        for attempt in range(2):
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": USER_AGENT,
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
                log.info("Codex 401; forcing token refresh and retrying")
                self._credentials = None
                continue
            if resp.status_code != 200:
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

            # We always send stream=true, so always try SSE parsing
            # first. The content-type can vary (text/event-stream,
            # text/plain, application/json with SSE body, etc.) — don't
            # rely on it for dispatch.
            raw = resp.text
            content_type = resp.headers.get("content-type", "")
            log.info(
                "Codex response: status=%d content_type=%s body_bytes=%d",
                resp.status_code, content_type, len(raw),
            )

            # Try SSE parse first (handles data: lines).
            if "data: " in raw:
                return self._parse_sse_body(raw)

            # Fallback: plain JSON (tests, future API changes).
            if raw.strip():
                try:
                    return resp.json()
                except Exception:
                    log.warning(
                        "Codex response not SSE and not valid JSON "
                        "(content_type=%s, body_head=%s)",
                        content_type, raw[:200],
                    )
            # Empty body — return empty output so the caller exits
            # gracefully instead of crashing.
            log.warning("Codex response body is empty")
            return {"output": []}

        raise classify(RuntimeError("Codex POST failed after 401-retry"))

    # ---- play_turn ------------------------------------------------------

    # Compaction constants — match openai.py so the two adapters
    # behave identically at the transcript-shrinking layer.
    _STUB_TOOL_RESULT = "[result trimmed for context bound]"
    _ASST_CONTENT_CAP = 1500
    _USER_CONTENT_CAP = 3000

    def _compact_prior_turns(self) -> None:
        """Shrink completed turns in self._input without breaking the
        Responses-API pairing invariants.

        Responses-API item types and how we handle each:

          - message(role=user) — user turn prompt. Truncate .content
            text over _USER_CONTENT_CAP chars — catches the turn-1
            bootstrap snapshot while letting small deltas pass through.
          - message(role=assistant, output_text) — assistant's prose.
            Truncate text over _ASST_CONTENT_CAP chars.
          - function_call — model's tool call. KEEP intact; we need
            the paired function_call_output below it to stay valid.
          - function_call_output — tool result. Replace .output with
            _STUB_TOOL_RESULT. Pairing with function_call preserved.
          - reasoning — chain-of-thought. DROP; old reasoning adds
            significant token weight and rarely carries cross-turn
            signal. The model's final decisions are already captured
            in the function_call items that followed.

        Note: system prompt lives in the top-level `instructions`
        field, not in _input — no developer messages to handle here.
        """
        if len(self._input) <= 1:
            return

        compacted: list[dict] = []
        for item in self._input:
            if not isinstance(item, dict):
                compacted.append(item)
                continue
            t = item.get("type")

            if t == "message":
                role = item.get("role")

                # user / assistant — truncate their text content.
                cap = (
                    self._USER_CONTENT_CAP if role == "user"
                    else self._ASST_CONTENT_CAP
                )
                new_content: list[dict] = []
                truncated = False
                for c in item.get("content") or []:
                    if not isinstance(c, dict):
                        new_content.append(c)
                        continue
                    text = c.get("text")
                    if isinstance(text, str) and len(text) > cap:
                        suffix = (
                            "…[bootstrap snapshot truncated; call "
                            "`get_state` if you need fresh board data]"
                            if role == "user"
                            else "…[truncated]"
                        )
                        new_c = dict(c)
                        new_c["text"] = text[:cap] + suffix
                        new_content.append(new_c)
                        truncated = True
                    else:
                        new_content.append(c)
                if truncated:
                    new_item = dict(item)
                    new_item["content"] = new_content
                    compacted.append(new_item)
                else:
                    compacted.append(item)
                continue

            if t == "function_call":
                # Keep the call intact so its output pairing stays
                # valid. (Also preserves the native-tool-call protocol
                # shape in the model's own history.)
                compacted.append(item)
                continue

            if t == "function_call_output":
                compacted.append({
                    "type": "function_call_output",
                    "call_id": item.get("call_id", ""),
                    "output": self._STUB_TOOL_RESULT,
                })
                continue

            if t == "reasoning":
                # Drop old chain-of-thought — it's the biggest
                # token sink across turns and rarely informs the
                # next turn's play beyond what's already encoded
                # in the function_calls the model subsequently made.
                continue

            # Unknown item types pass through so a future Responses-API
            # extension doesn't get silently dropped.
            compacted.append(item)

        self._input = compacted

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
        # Init: seed the persistent input with system + user.
        if not self._initialized:
            # System prompt goes in the top-level `instructions` field,
            # NOT as an input item. The Codex Responses API requires it.
            self._system_prompt = system_prompt
            self._initialized = True
            log.info(
                "codex session init: system_prompt_bytes=%d (~%d est_tokens)",
                len(system_prompt), len(system_prompt) // 4,
            )
        else:
            # Turn 2+: shrink completed turns before we append the new
            # user prompt. Same cadence and rationale as openai.py.
            before = self._estimate_tokens(self._input)
            self._compact_prior_turns()
            after = self._estimate_tokens(self._input)
            if before != after:
                log.info(
                    "codex compacted: %d→%d est_tokens (%d items)",
                    before, after, len(self._input),
                )

        # Retry-bloat guard: if agent_bridge retries play_turn after a
        # crash, the previous call's user prompt is still in _input.
        # Roll back to pre-turn state before appending the new prompt.
        if len(self._input) > self._pre_turn_input_len:
            self._input = self._input[:self._pre_turn_input_len]
        self._pre_turn_input_len = len(self._input)
        self._input.append(user_to_input_item(user_prompt))
        self._corrections_this_turn = 0

        responses_tools = [to_responses_tool(s) for s in tools]
        start = time.time()

        # No mid-turn nudges — see openai.py for the rationale. The
        # soft-token "wrap up" system message was removed; model is
        # free to reason as long as it wants until the server-side
        # turn timer forfeits. HARD token limit is a cheap safety
        # check so we don't send a request the provider will hard-400.
        HARD_TOKEN_LIMIT = 180_000

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

            # Selective Layer 2: one MUTATION per response, unlimited
            # READS. Walk function_calls in order — reads always run,
            # first mutation runs, subsequent mutations get synthetic
            # dropped_parallel_mutation errors. See openai.py for the
            # motivation; same contract here.
            mutates_by_name = {s.name: s.mutates for s in tools}
            executed_calls: list[dict] = []
            dropped_calls: list[dict] = []
            mutation_seen = False
            for tc in parsed["tool_calls"]:
                is_mutation = mutates_by_name.get(tc["name"], False)
                if is_mutation and mutation_seen:
                    dropped_calls.append(tc)
                else:
                    executed_calls.append(tc)
                    if is_mutation:
                        mutation_seen = True
            if dropped_calls:
                log.warning(
                    "codex iter %d: dropping %d excess mutation "
                    "function_calls (Layer 2 selective: one mutation "
                    "per message). executed=%s dropped=%s",
                    _iter, len(dropped_calls),
                    [c["name"] for c in executed_calls],
                    [c["name"] for c in dropped_calls],
                )
            for tc in executed_calls:
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
            # Layer 2: synthetic function_call_output per dropped
            # mutation. Error envelope mirrors openai.py so the model
            # sees the same dropped_parallel_mutation code.
            for tc in dropped_calls:
                err_text = json.dumps({
                    "error": {
                        "code": "dropped_parallel_mutation",
                        "message": (
                            "Only ONE mutating tool call (move / attack "
                            "/ heal / wait / end_turn) is executed per "
                            f"assistant message. Your call to "
                            f"{tc['name']!r} was DROPPED — it did NOT "
                            "run and the game state did NOT change. "
                            "Read-only calls (get_state, "
                            "get_legal_actions, simulate_attack, etc.) "
                            "CAN be batched freely — observe as much "
                            "as you want in one response, commit to "
                            "one action, then observe the result on "
                            "your next turn-step. Re-issue this call "
                            "in your next message."
                        ),
                    }
                })
                self._input.append(
                    function_call_output_to_input_item(
                        call_id=tc["call_id"], output=err_text,
                    )
                )

        # Turn completed (or exited via budget/token-limit). Snapshot
        # the input length so a retry won't re-append the user prompt.
        self._pre_turn_input_len = len(self._input)

    @staticmethod
    def _parse_sse_body(text: str) -> dict:
        """Extract the response from an SSE-formatted body.

        The Codex streaming API delivers output items incrementally:
        each item arrives via ``response.output_item.done`` events.
        The final ``response.completed`` event carries the full
        response *but* its ``output`` array can be empty in some API
        versions. We therefore accumulate items from the stream and
        merge them into the completed response if its output is empty.
        """
        result: dict = {}
        # Accumulate completed output items from the stream — the
        # response.completed event's output may be empty.
        streamed_items: list[dict] = []
        event_types: list[str] = []
        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            event_types.append(etype)
            if etype == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    streamed_items.append(item)
            elif etype == "response.completed":
                result = event.get("response", event)
            elif etype == "response.failed":
                err_detail = (event.get("response", {})
                              .get("error", {}).get("message", "unknown"))
                raise RuntimeError(f"Codex response failed: {err_detail}")

        if not result:
            log.warning(
                "Codex stream ended without response.completed; "
                "event_types=%s body_head=%s",
                event_types, text[:500],
            )
            result = {"output": streamed_items}
        else:
            # Prefer the completed event's output, but if it's empty
            # (common in current API) use the items we accumulated.
            if not result.get("output") and streamed_items:
                result["output"] = streamed_items
            log.info(
                "Codex SSE parsed: event_types=%s output_items=%d "
                "(from_stream=%d)",
                event_types, len(result.get("output", [])),
                len(streamed_items),
            )
        return result

    def _build_request_body(self, tools: list[dict]) -> dict:
        body: dict = {
            "model": self.model,
            "input": self._input,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "store": False,
            "stream": True,
            # "effort": "high" allocates extended reasoning tokens.
            # "summary": "auto" gives us a readable digest for the TUI.
            # Without effort=high the model skips multi-step planning
            # and defaults to single-unit-then-end-turn patterns.
            "reasoning": {"effort": "high", "summary": "auto"},
            # Required for the model to actually produce reasoning
            # content (encrypted) that it can reference across turns.
            # Without this, reasoning tokens aren't allocated.
            "include": ["reasoning.encrypted_content"],
        }
        # The Codex Responses API requires `instructions` (the system
        # prompt) as a top-level field. Extract it from the first
        # developer message in _input.
        if self._system_prompt:
            body["instructions"] = self._system_prompt
        return body

    # ---- token accounting -----------------------------------------------

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
            "instructions": (
                "You are a tactical post-mortem writer. Return JSON only."
            ),
            "input": [user_to_input_item(prompt)],
            "store": False,
            "stream": True,
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
