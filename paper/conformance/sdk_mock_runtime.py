#!/usr/bin/env python3
"""OpenAI-compatible mock runtime for the client-SDK conformance suite.

The cost-of-compliance mock answers one route without streaming, which is enough to
measure gateway overhead and not enough to drive a real client SDK. This one covers the
surfaces the SDKs actually exercise: chat (streaming and not), legacy completions, and
embeddings, including the terminal usage event a streamed response needs.

It is deliberately a plain ``http.server``: the point of the suite is the SDK's own parser
acting as the oracle for the gateway's output, so the runtime behind it should be as dull
and predictable as possible.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COMPLETION_TEXT = "hello from the mock runtime"
TOOL_CALL_ID = "call_mock_0"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path in ("/healthz", "/health", "/v1/models"):
            self._json(200, {"status": "ok", "object": "list", "data": []})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid json"})
            return
        if self.path == "/v1/chat/completions":
            if payload.get("stream"):
                self._stream_chat(payload)
            else:
                self._json(200, self._chat_body(payload))
            return
        if self.path == "/v1/completions":
            self._json(200, self._completion_body(payload))
            return
        if self.path == "/v1/embeddings":
            self._json(200, self._embeddings_body(payload))
            return
        self._json(404, {"error": "not found"})

    def _chat_body(self, payload: dict) -> dict:
        message: dict = {"role": "assistant", "content": COMPLETION_TEXT}
        finish_reason = "stop"
        if payload.get("tools"):
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": TOOL_CALL_ID,
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Berlin"}'},
                    }
                ],
            }
            finish_reason = "tool_calls"
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": payload.get("model", "mock-model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }

    def _completion_body(self, payload: dict) -> dict:
        return {
            "id": "cmpl-mock",
            "object": "text_completion",
            "created": 1700000000,
            "model": payload.get("model", "mock-model"),
            "choices": [{"index": 0, "text": COMPLETION_TEXT, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        }

    def _embeddings_body(self, payload: dict) -> dict:
        raw = payload.get("input")
        items = raw if isinstance(raw, list) else [raw]
        return {
            "object": "list",
            "model": payload.get("model", "mock-model"),
            "data": [
                {"object": "embedding", "index": index, "embedding": [0.01 * index, 0.02, 0.03]}
                for index, _ in enumerate(items)
            ],
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }

    def _stream_chat(self, payload: dict) -> None:
        model = payload.get("model", "mock-model")
        chunks: list[dict] = []
        if payload.get("tools"):
            chunks.append(self._delta(model, {"role": "assistant", "content": "looking that up"}))
            chunks.append(
                self._delta(
                    model,
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": TOOL_CALL_ID,
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ]
                    },
                )
            )
            for fragment in ('{"city"', ':"Berlin"}'):
                chunks.append(self._delta(model, {"tool_calls": [{"index": 0, "function": {"arguments": fragment}}]}))
            chunks.append(self._delta(model, {}, finish_reason="tool_calls"))
        else:
            for word in COMPLETION_TEXT.split(" "):
                chunks.append(self._delta(model, {"content": word + " "}))
            chunks.append(self._delta(model, {}, finish_reason="stop"))
        if (payload.get("stream_options") or {}).get("include_usage"):
            chunks.append(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": model,
                    "choices": [],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                }
            )

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    @staticmethod
    def _delta(model: str, delta: dict, finish_reason: str | None = None) -> dict:
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible runtime for SDK conformance.")
    parser.add_argument("--port", type=int, default=9099)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
