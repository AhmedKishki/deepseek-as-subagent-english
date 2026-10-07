"""Bounded, workspace-confined tools used by the DeepSeek agent loop."""
from __future__ import annotations
from pathlib import Path
from typing import Callable
from .bash_tool import execute_bash
from .config import Config
from .file_io import (
    MAX_TEXT_FILE_BYTES,
    MISSING_FILE,
    FileIdentity,
    MutationCommittedWarning,
    ToolInputError,
    WorkspaceFileNotFound,
    atomic_write_workspace_text as _atomic_write_workspace_text,
    read_workspace_text as _read_workspace_text,
)
from .file_identity import bounded_integer
from .safe_regex import RegexPattern, SafeRegexError, compile_safe_regex
from .resource_budget import MutationBudget, ResourceBudgetExceeded, apply_mutation
from .safety import SandboxViolation
from .tool_evidence import (
    GrepStats,
    bind_tool_evidence,
    emit_error,
    emit_glob,
    emit_grep,
    emit_read,
    emit_mutation,
)
from .tool_notebook import (
    MAX_WRITE_BYTES,
    _committed_result,
    _execute_notebook_edit,
    _utf8_size,
)
from .tool_schemas import build_tool_schemas
from .walk_support import WorkspaceEntryTooLarge
from .workspace_walk import WalkEntry, WorkspaceWalk
MAX_TOOL_OUTPUT = 50_000  # Maximum characters returned from one tool call.
MAX_GLOB_RESULTS = 500
MAX_GREP_FILES = 10_000
MAX_GREP_LINE_CHARS = 2_000
def _truncate(text: str) -> str:
    if len(text) > MAX_TOOL_OUTPUT:
        return (
            text[:MAX_TOOL_OUTPUT]
            + f"\n... [truncated, total {len(text)} chars, showing first {MAX_TOOL_OUTPUT}]"
        )
    return text

def _slice_lines(text: str, args: dict) -> str:
    if "offset" not in args and "limit" not in args:
        return text
    offset = bounded_integer(args.get("offset", 0), "offset", minimum=0)
    raw_limit = args.get("limit")
    limit = None if raw_limit is None else bounded_integer(
        raw_limit, "limit", minimum=0
    )
    lines = text.splitlines()
    end = offset + limit if limit is not None else len(lines)
    return "\n".join(lines[offset:end])

def _execute_read(args: dict, workspace: Path) -> str:
    """Read a file. args: {path: str, offset?: int, limit?: int}"""
    path = args.get("path", "")
    if not isinstance(path, str) or not path:
        emit_error("Read", "invalid_input")
        return "ERROR: missing required 'path' argument"
    try:
        raw, identity = _read_workspace_text(
            workspace, path, reject_binary=True
        )
        text = _slice_lines(raw, args)
    except WorkspaceFileNotFound:
        emit_error("Read", "not_found")
        return f"ERROR: file not found: {path}"
    except (OSError, SandboxViolation, ToolInputError) as e:
        emit_error("Read", "read_failed")
        return f"ERROR: failed to read {path}: {e}"
    emit_read(workspace, path, identity, raw, text[:MAX_TOOL_OUTPUT], args,
              len(text) > MAX_TOOL_OUTPUT)
    return _truncate(text)

