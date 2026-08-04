"""Anthropic Messages API (``/v1/messages``) translation to and from the internal chat path.

The gateway speaks the OpenAI chat-completions protocol end to end (admission, budget,
guardrail, audit). This module is a thin, faithful-but-pragmatic translation layer so a
native Anthropic ``POST /v1/messages`` request runs through the *same* governance path as
chat rather than a second, weaker one: an Anthropic request is translated into an OpenAI
chat payload here, the caller feeds that payload through the existing chat governance path,
and the resulting OpenAI chat completion is translated back into an Anthropic ``Message``.

Text is the must-have and is exact; ``tool_use``/``tool_result`` blocks and Anthropic tool
definitions are mapped to their closest OpenAI equivalents on a best-effort basis (see the
per-function docstrings for the fidelity caveats).

Streaming works the same way: :class:`AnthropicStreamTranslator` turns the runtime's flat
OpenAI chunk stream into Anthropic's block-structured event sequence, so a streaming
``/v1/messages`` call runs the same governed chat stream as ``/v1/chat/completions``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

# Anthropic ``stop_reason`` for each OpenAI ``finish_reason``. ``tool_calls`` maps to
# ``tool_use``; ``content_filter`` maps to ``end_turn`` because Anthropic has no distinct
# content-filter stop reason and the guardrail already rewrote the content. An unknown or
# absent finish_reason falls back to ``end_turn`` (a completed turn).
_STOP_REASON_BY_FINISH_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}


class MessagesRequest(BaseModel):
    """Request body for a native Anthropic ``POST /v1/messages`` call.

    Mirrors the Anthropic Messages API shape. ``max_tokens`` is required by Anthropic, so
    it is modelled as required here and its absence is a 422 before the governance path
    runs (it also becomes the OpenAI ``max_tokens`` cap that admission enforces). ``system``
    accepts either a plain string or an Anthropic block array. ``extra="allow"`` forwards
    any other Anthropic sampling field (``temperature``, ``top_p``, ``stop_sequences``,
    ``tools``, ``tool_choice``, ``stream``, ``metadata``, ...) so nothing is silently dropped.
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[dict[str, Any]]
    system: str | list[dict[str, Any]] | None = None
    max_tokens: int
    temperature: float | None = None
    top_p: float | None = None
    stop_sequences: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    stream: bool | None = False
    metadata: dict[str, Any] | None = None


def _system_text(system: Any) -> str:
    """Flatten an Anthropic ``system`` (string or block array) into a single string."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(system)


def _translate_content_blocks(content: Any) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """Translate one Anthropic message ``content`` into OpenAI shape.

    Returns ``(openai_content, tool_calls, tool_results)``:

    - ``openai_content`` is the OpenAI ``content`` for this message: a plain string when the
      Anthropic content was a string or a single text block, else an OpenAI content-part
      array (``{"type": "text", "text": ...}`` parts; unrecognized blocks are preserved).
    - ``tool_calls`` are OpenAI assistant ``tool_calls`` translated from Anthropic
      ``tool_use`` blocks (best-effort: the model/id/name/arguments are mapped).
    - ``tool_results`` are OpenAI ``tool`` messages translated from Anthropic ``tool_result``
      blocks; the caller emits them as separate ``role: "tool"`` messages.
    """
    if content is None:
        return "", [], []
    if isinstance(content, str):
        return content, [], []
    if not isinstance(content, list):
        return str(content), [], []

    text_parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            if isinstance(block, str):
                text_parts.append({"type": "text", "text": block})
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            text_parts.append({"type": "text", "text": text if isinstance(text, str) else ""})
        elif block_type == "tool_use":
            # Anthropic tool_use -> OpenAI assistant tool_call. ``input`` is an object;
            # OpenAI ``arguments`` is a JSON string, so serialize it.
            tool_calls.append(
                {
                    "id": str(block.get("id") or f"call_{uuid4().hex[:24]}"),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
        elif block_type == "tool_result":
            # Anthropic tool_result -> OpenAI ``role: "tool"`` message keyed by tool_use_id.
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": _tool_result_text(block.get("content")),
                }
            )
        elif block_type == "image":
            # Convert an Anthropic image block to the OpenAI image_url shape so a
            # vision-capable runtime still receives it AND the shared admission metering
            # (max_image_bytes / image_part_token_estimate, which only understand
            # image_url parts) applies to it. Fall back to verbatim on an unexpected shape.
            source = block.get("source")
            if isinstance(source, dict) and source.get("type") == "base64" and isinstance(source.get("data"), str):
                media_type = source.get("media_type") or "image/png"
                url = f"data:{media_type};base64,{source['data']}"
                text_parts.append({"type": "image_url", "image_url": {"url": url}})
            elif isinstance(source, dict) and source.get("type") == "url" and isinstance(source.get("url"), str):
                text_parts.append({"type": "image_url", "image_url": {"url": source["url"]}})
            else:
                text_parts.append(block)
        else:
            # Unknown block type: preserve it rather than drop it (forward-compat / audit).
            text_parts.append(block)

    if not text_parts:
        openai_content: Any = ""
    elif len(text_parts) == 1 and text_parts[0].get("type") == "text":
        openai_content = text_parts[0]["text"]
    else:
        openai_content = text_parts
    return openai_content, tool_calls, tool_results


def _tool_result_text(content: Any) -> str:
    """Flatten an Anthropic ``tool_result`` block's content into a string for OpenAI."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "".join(parts)
        return json.dumps(content)
    return str(content)


