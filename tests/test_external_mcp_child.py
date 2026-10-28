from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deepseek_mcp import external_mcp_child as child


def _tool(name: str, *, read_only=True, destructive=False, annotations=True):
    values = {}
    if annotations:
        values["readOnlyHint"] = read_only
        values["destructiveHint"] = destructive
    return SimpleNamespace(
        name=name, annotations=SimpleNamespace(**values) if annotations else None
    )


def _result(text: str, *, is_error=False, content_type="text", structured=None):
    block = SimpleNamespace(type=content_type, text=text)
    return SimpleNamespace(
        content=[block], isError=is_error, structuredContent=structured
    )


class _FakeSession:
    def __init__(self, tools, result, pages=None):
        self._tools = tools
        self._result = result
        self._pages = pages
        self.initialized = False
        self.calls: list[tuple] = []
        self.cursors: list = []

    async def initialize(self):
        self.initialized = True

    async def list_tools(self, cursor=None):
        self.cursors.append(cursor)
        if self._pages is not None:
            return self._pages[cursor]
        return SimpleNamespace(tools=self._tools, nextCursor=None)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._result


class ExternalMcpChildTests(unittest.TestCase):
    def test_select_tool_requires_read_only_annotations(self) -> None:
        listing = [_tool("search")]
        self.assertEqual(child._select_tool(listing, "search").name, "search")

        for tool in (
            _tool("missing", annotations=False),
            _tool("unannotated", read_only=None),
            _tool("destructive", destructive=True),
        ):
            with self.subTest(tool=tool.name):
                with self.assertRaises(child._ExternalFailure) as caught:
                    child._select_tool([tool], tool.name)
                self.assertEqual(caught.exception.category, "annotation")

        with self.assertRaises(child._ExternalFailure) as caught:
            child._select_tool(listing, "absent")
        self.assertEqual(caught.exception.category, "tool")

    def test_list_tools_follows_bounded_pages(self) -> None:
        pages = {
            None: SimpleNamespace(tools=[_tool("a")], nextCursor="p2"),
            "p2": SimpleNamespace(tools=[_tool("b")], nextCursor=None),
        }
        session = _FakeSession([], _result("x"), pages=pages)
        tools = asyncio.run(child._list_tools(session))
        self.assertEqual([tool.name for tool in tools], ["a", "b"])
        self.assertEqual(session.cursors, [None, "p2"])

    def test_list_tools_refuses_cursor_cycle(self) -> None:
        pages = {
            None: SimpleNamespace(tools=[], nextCursor="p2"),
            "p2": SimpleNamespace(tools=[], nextCursor="p2"),
        }
        session = _FakeSession([], _result("x"), pages=pages)
        with self.assertRaises(child._ExternalFailure) as caught:
            asyncio.run(child._list_tools(session))
        self.assertEqual(caught.exception.category, "protocol")

    def test_list_tools_refuses_too_many_pages(self) -> None:
        class Endless:
            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=[], nextCursor=str(id(cursor)))

        with self.assertRaises(child._ExternalFailure) as caught:
            asyncio.run(child._list_tools(Endless()))
        self.assertEqual(caught.exception.category, "limit")

    def test_envelope_extracts_text_and_flags_errors(self) -> None:
        envelope = child._envelope(_result("hello", is_error=True), 1024)
        self.assertEqual(envelope["kind"], "ok")
        self.assertEqual(envelope["text"], "hello")
        self.assertTrue(envelope["is_error"])
        self.assertFalse(envelope["truncated"])
        self.assertEqual(envelope["content_types"], ["text"])

    def test_structured_content_used_when_no_text_blocks(self) -> None:
        envelope = child._envelope(
            _result("", content_type="image", structured={"a": 1}), 1024
        )
        self.assertIn('{"a": 1}', envelope["text"])
        self.assertEqual(envelope["content_types"], ["image", "structured"])

    def test_output_is_capped(self) -> None:
        envelope = child._envelope(_result("x" * 100), 10)
        self.assertTrue(envelope["truncated"])
        self.assertLessEqual(len(envelope["text"].encode()), 10 + 64)

    def test_with_session_initializes_lists_and_calls(self) -> None:
        session = _FakeSession([_tool("search")], _result("body"))
        result = asyncio.run(
            child._with_session(session, "search", {"q": "x"}, 1024)
        )
        self.assertTrue(session.initialized)
        self.assertEqual(session.calls, [("search", {"q": "x"})])
        self.assertEqual(result["text"], "body")

    def test_with_session_fails_closed_without_read_only_annotation(self) -> None:
        session = _FakeSession([_tool("search", destructive=True)], _result("x"))
        with self.assertRaises(child._ExternalFailure):
            asyncio.run(child._with_session(session, "search", {}, 1024))
        self.assertEqual(session.calls, [])

    def test_bounded_stream_aborts_oversized_body(self) -> None:
        async def source():
            yield b"abc"
            yield b"def"

        async def scenario():
            stream = child._BoundedStream(source(), 4)
            with self.assertRaises(httpx.ReadError):
                async for _chunk in stream:
                    pass

        asyncio.run(scenario())

    def test_client_disables_ambient_network_and_redirects(self) -> None:
        client = child._build_client({"Authorization": "x"}, 5.0)
        try:
            self.assertFalse(client.trust_env)
            self.assertFalse(client.follow_redirects)
            self.assertEqual(client.headers["Authorization"], "x")
            self.assertIsInstance(client._transport, child._BoundedTransport)
        finally:
            asyncio.run(client.aclose())

    def test_client_forces_identity_encoding_over_user_headers(self) -> None:
        client = child._build_client({"Accept-Encoding": "gzip"}, 5.0)
        try:
            self.assertEqual(client.headers["Accept-Encoding"], "identity")
        finally:
            asyncio.run(client.aclose())

    def test_compressed_response_is_rejected_before_buffering(self) -> None:
        request = httpx.Request("POST", "https://mcp.example.com/mcp")
        compressed = httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=b"\x1f\x8b\x08\x00",
            request=request,
        )

        class _Inner(httpx.AsyncBaseTransport):
            async def handle_async_request(self, _request):
                return compressed

            async def aclose(self):
                return None

        transport = child._BoundedTransport(_Inner(), 1024)
        with self.assertRaises(child._ExternalFailure) as caught:
            asyncio.run(transport.handle_async_request(request))
        self.assertEqual(caught.exception.category, "protocol")

    def test_identity_response_is_stream_wrapped(self) -> None:
        request = httpx.Request("POST", "https://mcp.example.com/mcp")
        plain = httpx.Response(200, content=b"ok", request=request)

        class _Inner(httpx.AsyncBaseTransport):
            async def handle_async_request(self, _request):
                return plain

            async def aclose(self):
                return None

        transport = child._BoundedTransport(_Inner(), 1024)
        result = asyncio.run(transport.handle_async_request(request))
        self.assertIsInstance(result.stream, child._BoundedStream)

    def test_payload_validation_rejects_bad_input(self) -> None:
        valid = {
            "url": "https://mcp.example.com/mcp",
            "tool": "search",
            "headers": {},
            "arguments": {},
            "timeout": 5,
        }
        self.assertEqual(child._validate_payload(valid)["max_text_bytes"], child.MAX_TEXT_BYTES)
        for mutate in (
            {"tool": ""},
            {"headers": {"H": 1}},
            {"arguments": []},
            {"timeout": 0},
            {"max_text_bytes": 0},
        ):
            with self.subTest(mutate=mutate):
                with self.assertRaises(ValueError):
                    child._validate_payload({**valid, **mutate})


if __name__ == "__main__":
    unittest.main()
