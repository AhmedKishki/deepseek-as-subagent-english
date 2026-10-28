"""Actual MCP SDK and isolated child against a harmless loopback fixture."""
from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def fixture_server(*, readonly=True):
    methods = []
    authorization = "Bearer fixture-only-not-a-real-secret"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.headers.get("Authorization") != authorization:
                self.send_error(401)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = request["method"]
            methods.append(method)
            if "id" not in request:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            results = {
                "initialize": {
                    "protocolVersion": request.get("params", {}).get("protocolVersion"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "loopback-fixture", "version": "1"},
                },
                "tools/list": {"tools": [{
                    "name": "lookup", "description": "Read a fixture token",
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {"readOnlyHint": readonly, "destructiveHint": False},
                }]},
                "tools/call": {
                    "content": [{"type": "text", "text": "LARK-4172"}], "isError": False,
                },
            }
            payload = json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                  "result": results[method]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", authorization, methods
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@unittest.skipUnless(
    importlib.util.find_spec("mcp") and importlib.util.find_spec("httpx"),
    "MCP SDK dependencies are required for the loopback integration test",
)
class ExternalMcpLoopbackTests(unittest.TestCase):
    def _execute(self, url, authorization):
        from deepseek_mcp.external_mcp_config import ExternalServerSpec, ExternalToolSpec
        from deepseek_mcp.external_mcp_process import execute_external_in_subprocess

        tool = ExternalToolSpec("fixture", "lookup", "ext__fixture__lookup",
                                "Read fixture", {"type": "object", "properties": {}})
        server = ExternalServerSpec("fixture", url, (tool,), (), frozenset({"readonly"}))
        return execute_external_in_subprocess(
            server, tool, {}, {"Authorization": authorization}, 10, None, None,
        )

    def test_real_sdk_and_child_deliver_selected_readonly_tool(self):
        with fixture_server() as (url, authorization, methods):
            result = self._execute(url, authorization)
        self.assertTrue(result.ok, result)
        self.assertFalse(result.is_error)
        self.assertEqual(result.text, "LARK-4172")
        self.assertNotIn(authorization, result.text)
        self.assertEqual(methods, ["initialize", "notifications/initialized",
                                   "tools/list", "tools/call"])

    def test_remote_write_annotation_refused_before_call(self):
        with fixture_server(readonly=False) as (url, authorization, methods):
            result = self._execute(url, authorization)
        self.assertFalse(result.ok)
        self.assertEqual(result.category, "annotation")
        self.assertNotIn("tools/call", methods)