def _translate_tools(tools: Any) -> list[dict[str, Any]] | None:
    """Translate Anthropic tool definitions to OpenAI function tools (best-effort).

    Anthropic ``{name, description, input_schema}`` maps to OpenAI
    ``{"type": "function", "function": {name, description, parameters}}``. A tool that is
    already OpenAI-shaped (carries a ``function`` key) or is not a mapping is passed through
    unchanged so a caller mixing shapes is not broken.
    """
    if not isinstance(tools, list):
        return None
    translated: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if "function" in tool or tool.get("type") == "function":
            translated.append(tool)
            continue
        function: dict[str, Any] = {"name": str(tool.get("name") or "")}
        description = tool.get("description")
        if description is not None:
            function["description"] = description
        function["parameters"] = tool.get("input_schema") or {"type": "object", "properties": {}}
        translated.append({"type": "function", "function": function})
    return translated


def _translate_tool_choice(tool_choice: Any) -> Any:
    """Translate an Anthropic ``tool_choice`` to the OpenAI equivalent (best-effort)."""
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return None


def anthropic_to_chat_payload(request: MessagesRequest) -> dict[str, Any]:
    """Translate an Anthropic Messages request into an internal OpenAI chat payload.

    The returned dict is fed to the *same* chat governance path as ``/v1/chat/completions``
    (allowlist, admission (including the ``max_tokens`` cap and prompt-secret modes on the
    translated messages), budget reserve, runtime call, output guardrail, audit). Mapping:

    - ``system`` is prepended as a ``role: "system"`` message (string or flattened blocks).
    - each Anthropic message's content is translated (text blocks -> text; ``tool_use`` ->
      assistant ``tool_calls``; ``tool_result`` -> separate ``role: "tool"`` messages).
    - ``max_tokens`` -> ``max_tokens``; ``stop_sequences`` -> ``stop``; ``temperature`` /
      ``top_p`` pass through; ``tools`` and ``tool_choice`` are translated best-effort.

    ``stream`` and ``metadata`` are intentionally not forwarded to the runtime here: the
    handler decides streaming, and ``metadata`` is an Anthropic-only field.
    """
    payload = request.model_dump(exclude_none=True)
    messages: list[dict[str, Any]] = []

    system_text = _system_text(payload.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for message in request.messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        openai_content, tool_calls, tool_results = _translate_content_blocks(message.get("content"))
        if role == "assistant":
            assistant: dict[str, Any] = {"role": "assistant"}
            # An assistant turn that carries only tool_use blocks has empty text content;
            # OpenAI represents that as content=null alongside tool_calls.
            assistant["content"] = openai_content if openai_content != "" else None
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)
        else:
            # ``user`` (or any non-assistant role): tool_result blocks become their own
            # ``role: "tool"`` messages, appended after any user text content.
            if openai_content != "" or not tool_results:
                messages.append({"role": "user", "content": openai_content})
            messages.extend(tool_results)

    chat_payload: dict[str, Any] = {"messages": messages, "max_tokens": request.max_tokens}
    if request.model is not None:
        chat_payload["model"] = request.model
    if request.temperature is not None:
        chat_payload["temperature"] = request.temperature
    if request.top_p is not None:
        chat_payload["top_p"] = request.top_p
    if request.stop_sequences is not None:
        chat_payload["stop"] = request.stop_sequences

    translated_tools = _translate_tools(payload.get("tools"))
    if translated_tools:
        chat_payload["tools"] = translated_tools
    translated_choice = _translate_tool_choice(payload.get("tool_choice"))
    if translated_choice is not None:
        chat_payload["tool_choice"] = translated_choice
    return chat_payload


