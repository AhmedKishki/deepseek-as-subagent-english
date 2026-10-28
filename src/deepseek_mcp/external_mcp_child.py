"""Private stdio child that performs exactly one read-only external MCP call.

The parent resolves configured header credentials and sends them only on this
child's private stdin pipe. The child owns the MCP SDK streamable-HTTP client
and a fresh ``ClientSession``; the model never sees the URL, headers, or raw
transport. One process per call keeps execution cancellable and bounded.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
from datetime import timedelta

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .external_mcp_config import (
    MAX_CALL_REQUEST_BYTES,
    MAX_CALL_RESPONSE_BYTES,
    MAX_CALL_TEXT_BYTES,
    MAX_HTTP_BODY_BYTES,
)
from .parent_liveness import wait_for_parent_loss_or_timeout
from .process_hardening import harden_provider_process

MAX_TIMEOUT_SECONDS = 120
CONNECT_TIMEOUT_SECONDS = 10.0
WRITE_TIMEOUT_SECONDS = 15.0
TERMINATION_GRACE_SECONDS = 2.0
MAX_REQUEST_BYTES = MAX_CALL_REQUEST_BYTES
MAX_RESPONSE_BYTES = MAX_CALL_RESPONSE_BYTES
MAX_TEXT_BYTES = MAX_CALL_TEXT_BYTES
_ERROR_CATEGORIES = frozenset(
    {"client", "connect", "timeout", "protocol", "annotation", "tool", "limit"}
)
for _name in ("mcp", "httpx", "httpcore", "anyio", "mcp.client"):
    _child_logger = logging.getLogger(_name)
    _child_logger.handlers.clear()
    _child_logger.addHandler(logging.NullHandler())
    _child_logger.propagate = False
    _child_logger.setLevel(logging.ERROR)


class _ExternalFailure(Exception):
    """A bounded, non-sensitive failure category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category if category in _ERROR_CATEGORIES else "protocol"


class _BoundedStream(httpx.AsyncByteStream):
    """Abort a decoded HTTP body once it exceeds the configured cap."""

    def __init__(self, stream, limit: int) -> None:
        self._stream = stream
        self._limit = limit

    async def __aiter__(self):
        total = 0
        async for chunk in self._stream:
            total += len(chunk)
            if total > self._limit:
                raise httpx.ReadError("external MCP response exceeded its limit")
            yield chunk

    async def aclose(self) -> None:
        aclose = getattr(self._stream, "aclose", None)
        if aclose is not None:
            await aclose()