def _execute_write(
    args: dict, workspace: Path, mutation_budget: MutationBudget | None = None
) -> str:
    """Write a file (overwrite). args: {path: str, content: str}"""
    path = args.get("path", "")
    content = args.get("content", "")
    if not isinstance(path, str) or not path:
        emit_error("Write", "invalid_input")
        return "ERROR: missing required 'path' argument"
    if not isinstance(content, str):
        emit_error("Write", "invalid_input")
        return "ERROR: 'content' must be a string"
    if _utf8_size(content) > MAX_WRITE_BYTES:
        emit_error("Write", "invalid_input")
        return f"ERROR: content exceeds {MAX_WRITE_BYTES} bytes; split into smaller writes."
    try:
        apply_mutation(
            mutation_budget, _utf8_size(content),
            lambda: _atomic_write_workspace_text(
                workspace, path, content, expected=MISSING_FILE
            ),
        )
    except ResourceBudgetExceeded:
        raise
    except MutationCommittedWarning as exc:
        emit_mutation("Write", incomplete=True)
        return _committed_result(f"OK: wrote {len(content)} chars to {path}", exc)
    except ToolInputError as e:
        emit_error("Write", "invalid_input")
        if "appeared during edit" in str(e):
            return f"ERROR: file already exists: {path}; use Edit for existing files"
        return f"ERROR: failed to write {path}: {e}"
    except Exception as e:
        emit_error("Write", "internal")
        return f"ERROR: failed to write {path}: {e}"
    emit_mutation("Write")
    return f"OK: wrote {len(content)} chars to {path}"

def _parse_edit_request(args: dict) -> tuple[str, str, str, bool]:
    path = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = args.get("replace_all", False)
    if not isinstance(path, str) or not path:
        raise ToolInputError("missing required 'path' argument")
    if not isinstance(old, str) or old == "":
        raise ToolInputError("missing required 'path' or 'old_string'")
    if not isinstance(new, str):
        raise ToolInputError("'old_string' and 'new_string' must be strings")
    if not isinstance(replace_all, bool):
        raise ToolInputError("'replace_all' must be a boolean")
    return path, old, new, replace_all

def _read_edit_target(path: str, workspace: Path) -> tuple[str, FileIdentity]:
    try:
        return _read_workspace_text(
            workspace, path, strict_utf8=True, reject_binary=True
        )
    except WorkspaceFileNotFound:
        raise ToolInputError(f"file not found: {path}") from None
    except (OSError, ToolInputError) as exc:
        raise ToolInputError(f"failed to read {path}: {exc}") from exc

def _build_replacement(
    text: str, old: str, new: str, replace_all: bool, path: str
) -> tuple[str, int]:
    count = text.count(old)
    if count == 0:
        raise ToolInputError(f"old_string not found in {path}")
    if count > 1 and not replace_all:
        raise ToolInputError(
            f"old_string appears {count} times in {path}. "
            f"Use replace_all=true or provide more context to make it unique."
        )
    replacements = count if replace_all else 1
    output_size = _utf8_size(text) + replacements * (
        _utf8_size(new) - _utf8_size(old)
    )
    if output_size > MAX_WRITE_BYTES:
        raise ToolInputError(f"edited content exceeds {MAX_WRITE_BYTES} bytes")
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return new_text, replacements

def _execute_edit(
    args: dict, workspace: Path, mutation_budget: MutationBudget | None = None
) -> str:
    """Exact string replacement. args: {path, old_string, new_string, replace_all?}"""
    try:
        path, old, new, replace_all = _parse_edit_request(args)
        text, identity = _read_edit_target(path, workspace)
        new_text, replacements = _build_replacement(
            text, old, new, replace_all, path
        )
    except (SandboxViolation, ToolInputError) as exc:
        emit_error("Edit", "invalid_input")
        return f"ERROR: {exc}"
    try:
        apply_mutation(
            mutation_budget, _utf8_size(new_text),
            lambda: _atomic_write_workspace_text(
                workspace, path, new_text, expected=identity
            ),
        )
    except ResourceBudgetExceeded:
        raise
    except MutationCommittedWarning as exc:
        emit_mutation("Edit", incomplete=True)
        return _committed_result(
            f"OK: replaced {replacements} occurrence(s) in {path}", exc
        )
    except Exception as e:
        emit_error("Edit", "internal")
        return f"ERROR: failed to write {path}: {e}"
    emit_mutation("Edit")
    return f"OK: replaced {replacements} occurrence(s) in {path}"

