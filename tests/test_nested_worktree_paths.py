"""In-repo controller storage is hidden without blocking a worker's own root."""
import tempfile
import unittest
from pathlib import Path

from deepseek_mcp.config import Config
from deepseek_mcp.safety import SandboxViolation, is_unsafe_workspace_root, resolve_safe_path
from deepseek_mcp.workspace_walk import WorkspaceWalk


class NestedWorktreePathTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.parent = Path(self.folder.name)
        self.control = self.parent / ".deepseek-as-subagent"
        self.instance = self.control / ".state" / "run-000000000001"
        self.instance.mkdir(parents=True)
        self.checkout = self.control / "run-000000000001"
        self.checkout.mkdir(parents=True)
        self.sibling = self.control / "run-000000000002"
        self.sibling.mkdir(parents=True)
        (self.checkout / "notes.txt").write_text("own task\n")
        (self.sibling / "notes.txt").write_text("another task\n")
        (self.instance / "manifest.json").write_text("{}")
        self.hooks = self.instance / "hooks"
        self.hooks.mkdir()
        Config("", workspace=self.checkout)
        Config("", workspace=self.parent)

    def test_own_checkout_paths_remain_accessible(self):
        self.assertEqual(resolve_safe_path("notes.txt", self.checkout), self.checkout / "notes.txt")
        self.assertFalse(is_unsafe_workspace_root(self.checkout))
        with WorkspaceWalk("", self.checkout, "**/*.txt") as entries:
            self.assertEqual([entry.path for entry in entries], [self.checkout / "notes.txt"])

    def test_parent_workspace_cannot_access_controller_storage(self):
        for path in (self.control, self.instance / "manifest.json", self.sibling / "notes.txt"):
            with self.subTest(path=path), self.assertRaises(SandboxViolation):
                resolve_safe_path(str(path), self.parent)
        with WorkspaceWalk("", self.parent, ".deepseek-as-subagent/**") as entries:
            self.assertEqual(list(entries), [])

    def test_worker_cannot_access_sibling_or_manifest(self):
        for path in (self.instance / "manifest.json", self.sibling / "notes.txt"):
            with self.subTest(path=path), self.assertRaises(SandboxViolation):
                resolve_safe_path(str(path), self.checkout)

    def test_controller_metadata_cannot_be_assigned_as_workspace(self):
        for path in (self.control, self.instance, self.hooks):
            with self.subTest(path=path):
                self.assertTrue(is_unsafe_workspace_root(path))

    def test_nested_controller_storage_is_not_worker_task_data(self):
        with self.assertRaises(SandboxViolation):
            resolve_safe_path(".deepseek-as-subagent/another/manifest.json", self.checkout)
