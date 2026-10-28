"""Server-owned managed-worker policy for orchestrator-managed worktrees.

The host CLI launches the server with a fixed checkout, branch, and manifest.
Instead of removing tools, the server keeps the trusted-host coding tool set
(including Bash) and injects the instruction block below into the agent system
prompt: stay in the fixed checkout and branch, never reach outside them, never
manage the shared Git lifecycle, and use Bash for assigned work in the checkout,
including tests. All Git access belongs to the host, including inspection.

This is a trust boundary plus explicit instructions, not an OS sandbox. The mode
is host-set through ``DEEPSEEK_WORKTREE_MODE``; an invalid value or a manifest
that does not match the fixed checkout fails closed before any instruction is
issued.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .worktree_support import MANIFEST_ENV, MODE_ENV

MANAGED_WORKER_INSTRUCTIONS = """Managed worktree policy (server-owned and fixed by the host):

- Your checkout is fixed at: {checkout}
- Your branch is fixed at: {branch}

Work only inside your checkout. Do not read or write local files outside it, and do
not run commands that target paths outside it. Stay on your branch: do not
switch, create, rename, or delete branches, and do not commit, merge, rebase,
cherry-pick, revert, tag, push, pull, fetch, or otherwise take part in the
shared Git lifecycle. The host owns commits and integration.

You may use Bash to implement your assigned task, edit files, build, and run
your own tests inside the checkout. Never use Git, including read-only commands
such as `git status`, `git diff`, or `git log`, or Git tools through MCP.
Approved external read-only MCP queries are limited to your assigned remote
resources; do not use them to bypass local checkout restrictions.
Ask the host for repository information when necessary. The host trusts you to respect your own
folder and branch; acting outside them is a policy violation even though Bash
is available to you."""


def _fixed_identity(manifest: Path, workspace: Path) -> tuple[str, str]:
    """Return the manifest's (checkout, branch) after a minimal consistency check."""
    try:
        document = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"cannot read managed worktree manifest: {error}") from error
    if not isinstance(document, dict):
        raise RuntimeError("managed worktree manifest is not an object")
    checkout = document.get("checkout")
    branch = document.get("branch")
    path = checkout.get("path") if isinstance(checkout, dict) else None
    if not isinstance(path, str) or not path:
        raise RuntimeError("managed worktree manifest has no checkout path")
    if not isinstance(branch, str) or not branch:
        raise RuntimeError("managed worktree manifest has no branch")
    if os.path.realpath(path) != os.path.realpath(workspace):
        raise RuntimeError("managed worktree checkout does not match the workspace")
    return path, branch


def managed_worker_instructions(workspace: Path) -> str | None:
    """Server-owned instructions for the fixed checkout, or ``None`` outside it."""
    mode = os.environ.get(MODE_ENV)
    if mode is None:
        return None
    if mode != "1":
        raise RuntimeError(f"{MODE_ENV} must be 1 when set")
    manifest = os.environ.get(MANIFEST_ENV)
    if not manifest:
        raise RuntimeError(f"managed worktree mode requires {MANIFEST_ENV}")
    checkout, branch = _fixed_identity(Path(manifest), Path(workspace))
    return MANAGED_WORKER_INSTRUCTIONS.format(checkout=checkout, branch=branch)
