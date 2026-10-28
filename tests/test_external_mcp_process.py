from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deepseek_mcp import external_mcp_process as process
from deepseek_mcp.external_mcp_config import ExternalServerSpec, ExternalToolSpec

_FAKE_OK = (
    "import json,sys;sys.stdin.buffer.read();"
    "sys.stdout.write(json.dumps("
    "{'kind':'ok','text':'hi','truncated':False,'is_error':False}))"
)
_FAKE_OVERSIZE = (
    "import json,sys;sys.stdin.buffer.read();"
    "sys.stdout.write(json.dumps({'kind':'error','category':'limit'}))"
)
_FAKE_SLEEP = "import sys,time;sys.stdin.buffer.read();time.sleep(30)"
_FAKE_NOISE = (
    "import json,sys;sys.stdin.buffer.read();"
    "sys.stdout.write('noise\\n'+json.dumps({'kind':'ok','text':'t',"
    "'truncated':False,'is_error':False}))"
)


def _fake(program: str):
    def start(_seconds: float, _lease_fd: int | None = None) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-c", program],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    return start


def _server() -> ExternalServerSpec:
    tool = ExternalToolSpec(
        server="issues",
        remote_name="search_issues",
        published_name="ext__issues__search_issues",
        description="d",
        input_schema={"type": "object"},
    )
    return ExternalServerSpec(
        name="issues",
        url="https://mcp.example.com/mcp",
        tools=(tool,),
        header_env=(),
        capabilities=frozenset({"coding", "readonly"}),
    )


class ExternalMcpProcessTests(unittest.TestCase):
    def test_cancelled_call_does_not_launch_child(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(process, "_start_external") as start:
            with self.assertRaises(process.ExternalCallCancelled):
                process.execute_external_in_subprocess(
                    _server(), _server().tools[0], {}, {}, 5, cancelled, None,
                )
            start.assert_not_called()

    def test_parent_boundary_never_imports_the_child_module(self) -> None:
        source = (ROOT / "src" / "deepseek_mcp" / "external_mcp_process.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("from .external_mcp_child", source)
        self.assertNotIn("import external_mcp_child", source)

    def tearDown(self) -> None:
        for process_handle in getattr(self, "_processes", []):
            if process_handle.poll() is None:
                process_handle.kill()
                process_handle.wait()

    def _run(self, program: str) -> process.ExternalCallOutcome:
        self._processes = []

        def start(seconds: float, lease_fd: int | None = None):
            handle = _fake(program)(seconds, lease_fd)
            self._processes.append(handle)
            return handle

        with patch.object(process, "_start_external", side_effect=start):
            return process.execute_external_in_subprocess(
                _server(), _server().tools[0], {"q": 1}, {}, 5, None, None
            )

    def test_success_outcome_round_trips(self) -> None:
        outcome = self._run(_FAKE_OK)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.text, "hi")

    def test_noise_before_envelope_is_rejected(self) -> None:
        outcome = self._run(_FAKE_NOISE)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.category, "protocol")

    def test_error_envelope_is_mapped_to_category(self) -> None:
        outcome = self._run(_FAKE_OVERSIZE)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.category, "limit")

    def test_decode_outcome_shapes(self) -> None:
        ok = process._decode_outcome(
            json.dumps(
                {"kind": "ok", "text": "x", "truncated": True, "is_error": True}
            ).encode()
        )
        self.assertTrue(ok.ok)
        self.assertTrue(ok.truncated and ok.is_error)
        self.assertEqual(
            process._decode_outcome(b"[]").category, "protocol"
        )
        self.assertEqual(
            process._decode_outcome(b'{"kind":"error","category":"evil"}').category,
            "protocol",
        )

    def test_environment_excludes_home_proxy_and_pythonpath(self) -> None:
        with patch.dict(
            os.environ,
            {"HOME": "/home/x", "PYTHONPATH": "/tmp", "HTTP_PROXY": "http://p"},
            clear=False,
        ):
            environment = process._external_environment()
        self.assertEqual(environment["PATH"], os.defpath)
        for name in ("HOME", "PYTHONPATH", "HTTP_PROXY"):
            self.assertNotIn(name, environment)

    def test_cancellation_kills_the_child(self) -> None:
        cancelled = threading.Event()
        errors: list[BaseException] = []
        self._processes = []

        def invoke() -> None:
            try:
                process.execute_external_in_subprocess(
                    _server(), _server().tools[0], {}, {}, 30, cancelled, None
                )
            except BaseException as error:  # noqa: BLE001 - test capture
                errors.append(error)

        with patch.object(process, "_start_external", side_effect=_fake(_FAKE_SLEEP)):
            worker = threading.Thread(target=invoke)
            worker.start()
            time.sleep(0.3)
            cancelled.set()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], process.ExternalCallCancelled)

    def test_expired_deadline_raises(self) -> None:
        with patch.object(process, "_start_external", side_effect=_fake(_FAKE_SLEEP)):
            with self.assertRaises(process.ExternalCallDeadline):
                process.execute_external_in_subprocess(
                    _server(), _server().tools[0], {}, {}, 30, None,
                    time.monotonic() + 0.4,
                )

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor inheritance")
    def test_unavailable_lease_is_rejected_before_any_launch(self) -> None:
        # A descriptor number the OS will not recycle while the liveness pipe is
        # created, standing in for a lease that was closed after acquisition.
        invalid_descriptor = 1 << 20
        with patch.object(process.subprocess, "Popen") as popen:
            with self.assertRaises(process.ExternalCallError):
                process.execute_external_in_subprocess(
                    _server(), _server().tools[0], {}, {}, 5, None, None,
                    lease_fd=invalid_descriptor,
                )
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor inheritance")
    def test_child_options_include_the_lease_descriptor(self) -> None:
        liveness = process.ParentLiveness.create()
        descriptor = os.open(os.devnull, os.O_RDONLY)
        try:
            options = process._child_options(
                liveness, descriptor, process.ChildLeaseAnchor()
            )
            self.assertIn(descriptor, options["pass_fds"])
        finally:
            os.close(descriptor)
            liveness.close()

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor inheritance")
    def test_start_external_forwards_lease_descriptor_to_popen(self) -> None:
        descriptor = os.open(os.devnull, os.O_RDONLY)
        captured: dict = {}

        class _Proc:
            pid = 4321
            stdin = stdout = stderr = None

        fake_process = _Proc()

        def fake_popen(_argv, **kwargs):
            captured.update(kwargs)
            return fake_process

        try:
            with patch.object(process.subprocess, "Popen", side_effect=fake_popen):
                started = process._start_external(5.0, descriptor)
            self.assertIs(started, fake_process)
            self.assertIn(descriptor, captured["pass_fds"])
        finally:
            process.close_parent_liveness(fake_process)
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
