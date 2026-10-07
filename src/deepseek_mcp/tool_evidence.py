"""Host-observed per-tool metadata: bounded, typed, validated, never inferred.

Evidence is produced inside the real tool implementations through a contextvar
reporter, carried only inside the validated tool-child response envelope, and
delivered to the parent through an explicit callback. Status, truncation, and
counts come from the host execution itself and are never derived from the
model-visible result text. Metadata is never written to logs or usage files.

Schema constants and strict validators live in ``tool_evidence_validation`` and
are re-exported here so existing imports keep working unchanged.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator

from .safety import resolve_safe_path
from .tool_evidence_validation import (
    MAX_COUNT,
    MAX_EVIDENCE_BYTES,
    MAX_EVIDENCE_RECORDS,
    MAX_TEXT_FIELD,
    SCHEMA_ID,
    _CATEGORIES,
    decode_evidence,
    encode_evidence,
    validate_observation,
)

EvidenceReporter = Callable[[dict], None]
_REPORTER: ContextVar[EvidenceReporter | None] = ContextVar(
    "deepseek_tool_evidence_reporter", default=None
)

__all__ = [
    "EvidenceReporter",
    "GrepStats",
    "MAX_COUNT",
    "MAX_EVIDENCE_BYTES",
    "MAX_EVIDENCE_RECORDS",
    "MAX_TEXT_FIELD",
    "SCHEMA_ID",
    "bind_tool_evidence",
    "decode_evidence",
    "deliver_evidence",
    "emit_bash",
    "emit_error",
    "emit_glob",
    "emit_grep",
    "emit_mutation",
    "emit_read",
    "encode_evidence",
    "report_evidence",
    "validate_observation",
]


@contextmanager
def bind_tool_evidence(reporter: EvidenceReporter | None) -> Iterator[None]:
    """Bind a reporter for the current execution context and restore it after."""
    token = _REPORTER.set(reporter)
    try:
        yield
    finally:
        _REPORTER.reset(token)


def report_evidence(observation: dict) -> None:
    """Hand one observation to the bound reporter; a no-op without one."""
    reporter = _REPORTER.get()
    if reporter is not None:
        reporter(observation)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _digest_hex(value: bytes) -> str:
    return hashlib.sha256(bytes(value)).hexdigest()


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), MAX_COUNT)


def _exit_code(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, -(2**31)), 2**31 - 1)


def _relative_posix(root, absolute) -> str:
    try:
        relative = absolute.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return "?"
    return relative.as_posix() if relative.parts else "."


def _workspace_relative(workspace, label: object) -> str:
    """Canonical workspace-relative target, or '?' when it cannot be resolved."""
    if not isinstance(label, str) or not label:
        return "?"
    try:
        root = workspace.resolve()
        absolute = resolve_safe_path(label, root)
    except Exception:
        return "?"
    return _relative_posix(root, absolute)


def _scope_relative(workspace, absolute) -> str:
    try:
        root = workspace.resolve()
    except (OSError, RuntimeError, ValueError):
        return "?"
    return _relative_posix(root, absolute)


def _observation(tool: str, status: str, *, truncated: bool, incomplete: bool,
                 subobject=None, error: str | None = None) -> None:
    observation = {
        "schema": SCHEMA_ID, "tool": tool, "status": status,
        "truncated": bool(truncated), "incomplete": bool(incomplete),
    }
    if subobject is not None:
        key, value = subobject
        observation[key] = value
    if error is not None:
        observation["error"] = {"category": error}
    report_evidence(observation)


def emit_error(tool: str, category: str) -> None:
    """Record a structured error from the host branch that produced it."""
    safe = category if category in _CATEGORIES else "internal"
    _observation(tool, "error", truncated=False, incomplete=True, error=safe)


def emit_mutation(tool: str, incomplete: bool = False) -> None:
    _observation(tool, "ok", truncated=False, incomplete=incomplete)


def _read_range(raw_text: str, sliced_text: str, args: dict) -> tuple[int, int, int]:
    returned = len(sliced_text.splitlines())
    offset = args.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        offset = 0
    if returned == 0:
        return 0, 0, 0
    return offset + 1, offset + returned, returned


def emit_read(workspace, label: object, identity, raw_text: str,
              sliced_text: str, args: dict, clipped: bool) -> None:
    """Record a successful Read from the real read identity and line selection."""
    first, last, returned = _read_range(raw_text, sliced_text, args)
    source = {
        "path": _workspace_relative(workspace, label),
        "total_lines": len(raw_text.splitlines()),
        "returned_lines": returned,
        "first_line": first,
        "last_line": last,
        "output_clipped": bool(clipped),
    }
    digest = getattr(identity, "digest", None)
    if isinstance(digest, (bytes, bytearray)) and digest:
        source["sha256"] = bytes(digest).hex()
    _observation("Read", "ok", truncated=bool(clipped), incomplete=bool(clipped),
                 subobject=("source", source))


def emit_glob(workspace, scope, pattern: str, results: int,
              result_limited: bool, traversal_limited: bool, clipped: bool) -> None:
    """Record a Glob search with digests, counts, and scope limitations."""
    exhaustive = not (result_limited or traversal_limited or clipped)
    search = {
        "kind": "glob",
        "scope": _scope_relative(workspace, scope),
        "pattern_sha256": _sha256_hex(pattern),
        "results": _count(results),
        "result_limit_reached": bool(result_limited),
        "traversal_limited": bool(traversal_limited),
        "exhaustive": exhaustive,
    }
    _observation("Glob", "ok", truncated=bool(clipped), incomplete=not exhaustive,
                 subobject=("search", search))


@dataclass
class GrepStats:
    """Host-observed counters accumulated while scanning for matches."""

    files_scanned: int = 0
    matches: int = 0
    binary_skipped: int = 0
    oversize_skipped: int = 0
    lines_clipped: int = 0
    match_limit_reached: bool = False
    file_limit_reached: bool = False
    traversal_limited: bool = False

    def incomplete(self) -> bool:
        return (
            self.match_limit_reached or self.file_limit_reached
            or self.traversal_limited or self.binary_skipped > 0
            or self.oversize_skipped > 0
        )


def emit_grep(workspace, scope, pattern: str, file_glob: str,
              stats: GrepStats, clipped: bool) -> None:
    """Record a Grep search; never claim exhaustive scope across skips."""
    search = {
        "kind": "grep",
        "scope": _scope_relative(workspace, scope),
        "pattern_sha256": _sha256_hex(pattern),
        "glob_sha256": _sha256_hex(file_glob),
        "matches": _count(stats.matches),
        "files_scanned": _count(stats.files_scanned),
        "binary_skipped": _count(stats.binary_skipped),
        "oversize_skipped": _count(stats.oversize_skipped),
        "lines_clipped": _count(stats.lines_clipped),
        "match_limit_reached": bool(stats.match_limit_reached),
        "file_limit_reached": bool(stats.file_limit_reached),
        "traversal_limited": bool(stats.traversal_limited),
        "exhaustive": not (stats.incomplete() or clipped or stats.lines_clipped > 0),
    }
    _observation("Grep", "ok", truncated=bool(clipped or stats.lines_clipped > 0),
                 incomplete=not search["exhaustive"], subobject=("search", search))


def emit_bash(command: str, result, output_clipped: bool) -> None:
    """Record a real Bash outcome; exit 0 makes no semantic claim."""
    stdout, stderr = result.stdout, result.stderr
    bash = {
        "command_sha256": _sha256_hex(command),
        "exit_code": _exit_code(result.returncode),
        "timed_out": bool(result.timed_out),
        "stdout_bytes": _count(result.stdout_total),
        "stderr_bytes": _count(result.stderr_total),
        "stdout_clipped": result.stdout_total > len(stdout),
        "stderr_clipped": result.stderr_total > len(stderr),
        "stdout_sha256": _digest_hex(stdout),
        "stderr_sha256": _digest_hex(stderr),
    }
    stream_clipped = bash["stdout_clipped"] or bash["stderr_clipped"]
    failed = result.timed_out or result.returncode != 0
    category = "timeout" if result.timed_out else "command_failed"
    _observation("Bash", "error" if failed else "ok",
                 truncated=bool(output_clipped or stream_clipped),
                 incomplete=bool(result.timed_out), subobject=("bash", bash),
                 error=category if failed else None)


def deliver_evidence(observations: list[dict], reporter: EvidenceReporter | None) -> None:
    if reporter is None:
        return
    try:
        for observation in observations:
            reporter(observation)
    except Exception:
        pass  # An observer must not bypass cleanup or mask a mutation failure.
