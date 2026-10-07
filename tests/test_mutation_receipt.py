"""Receipt identities come from an existing durable intent, never worker prose."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp import transaction_journal as journal
from deepseek_mcp.config import Config
from deepseek_mcp.mutation_receipt import intent_identity


class MutationReceiptTests(unittest.TestCase):
    def test_only_matching_intent_exposes_path_and_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "target.txt").write_text("changed")
            config = Config("", workspace, allowed_tools=["Write"])
            digest = hashlib.sha256(b"changed").digest()
            identifier = "a" * 32
            with patch.object(journal, "JOURNAL_DIRECTORY", root / "journal"):
                journal.record_intent(config, identifier, "Write", {"path": "target.txt"}, digest)
                expected = {"path": "target.txt", "sha256": digest.hex()}
                self.assertEqual(intent_identity(config, identifier, "Write", digest), expected)
                self.assertEqual(intent_identity(config, identifier, "Edit", digest), {})
                self.assertEqual(intent_identity(config, identifier, "Write", b"x" * 32), {})
                self.assertEqual(intent_identity(config, "b" * 32, "Write", digest), {})
                self.assertEqual(intent_identity(config, identifier, "Write", None), {})
                self.assertEqual(len(journal.pending_records(config)), 1)


if __name__ == "__main__":
    unittest.main()