def chat_completion_to_anthropic(response: dict[str, Any], *, request_model: str | None) -> dict[str, Any]:
    """Translate an OpenAI chat completion into an Anthropic ``Message`` response body.

    Produces ``{id, type:"message", role:"assistant", model, content:[...], stop_reason,
    stop_sequence, usage}``. The assistant text becomes a ``text`` content block; any
    assistant ``tool_calls`` become ``tool_use`` blocks (best-effort: the id/name are
    preserved and the JSON ``arguments`` string is parsed back into an ``input`` object).
    ``finish_reason`` is mapped to an Anthropic ``stop_reason`` and the runtime ``usage`` is
    surfaced as ``input_tokens``/``output_tokens``.
    """
    choices = response.get("choices") if isinstance(response, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        message = {}

    content_blocks: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})
    elif isinstance(text, list):
        # Content-part array (rare on a completion): keep any text parts.
        for part in text:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                content_blocks.append({"type": "text", "text": part["text"]})

    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        raw_arguments = function.get("arguments")
        try:
            tool_input = json.loads(raw_arguments) if isinstance(raw_arguments, str) and raw_arguments else {}
        except (ValueError, TypeError):
            # Preserve the un-parseable arguments string so nothing is lost.
            tool_input = {"_raw_arguments": raw_arguments}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": str(tool_call.get("id") or f"toolu_{uuid4().hex[:24]}"),
                "name": str(function.get("name") or ""),
                "input": tool_input if isinstance(tool_input, dict) else {"value": tool_input},
            }
        )

    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    stop_reason = _STOP_REASON_BY_FINISH_REASON.get(finish_reason or "", "end_turn")

    usage = response.get("usage") if isinstance(response, dict) else None
    prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None

    message_id = response.get("id") if isinstance(response, dict) else None
    model = response.get("model") if isinstance(response, dict) else None

    return {
        "id": str(message_id) if message_id else f"msg_{uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model or request_model or "",
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(prompt_tokens) if isinstance(prompt_tokens, (int, float)) else 0,
            "output_tokens": int(completion_tokens) if isinstance(completion_tokens, (int, float)) else 0,
        },
    }


def _sse_event(event_type: str, data: dict[str, Any]) -> bytes:
    """Serialize one Anthropic SSE event: a named ``event:`` line and its ``data:`` line."""
    body = json.dumps(data, separators=(",", ":"))
    return f"event: {event_type}\ndata: {body}\n\n".encode()


def anthropic_stream_error_event(message: str, *, request_id: str, sandbox_id: str) -> bytes:
    """Build the terminal Anthropic ``error`` event for an upstream failure mid-stream.

    The OpenAI-shaped equivalent (``streaming._terminal_stream_error_event``) is not usable
    here: an Anthropic client parses named events and would treat a bare OpenAI error object
    as an unknown event rather than a failed message.
    """
    return _sse_event(
        "error",
        {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": message,
                "request_id": request_id,
                "sandbox_id": sandbox_id,
            },
        },
    )


def iter_sse_data_objects(segment: bytes) -> Iterator[dict[str, Any]]:
    """Yield each decoded ``data:`` JSON object from a complete run of SSE text.

    ``[DONE]``, blank lines, and undecodable payloads are skipped: the Anthropic stream is
    rebuilt from the decoded chunks, so anything that is not a chat-completion chunk has no
    translation and is dropped rather than forwarded in the wrong protocol.
    """
    for line in segment.split(b"\n"):
        stripped = line.strip()
        if not stripped.startswith(b"data:"):
            continue
        data = stripped[5:].strip()
        if not data or data == b"[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(parsed, dict):
            yield parsed


def _delta_text(content: Any) -> str:
    """Flatten a streamed OpenAI ``delta.content`` (string or content parts) into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def _int_or_none(value: Any) -> int | None:
    """Coerce a runtime-reported token count to int, rejecting bools and non-numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


