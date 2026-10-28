"""Git belongs exclusively to the orchestrator, not coding workers."""
import unittest
from unittest.mock import patch

from deepseek_mcp.safety import SandboxViolation, check_command
from deepseek_mcp.host_instructions import HOST_INSTRUCTIONS
from deepseek_mcp.worktree_policy import MANAGED_WORKER_INSTRUCTIONS
from deepseek_mcp.bash_tool import execute_bash


class NoGitPolicyTests(unittest.TestCase):
    def test_host_and_worker_instructions_prohibit_all_git(self):
        self.assertIn("Subagents never use Git, including read-only", HOST_INSTRUCTIONS)
        self.assertIn("Never use Git, including read-only commands", MANAGED_WORKER_INSTRUCTIONS)

    def test_git_read_and_write_commands_denied(self):
        for command in (
            "git status", "git diff", "git log", "git commit -m change",
            "/usr/bin/git status", "pwd && git status", "command git status",
            "env git status", "git --version", "git", "git.exe status",
            "pwd; git status", "pwd | git diff", "pwd || git log",
            "GIT status", r"\git status", "env -i git status",
        ):
            with self.subTest(command=command), self.assertRaises(SandboxViolation):
                check_command(command)

    def test_non_git_commands_remain_allowed(self):
        for command in ("pwd", "python3 -m unittest", "npm test"):
            check_command(command)

    def test_bash_dispatch_refuses_git_before_starting_process(self):
        with patch("deepseek_mcp.bash_tool._run_on_trusted_host") as run:
            result = execute_bash({"command": "git status"}, None)
        self.assertTrue(result.startswith("ERROR:"))
        run.assert_not_called()
