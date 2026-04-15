"""Shape conversion between our chat-completions-style interface and
OpenAI's Responses API request/response.

The Responses API differs from Chat Completions in five ways that
matter for us:

  1. `messages: [...]` is renamed `input: [...]` and each entry is
     wrapped in `{type: "message", role, content: [{type: "input_text",
     text}]}` instead of plain `{role, content}`.
  2. Tool calls in the response come back as `function_call` items
     interleaved in the `output` array (not as a `tool_calls` field
     on an assistant message).
  3. Tool results are sent back as `function_call_output` items in
     the next request's `input` array, referenced by `call_id`.
  4. Reasoning tokens (chain-of-thought) come back as `reasoning`
     items with a `summary` array.
  5. `tools` schema uses `{type: "function", name, description,
     parameters}` flat — no nested `function` object.

This module is the single point where those translations live so the
adapter stays readable.
"""

from __future__ import annotations

from typing import Any

from silicon_pantheon.client.providers.base import ToolSpec


# ---- request side -------------------------------------------------------


def to_responses_tool(spec: ToolSpec) -> dict:
    """Convert our ToolSpec into a Responses-API tool entry.

    Chat Completions wraps under {"type":"function","function":{...}};
    Responses flattens to {"type":"function", name, description,
    parameters}.
    """
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.input_schema,
    }


def system_to_input_item(text: str) -> dict:
    """A `developer` (or `system`) message in Responses goes inside
    the `input` array, not as a separate field.

    We use role="developer" because that's what the codex CLI sends
    and what the codex-tuned models are tuned to honor."""
    return {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": text}],
    }


def user_to_input_item(text: str) -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def assistant_text_to_input_item(text: str) -> dict:
    """An assistant prose response from a previous turn is replayed
    as `output_text` content under role=assistant."""
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def function_call_to_input_item(
    *, call_id: str, name: str, arguments: str
) -> dict:
    """Replay a previous-turn tool call so the next request includes
    the model's call → our result pairing."""
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def function_call_output_to_input_item(
    *, call_id: str, output: str
) -> dict:
    """Send back the result we obtained from running the tool."""
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }


# ---- response side ------------------------------------------------------


def parse_response_output(output: list[dict]) -> dict:
    """Walk the `output` array from a Responses-API response and
    classify each item.

    Returns a dict with three buckets:
      - `text`: list of all assistant prose (`output_text` items)
      - `reasoning`: list of all reasoning summaries (chain-of-thought
        when the model is a reasoning model)
      - `tool_calls`: list of {call_id, name, arguments} for every
        function_call item
      - `raw_items`: the original list, retained so the adapter can
        re-feed them as input on the next iteration without lossy
        round-tripping
    """
    text: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict] = []
    raw_items: list[dict] = []
    for item in output or []:
        if not isinstance(item, dict):
            continue
        raw_items.append(item)
        item_type = item.get("type")
        if item_type == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    t = c.get("text") or ""
                    if t:
                        text.append(t)
        elif item_type == "reasoning":
            # Reasoning items have a `summary` array of {type:
            # "summary_text", text}. Some models also include
            # `content` with the same shape.
            for s in item.get("summary") or []:
                if isinstance(s, dict):
                    rt = s.get("text") or ""
                    if rt:
                        reasoning.append(rt)
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "reasoning_text":
                    rt = c.get("text") or ""
                    if rt:
                        reasoning.append(rt)
        elif item_type == "function_call":
            tool_calls.append({
                "call_id": item.get("call_id") or item.get("id") or "",
                "name": item.get("name", ""),
                "arguments": item.get("arguments", "{}"),
            })
    return {
        "text": text,
        "reasoning": reasoning,
        "tool_calls": tool_calls,
        "raw_items": raw_items,
    }
