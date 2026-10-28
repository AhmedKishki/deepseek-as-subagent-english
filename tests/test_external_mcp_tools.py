from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deepseek_mcp import external_mcp_tools as tools
from deepseek_mcp.external_mcp_config import (
    ExternalMcpConfig,
    ExternalMcpError,
    ExternalServerSpec,
    ExternalToolSpec,
)
from deepseek_mcp.external_mcp_process import (
    ExternalCallCancelled,
    ExternalCallDeadline,
    ExternalCallError,
    ExternalCallOutcome,
)
from deepseek_mcp.provider_retry import AgentLoopCancelled, AgentLoopError


def _config(capabilities=frozenset({"coding", "readonly"})) -> ExternalMcpConfig:
    tool = ExternalToolSpec(
        server="issues",
        remote_name="search_issues",
        published_name="ext__issues__search_issues",
        description="Search issues",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    server = ExternalServerSpec(
        name="issues",
        url="https://mcp.example.com/mcp",
        tools=(tool,),
        header_env=(("Authorization", "MCP_TOKEN"),),
        capabilities=capabilities,
    )
    return ExternalMcpConfig(servers=(server,), max_calls_per_run=2)


class ExternalMcpToolsTests(unittest.TestCase):
    def test_build_schemas_and_names(self) -> None:
        schemas = tools.build_external_tool_schemas(_config(), "coding")
        self.assertEqual(len(schemas), 1)
        function = schemas[0]["function"]
        self.assertEqual(function["name"], "ext__issues__search_issues")
        self.assertIn("untrusted", function["description"])
        self.assertEqual(
            tools.external_tool_names(_config(), "readonly"),
            frozenset({"ext__issues__search_issues"}),
        )
        self.assertEqual(
            tools.build_external_tool_schemas(_config(frozenset({"readonly"})), "coding"),
            [],
        )

    def test_find_and_describe(self) -> None:
        config = _config()
        self.assertIsNotNone(
            tools.find_external_tool(config, "coding", "ext__issues__search_issues")
        )
        self.assertIsNone(tools.find_external_tool(config, "coding", "Read"))
        described = tools.describe_external_tools(config, "coding", "Read, Grep")
        self.assertIn("ext__issues__search_issues", described)

    def test_claim_external_call_enforces_budget(self) -> None:
        config = _config()
        self.assertEqual(tools.claim_external_call(config, "coding", "Read", 0), 0)
        self.assertEqual(
            tools.claim_external_call(
                config, "coding", "ext__issues__search_issues", 0
            ),
            1,
        )
        with self.assertRaises(AgentLoopError):
            tools.claim_external_call(
                config, "coding", "ext__issues__search_issues", 2
            )

    def _execute(self, outcome, *, reporter=None, headers=None):
        config = _config()
        arguments = {"q": "private-query"}
        with (
            patch.object(tools, "resolve_header_values", return_value=headers or {"Authorization": "secret"}),
            patch.object(tools, "execute_external_in_subprocess", return_value=outcome) as call,
        ):
            text = tools.execute_external_tool(
                config,
                "coding",
                "ext__issues__search_issues",
                arguments,
                budget_seconds=5,
                cancel_signal=None,
                deadline=None,
                evidence_reporter=reporter,
            )
        return text, call, arguments

    def test_success_is_marked_untrusted_and_evidenced_without_headers(self) -> None:
        observations: list[dict] = []
        text, _call, _args = self._execute(
            ExternalCallOutcome(True, text="remote body"), reporter=observations.append
        )
        self.assertIn("untrusted", text)
        self.assertIn("remote body", text)
        self.assertEqual(len(observations), 1)
        observation = observations[0]
        self.assertEqual(observation["status"], "ok")
        self.assertEqual(observation["remote_tool"], "search_issues")
        self.assertNotIn("secret", str(observation))
        self.assertNotIn("private-query", str(observation))

    def test_tool_error_is_evidenced_as_error(self) -> None:
        observations: list[dict] = []
        text, _call, _args = self._execute(
            ExternalCallOutcome(True, text="boom", is_error=True),
            reporter=observations.append,
        )
        self.assertIn("tool reported an error", text)
        self.assertEqual(observations[0]["status"], "error")

    def test_failed_outcome_maps_to_bounded_error(self) -> None:
        observations: list[dict] = []
        text, _call, _args = self._execute(
            ExternalCallOutcome(False, category="connect"),
            reporter=observations.append,
        )
        self.assertTrue(text.startswith("ERROR:"))
        self.assertIn("could not be reached", text)
        self.assertEqual(observations[0]["status"], "error")

    def test_missing_header_credentials_fail_closed(self) -> None:
        config = _config()
        observations: list[dict] = []
        with patch.object(
            tools, "resolve_header_values", side_effect=ExternalMcpError("unset")
        ):
            text = tools.execute_external_tool(
                config,
                "coding",
                "ext__issues__search_issues",
                {},
                budget_seconds=5,
                cancel_signal=None,
                deadline=None,
                evidence_reporter=observations.append,
            )
        self.assertTrue(text.startswith("ERROR:"))
        self.assertEqual(observations[0]["status"], "error")

    def test_cancellation_and_deadline_map_to_agent_loop_errors(self) -> None:
        config = _config()
        for error, expected in (
            (ExternalCallCancelled("x"), AgentLoopCancelled),
            (ExternalCallDeadline("x"), AgentLoopError),
        ):
            with self.subTest(error=type(error).__name__):
                with (
                    patch.object(tools, "resolve_header_values", return_value={}),
                    patch.object(
                        tools, "execute_external_in_subprocess", side_effect=error
                    ),
                    self.assertRaises(expected),
                ):
                    tools.execute_external_tool(
                        config,
                        "coding",
                        "ext__issues__search_issues",
                        {},
                        budget_seconds=5,
                        cancel_signal=None,
                        deadline=None,
                    )

    def test_unknown_tool_is_refused(self) -> None:
        with self.assertRaises(AgentLoopError):
            tools.execute_external_tool(
                _config(),
                "coding",
                "ext__issues__delete_everything",
                {},
                budget_seconds=5,
                cancel_signal=None,
                deadline=None,
            )

    def test_execution_lease_descriptor_is_forwarded(self) -> None:
        with (
            patch.object(tools, "resolve_header_values", return_value={}),
            patch.object(
                tools, "execute_external_in_subprocess",
                return_value=ExternalCallOutcome(True, text="x"),
            ) as call,
        ):
            tools.execute_external_tool(
                _config(),
                "coding",
                "ext__issues__search_issues",
                {},
                budget_seconds=5,
                cancel_signal=None,
                deadline=None,
                execution_lease_fd=7,
            )
        self.assertEqual(call.call_args.args[7], 7)

    def test_launch_error_maps_to_bounded_agent_error(self) -> None:
        with (
            patch.object(tools, "resolve_header_values", return_value={}),
            patch.object(
                tools, "execute_external_in_subprocess",
                side_effect=ExternalCallError("lease unavailable"),
            ),
            self.assertRaises(AgentLoopError),
        ):
            tools.execute_external_tool(
                _config(),
                "coding",
                "ext__issues__search_issues",
                {},
                budget_seconds=5,
                cancel_signal=None,
                deadline=None,
            )


if __name__ == "__main__":
    unittest.main()
