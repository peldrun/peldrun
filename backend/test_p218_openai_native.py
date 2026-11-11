# backend/test_p218_openai_native.py
"""P2 Item 18 verification: terminal marker in openai_native.py.

Run from backend/ directory:
    python test_p218_openai_native.py
"""

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from peldrun.llm.contracts import ConnectionContext, StreamEventType


def _make_conn() -> ConnectionContext:
    return ConnectionContext(
        connection_id="t",
        provider_id="openai_native",
        base_url="http://test-openai",
        api_key="sk-test",
        timeout=10.0,
    )


def _make_req():
    from peldrun.llm.request import LLMRequest
    return LLMRequest(
        provider_id="openai_native",
        model_id="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        connection=_make_conn(),
    )


async def _collect(events) -> dict:
    last = None
    async for ev in events:
        if ev.type == StreamEventType.MESSAGE_COMPLETE:
            last = ev
    assert last is not None, "no MESSAGE_COMPLETE"
    return {"finish_reason": last.finish_reason}


def _patch_client(adapter, handler):
    adapter._client = type("FakeClient", (), {
        "chat": type("Chat", (), {
            "completions": type("Comp", (), {
                "create": None,
            })(),
        })(),
        "responses": type("Resp", (), {
            "create": None,
        })(),
        "close": None,
    })()
    return adapter


async def _test_chat_with_marker():
    from peldrun.llm.providers.openai_native import OpenAINativeAdapter

    raw = (
        b'data: {"id":"x","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        b'data: {"id":"x","choices":[{"delta":{"content":" there"},"finish_reason":null}]}\n\n'
        b'data: {"id":"x","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: [DONE]\n\n'
    )

    async def fake_stream(**kwargs):
        class Stream:
            def __init__(self, raw):
                self._raw = raw
                self._lines = [ln for ln in raw.split(b"\n\n") if ln.strip()]
                self._idx = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._idx >= len(self._lines):
                    raise StopAsyncIteration
                line = self._lines[self._idx]
                self._idx += 1
                # Fake chunk object
                import json as _json
                if line.startswith(b"data: "):
                    body = line[len(b"data: "):].strip()
                    if body == b"[DONE]":
                        raise StopAsyncIteration
                    data = _json.loads(body)
                    class Choice:
                        def __init__(self, data):
                            self.delta = type("D", (), {
                                "content": data.get("choices", [{}])[0].get("delta", {}).get("content"),
                                "model_extra": {},
                                "tool_calls": None,
                            })()
                            self.finish_reason = data.get("choices", [{}])[0].get("finish_reason")
                    class Chunk:
                        def __init__(self, data):
                            self.choices = [Choice(data)]
                            self.usage = None
                    return Chunk(data)
                raise StopAsyncIteration

            async def close(self):
                pass

        return Stream(raw)

    adapter = OpenAINativeAdapter(connection=_make_conn())
    await adapter._client.close() if hasattr(adapter._client, "close") and adapter._client.close else None
    adapter._client.chat.completions.create = fake_stream

    try:
        result = await _collect(adapter.stream(_make_req()))
        ok = result["finish_reason"] == "stop"
        print(f"[{'PASS' if ok else 'FAIL'}] chat with finish_reason → '{result['finish_reason']}' (expect 'stop')")
        return ok
    finally:
        await adapter.close()


