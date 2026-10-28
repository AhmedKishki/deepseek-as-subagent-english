from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deepseek_mcp.external_mcp_config import (
    EXTERNAL_CONFIG_ENV,
    ExternalMcpError,
    load_external_mcp_config,
    resolve_header_values,
)


def _document() -> dict:
    return {
        "request_timeout_seconds": 20,
        "max_calls_per_run": 5,
        "servers": [
            {
                "name": "issues",
                "url": "https://mcp.example.com/mcp",
                "headers": {"Authorization": "MCP_ISSUES_TOKEN"},
                "tools": [
                    {
                        "name": "search_issues",
                        "description": "Search issues",
                        "input_schema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                        "read_only": True,
                    }
                ],
            }
        ],
    }


def _write(tmp: str, document: dict, *, mode: int = 0o600) -> Path:
    path = Path(tmp) / "external-mcp.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(path, mode)
    return path


class ExternalMcpConfigTests(unittest.TestCase):
    def test_worker_cannot_own_its_permissions_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, _document())
            with self.assertRaisesRegex(ExternalMcpError, "outside the worker workspace"):
                load_external_mcp_config(path, workspace=Path(tmp))

    def test_permissions_outside_worker_workspace_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as workspace:
            path = _write(tmp, _document())
            self.assertIsNotNone(load_external_mcp_config(path, workspace=Path(workspace)))

    def test_loads_valid_config_and_namespaces_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, _document())
            config = load_external_mcp_config(path)

        assert config is not None
        server = config.servers[0]
        self.assertEqual(server.name, "issues")
        self.assertEqual(server.url, "https://mcp.example.com/mcp")
        self.assertEqual(server.header_env, (("Authorization", "MCP_ISSUES_TOKEN"),))
        self.assertEqual(server.capabilities, frozenset({"coding", "readonly"}))
        tool = server.tools[0]
        self.assertEqual(tool.published_name, "ext__issues__search_issues")
        self.assertEqual(config.request_timeout_seconds, 20)
        self.assertEqual(config.max_calls_per_run, 5)

    def test_missing_file_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_external_mcp_config(Path(tmp) / "absent.json"))

    def test_env_override_path_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, _document())
            with patch.dict(os.environ, {EXTERNAL_CONFIG_ENV: str(path)}):
                config = load_external_mcp_config()
        self.assertIsNotNone(config)

    def test_capability_filter_selects_servers(self) -> None:
        document = _document()
        document["servers"][0]["capabilities"] = ["readonly"]
        with tempfile.TemporaryDirectory() as tmp:
            config = load_external_mcp_config(_write(tmp, document))
        assert config is not None
        self.assertEqual(config.allows("readonly"), config.servers)
        self.assertEqual(config.allows("coding"), ())

    def test_remote_http_is_rejected(self) -> None:
        document = _document()
        document["servers"][0]["url"] = "http://mcp.example.com/mcp"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ExternalMcpError, "HTTPS"):
                load_external_mcp_config(_write(tmp, document))

    def test_loopback_http_is_allowed(self) -> None:
        document = _document()
        document["servers"][0]["url"] = "http://127.0.0.1:8765/mcp"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNotNone(load_external_mcp_config(_write(tmp, document)))

    def test_url_userinfo_query_and_fragment_are_rejected(self) -> None:
        for url in (
            "https://user:pass@mcp.example.com/mcp",
            "https://mcp.example.com/mcp?x=1",
            "https://mcp.example.com/mcp#frag",
        ):
            with self.subTest(url=url):
                document = _document()
                document["servers"][0]["url"] = url
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(ExternalMcpError):
                        load_external_mcp_config(_write(tmp, document))

    def test_git_and_repo_write_tool_names_are_rejected(self) -> None:
        for name in ("git_status", "git.diff", "create_pull_request", "merge_pull_request"):
            with self.subTest(name=name):
                document = _document()
                document["servers"][0]["tools"][0]["name"] = name
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(ExternalMcpError):
                        load_external_mcp_config(_write(tmp, document))

    def test_non_git_generic_tool_names_are_allowed(self) -> None:
        for name in ("fetch_document", "get_service_status", "show_report", "log_query"):
            with self.subTest(name=name):
                document = _document()
                document["servers"][0]["tools"][0]["name"] = name
                with tempfile.TemporaryDirectory() as tmp:
                    self.assertIsNotNone(load_external_mcp_config(_write(tmp, document)))

    def test_read_only_must_be_declared_true(self) -> None:
        for value in (None, False):
            with self.subTest(value=value):
                document = _document()
                tool = document["servers"][0]["tools"][0]
                if value is None:
                    tool.pop("read_only")
                else:
                    tool["read_only"] = value
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(ExternalMcpError):
                        load_external_mcp_config(_write(tmp, document))

    def test_namespacing_collision_is_rejected(self) -> None:
        document = _document()
        tool = document["servers"][0]["tools"][0]
        clone = dict(tool, name="search.issues")
        document["servers"][0]["tools"] = [tool, clone]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ExternalMcpError, "collide"):
                load_external_mcp_config(_write(tmp, document))

    def test_unsupported_keys_and_duplicate_keys_fail_closed(self) -> None:
        document = _document()
        document["extra"] = True
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ExternalMcpError):
                load_external_mcp_config(_write(tmp, document))
        raw = json.dumps(_document()).replace('"name": "issues"', '"name": "issues", "name": "issues"', 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "external-mcp.json"
            path.write_text(raw, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaises(ExternalMcpError):
                load_external_mcp_config(path)

    @unittest.skipIf(os.name == "nt", "POSIX privacy boundary")
    def test_non_private_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, _document(), mode=0o644)
            with self.assertRaisesRegex(ExternalMcpError, "private"):
                load_external_mcp_config(path)

    def test_header_values_resolve_only_from_declared_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_external_mcp_config(_write(tmp, _document()))
        assert config is not None
        server = config.servers[0]
        with patch.dict(os.environ, {"MCP_ISSUES_TOKEN": "secret"}, clear=False):
            self.assertEqual(resolve_header_values(server), {"Authorization": "secret"})
        with patch.dict(os.environ, {"MCP_ISSUES_TOKEN": ""}, clear=False):
            with self.assertRaisesRegex(ExternalMcpError, "MCP_ISSUES_TOKEN"):
                resolve_header_values(server)

    def test_bounded_integer_settings(self) -> None:
        document = _document()
        document["max_calls_per_run"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ExternalMcpError):
                load_external_mcp_config(_write(tmp, document))


if __name__ == "__main__":
    unittest.main()