class AnthropicStreamTranslator:
    """Translate an OpenAI chat-completion chunk stream into Anthropic Messages events.

    Anthropic streams are block-structured: ``message_start``, then one
    ``content_block_start`` / ``content_block_delta``* / ``content_block_stop`` run per
    content block, then ``message_delta`` carrying the stop reason and usage, then
    ``message_stop``. An OpenAI stream is a flat sequence of choice deltas with no block
    framing, so this class holds the block state needed to bridge the two.

    Two governance properties are structural here rather than incidental:

    - Reasoning and thinking deltas cannot leak, because the translator only ever reads
      ``delta.content`` and ``delta.tool_calls``. Anything else the runtime streams has no
      path into the Anthropic events, which is a stronger guarantee than the chat path's
      field-by-field strip.
    - :attr:`scanned_text` accumulates the assistant text in decoded form, so the
      end-of-stream output-guardrail scan inspects the real text rather than its JSON
      escaping inside SSE frames.

    Token counts are only reported by OpenAI-compatible runtimes on the terminal usage
    event, which arrives after ``message_start`` must already have been sent. ``message_start``
    therefore carries zeros and the true counts ride on the terminal ``message_delta``, where
    Anthropic clients already expect the final usage to be reconciled.
    """

    # Ceiling on the retained assistant text, matching the chat stream's scan bound so a
    # long generation cannot grow gateway memory in proportion to its own output.
    SCAN_LIMIT = 262144

    def __init__(self, *, request_model: str | None = None) -> None:
        self._request_model = request_model
        self._scanned_chars = 0
        self._message_id: str | None = None
        self._model: str | None = None
        self._started = False
        self._stopped = False
        self._next_index = 0
        self._open_kind: str | None = None
        self._open_call_index: int | None = None
        self._open_index: int | None = None
        self._tool_block_index: dict[int, int] = {}
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] | None = None
        self._text_parts: list[str] = []

    @property
    def usage(self) -> dict[str, Any] | None:
        """The last OpenAI ``usage`` object seen on the stream, or None if never reported."""
        return self._usage

    @property
    def scanned_text(self) -> str:
        """The decoded assistant text streamed so far, for the output-guardrail scan."""
        return "".join(self._text_parts)

    def feed(self, chunk: dict[str, Any]) -> bytes:
        """Translate one decoded OpenAI chunk into zero or more Anthropic SSE events."""
        if self._stopped:
            return b""
        events: list[bytes] = []
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        if not self._started:
            events.append(self._message_start(chunk))
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            events.extend(self._feed_choice(choices[0]))
        return b"".join(events)

    def finish(self) -> bytes:
        """Close any open block and emit the terminal ``message_delta``/``message_stop``.

        Idempotent, so the stream body can call it from a ``finally`` without tracking
        whether the error path already closed the message.
        """
        if self._stopped:
            return b""
        events: list[bytes] = []
        if not self._started:
            # An empty stream still owes the client a well-formed message.
            events.append(self._message_start({}))
        self._stopped = True
        events.extend(self._close_open_block())
        usage: dict[str, Any] = {}
        input_tokens = _int_or_none((self._usage or {}).get("prompt_tokens"))
        output_tokens = _int_or_none((self._usage or {}).get("completion_tokens"))
        if input_tokens is not None:
            usage["input_tokens"] = input_tokens
        usage["output_tokens"] = output_tokens if output_tokens is not None else 0
        events.append(
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _STOP_REASON_BY_FINISH_REASON.get(self._finish_reason or "", "end_turn"),
                        "stop_sequence": None,
                    },
                    "usage": usage,
                },
            )
        )
        events.append(_sse_event("message_stop", {"type": "message_stop"}))
        return b"".join(events)

    def _message_start(self, chunk: dict[str, Any]) -> bytes:
        self._started = True
        self._message_id = str(chunk.get("id") or f"msg_{uuid4().hex[:24]}")
        self._model = str(chunk.get("model") or self._request_model or "")
        return _sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self._message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self._model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def _feed_choice(self, choice: dict[str, Any]) -> list[bytes]:
        events: list[bytes] = []
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            self._finish_reason = finish_reason
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return events
        text = _delta_text(delta.get("content"))
        if text:
            events.extend(self._open_text_block())
            if self._scanned_chars < self.SCAN_LIMIT:
                self._text_parts.append(text)
                self._scanned_chars += len(text)
            events.append(
                _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._open_index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            )
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    events.extend(self._feed_tool_call(tool_call))
        return events

    def _feed_tool_call(self, tool_call: dict[str, Any]) -> list[bytes]:
        call_index = tool_call.get("index")
        if isinstance(call_index, bool) or not isinstance(call_index, int):
            call_index = 0
        function = tool_call.get("function")
        function = function if isinstance(function, dict) else {}
        events: list[bytes] = []
        if call_index not in self._tool_block_index:
            events.extend(self._close_open_block())
            index = self._next_index
            self._next_index += 1
            self._tool_block_index[call_index] = index
            self._open_kind = "tool_use"
            self._open_call_index = call_index
            self._open_index = index
            events.append(
                _sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": str(tool_call.get("id") or f"toolu_{uuid4().hex[:24]}"),
                            "name": str(function.get("name") or ""),
                            "input": {},
                        },
                    },
                )
            )
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            # Argument deltas are addressed by the block index recorded when the call was
            # first seen. Runtimes stream tool calls one at a time, so in practice that is
            # the open block; addressing by index keeps a resumed call correct anyway.
            events.append(
                _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._tool_block_index[call_index],
                        "delta": {"type": "input_json_delta", "partial_json": arguments},
                    },
                )
            )
        return events

    def _open_text_block(self) -> list[bytes]:
        if self._open_kind == "text":
            return []
        events = self._close_open_block()
        index = self._next_index
        self._next_index += 1
        self._open_kind = "text"
        self._open_call_index = None
        self._open_index = index
        events.append(
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        )
        return events

    def _close_open_block(self) -> list[bytes]:
        if self._open_kind is None:
            return []
        index = self._open_index
        self._open_kind = None
        self._open_call_index = None
        self._open_index = None
        return [_sse_event("content_block_stop", {"type": "content_block_stop", "index": index})]
