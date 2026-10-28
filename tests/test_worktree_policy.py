"""Managed worktrees trust the worker with Bash plus a fixed server-owned policy."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.bash_tool import execute_bash
from deepseek_mcp.config import Config
from deepseek_mcp.execution_profile import (
    CODING_PROFILE,
    READONLY_PROFILE,
    configure_delegation,
)
from deepseek_mcp.safety import SandboxViolation, resolve_safe_path
from deepseek_mcp.worktree_policy import managed_worker_instructions

BRANCH = "deepseek-worktree-abc123"


def _write_manifest(root: Path, checkout: Path, branch: str = BRANCH) -> Path:
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"version": 1, "branch": branch, "checkout": {"path": str(checkout)}}
        ),
        encoding="utf-8",
    )
    return manifest


class WorktreePolicyTests(unittest.TestCase):
    def test_file_paths_cannot_escape_or_access_git(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder) / "checkout"
            checkout.mkdir()
            for path in ("../manifest.json", ".git", ".git/config"):
                with self.subTest(path=path), self.assertRaises(SandboxViolation):
                    resolve_safe_path(path, checkout)

    def test_config_keeps_bash_in_managed_coding(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"DEEPSEEK_WORKTREE_MODE": "1"}
        ):
            config = Config("", workspace=Path(folder))
            self.assertIn("Bash", config.allowed_tools)
            self.assertIn("Edit", config.allowed_tools)
            bound = configure_delegation(config, CODING_PROFILE)
            self.assertIn("Bash", bound.allowed_tools)
            self.assertIn("Edit", bound.allowed_tools)

    def test_readonly_profile_still_excludes_bash(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"DEEPSEEK_WORKTREE_MODE": "1"}
        ):
            config = Config("", workspace=Path(folder))
            readonly = configure_delegation(config, READONLY_PROFILE)
            self.assertEqual(readonly.allowed_tools, ["Read", "Glob", "Grep"])
            self.assertNotIn("Bash", readonly.allowed_tools)

    def test_no_mode_yields_no_managed_instructions(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(managed_worker_instructions(Path(".")))

    def test_invalid_mode_fails_closed(self):
        with patch.dict(os.environ, {"DEEPSEEK_WORKTREE_MODE": "0"}, clear=True):
            with self.assertRaises(RuntimeError):
                managed_worker_instructions(Path("."))

    def test_missing_manifest_fails_closed(self):
        with patch.dict(os.environ, {"DEEPSEEK_WORKTREE_MODE": "1"}, clear=True):
            with self.assertRaises(RuntimeError):
                managed_worker_instructions(Path("."))

    def test_instructions_fix_checkout_and_branch_and_set_boundaries(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as work:
            checkout = Path(work) / "checkout"
            checkout.mkdir()
            manifest = _write_manifest(Path(folder), checkout)
            env = {
                "DEEPSEEK_WORKTREE_MODE": "1",
                "DEEPSEEK_WORKTREE_MANIFEST": str(manifest),
            }
            with patch.dict(os.environ, env, clear=True):
                text = managed_worker_instructions(checkout)
        self.assertIsNotNone(text)
        self.assertIn(str(checkout), text)
        self.assertIn(BRANCH, text)
        for forbidden in ("commit", "merge", "switch", "shared Git lifecycle"):
            self.assertIn(forbidden, text)
        self.assertIn("Never use Git, including read-only commands", text)
        self.assertIn("own tests", text)

    def test_checkout_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as work:
            checkout = Path(work) / "checkout"
            checkout.mkdir()
            other = Path(work) / "other"
            other.mkdir()
            manifest = _write_manifest(Path(folder), checkout)
            env = {
                "DEEPSEEK_WORKTREE_MODE": "1",
                "DEEPSEEK_WORKTREE_MANIFEST": str(manifest),
            }
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(RuntimeError):
                    managed_worker_instructions(other)

    def test_direct_bash_dispatch_is_not_denied(self):
        with patch.dict(os.environ, {"DEEPSEEK_WORKTREE_MODE": "1"}), patch(
            "deepseek_mcp.bash_tool._run_on_trusted_host", side_effect=RuntimeError("boom")
        ) as run:
            result = execute_bash({"command": "pwd"}, None)
            run.assert_called_once()
        self.assertNotIn("disabled", result)
        self.assertIn("trusted host unavailable", result)


if __name__ == "__main__":
    unittest.main()
