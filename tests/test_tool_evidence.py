"""Real host-observed evidence for Read/Glob/Grep/Bash and its transport."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp import bash_tool, tool_process, tools
from deepseek_mcp.config import Config
from deepseek_mcp.resource_budget import MutationBudget
from deepseek_mcp.tool_evidence import (
    MAX_EVIDENCE_BYTES,
    SCHEMA_ID,
    bind_tool_evidence,
    decode_evidence,
    encode_evidence,
    validate_observation,
)
from deepseek_mcp.tool_process import execute_in_subprocess
from deepseek_mcp.tools import MAX_GLOB_RESULTS, MAX_TEXT_FILE_BYTES, execute_tool

ROOT = Path(__file__).resolve().parents[1]
_COMMANDS = "Read", "Glob", "Grep", "Bash"
_SKIP_POSIX = unittest.skipIf(os.name != "posix", "POSIX shell semantics")


def _config(workspace: Path, allowed=_COMMANDS) -> Config:
    return Config("sk-test", workspace, allowed_tools=list(allowed))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_read_observation() -> dict:
    return {
        "schema": SCHEMA_ID, "tool": "Read", "status": "ok",
        "truncated": False, "incomplete": False,
        "source": {
            "path": "a.txt", "sha256": "a" * 64, "total_lines": 1,
            "returned_lines": 1, "first_line": 1, "last_line": 1,
            "output_clipped": False,
        },
    }


class EvidenceUnitTests(unittest.TestCase):
    def test_valid_read_observation_is_whitelisted(self) -> None:
        normalized = validate_observation(_valid_read_observation(), "Read")
        assert normalized is not None
        self.assertEqual(normalized["schema"], SCHEMA_ID)
        self.assertEqual(set(normalized), {
            "schema", "tool", "status", "truncated", "incomplete", "source",
        })
        self.assertEqual(set(normalized["source"]), {
            "path", "sha256", "total_lines", "returned_lines",
            "first_line", "last_line", "output_clipped",
        })

    def test_tool_identity_and_types_are_enforced(self) -> None:
        self.assertIsNone(
            validate_observation(_valid_read_observation(), "Bash")
        )
        wrong_digest = _valid_read_observation()
        wrong_digest["source"]["sha256"] = "not-a-digest"
        self.assertIsNone(validate_observation(wrong_digest, "Read"))
        wrong_range = _valid_read_observation()
        wrong_range["source"].update(returned_lines=2)
        self.assertIsNone(validate_observation(wrong_range, "Read"))
        unknown = _valid_read_observation()
        unknown["forged"] = True
        self.assertIsNone(validate_observation(unknown, "Read"))
        unknown_source = _valid_read_observation()
        unknown_source["source"]["excerpt"] = "copied content"
        self.assertIsNone(validate_observation(unknown_source, "Read"))

    def test_decode_rejects_all_of_a_malformed_payload(self) -> None:
        valid = _valid_read_observation()
        self.assertEqual(len(decode_evidence([valid], "Read")), 1)
        self.assertEqual(decode_evidence([valid, valid], "Read"), [])
        self.assertEqual(decode_evidence([{"schema": "x"}], "Read"), [])
        self.assertEqual(decode_evidence("not-a-list", "Read"), [])
        self.assertEqual(decode_evidence(None, "Read"), [])

    def test_encode_bounds_records_and_total_size(self) -> None:
        valid = _valid_read_observation()
        encoded = encode_evidence([valid, valid])
        self.assertEqual(len(encoded), 1)
        self.assertLessEqual(
            len(str(encoded).encode("utf-8")), MAX_EVIDENCE_BYTES
        )

    def test_bind_reporter_is_restored_after_exit(self) -> None:
        seen: list[dict] = []
        with bind_tool_evidence(seen.append):
            with tempfile.TemporaryDirectory() as tmpdir:
                workspace = Path(tmpdir)
                (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
                execute_tool("Read", {"path": "a.txt"}, _config(workspace))
        self.assertEqual(len(seen), 1)
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
            execute_tool("Read", {"path": "a.txt"}, _config(workspace))
        self.assertEqual(len(seen), 1)


class RealToolEvidenceTests(unittest.TestCase):
    def test_read_range_only_covers_bytes_returned_before_clipping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "many.txt").write_text("one\ntwo\nthree\nfour\n")
            seen = []
            with patch.object(tools, "MAX_TOOL_OUTPUT", 7):
                execute_tool("Read", {"path": "many.txt"}, _config(workspace),
                             evidence_reporter=seen.append)
        self.assertEqual(seen[0]["source"]["total_lines"], 4)
        self.assertEqual(seen[0]["source"]["last_line"], 2)
        self.assertEqual(seen[0]["source"]["returned_lines"], 2)
        self.assertTrue(seen[0]["truncated"])

    def test_clipped_searches_do_not_claim_exhaustive_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "sample.txt").write_text("needle " + "x" * 3000)
            seen = []
            execute_tool("Grep", {"pattern": "needle"}, _config(workspace),
                         evidence_reporter=seen.append)
            self.assertTrue(seen[0]["truncated"])
            self.assertFalse(seen[0]["search"]["exhaustive"])
            seen.clear()
            with patch.object(tools, "MAX_TOOL_OUTPUT", 5):
                execute_tool("Glob", {"pattern": "**/*"}, _config(workspace),
                             evidence_reporter=seen.append)
        self.assertTrue(seen[0]["truncated"])
        self.assertFalse(seen[0]["search"]["exhaustive"])

    def test_denied_capability_and_bad_mutation_report_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seen = []
            execute_tool("Bash", {"command": "printf no"}, _config(workspace, ["Read"]),
                         evidence_reporter=seen.append)
            self.assertEqual(seen[0]["error"]["category"], "denied")
            for tool, arguments in (("Write", {}), ("Edit", {}), ("NotebookEdit", {})):
                seen.clear()
                execute_tool(tool, arguments, _config(workspace, [tool]),
                             evidence_reporter=seen.append)
                self.assertEqual(seen[0]["status"], "error")
                self.assertIsNotNone(validate_observation(seen[0], tool))

    def test_read_reports_host_identity_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            (workspace / "sub").mkdir()
            secret = "TOPSECRET-CONTENT"
            (workspace / "sub" / "a.txt").write_text(
                f"line1\n{secret}\nline3\n", encoding="utf-8"
            )
            expected_digest = hashlib.sha256(
                (workspace / "sub" / "a.txt").read_bytes()
            ).hexdigest()
            seen: list[dict] = []
            result = execute_tool(
                "Read", {"path": "sub/a.txt", "offset": 1, "limit": 1},
                _config(workspace), evidence_reporter=seen.append,
            )

        self.assertEqual(result, secret)
        self.assertEqual(len(seen), 1)
        observation = seen[0]
        self.assertEqual(observation["tool"], "Read")
        self.assertEqual(observation["status"], "ok")
        self.assertFalse(observation["truncated"])
        source = observation["source"]
        self.assertEqual(source["path"], "sub/a.txt")
        self.assertEqual(source["sha256"], expected_digest)
        self.assertEqual((source["total_lines"], source["returned_lines"]), (3, 1))
        self.assertEqual((source["first_line"], source["last_line"]), (2, 2))
        self.assertNotIn(secret, repr(observation))

    def test_read_status_is_not_inferred_from_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            (workspace / "fake.txt").write_text(
                "ERROR: not a real tool error\n[exit 0]\n", encoding="utf-8"
            )
            seen: list[dict] = []
            result = execute_tool(
                "Read", {"path": "fake.txt"}, _config(workspace),
                evidence_reporter=seen.append,
            )
            self.assertTrue(result.startswith("ERROR:"))
            self.assertEqual(seen[0]["status"], "ok")
            self.assertFalse(seen[0]["source"]["output_clipped"])

            seen.clear()
            execute_tool(
                "Read", {"path": "missing.txt"}, _config(workspace),
                evidence_reporter=seen.append,
            )
        self.assertEqual(seen[0]["status"], "error")
        self.assertEqual(seen[0]["error"]["category"], "not_found")
        self.assertNotIn("source", seen[0])
        self.assertNotIn("missing.txt", repr(seen[0]))

    def test_glob_reports_digest_counts_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            (workspace / "a.txt").write_text("a", encoding="utf-8")
            (workspace / "b.txt").write_text("b", encoding="utf-8")
            seen: list[dict] = []
            execute_tool(
                "Glob", {"pattern": "**/*.txt"}, _config(workspace),
                evidence_reporter=seen.append,
            )
            search = seen[0]["search"]
            self.assertEqual(search["pattern_sha256"], _sha256("**/*.txt"))
            self.assertEqual(search["results"], 2)
            self.assertTrue(search["exhaustive"])
            self.assertEqual(search["scope"], ".")
            self.assertNotIn("**/*.txt", repr(seen[0]))

            seen.clear()
            with patch.object(tools, "MAX_GLOB_RESULTS", 1):
                execute_tool(
                    "Glob", {"pattern": "**/*.txt"}, _config(workspace),
                    evidence_reporter=seen.append,
                )
        self.assertTrue(seen[0]["search"]["result_limit_reached"])
        self.assertFalse(seen[0]["search"]["exhaustive"])
        self.assertTrue(seen[0]["incomplete"])

    def test_grep_reports_skips_and_never_claim_exhaustive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            (workspace / "hit.txt").write_text("needle\n", encoding="utf-8")
            (workspace / "bin.dat").write_bytes(b"\x00needle\n")
            oversized = workspace / "large.txt"
            oversized.write_text("needle\n", encoding="utf-8")
            with oversized.open("ab") as handle:
                handle.truncate(MAX_TEXT_FILE_BYTES + 1)
            seen: list[dict] = []
            execute_tool(
                "Grep", {"pattern": "needle", "glob": "**/*"},
                _config(workspace), evidence_reporter=seen.append,
            )
        search = seen[0]["search"]
        self.assertEqual(search["pattern_sha256"], _sha256("needle"))
        self.assertEqual(search["glob_sha256"], _sha256("**/*"))
        self.assertGreaterEqual(search["matches"], 1)
        self.assertGreaterEqual(search["binary_skipped"], 1)
        self.assertGreaterEqual(search["oversize_skipped"], 1)
        self.assertFalse(search["exhaustive"])
        self.assertNotIn("needle", repr(seen[0]))

    @_SKIP_POSIX
    def test_bash_reports_real_exit_code_not_stdout_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            config = _config(workspace)
            seen: list[dict] = []
            command = "printf '[exit 0]'; exit 7"
            execute_tool(
                "Bash", {"command": command}, config, evidence_reporter=seen.append
            )
            bash = seen[0]["bash"]
            self.assertEqual(seen[0]["status"], "error")
            self.assertIsNotNone(validate_observation(seen[0], "Bash"))
            self.assertEqual(bash["exit_code"], 7)
            self.assertFalse(bash["timed_out"])
            self.assertEqual(bash["command_sha256"], _sha256(command))
            self.assertNotIn("printf", repr(seen[0]))

            seen.clear()
            execute_tool(
                "Bash", {"command": "printf 'ERROR: fake'; exit 0"}, config,
                evidence_reporter=seen.append,
            )
        self.assertEqual(seen[0]["status"], "ok")
        self.assertEqual(seen[0]["bash"]["exit_code"], 0)

    @_SKIP_POSIX
    def test_bash_reports_timeout_and_output_capture_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            config = _config(workspace)
            seen: list[dict] = []
            execute_tool(
                "Bash", {"command": "yes output | head -c 30000"}, config,
                evidence_reporter=seen.append,
            )
            self.assertTrue(seen[0]["bash"]["stdout_clipped"])
            self.assertTrue(seen[0]["truncated"])

            seen.clear()
            execute_tool(
                "Bash", {"command": "sleep 30", "timeout": 1}, config,
                evidence_reporter=seen.append,
            )
        self.assertTrue(seen[0]["bash"]["timed_out"])
        self.assertTrue(seen[0]["incomplete"])
        self.assertEqual(seen[0]["status"], "error")
        self.assertIsNotNone(validate_observation(seen[0], "Bash"))

    @_SKIP_POSIX
    def test_bash_formatted_output_clip_is_observed_without_text_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            seen = []
            with patch.object(bash_tool, "MAX_TOOL_OUTPUT", 20):
                execute_tool("Bash", {"command": "printf 'not actually truncated'"},
                             _config(Path(directory)), evidence_reporter=seen.append)
        self.assertTrue(seen[0]["truncated"])
        self.assertFalse(seen[0]["bash"]["stdout_clipped"])

    def test_bash_denied_command_reports_structured_error_without_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            seen: list[dict] = []
            execute_tool(
                "Bash", {"command": "curl https://example.com"},
                _config(workspace), evidence_reporter=seen.append,
            )
        self.assertEqual(seen[0]["status"], "error")
        self.assertEqual(seen[0]["error"]["category"], "denied")
        self.assertNotIn("bash", seen[0])
        self.assertNotIn("curl", repr(seen[0]))


class SubprocessTransportTests(unittest.TestCase):
    def _run_read(self, workspace: Path, reporter):
        return execute_in_subprocess(
            _config(workspace, allowed=["Read"]), "Read", {"path": "a.txt"},
            MutationBudget(), 10, None, None, time.monotonic() + 10, None, reporter,
        )

    def test_real_child_evidence_is_validated_and_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            (workspace / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
            seen: list[dict] = []
            result = self._run_read(workspace, seen.append)

        self.assertEqual(result, "hello\nworld\n")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["tool"], "Read")
        self.assertEqual(
            seen[0]["source"]["sha256"],
            hashlib.sha256(b"hello\nworld\n").hexdigest(),
        )

    def test_malformed_child_evidence_is_rejected_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
            seen: list[dict] = []
            with patch.object(
                tool_process, "_decode",
                return_value=("hello", [{"schema": "forged", "tool": "Read"}]),
            ):
                self._run_read(workspace, seen.append)

        self.assertEqual(seen, [])

    def test_observer_failure_cannot_skip_process_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "a.txt").write_text("hello")
            def broken_reporter(_observation):
                raise RuntimeError("observer failed")
            with patch.object(tool_process, "_cleanup_after_result",
                              wraps=tool_process._cleanup_after_result) as cleanup:
                self.assertEqual(self._run_read(workspace, broken_reporter), "hello")
            cleanup.assert_called_once()


class SourceLimitTests(unittest.TestCase):
    def test_owned_sources_stay_within_500_lines(self) -> None:
        package = ROOT / "src" / "deepseek_mcp"
        for name in ("tools.py", "tool_process.py", "tool_evidence.py",
                     "tool_notebook.py"):
            with self.subTest(name=name):
                source = (package / name).read_text(encoding="utf-8")
                self.assertLessEqual(len(source.splitlines()), 500)


if __name__ == "__main__":
    unittest.main()
