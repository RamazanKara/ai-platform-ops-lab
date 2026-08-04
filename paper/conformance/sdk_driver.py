#!/usr/bin/env python3
"""Drive the real vendor client SDKs against a running gateway and report what worked.

Run by ``sdk_conformance.py`` inside a throwaway virtualenv that has ``openai`` and
``anthropic`` installed. It is a separate process on purpose: those SDKs are not in the
gateway's hash-pinned runtime or dev locks and should not become dependencies of the
service just to be tested against it.

The value of this file is that **the SDK is the oracle**. The gateway's own OpenAPI
snapshot proves the gateway did not change; it cannot prove the gateway matches what
upstream clients expect. Here the assertion is that the vendor's own parser accepts the
bytes: a stream the SDK cannot accumulate, an error envelope it cannot classify, or a
tool call it cannot reconstruct all fail here and nowhere else.

Prints a single JSON object of check results to stdout.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

import anthropic
import openai

GATEWAY_URL = os.environ["CONF_GATEWAY_URL"]
API_KEY = os.environ["CONF_API_KEY"]
MODEL = os.environ["CONF_MODEL"]


def openai_client() -> openai.OpenAI:
    return openai.OpenAI(base_url=f"{GATEWAY_URL}/v1", api_key=API_KEY, max_retries=0)


def anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(base_url=GATEWAY_URL, api_key=API_KEY, max_retries=0)


def check_openai_chat_completion() -> dict:
    response = openai_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "hello"}],
    )
    content = response.choices[0].message.content
    assert content, "assistant content was empty"
    assert response.usage.total_tokens > 0, "usage was not reported"
    return {"content_chars": len(content), "total_tokens": response.usage.total_tokens}


def check_openai_chat_streaming() -> dict:
    stream = openai_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )
    text = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
    assert text.strip(), "streamed assistant text was empty"
    return {"streamed_chars": len(text)}


def check_openai_chat_streaming_usage() -> dict:
    stream = openai_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    usage = None
    for chunk in stream:
        if chunk.usage is not None:
            usage = chunk.usage
    assert usage is not None, "no terminal usage event reached the client"
    return {"total_tokens": usage.total_tokens}


def check_openai_completions() -> dict:
    response = openai_client().completions.create(model=MODEL, prompt="hello", max_tokens=16)
    assert response.choices[0].text, "legacy completion text was empty"
    return {"text_chars": len(response.choices[0].text)}


def check_openai_embeddings() -> dict:
    response = openai_client().embeddings.create(model=MODEL, input=["one", "two"])
    assert len(response.data) == 2, "embedding count did not match the input count"
    assert response.data[0].embedding, "embedding vector was empty"
    return {"vectors": len(response.data), "dimensions": len(response.data[0].embedding)}


def check_openai_models_list() -> dict:
    models = openai_client().models.list()
    ids = [model.id for model in models.data]
    assert MODEL in ids, f"{MODEL} missing from the model list"
    return {"models": len(ids)}


def check_openai_error_envelope() -> dict:
    # The SDK classifies by the error envelope's shape. A gateway rejection the client
    # cannot classify surfaces to users as an opaque failure instead of a 400 they can act on.
    try:
        openai_client().chat.completions.create(
            model="definitely-not-approved",
            messages=[{"role": "user", "content": "hello"}],
        )
    except openai.BadRequestError as exc:
        return {"status": exc.status_code, "code": (exc.body or {}).get("code")}
    raise AssertionError("an unapproved model did not raise BadRequestError")


def check_openai_auth_error() -> dict:
    client = openai.OpenAI(base_url=f"{GATEWAY_URL}/v1", api_key="wrong-key", max_retries=0)
    try:
        client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "hello"}])
    except openai.AuthenticationError as exc:
        return {"status": exc.status_code}
    raise AssertionError("a bad API key did not raise AuthenticationError")


def check_openai_responses() -> dict:
    response = openai_client().responses.create(model=MODEL, input="hello")
    assert response.output_text, "responses output_text was empty"
    return {"output_chars": len(response.output_text)}


def check_anthropic_message() -> dict:
    message = anthropic_client().messages.create(
        model=MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "hello"}],
    )
    assert message.content and message.content[0].text, "Anthropic message text was empty"
    assert message.stop_reason == "end_turn", f"unexpected stop_reason {message.stop_reason}"
    assert message.usage.input_tokens > 0, "input_tokens was not reported"
    return {
        "text_chars": len(message.content[0].text),
        "stop_reason": message.stop_reason,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }


def check_anthropic_streaming() -> dict:
    # The strongest check in the suite. The SDK's stream accumulator asserts the full
    # Anthropic event grammar: message_start, correctly indexed content blocks, deltas,
    # and a terminal message_delta carrying the stop reason and usage. A translator that
    # emits a plausible-looking but malformed sequence fails right here.
    client = anthropic_client()
    text_parts: list[str] = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "hello"}],
    ) as stream:
        for chunk in stream.text_stream:
            text_parts.append(chunk)
        final = stream.get_final_message()
    streamed = "".join(text_parts)
    assert streamed.strip(), "no streamed text reached the client"
    assert final.content[0].text == streamed, "accumulated message text diverged from the stream"
    assert final.stop_reason == "end_turn", f"unexpected stop_reason {final.stop_reason}"
    assert final.usage.output_tokens > 0, "streamed usage was not reported"
    return {
        "streamed_chars": len(streamed),
        "stop_reason": final.stop_reason,
        "output_tokens": final.usage.output_tokens,
    }


def check_anthropic_streaming_tool_use() -> dict:
    # Tool calls stream as partial JSON fragments the SDK reassembles into an input
    # object. This is where an off-by-one in block indexing or a dropped fragment shows up.
    client = anthropic_client()
    with client.messages.stream(
        model=MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "weather in Berlin?"}],
        tools=[
            {
                "name": "get_weather",
                "description": "Look up the weather",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ],
    ) as stream:
        final = stream.get_final_message()
    tool_blocks = [block for block in final.content if block.type == "tool_use"]
    assert tool_blocks, "no tool_use block was reconstructed from the stream"
    assert tool_blocks[0].input == {"city": "Berlin"}, f"tool input was {tool_blocks[0].input}"
    assert final.stop_reason == "tool_use", f"unexpected stop_reason {final.stop_reason}"
    return {"tool_name": tool_blocks[0].name, "tool_input": tool_blocks[0].input}


def check_anthropic_error_envelope() -> dict:
    try:
        anthropic_client().messages.create(
            model="definitely-not-approved",
            max_tokens=64,
            messages=[{"role": "user", "content": "hello"}],
        )
    except anthropic.BadRequestError as exc:
        return {"status": exc.status_code}
    raise AssertionError("an unapproved model did not raise BadRequestError")


CHECKS = [
    ("openai_chat_completion", check_openai_chat_completion),
    ("openai_chat_streaming", check_openai_chat_streaming),
    ("openai_chat_streaming_usage", check_openai_chat_streaming_usage),
    ("openai_completions", check_openai_completions),
    ("openai_embeddings", check_openai_embeddings),
    ("openai_models_list", check_openai_models_list),
    ("openai_error_envelope", check_openai_error_envelope),
    ("openai_auth_error", check_openai_auth_error),
    ("openai_responses", check_openai_responses),
    ("anthropic_message", check_anthropic_message),
    ("anthropic_streaming", check_anthropic_streaming),
    ("anthropic_streaming_tool_use", check_anthropic_streaming_tool_use),
    ("anthropic_error_envelope", check_anthropic_error_envelope),
]


def main() -> int:
    results = []
    for name, check in CHECKS:
        try:
            detail = check()
            results.append({"check": name, "passed": True, "detail": detail})
        except Exception as exc:
            results.append(
                {
                    "check": name,
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=6),
                }
            )
    json.dump(
        {
            "openai_version": openai.__version__,
            "anthropic_version": anthropic.__version__,
            "python": sys.version.split()[0],
            "checks": results,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
