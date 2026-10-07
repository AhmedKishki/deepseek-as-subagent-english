"""Authoritative schema constants and strict validators for tool evidence.

The runtime reporter/emitters live in ``tool_evidence``; this module owns the
single definition of the schema, its limits, allowed tool/subobject fields,
and the whitelisting validators plus bounded encode/decode helpers.
"""
from __future__ import annotations

import json

SCHEMA_ID = "deepseek.tool_evidence/1"
MAX_EVIDENCE_BYTES = 8_192
MAX_EVIDENCE_RECORDS = 1
MAX_COUNT = 2**53
MAX_TEXT_FIELD = 4_096
_DIGEST_CHARS = 64
_HEX_DIGITS = frozenset("0123456789abcdef")
_MUTATION_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
_TOOLS = frozenset({"Read", "Glob", "Grep", "Bash"}) | _MUTATION_TOOLS
_STATUSES = frozenset({"ok", "error"})
_SUBOBJECT = {"Read": "source", "Glob": "search", "Grep": "search", "Bash": "bash"}
_SUBOBJECT_KEYS = ("source", "search", "bash")
_BASE_KEYS = frozenset({"schema", "tool", "status", "truncated", "incomplete"})
_SOURCE_KEYS = frozenset({
    "path", "sha256", "total_lines", "returned_lines",
    "first_line", "last_line", "output_clipped",
})
_GLOB_KEYS = frozenset({
    "kind", "scope", "pattern_sha256", "results",
    "result_limit_reached", "traversal_limited", "exhaustive",
})
_GREP_KEYS = _GLOB_KEYS | frozenset({
    "glob_sha256", "matches", "files_scanned", "binary_skipped",
    "oversize_skipped", "lines_clipped", "match_limit_reached",
    "file_limit_reached",
})
_BASH_KEYS = frozenset({
    "command_sha256", "exit_code", "timed_out", "stdout_bytes",
    "stderr_bytes", "stdout_clipped", "stderr_clipped",
    "stdout_sha256", "stderr_sha256",
})
_CATEGORIES = frozenset({
    "invalid_input", "not_found", "read_failed", "denied",
    "timeout", "host_unavailable", "internal", "command_failed",
})