def _parse_glob_request(args: dict, workspace: Path) -> WorkspaceWalk:
    pattern = args.get("pattern", "")
    if not isinstance(pattern, str) or not pattern:
        raise ToolInputError("missing required 'pattern' argument")
    base = args.get("path", "")
    if not isinstance(base, str):
        raise ToolInputError("'path' must be a string")
    return WorkspaceWalk(base, workspace, pattern)

def _scan_glob_matches(walk: WorkspaceWalk) -> tuple[list[Path], bool]:
    matches: list[Path] = []
    for entry in walk:
        if len(matches) >= MAX_GLOB_RESULTS:
            return matches, True
        matches.append(entry.path)
    return matches, False

def _format_glob_result(
    matches: list[Path], result_limit: bool, traversal_limit: bool, root: Path
) -> str:
    matches.sort()
    rel_matches = [path.relative_to(root).as_posix() for path in matches]
    truncated = result_limit or traversal_limit
    prefix = "Found at least" if truncated else "Found"
    summary = f"{prefix} {len(matches)} match(es)"
    if result_limit:
        summary += f" (limit {MAX_GLOB_RESULTS} reached)"
    elif traversal_limit:
        summary += " (configured traversal limit reached)"
    return summary + ":\n" + "\n".join(rel_matches)


def _execute_glob(args: dict, workspace: Path) -> str:
    """Match file names by pattern. args: {pattern: str, path?: str}"""
    try:
        with _parse_glob_request(args, workspace) as walk:
            matches, result_limit = _scan_glob_matches(walk)
            traversal_limit = walk.truncated
            scope = walk.base
    except (SandboxViolation, ToolInputError) as exc:
        emit_error("Glob", "invalid_input")
        return f"ERROR: {exc}"
    body = _format_glob_result(
        matches, result_limit, traversal_limit, workspace.resolve()
    )
    emit_glob(
        workspace, scope, args["pattern"], len(matches), result_limit,
        traversal_limit, len(body) > MAX_TOOL_OUTPUT,
    )
    return _truncate(body)


def _grep_file(
    entry: WalkEntry, regex: RegexPattern, limit: int
) -> tuple[list[str], str, int]:
    data = entry.read_bytes(MAX_TEXT_FILE_BYTES)
    if b"\x00" in data[:8192]:
        return [], "binary", 0
    lines = data.decode("utf-8", errors="replace").splitlines()
    matches: list[str] = []
    clipped = 0
    for line_number, line in enumerate(lines, 1):
        if not regex.search(line):
            continue
        displayed = line[:MAX_GREP_LINE_CHARS]
        if len(line) > MAX_GREP_LINE_CHARS:
            displayed += "... [line truncated]"
            clipped += 1
        matches.append(f"{entry.relative_to_workspace}:{line_number}: {displayed}")
        if len(matches) >= limit:
            break
    return matches, "text", clipped

def _parse_grep_request(
    args: dict, workspace: Path
) -> tuple[WorkspaceWalk, RegexPattern, int]:
    pattern = args.get("pattern", "")
    file_glob = args.get("glob", "**/*")
    if not isinstance(pattern, str) or not pattern:
        raise ToolInputError("missing required 'pattern' argument")
    if not isinstance(file_glob, str) or not file_glob:
        raise ToolInputError("'glob' must be a non-empty string")
    base = args.get("path", "")
    if not isinstance(base, str):
        raise ToolInputError("'path' must be a string")
    limit = bounded_integer(
        args.get("max_matches", 100), "max_matches", minimum=1, maximum=1000
    )
    walk = WorkspaceWalk(base, workspace, file_glob, open_files=True)
    return walk, compile_safe_regex(pattern), limit