class _BoundedTransport(httpx.AsyncBaseTransport):
    """Bound the wire body and refuse any compressed (non-identity) response.

    The cap is applied to the raw stream before httpx would decode it, so a
    compressed body could otherwise expand past the cap. External MCP responses
    must be uncompressed: ``Accept-Encoding: identity`` is forced on the client
    and any other ``Content-Encoding`` fails closed.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            await response.aclose()
            raise _ExternalFailure("protocol")
        response.stream = _BoundedStream(response.stream, self._limit)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _timeout(value: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError("invalid external MCP deadline") from None
    if not 0 < seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError("invalid external MCP deadline")
    return seconds


def _read_payload() -> dict:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("external MCP request is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("external MCP request is invalid")
    return _validate_payload(payload)


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"external MCP request {label} is invalid")
    return value


def _require_headers(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("external MCP request headers are invalid")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError("external MCP request headers are invalid")
    return dict(value)


def _validate_payload(payload: dict) -> dict:
    url = _require_text(payload.get("url"), "url")
    tool = _require_text(payload.get("tool"), "tool")
    headers = _require_headers(payload.get("headers"))
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("external MCP request arguments are invalid")
    seconds = payload.get("timeout")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("external MCP request timeout is invalid")
    if not 0 < float(seconds) <= MAX_TIMEOUT_SECONDS:
        raise ValueError("external MCP request timeout is invalid")
    max_text = payload.get("max_text_bytes", MAX_TEXT_BYTES)
    if isinstance(max_text, bool) or not isinstance(max_text, int):
        raise ValueError("external MCP request text cap is invalid")
    if not 1 <= max_text <= MAX_TEXT_BYTES:
        raise ValueError("external MCP request text cap is invalid")
    return {
        "url": url,
        "tool": tool,
        "headers": headers,
        "arguments": arguments,
        "timeout": float(seconds),
        "max_text_bytes": max_text,
    }


def _build_client(headers: dict[str, str], seconds: float) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=min(CONNECT_TIMEOUT_SECONDS, seconds),
        read=seconds,
        write=min(WRITE_TIMEOUT_SECONDS, seconds),
        pool=min(CONNECT_TIMEOUT_SECONDS, seconds),
    )
    transport = _BoundedTransport(
        httpx.AsyncHTTPTransport(retries=0, trust_env=False), MAX_HTTP_BODY_BYTES,
    )
    client = httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    )
    # Never accept a compressed response we cannot bound before decoding.
    client.headers["Accept-Encoding"] = "identity"
    return client


MAX_TOOL_PAGES = 16


def _select_tool(tools, name: str):
    if not isinstance(tools, list):
        raise _ExternalFailure("protocol")
    for tool in tools:
        if getattr(tool, "name", None) != name:
            continue
        annotations = getattr(tool, "annotations", None)
        if annotations is None:
            raise _ExternalFailure("annotation")
        if getattr(annotations, "readOnlyHint", None) is not True:
            raise _ExternalFailure("annotation")
        if getattr(annotations, "destructiveHint", None) is True:
            raise _ExternalFailure("annotation")
        return tool
    raise _ExternalFailure("tool")


async def _list_tools(session) -> list:
    """Collect tools across bounded pages, refusing cursor cycles."""
    tools: list = []
    cursor = None
    seen: set[str] = set()
    for _page in range(MAX_TOOL_PAGES):
        listing = await session.list_tools(cursor=cursor)
        page_tools = getattr(listing, "tools", None)
        if not isinstance(page_tools, list):
            raise _ExternalFailure("protocol")
        tools.extend(page_tools)
        cursor = getattr(listing, "nextCursor", None)
        if not cursor:
            return tools
        if cursor in seen:
            raise _ExternalFailure("protocol")
        seen.add(cursor)
    raise _ExternalFailure("limit")


def _cap_text(text: str, maximum: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text, False
    clipped = encoded[:maximum].decode("utf-8", "ignore")
    return clipped + "\n... [external output truncated]", True


def _extract(result, maximum: int) -> tuple[str, bool, list[str], bool]:
    parts: list[str] = []
    kinds: list[str] = []
    has_text = False
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", "unknown")
        kinds.append(kind)
        text = getattr(block, "text", None) if kind == "text" else None
        if isinstance(text, str):
            has_text = True
            parts.append(text)
        else:
            parts.append(f"[{kind} content omitted]")
    if not has_text and getattr(result, "structuredContent", None) is not None:
        try:
            parts.append(json.dumps(result.structuredContent, ensure_ascii=True))
            kinds.append("structured")
        except (TypeError, ValueError):
            parts.append("[structured content omitted]")
    text, truncated = _cap_text("\n".join(parts), maximum)
    return text, truncated, kinds, bool(getattr(result, "isError", False))


def _envelope(result, maximum: int) -> dict:
    text, truncated, kinds, is_error = _extract(result, maximum)
    return {
        "kind": "ok",
        "text": text,
        "truncated": truncated,
        "is_error": is_error,
        "content_types": kinds,
    }


async def _with_session(session, name: str, arguments: dict, maximum: int) -> dict:
    await session.initialize()
    tools = await _list_tools(session)
    _select_tool(tools, name)
    result = await session.call_tool(name, arguments)
    return _envelope(result, maximum)


def _category_of(error: BaseException) -> str | None:
    """Recover a bounded category from SDK/anyio exception groups."""
    if isinstance(error, _ExternalFailure):
        return error.category
    children = getattr(error, "exceptions", None)
    if isinstance(children, tuple):
        for child in children:
            found = _category_of(child)
            if found is not None:
                return found
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    return None


async def _run(payload: dict) -> dict:
    client = _build_client(payload["headers"], payload["timeout"])
    try:
        async with client:
            async with streamable_http_client(
                payload["url"], http_client=client, terminate_on_close=True
            ) as streams:
                session = ClientSession(
                    streams[0],
                    streams[1],
                    read_timeout_seconds=timedelta(seconds=payload["timeout"]),
                )
                async with session:
                    return await _with_session(
                        session,
                        payload["tool"],
                        payload["arguments"],
                        payload["max_text_bytes"],
                    )
    except _ExternalFailure as failure:
        return {"kind": "error", "category": failure.category}
    except (httpx.TimeoutException, TimeoutError):
        return {"kind": "error", "category": "timeout"}
    except BaseException as error:
        return {"kind": "error", "category": _category_of(error) or "connect"}


def _write_payload(payload: dict) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        encoded = b'{"kind":"error","category":"limit"}'
    view = memoryview(encoded)
    while view:
        written = os.write(1, view)
        if written <= 0:
            os._exit(125)
        view = view[written:]


def _exit_if_stalled(completed: threading.Event, timeout: float, liveness: str) -> None:
    wait_for_parent_loss_or_timeout(liveness, max(0.001, timeout))
    if completed.is_set():
        return
    if os.name != "posix":
        os._exit(124)
    os.killpg(os.getpgrp(), signal.SIGTERM)
    if not completed.wait(TERMINATION_GRACE_SECONDS):
        os._exit(124)


def main() -> None:
    completed = threading.Event()
    try:
        timeout = _timeout(sys.argv[1])
        liveness = sys.argv[2]
        harden_provider_process()
    except (IndexError, ValueError):
        return
    except RuntimeError:
        try:
            _write_payload({"kind": "error", "category": "client"})
        except BaseException:
            pass
        return
    watchdog = threading.Thread(
        target=_exit_if_stalled,
        args=(completed, timeout, liveness),
        daemon=True,
        name="deepseek-external-mcp-deadline",
    )
    watchdog.start()
    try:
        envelope = asyncio.run(_run(_read_payload()))
    except BaseException:
        envelope = {"kind": "error", "category": "client"}
    try:
        _write_payload(envelope)
    except BaseException:
        pass
    finally:
        completed.set()


if __name__ == "__main__":
    main()
