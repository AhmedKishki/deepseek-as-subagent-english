from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deepseek_mcp import agent_loop
from deepseek_mcp.config import Config
from deepseek_mcp.external_mcp_config import (
    ExternalMcpConfig,
    ExternalServerSpec,
    ExternalToolSpec,
)
from deepseek_mcp.hard_deadline import HardDeadline
from deepseek_mcp.provider_retry import AgentLoopError

PUBLISHED = "ext__issues__search_issues"


def _external(max_calls: int = 2) -> ExternalMcpConfig:
    tool = ExternalToolSpec(
        server="issues",
        remote_name="search_issues",
        published_name=PUBLISHED,
        description="Search",
        input_schema={"type": "object"},
    )
    server = ExternalServerSpec(
        name="issues",
        url="https://mcp.example.com/mcp",
        tools=(tool,),
        header_env=(),
        capabilities=frozenset({"coding", "readonly"}),
    )
    return ExternalMcpConfig(servers=(server,), max_calls_per_run=max_calls)


def _state(external: ExternalMcpConfig, tool_calls: int = 0) -> agent_loop._AgentState:
    return agent_loop._AgentState(
        config=Config("", ROOT, allowed_tools=["Read", "Glob", "Grep"]),
        tools=[],
        controls=agent_loop._AgentControls(None, None, None),
        messages=[],
        started=time.time(),
        deadline=HardDeadline.after(60),
        external=external,
    )


def _tool_call(arguments: str = '{"q": 1}') -> SimpleNamespace:
    return SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name=PUBLISHED, arguments=arguments),
    )


class ExternalMcpAgentLoopTests(unittest.TestCase):
    def test_system_prompt_forbids_git_and_lists_external_tools(self) -> None:
        state = agent_loop._create_agent_state(
            "task", Config("", ROOT, allowed_tools=["Read"]),
            [], agent_loop._AgentControls(None, None, None), None, _external(),
        )
        prompt = state.messages[0]["content"]
        self.assertIn("Never use Git", prompt)
        self.assertIn(PUBLISHED, prompt)

    def test_external_call_is_dispatched_and_recorded(self) -> None:
        state = _state(_external())
        with patch.object(
            agent_loop, "execute_external_tool", return_value="[external] result"
        ) as call:
            agent_loop._execute_and_record_tool(state, _tool_call(), 0)
        self.assertEqual(state.external_calls, 1)
        self.assertEqual(state.tool_calls, 1)
        self.assertEqual(state.messages[-1]["content"], "[external] result")
        self.assertEqual(call.call_args.args[2], PUBLISHED)

    def test_external_budget_is_enforced(self) -> None:
        state = _state(_external(max_calls=1))
        with patch.object(agent_loop, "execute_external_tool", return_value="ok"):
            agent_loop._execute_and_record_tool(state, _tool_call(), 0)
        with self.assertRaises(AgentLoopError):
            agent_loop._execute_and_record_tool(state, _tool_call(), 0)

    def test_internal_tool_is_not_treated_as_external(self) -> None:
        state = _state(_external())
        read_call = SimpleNamespace(
            id="call-2",
            function=SimpleNamespace(name="Read", arguments='{"path": "x"}'),
        )
        with (
            patch.object(
                agent_loop, "_execute_one_tool", return_value="internal"
            ) as internal,
            patch.object(agent_loop, "execute_external_tool") as external,
        ):
            agent_loop._execute_and_record_tool(state, read_call, 0)
        internal.assert_called_once()
        external.assert_not_called()
        self.assertEqual(state.external_calls, 0)


if __name__ == "__main__":
    unittest.main()