def _digest(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != _DIGEST_CHARS:
        return None
    return value if all(char in _HEX_DIGITS for char in value) else None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _bounded_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_COUNT else None


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT_FIELD:
        return None
    return value


def _valid_source(source: object) -> dict | None:
    if not isinstance(source, dict) or not set(source) <= _SOURCE_KEYS:
        return None
    path = _text(source.get("path"))
    total = _bounded_count(source.get("total_lines"))
    returned = _bounded_count(source.get("returned_lines"))
    first = _bounded_count(source.get("first_line"))
    last = _bounded_count(source.get("last_line"))
    clipped = _bool(source.get("output_clipped"))
    if None in (path, total, returned, first, last, clipped):
        return None
    if first > total + 1 or last > total:
        return None
    if returned != (last - first + 1 if first else 0):
        return None
    valid = {
        "path": path, "total_lines": total, "returned_lines": returned,
        "first_line": first, "last_line": last, "output_clipped": clipped,
    }
    if source.get("sha256") is not None:
        digest = _digest(source.get("sha256"))
        if digest is None:
            return None
        valid["sha256"] = digest
    return valid


def _valid_search(search: object) -> dict | None:
    if not isinstance(search, dict):
        return None
    kind = search.get("kind")
    allowed = {"glob": _GLOB_KEYS, "grep": _GREP_KEYS}.get(kind)
    if allowed is None or not set(search) <= allowed:
        return None
    scope = _text(search.get("scope"))
    pattern = _digest(search.get("pattern_sha256"))
    exhaustive = _bool(search.get("exhaustive"))
    if None in (scope, pattern, exhaustive):
        return None
    common = {"kind": kind, "scope": scope,
              "pattern_sha256": pattern, "exhaustive": exhaustive}
    if kind == "glob":
        return _valid_glob(search, common)
    return _valid_grep(search, common)


def _valid_glob(search: dict, common: dict) -> dict | None:
    results = _bounded_count(search.get("results"))
    result_limited = _bool(search.get("result_limit_reached"))
    traversal = _bool(search.get("traversal_limited"))
    if None in (results, result_limited, traversal):
        return None
    valid = dict(common)
    valid.update(results=results, result_limit_reached=result_limited,
                 traversal_limited=traversal)
    return valid


def _valid_grep(search: dict, common: dict) -> dict | None:
    valid = dict(common)
    for name in ("matches", "files_scanned", "binary_skipped",
                 "oversize_skipped", "lines_clipped"):
        count = _bounded_count(search.get(name))
        if count is None:
            return None
        valid[name] = count
    for name in ("match_limit_reached", "file_limit_reached", "traversal_limited"):
        flag = _bool(search.get(name))
        if flag is None:
            return None
        valid[name] = flag
    digest = _digest(search.get("glob_sha256"))
    if digest is None:
        return None
    valid["glob_sha256"] = digest
    return valid


def _valid_bash(bash: object) -> dict | None:
    if not isinstance(bash, dict) or not set(bash) <= _BASH_KEYS:
        return None
    command = _digest(bash.get("command_sha256"))
    exit_code = bash.get("exit_code")
    if command is None or isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return None
    if not -(2**31) <= exit_code <= 2**31 - 1:
        return None
    valid = {"command_sha256": command, "exit_code": exit_code}
    for name in ("timed_out", "stdout_clipped", "stderr_clipped"):
        flag = _bool(bash.get(name))
        if flag is None:
            return None
        valid[name] = flag
    for name in ("stdout_bytes", "stderr_bytes"):
        count = _bounded_count(bash.get(name))
        if count is None:
            return None
        valid[name] = count
    for name in ("stdout_sha256", "stderr_sha256"):
        digest = _digest(bash.get(name))
        if digest is None:
            return None
        valid[name] = digest
    return valid


def _valid_header(value: dict, expected_tool: str) -> bool:
    if not set(value) <= _BASE_KEYS | set(_SUBOBJECT_KEYS) | {"error"}:
        return False
    if value.get("schema") != SCHEMA_ID:
        return False
    if value.get("tool") != expected_tool or value.get("tool") not in _TOOLS:
        return False
    if value.get("status") not in _STATUSES:
        return False
    return isinstance(value.get("truncated"), bool) and isinstance(
        value.get("incomplete"), bool
    )


def _base(value: dict) -> dict:
    return {
        "schema": SCHEMA_ID, "tool": value["tool"], "status": value["status"],
        "truncated": value["truncated"], "incomplete": value["incomplete"],
    }


def _validate_error(value: dict) -> dict | None:
    error = value.get("error")
    if (not isinstance(error, dict) or set(error) != {"category"}
            or error.get("category") not in _CATEGORIES):
        return None
    valid = _base(value)
    valid["error"] = {"category": error["category"]}
    if any(key in value for key in _SUBOBJECT_KEYS):
        if value["tool"] != "Bash" or "source" in value or "search" in value:
            return None
        bash = _valid_bash(value.get("bash"))
        if bash is None:
            return None
        valid["bash"] = bash
    return valid


def _validate_ok(value: dict, tool: str) -> dict | None:
    if tool in _MUTATION_TOOLS:
        return _base(value) if set(value) == _BASE_KEYS else None
    key = _SUBOBJECT[tool]
    if not set(value) <= _BASE_KEYS | {key}:
        return None
    present = [name for name in _SUBOBJECT_KEYS if name in value]
    if present != [key]:
        return None
    if key == "source":
        sub = _valid_source(value[key])
    elif key == "search":
        sub = _valid_search(value[key])
    else:
        sub = _valid_bash(value[key])
    if sub is None:
        return None
    valid = _base(value)
    valid[key] = sub
    return valid


def validate_observation(value: object, expected_tool: str) -> dict | None:
    """Return a strictly whitelisted observation, or None when malformed."""
    if not isinstance(value, dict) or not _valid_header(value, expected_tool):
        return None
    if value["status"] == "error":
        return _validate_error(value)
    return _validate_ok(value, value["tool"])


def decode_evidence(payload: object, expected_tool: str) -> list[dict]:
    """Validate a child evidence payload; reject all of it when malformed."""
    if payload is None:
        return []
    if (not isinstance(payload, list) or len(payload) > MAX_EVIDENCE_RECORDS
            or _encoded_size(payload) > MAX_EVIDENCE_BYTES):
        return []
    accepted: list[dict] = []
    for item in payload:
        observation = validate_observation(item, expected_tool)
        if observation is None:
            return []
        accepted.append(observation)
    return accepted


def encode_evidence(observations: list[dict]) -> list[dict]:
    """Normalize and bound observations so the response envelope stays capped."""
    accepted: list[dict] = []
    for observation in observations[:MAX_EVIDENCE_RECORDS]:
        tool = observation.get("tool") if isinstance(observation, dict) else None
        normalized = validate_observation(observation, tool)
        if normalized is None:
            continue
        if _encoded_size(accepted + [normalized]) > MAX_EVIDENCE_BYTES:
            break
        accepted.append(normalized)
    return accepted


def _encoded_size(value: object) -> int:
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    return len(encoded.encode("utf-8"))
