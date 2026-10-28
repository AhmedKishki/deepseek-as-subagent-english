"""Killable subprocess boundary for exactly one external MCP tool call.

Mirrors the provider/tool child pattern: the parent resolves credentials, encodes
a bounded request, launches an isolated child, polls cancellation and a
monotonic deadline, then kills the whole process group and joins it.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass

from .child_runtime import (
    child_working_directory,
    isolated_child_argv,
    sanitized_python_environment,
)
from .external_mcp_config import (
    MAX_CALL_REQUEST_BYTES,
    MAX_CALL_RESPONSE_BYTES,
    MAX_CALL_TEXT_BYTES,
)
from .hard_deadline import Deadline, limited, remaining as deadline_remaining
from .lease_inheritance import ChildLeaseAnchor
from .parent_liveness import ParentLiveness, close_parent_liveness
from .provider_process import (
    CONTROL_CHECK_SECONDS,
    PROCESS_STOP_SECONDS,
    _close_process_pipes,
    _stop_process,
)

MAX_CALL_SECONDS = 120.0
MAX_REQUEST_BYTES = MAX_CALL_REQUEST_BYTES
MAX_RESPONSE_BYTES = MAX_CALL_RESPONSE_BYTES
MAX_TEXT_BYTES = MAX_CALL_TEXT_BYTES
_ERROR_CATEGORIES = frozenset(
    {"client", "connect", "timeout", "protocol", "annotation", "tool", "limit"}
)


class ExternalCallError(Exception):
    """An external call could not be safely launched (for example a lost lease)."""


class ExternalCallCancelled(RuntimeError):
    pass


class ExternalCallDeadline(RuntimeError):
    pass


@dataclass(frozen=True)
class ExternalCallOutcome:
    ok: bool
    text: str = ""
    truncated: bool = False
    is_error: bool = False
    category: str = ""


@dataclass
class _Communication:
    output: bytes = b""
    error: bool = False


def _external_environment() -> dict[str, str]:
    """Minimal environment: no proxy, netrc, HOME, or Python import controls."""
    environment = {
        name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ
    }
    environment["PATH"] = os.defpath
    return sanitized_python_environment(environment)


def _encoded_request(
    server, tool, arguments: dict, seconds: float, headers: dict[str, str]
) -> bytes:
    payload = {
        "url": server.url,
        "tool": tool.remote_name,
        "headers": headers,
        "arguments": arguments,
        "timeout": seconds,
        "max_text_bytes": MAX_TEXT_BYTES,
    }
    output = bytearray()
    encoder = json.JSONEncoder(separators=(",", ":"), ensure_ascii=True)
    for text in encoder.iterencode(payload):
        chunk = text.encode("utf-8")
        if len(chunk) > MAX_REQUEST_BYTES - len(output):
            raise ExternalCallDeadline("external MCP request input exceeds its hard limit")
        output.extend(chunk)
    return bytes(output)


def _child_options(
    liveness: ParentLiveness, lease_fd: int | None, anchor: ChildLeaseAnchor
) -> dict:
    descriptors = list(liveness.inherited_fds)
    inherited = lease_fd if os.name == "posix" else None
    if inherited is not None:
        try:
            os.fstat(inherited)
        except OSError:
            raise ExternalCallError("workspace execution lease is unavailable") from None
        descriptors.append(inherited)
    if os.name == "posix":
        return {"pass_fds": tuple(descriptors)}
    if anchor.startupinfo is not None:
        return {"startupinfo": anchor.startupinfo}
    return {}


def _start_external(seconds: float, lease_fd: int | None = None) -> subprocess.Popen[bytes]:
    liveness = ParentLiveness.create()
    anchor = ChildLeaseAnchor()
    process: subprocess.Popen[bytes] | None = None
    try:
        anchor = ChildLeaseAnchor.create(lease_fd)
        options = _child_options(liveness, lease_fd, anchor)
        process = subprocess.Popen(
            isolated_child_argv(
                "deepseek_mcp.external_mcp_child",
                f"{seconds:.6f}",
                liveness.argument,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_external_environment(),
            cwd=child_working_directory(),
            shell=False,
            close_fds=True,
            start_new_session=True,
            **options,
        )
        anchor.close_parent_copy()
        liveness.attach(process)
        return process
    except BaseException:
        try:
            if process is not None:
                _stop_process(process)
        finally:
            if process is not None:
                _close_process_pipes(process)
            liveness.close()
            try:
                anchor.close_parent_copy()
            except OSError:
                pass
        raise


def _communicate(
    process: subprocess.Popen[bytes], request: bytes, state: _Communication,
    ready: threading.Event,
) -> None:
    try:
        stdout, _ = process.communicate(request)
        if len(stdout) <= MAX_RESPONSE_BYTES:
            state.output = stdout
        else:
            state.error = True
    except BaseException:
        state.error = True
    finally:
        ready.set()


def _wait_for_external(
    ready: threading.Event, cancel_signal, request_end: Deadline, run_deadline: bool
) -> bool:
    while not ready.wait(CONTROL_CHECK_SECONDS):
        if cancel_signal is not None and cancel_signal.is_set():
            raise ExternalCallCancelled("external MCP call cancelled")
        if deadline_remaining(request_end) <= 0:
            if run_deadline:
                raise ExternalCallDeadline("run time budget exceeded")
            return False
    return True


def _cleanup_external_process(
    process: subprocess.Popen[bytes],
    communication: threading.Thread,
    started: bool,
) -> None:
    try:
        if not _stop_process(process):
            raise ExternalCallDeadline("external MCP process cleanup failed")
        if started:
            communication.join(PROCESS_STOP_SECONDS)
        if started and communication.is_alive():
            raise ExternalCallDeadline("external MCP pipe cleanup failed")
    finally:
        try:
            close_parent_liveness(process)
        finally:
            _close_process_pipes(process)


def _decode_outcome(raw: bytes) -> ExternalCallOutcome:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ExternalCallOutcome(False, category="protocol")
    if not isinstance(payload, dict):
        return ExternalCallOutcome(False, category="protocol")
    if payload.get("kind") == "ok":
        return _decode_ok(payload)
    if payload.get("kind") == "error":
        category = payload.get("category")
        if category not in _ERROR_CATEGORIES:
            category = "protocol"
        return ExternalCallOutcome(False, category=category)
    return ExternalCallOutcome(False, category="protocol")


def _decode_ok(payload: dict) -> ExternalCallOutcome:
    text = payload.get("text")
    truncated = payload.get("truncated")
    is_error = payload.get("is_error")
    if not isinstance(text, str) or not isinstance(truncated, bool) or not isinstance(is_error, bool):
        return ExternalCallOutcome(False, category="protocol")
    encoded = text.encode("utf-8", "surrogatepass")
    if len(encoded) > MAX_TEXT_BYTES + 64:
        return ExternalCallOutcome(False, category="protocol")
    return ExternalCallOutcome(True, text=text, truncated=truncated, is_error=is_error)


def execute_external_in_subprocess(
    server,
    tool,
    arguments: dict,
    headers: dict[str, str],
    budget_seconds: float,
    cancel_signal,
    deadline: Deadline | None,
    lease_fd: int | None = None,
) -> ExternalCallOutcome:
    """Run one external call under a bounded child; kill on cancel/deadline."""
    if cancel_signal is not None and cancel_signal.is_set():
        raise ExternalCallCancelled("external MCP call cancelled")
    request_end, run_deadline = limited(deadline, min(MAX_CALL_SECONDS, budget_seconds))
    request_seconds = deadline_remaining(request_end)
    if request_seconds <= 0:
        raise ExternalCallDeadline("run time budget exceeded")
    request = _encoded_request(server, tool, arguments, request_seconds, headers)
    try:
        process = _start_external(request_seconds, lease_fd)
    except ExternalCallError:
        raise
    except (OSError, RuntimeError):
        return ExternalCallOutcome(False, category="client")
    state = _Communication()
    ready = threading.Event()
    communication = threading.Thread(
        target=_communicate,
        args=(process, request, state, ready),
        daemon=True,
        name="deepseek-external-mcp-pipe",
    )
    started = False
    try:
        try:
            communication.start()
            started = True
        except RuntimeError:
            return ExternalCallOutcome(False, category="client")
        if not _wait_for_external(ready, cancel_signal, request_end, run_deadline):
            return ExternalCallOutcome(False, category="timeout")
        communication.join(PROCESS_STOP_SECONDS)
        if state.error or process.returncode != 0:
            return ExternalCallOutcome(False, category="client")
        return _decode_outcome(state.output)
    finally:
        _cleanup_external_process(process, communication, started)