async def _test_chat_without_marker():
    from peldrun.llm.providers.openai_native import OpenAINativeAdapter

    async def fake_stream(**kwargs):
        class Stream:
            def __init__(self):
                self._idx = 0
                self._chunks = [
                    {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
                    {"choices": [{"delta": {"content": " the"}, "finish_reason": None}]},
                    # no finish_reason — stream cut
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._idx >= len(self._chunks):
                    raise StopAsyncIteration
                data = self._chunks[self._idx]
                self._idx += 1
                class Choice:
                    def __init__(self, data):
                        self.delta = type("D", (), {
                            "content": data["choices"][0]["delta"].get("content"),
                            "model_extra": {},
                            "tool_calls": None,
                        })()
                        self.finish_reason = data["choices"][0]["finish_reason"]
                class Chunk:
                    def __init__(self, data):
                        self.choices = [Choice(data)]
                        self.usage = None
                return Chunk(data)

            async def close(self):
                pass

        return Stream()

    adapter = OpenAINativeAdapter(connection=_make_conn())
    adapter._client.chat.completions.create = fake_stream

    try:
        result = await _collect(adapter.stream(_make_req()))
        ok = result["finish_reason"] == "incomplete"
        print(f"[{'PASS' if ok else 'FAIL'}] chat without finish_reason → '{result['finish_reason']}' (expect 'incomplete')")
        return ok
    finally:
        await adapter.close()


async def _test_responses_with_marker():
    from peldrun.llm.providers.openai_native import OpenAINativeAdapter

    async def fake_stream(**kwargs):
        class Stream:
            def __init__(self):
                self._idx = 0
                self._chunks = [
                    {"type": "response.output_text.delta", "delta": "hi"},
                    {"type": "response.output_text.delta", "delta": " there"},
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "usage": None,
                        },
                    },
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._idx >= len(self._chunks):
                    raise StopAsyncIteration
                data = self._chunks[self._idx]
                self._idx += 1
                class Event:
                    def __init__(self, data):
                        self.type = data["type"]
                        self.delta = data.get("delta")
                        self.response = data.get("response")
                return Event(data)

            async def close(self):
                pass

        return Stream()

    adapter = OpenAINativeAdapter(connection=_make_conn())
    adapter._client.responses.create = fake_stream

    try:
        # Set api_surface to responses
        adapter._api_surface = "responses"
        # Patch profile to force responses surface via provider_options
        # Simpler: patch _build_responses_kwargs to make it use responses
        # But the compiled.api_surface will use the profile. Let's just
        # call _stream_responses directly.
        from peldrun.llm.request_compiler import compile_request
        compiled = compile_request(_make_req(), adapter._profile)
        result = await _collect(adapter._stream_responses(compiled))
        ok = result["finish_reason"] == "stop"
        print(f"[{'PASS' if ok else 'FAIL'}] responses with completed → '{result['finish_reason']}' (expect 'stop')")
        return ok
    finally:
        await adapter.close()


async def _test_responses_without_marker():
    from peldrun.llm.providers.openai_native import OpenAINativeAdapter

    async def fake_stream(**kwargs):
        class Stream:
            def __init__(self):
                self._idx = 0
                self._chunks = [
                    {"type": "response.output_text.delta", "delta": "hi"},
                    {"type": "response.output_text.delta", "delta": " the"},
                    # no response.completed — stream cut
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._idx >= len(self._chunks):
                    raise StopAsyncIteration
                data = self._chunks[self._idx]
                self._idx += 1
                class Event:
                    def __init__(self, data):
                        self.type = data["type"]
                        self.delta = data.get("delta")
                        self.response = data.get("response")
                return Event(data)

            async def close(self):
                pass

        return Stream()

    adapter = OpenAINativeAdapter(connection=_make_conn())
    adapter._client.responses.create = fake_stream

    try:
        from peldrun.llm.request_compiler import compile_request
        compiled = compile_request(_make_req(), adapter._profile)
        result = await _collect(adapter._stream_responses(compiled))
        ok = result["finish_reason"] == "incomplete"
        print(f"[{'PASS' if ok else 'FAIL'}] responses without completed → '{result['finish_reason']}' (expect 'incomplete')")
        return ok
    finally:
        await adapter.close()


async def main() -> None:
    all_ok = True
    for test in (
        _test_chat_with_marker(),
        _test_chat_without_marker(),
        _test_responses_with_marker(),
        _test_responses_without_marker(),
    ):
        try:
            ok = await test
            all_ok = all_ok and ok
        except Exception as exc:
            print(f"[FAIL] test raised: {exc}")
            import traceback
            traceback.print_exc()
            all_ok = False
    print("\nRESULT:", "ALL PASS" if all_ok else "SOME FAILED")


if __name__ == "__main__":
    asyncio.run(main())