def _scan_grep(
    walk: WorkspaceWalk, regex: RegexPattern, limit: int
) -> tuple[list[str], GrepStats]:
    results: list[str] = []
    stats = GrepStats()
    for entry in walk:
        if not entry.is_file:
            continue
        stats.files_scanned += 1
        if stats.files_scanned > MAX_GREP_FILES:
            stats.file_limit_reached = True
            break
        try:
            found, kind, line_clipped = _grep_file(
                entry, regex, limit - len(results)
            )
        except WorkspaceEntryTooLarge:
            stats.oversize_skipped += 1
            continue
        if kind == "binary":
            stats.binary_skipped += 1
            continue
        results.extend(found)
        stats.lines_clipped += line_clipped
        if len(results) >= limit:
            stats.match_limit_reached = True
            break
    return results, stats


def _format_grep_result(results: list[str], pattern: str, truncated: bool) -> str:
    if not results:
        if truncated:
            return f"Search results incomplete for pattern: {pattern}"
        return f"No matches found for pattern: {pattern}"
    header = f"Found {len(results)} match(es)"
    if truncated:
        header += " (results incomplete)"
    return header + ":\n" + "\n".join(results)
def _execute_grep(args: dict, workspace: Path) -> str:
    """Regex search of file contents. args: {pattern, path?, glob?, max_matches?}"""
    try:
        walk, regex, limit = _parse_grep_request(args, workspace)
        with walk:
            results, stats = _scan_grep(walk, regex, limit)
            stats.traversal_limited = walk.truncated
            scope = walk.base
    except (SandboxViolation, ToolInputError, SafeRegexError) as exc:
        emit_error("Grep", "invalid_input")
        return f"ERROR: {exc}"
    stats.matches = len(results)
    truncated = (
        stats.match_limit_reached or stats.file_limit_reached
        or stats.traversal_limited or stats.oversize_skipped > 0
        or stats.binary_skipped > 0
    )
    body = _format_grep_result(results, regex.pattern, truncated)
    emit_grep(
        workspace, scope, regex.pattern, str(args.get("glob", "**/*")),
        stats, len(body) > MAX_TOOL_OUTPUT,
    )
    return _truncate(body)
TOOL_REGISTRY = {
    "Read": _execute_read,
    "Write": _execute_write,
    "Edit": _execute_edit,
    "Glob": _execute_glob,
    "Grep": _execute_grep,
    "NotebookEdit": _execute_notebook_edit,
}


def execute_tool(
    name: str,
    args: dict,
    config: Config,
    *,
    execution_lease_fd: int | None = None,
    mutation_budget: MutationBudget | None = None,
    max_bash_timeout: int | None = None,
    evidence_reporter: Callable[[dict], None] | None = None,
) -> str:
    """Dispatch entrypoint: call the implementation for a tool name."""
    if evidence_reporter is None:
        return _dispatch_tool(
            name, args, config, execution_lease_fd, mutation_budget, max_bash_timeout
        )
    with bind_tool_evidence(evidence_reporter):
        return _dispatch_tool(
            name, args, config, execution_lease_fd, mutation_budget, max_bash_timeout
        )


def _dispatch_tool(
    name: str,
    args: dict,
    config: Config,
    execution_lease_fd: int | None,
    mutation_budget: MutationBudget | None,
    max_bash_timeout: int | None,
) -> str:
    if not isinstance(name, str):
        return "ERROR: tool name must be a string"
    if not isinstance(args, dict):
        return "ERROR: tool arguments must be an object"
    if name not in config.allowed_tools:
        emit_error(name, "denied")
        return f"ERROR: tool '{name}' is not allowed by configuration"
    if name == "Bash":
        kwargs = {"lease_fd": execution_lease_fd}
        if max_bash_timeout is not None:
            kwargs["max_timeout"] = max_bash_timeout
        return execute_bash(args, config, **kwargs)
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        available = [*TOOL_REGISTRY.keys(), "Bash"]
        return f"ERROR: unknown tool '{name}'. Available: {available}"
    if name in {"Write", "Edit", "NotebookEdit"}:
        return fn(args, config.workspace, mutation_budget)
    return fn(args, config.workspace)
