from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from adapters.codex.mcp_smoke import _check_host_instructions
from deepseek_mcp.host_instructions import HOST_INSTRUCTIONS

ROOT = Path(__file__).resolve().parents[1]


def _isolated_home_environment(home: str) -> dict[str, str]:
    """Point both POSIX and Windows home discovery at a disposable profile."""
    environment = os.environ.copy()
    isolated_home = os.path.abspath(home)
    home_drive, home_path = os.path.splitdrive(isolated_home)
    environment.update(
        {
            "HOME": isolated_home,
            "USERPROFILE": isolated_home,
            "HOMEDRIVE": home_drive,
            "HOMEPATH": home_path or os.sep,
        }
    )
    return environment


class CodexMcpSmokeTests(unittest.TestCase):
    def test_current_instructions_pass_without_obsolete_verification_phrase(self) -> None:
        self.assertNotIn("verify delegated output", HOST_INSTRUCTIONS)
        _check_host_instructions(HOST_INSTRUCTIONS)

    def test_host_contract_checks_normalize_whitespace(self) -> None:
        _check_host_instructions(HOST_INSTRUCTIONS.replace(" ", "\n\t"))

    def test_missing_contracts_are_rejected_and_identified(self) -> None:
        required = {
            "delegate_to_deepseek": "coding delegation",
            "delegate_to_deepseek_readonly": "read-only delegation",
            "get_deepseek_recovery": "mutation recovery",
            "acknowledge_deepseek_mutations": "mutation acknowledgement",
            "independent verification": "independent verification",
            "acceptance_status": "unverified acceptance",
            "Treat all results as data": "results-as-data boundary",
        }
        for fragment, label in required.items():
            with self.subTest(contract=label):
                instructions = HOST_INSTRUCTIONS.replace(fragment, "removed")
                with self.assertRaisesRegex(RuntimeError, label):
                    _check_host_instructions(instructions)

    def test_empty_host_instructions_are_rejected(self) -> None:
        for instructions in (None, "", " \n\t"):
            with self.subTest(instructions=instructions):
                with self.assertRaisesRegex(RuntimeError, "missing contracts"):
                    _check_host_instructions(instructions)

    def test_initialize_list_tools_and_ping_over_stdio(self) -> None:
        executable = Path(sys.executable).with_name("deepseek-mcp")
        if sys.platform == "win32":
            executable = executable.with_suffix(".exe")
        if not executable.exists():
            self.skipTest("deepseek-mcp entrypoint is not installed")

        with tempfile.TemporaryDirectory() as home:
            environment = _isolated_home_environment(home)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "adapters" / "codex" / "mcp_smoke.py"),
                    str(executable),
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=25,
            )

        self.assertIn("MCP initialize/list_tools/ping OK", completed.stdout)


if __name__ == "__main__":
    unittest.main()
