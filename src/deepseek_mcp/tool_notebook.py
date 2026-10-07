"""Notebook editing tool plus the bounded-write helpers it shares with tools.py.

Extracted from ``tools.py`` so that file stays within the enforced 500-line
source limit while it gains host-observed evidence instrumentation.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from .file_io import (
    MISSING_FILE,
    FileIdentity,
    MissingFile,
    MutationCommittedWarning,
    ToolInputError,
    WorkspaceFileNotFound,
    read_workspace_text as _read_workspace_text,
)
from .resource_budget import MutationBudget, ResourceBudgetExceeded, apply_mutation
from .safety import SandboxViolation
from .transaction_report import mutation_warning
from .tool_evidence import emit_error, emit_mutation

MAX_WRITE_BYTES = 5_000_000  # Maximum bytes per Write (5 MB; prevents run-away disk use).


def _committed_result(result: str, warning: Exception) -> str:
    mutation_warning(str(warning))
    return f"{result}; WARNING: update committed but post-commit checks failed ({warning}); DO NOT RETRY."


def _utf8_size(text: str) -> int:
    if len(text) > MAX_WRITE_BYTES:
        return MAX_WRITE_BYTES + 1
    return len(text.encode("utf-8", errors="replace"))


def _new_notebook() -> dict:
    return {
        "cells": [],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _load_notebook(
    workspace: Path, label: str, edit_mode: str
) -> tuple[dict, FileIdentity | MissingFile]:
    try:
        text, identity = _read_workspace_text(workspace, label, strict_utf8=True)
    except WorkspaceFileNotFound:
        if edit_mode == "insert":
            return _new_notebook(), MISSING_FILE
        raise ToolInputError(f"notebook not found: {label}")
    try:
        notebook = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolInputError(f"failed to parse notebook JSON: {exc}") from exc
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise ToolInputError("not a valid notebook (missing 'cells' array)")
    return notebook, identity


def _find_cell_index(cells: list, args: dict, edit_mode: str, label: str) -> int | None:
    cell_id = args.get("cell_id")
    if cell_id is not None:
        for index, cell in enumerate(cells):
            if isinstance(cell, dict) and cell.get("id") == cell_id:
                return index
        if edit_mode != "insert":
            raise ToolInputError(f"cell_id '{cell_id}' not found in {label}")
        return None
    raw_index = args.get("cell_index")
    if raw_index is not None:
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise ToolInputError("cell_index must be an integer")
        index = raw_index
        if 0 <= index < len(cells):
            return index
        if edit_mode != "insert":
            raise ToolInputError(
                f"cell_index {index} out of range (0..{len(cells) - 1})"
            )
        return None
    if edit_mode != "insert":
        raise ToolInputError("replace/delete require cell_id or cell_index")
    return None


def _split_source(source: str) -> list[str]:
    if not source:
        return [""]
    lines = source.splitlines(keepends=True)
    return lines if lines else [""]


def _replace_cell(cells: list, index: int | None, source: str) -> str:
    if index is None or not isinstance(cells[index], dict):
        raise ToolInputError("target notebook cell is invalid")
    cell = cells[index]
    cell["source"] = _split_source(source)
    if cell.get("cell_type") == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    return f"OK: replaced cell at index {index} (id={cell.get('id', 'n/a')})"


def _insert_cell(cells: list, index: int | None, source: str, cell_type: str) -> str:
    if cell_type not in ("code", "markdown"):
        raise ToolInputError(f"invalid cell_type '{cell_type}' (must be code or markdown)")
    cell: dict = {
        "cell_type": cell_type,
        "id": uuid.uuid4().hex[:8],
        "source": _split_source(source),
        "metadata": {},
    }
    if cell_type == "code":
        cell.update({"outputs": [], "execution_count": None})
    insert_at = index + 1 if index is not None else len(cells)
    cells.insert(insert_at, cell)
    return f"OK: inserted {cell_type} cell at index {insert_at} (id={cell['id']})"


def _apply_notebook_edit(cells: list, args: dict, mode: str, index: int | None) -> str:
    source = args.get("new_source", "")
    if mode in ("replace", "insert") and not isinstance(source, str):
        raise ToolInputError("'new_source' must be a string")
    if isinstance(source, str) and _utf8_size(source) > MAX_WRITE_BYTES:
        raise ToolInputError(f"new_source exceeds {MAX_WRITE_BYTES} bytes")
    if mode == "replace":
        return _replace_cell(cells, index, source)
    if mode == "insert":
        return _insert_cell(cells, index, source, args.get("cell_type", "code"))
    if index is None or not isinstance(cells[index], dict):
        raise ToolInputError("target notebook cell is invalid")
    removed = cells.pop(index)
    return f"OK: deleted cell at index {index} (was id={removed.get('id', 'n/a')})"


def _atomic_write(workspace, label, content, *, expected) -> None:
    # Resolve through tools.py at call time so tests can patch its attribute.
    from . import tools
    tools._atomic_write_workspace_text(workspace, label, content, expected=expected)


def _save_notebook(
    workspace: Path,
    label: str,
    notebook: dict,
    expected: FileIdentity | MissingFile,
    mutation_budget: MutationBudget | None = None,
) -> None:
    content = json.dumps(notebook, ensure_ascii=False, indent=1) + "\n"
    if _utf8_size(content) > MAX_WRITE_BYTES:
        raise ToolInputError(f"notebook exceeds {MAX_WRITE_BYTES} bytes after edit")
    apply_mutation(
        mutation_budget,
        _utf8_size(content),
        lambda: _atomic_write(workspace, label, content, expected=expected),
    )


def _execute_notebook_edit(
    args: dict, workspace: Path, mutation_budget: MutationBudget | None = None
) -> str:
    path = args.get("path", "")
    if not isinstance(path, str) or not path:
        emit_error("NotebookEdit", "invalid_input")
        return "ERROR: missing required 'path' argument"
    if not path.endswith(".ipynb"):
        emit_error("NotebookEdit", "invalid_input")
        return f"ERROR: not an .ipynb file: {path}"
    edit_mode = args.get("edit_mode", "replace")
    if edit_mode not in ("replace", "insert", "delete"):
        emit_error("NotebookEdit", "invalid_input")
        return f"ERROR: invalid edit_mode '{edit_mode}' (must be replace/insert/delete)"
    try:
        notebook, identity = _load_notebook(workspace, path, edit_mode)
        cells = notebook["cells"]
        index = _find_cell_index(cells, args, edit_mode, path)
        result = _apply_notebook_edit(cells, args, edit_mode, index)
        _save_notebook(workspace, path, notebook, identity, mutation_budget)
    except ResourceBudgetExceeded:
        raise
    except MutationCommittedWarning as exc:
        emit_mutation("NotebookEdit", incomplete=True)
        return _committed_result(result + f" (total cells: {len(cells)})", exc)
    except (SandboxViolation, ToolInputError, OSError) as exc:
        emit_error("NotebookEdit", "invalid_input")
        return f"ERROR: {exc}"
    emit_mutation("NotebookEdit")
    return result + f" (total cells: {len(cells)})"
