"""Orchestrator-only temporary git worktree lifecycle (host CLI, not an MCP tool).

It assigns a private ``<repo-top>/.deepseek-as-subagent/run-<12hex>`` checkout and
keeps host-only metadata (manifest plus a private empty hooks dir) outside it at
``<repo-top>/.deepseek-as-subagent/.state/run-<12hex>``.  It serves an MCP process
fixed to the checkout, inspects real state, merges the host-committed branch into
a target with ``--no-ff`` (hooks/signing disabled, conflicts retained), and
removes only clean, inactive, integrated worktrees.  Lifecycle mutations hold the
repository common-dir lease plus execution leases on the serving metadata root
and on both checkouts, so a live MCP execution cannot edit a worktree mid-merge
or mid-cleanup.  Git and manifest plumbing lives in
:mod:`deepseek_mcp.worktree_support`.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from .worktree_support import (
    BRANCH_PREFIX,
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_OK,
    HOOKS,
    INSTANCE_PREFIX,
    LOCK_ENV,
    MANIFEST_ENV,
    MANIFEST_NAME,
    MODE_ENV,
    WorktreeBusy,
    WorktreeError,
    WorktreeStateError,
    WorktreeValidationError,
    _ancestor,
    _check,
    _common,
    _controller_parent,
    _document,
    _git,
    _gitdir,
    _HEX,
    _identity,
    _in_progress,
    _load,
    _lock_dir,
    _porcelain,
    _reject_in_progress,
    _rev,
    _state_parent,
    _top,
    _validate,
    _validate_repo,
    _validate_root,
    _validate_workspace,
    _write,
    attached_branch,
    common_lock,
    hold_lease,
    require_clean,
)


def _new_branch(common: Path) -> str:
    for _ in range(32):
        name = BRANCH_PREFIX + os.urandom(6).hex()
        if _git(common, ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], check=False).returncode:
            return name
    raise WorktreeError("cannot allocate a unique worktree branch")


def _prepare_parents(top: Path) -> tuple[Path, Path]:
    """Create and validate the controller and state parents without trusting them."""
    controller, state = _controller_parent(top), _state_parent(top)
    if os.path.lexists(controller) and (controller.is_symlink() or not controller.is_dir()):
        raise WorktreeValidationError("controller parent is not a real directory")
    created = not os.path.lexists(controller)
    controller.mkdir(mode=0o700, exist_ok=True)
    _check(controller, "controller parent", directory=True, private=True)
    relative = os.path.relpath(controller, top)
    ignored = _git(top, ["check-ignore", "-q", "--", relative], check=False)
    if ignored.returncode != 0:
        if created:
            try:
                os.rmdir(controller)
            except OSError:
                pass
        raise WorktreeStateError(
            f"{relative} must be git-ignored before nested worktrees; add it to .gitignore"
        )
    if os.path.lexists(state) and (state.is_symlink() or not state.is_dir()):
        raise WorktreeValidationError("state parent is not a real directory")
    state.mkdir(mode=0o700, exist_ok=True)
    _check(state, "state parent", directory=True, private=True)
    return controller, state


def _new_pair(controller: Path, state: Path) -> tuple[str, Path, Path]:
    """Reserve matching metadata and checkout roots exclusively before any git add."""
    for _ in range(32):
        name = INSTANCE_PREFIX + secrets.token_hex(6)
        metadata, checkout = state / name, controller / name
        if os.path.lexists(metadata) or os.path.lexists(checkout):
            continue
        try:
            metadata.mkdir(mode=0o700)
        except FileExistsError:
            continue
        try:
            checkout.mkdir(mode=0o700)
        except FileExistsError:
            try:
                os.rmdir(metadata)
            except OSError:
                pass
            continue
        return name, metadata, checkout
    raise WorktreeError("cannot allocate unique worktree instance roots")


def _rollback_create(repo: Path, metadata: Path, checkout: Path, branch: str | None, hooks: Path) -> None:
    _git(repo, ["worktree", "remove", os.fspath(checkout)], mutating=True, hooks=hooks, check=False)
    if branch:
        _git(repo, ["branch", "-d", branch], mutating=True, hooks=hooks, check=False)
    shutil.rmtree(metadata, ignore_errors=True)
    try:
        os.rmdir(checkout)  # only if it is empty and ours
    except OSError:
        pass


def create(repo: Path, base: str | None = None, *, lock_directory: Path | None = None) -> dict:
    lock_directory = lock_directory if lock_directory is not None else _lock_dir()
    repo = Path(os.path.abspath(os.fspath(repo)))
    if not repo.is_dir():
        raise WorktreeError(f"repository path is not a directory: {repo}")
    common, top = _common(repo), _top(repo)
    base_sha = _rev(repo, base or "HEAD")
    with common_lock(common, lock_directory):
        controller, state = _prepare_parents(top)
        _name, metadata, checkout = _new_pair(controller, state)
        hooks = metadata / HOOKS
        hooks.mkdir(mode=0o700)
        branch: str | None = None
        try:
            branch = _new_branch(common)
            _git(repo, ["worktree", "add", "-b", branch, os.fspath(checkout), base_sha],
                 mutating=True, hooks=hooks)
            _write(metadata / MANIFEST_NAME, _document(metadata, checkout, branch, base_sha, common, top))
        except BaseException:
            _rollback_create(repo, metadata, checkout, branch, hooks)
            raise
    return {"ok": True, "status": "created", "manifest": os.fspath(metadata / MANIFEST_NAME),
            "root": os.fspath(metadata), "metadata": os.fspath(metadata),
            "checkout": os.fspath(checkout), "branch": branch,
            "base": base_sha, "repo_top": os.fspath(top), "repo_common_dir": os.path.realpath(common)}


def _status_entries(text: str) -> tuple[list[dict], list[str]]:
    entries, untracked = [], []
    for line in text.splitlines():
        if not line:
            continue
        code, path = line[:2], line[3:] if len(line) > 3 else ""
        entries.append({"code": code, "path": path})
        if code == "??":
            untracked.append(path)
    return entries, untracked


def inspect(path: Path) -> dict:
    _root, checkout, document = _validate(path)
    head = _rev(checkout, "HEAD")
    entries, untracked = _status_entries(_porcelain(checkout))
    counts = _git(_common(checkout), ["rev-list", "--left-right", "--count",
                                      f"{document['base']}...{head}"], check=False).stdout.split()
    ahead = int(counts[1]) if len(counts) == 2 and counts[1].isdigit() else None
    return {"ok": True, "status": "inspected", "branch": document["branch"],
            "base": document["base"], "head": head, "clean": not entries,
            "in_progress": _in_progress(checkout), "files": entries, "untracked": untracked,
            "commits_ahead_of_base": ahead, "integration": document["integration"],
            "diff_stat": _git(checkout, ["diff", "--stat", "--no-color", "HEAD", "--", "."],
                              check=False).stdout.strip(),
            "committed_diff_stat": _git(checkout, ["diff", "--stat", "--no-color",
                                                   document["base"], "HEAD", "--", "."],
                                        check=False).stdout.strip()}


def serve(path: Path, *, lock_directory: Path | None = None, python: str | None = None) -> int:
    lock_directory = lock_directory if lock_directory is not None else _lock_dir()
    manifest, document = _load(path)
    root = manifest.parent
    _validate_root(root, document)
    with hold_lease(root, "serving worktree", lock_directory):
        manifest, document = _load(path)
        _validate_workspace(document)
        _validate_root(root, document)
        checkout = _validate_repo(root, document)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("PYTHON", "GIT_"))}
        environment.update({MODE_ENV: "1", MANIFEST_ENV: os.fspath(manifest),
                            "DEEPSEEK_WORKSPACE": os.fspath(checkout)})
        return subprocess.run(
            [python or sys.executable, "-I", "-m", "deepseek_mcp.server"], cwd=os.fspath(checkout),
            env=environment, shell=False, check=False,
        ).returncode


def _verify_integration(common: Path, checkout: Path, document: dict) -> None:
    """Removal may only proceed when the recorded merge verifiably holds source."""
    head = _rev(checkout, "HEAD")
    if head == document["base"]:
        return
    integration = document.get("integration") or {}
    if not integration:
        raise WorktreeStateError("branch has unintegrated commits; integrate or discard manually")
    if integration.get("source_head") != head:
        raise WorktreeStateError("branch advanced after integration; integrate again first")
    merge_head = integration.get("merge_head")
    if not isinstance(merge_head, str) or not _HEX.fullmatch(merge_head):
        raise WorktreeStateError("recorded integration merge is invalid")
    if not _git(common, ["cat-file", "-e", f"{merge_head}^{{commit}}"], check=False).returncode == 0:
        raise WorktreeStateError("recorded integration merge no longer exists")
    if not _ancestor(common, head, merge_head):
        raise WorktreeStateError("recorded integration merge does not contain the source")
    target = Path(integration.get("target", ""))
    if not target.is_dir() or _common(target) != Path(os.path.realpath(common)):
        raise WorktreeStateError("integration target is no longer reachable")
    target_head = _rev(target, "HEAD")
    if not _ancestor(common, head, target_head) or not _ancestor(common, merge_head, target_head):
        raise WorktreeStateError("integration target no longer contains the source")


def _cleanup_precheck(checkout: Path, common: Path, document: dict) -> None:
    if os.path.lexists(_controller_parent(checkout)):
        raise WorktreeStateError("worktree contains nested subagent storage; clean nested instances first")
    require_clean(checkout, "worktree")
    _reject_in_progress(checkout, "worktree")
    _verify_integration(common, checkout, document)


def _preconditions(checkout, target, document, expected_head) -> tuple[str, str, str]:
    if not _HEX.fullmatch(expected_head):
        raise WorktreeError("expected-head is not a commit id")
    if os.path.realpath(checkout) == os.path.realpath(target):
        raise WorktreeStateError("target is the source worktree")
    if _common(target) != Path(os.path.realpath(document["repo"]["common_dir"])):
        raise WorktreeStateError("target does not share the source repository")
    if _rev(target, "HEAD") != expected_head:
        raise WorktreeStateError("target HEAD does not match expected-head")
    target_branch = attached_branch(target, "target")
    require_clean(target, "target")
    _reject_in_progress(target, "target")
    require_clean(checkout, "source worktree")
    _reject_in_progress(checkout, "source worktree")
    source_branch = attached_branch(checkout, "source worktree")
    if source_branch != document["branch"]:
        raise WorktreeStateError("source branch changed")
    common = Path(document["repo"]["common_dir"])
    source_head = _rev(checkout, "HEAD")
    if not _ancestor(common, document["base"], source_head):
        raise WorktreeStateError("source branch is not descended from its base")
    if not _ancestor(common, document["base"], expected_head):
        raise WorktreeStateError("source base is not an ancestor of target HEAD")
    return target_branch, source_branch, source_head


def integrate(path: Path, target: Path, expected_head: str, *,
              lock_directory: Path | None = None) -> dict:
    lock_directory = lock_directory if lock_directory is not None else _lock_dir()
    manifest, document = _load(path)
    root = manifest.parent
    _validate_root(root, document)
    target = Path(os.path.abspath(os.fspath(target)))
    if not target.is_dir():
        raise WorktreeError(f"target is not a directory: {target}")
    common = Path(document["repo"]["common_dir"])
    with ExitStack() as stack:
        stack.enter_context(common_lock(common, lock_directory))
        stack.enter_context(hold_lease(root, "serving worktree", lock_directory))
        stack.enter_context(hold_lease(Path(document["checkout"]["path"]),
                                       "source worktree", lock_directory))
        stack.enter_context(hold_lease(target, "target worktree", lock_directory))
        root, checkout, document = _validate(path)
        target_branch, source_branch, source_head = _preconditions(
            checkout, target, document, expected_head)
        done = _git(target, ["merge", "--no-ff", "--no-edit", document["branch"]],
                    mutating=True, hooks=root / HOOKS, check=False)
        if done.returncode == 0:
            merge_head = _rev(target, "HEAD")
            if not _ancestor(common, source_head, merge_head):
                raise WorktreeStateError("merge did not include the source commits")
            document["integration"] = {"target": os.path.realpath(target), "target_branch": target_branch,
                                       "source_head": source_head, "merge_head": merge_head,
                                       "at": datetime.now(timezone.utc).isoformat()}
            _write(manifest, document)
            return {"ok": True, "status": "merged", "target": os.path.realpath(target),
                    "target_branch": target_branch, "source_branch": source_branch,
                    "source_head": source_head, "merge_head": merge_head}
        if _in_progress(target):
            conflicted = _git(target, ["diff", "--name-only", "--diff-filter=U"],
                              check=False).stdout.split()
            return {"ok": False, "status": "conflict", "retained": True,
                    "target": os.path.realpath(target), "target_branch": target_branch,
                    "source_branch": source_branch, "conflicted": [c for c in conflicted if c.strip()],
                    "guidance": "resolve manually, commit, then re-run integrate; "
                                "no abort or reset was performed"}
        detail = (done.stderr or done.stdout or "").strip()
        raise WorktreeError(f"git merge failed ({done.returncode}): {detail}")


def cleanup(path: Path, *, lock_directory: Path | None = None) -> dict:
    lock_directory = lock_directory if lock_directory is not None else _lock_dir()
    manifest, document = _load(path)
    root = manifest.parent
    _validate_root(root, document)
    common = Path(document["repo"]["common_dir"])
    with ExitStack() as stack:
        stack.enter_context(common_lock(common, lock_directory))
        stack.enter_context(hold_lease(root, "serving worktree", lock_directory))
        stack.enter_context(hold_lease(Path(document["checkout"]["path"]),
                                       "worktree execution", lock_directory))
        root, checkout, document = _validate(path)
        _cleanup_precheck(checkout, common, document)
        done = _git(common, ["worktree", "remove", os.fspath(checkout)], mutating=True,
                    hooks=root / HOOKS, check=False)
        if done.returncode != 0:
            raise WorktreeStateError(
                f"git worktree remove refused: {(done.stderr or done.stdout or '').strip()}")
        deleted = _git(common, ["branch", "-d", document["branch"]], mutating=True,
                       hooks=root / HOOKS, check=False)
        if deleted.returncode != 0:
            return {"ok": False, "status": "branch_retained",
                    "error": f"git branch -d refused: {(deleted.stderr or deleted.stdout or '').strip()}",
                    "worktree_removed": True, "manifest": os.fspath(manifest)}
        if (root / HOOKS).is_dir():
            shutil.rmtree(root / HOOKS, ignore_errors=True)
        try:
            manifest.unlink()
        except OSError:
            pass
        os.rmdir(root)
        for shared in (root.parent, root.parent.parent):  # state, then controller
            try:
                os.rmdir(shared)
            except OSError:
                pass
    return {"ok": True, "status": "cleaned", "branch": document["branch"],
            "root": os.path.realpath(root)}


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deepseek-worktree",
                                     description="Orchestrator-only temporary git worktree lifecycle.")
    sub = parser.add_subparsers(dest="command", required=True)
    create_cmd = sub.add_parser("create")
    create_cmd.add_argument("--repo", required=True)
    create_cmd.add_argument("--base", default=None)
    for name in ("serve", "inspect", "cleanup"):
        sub.add_parser(name).add_argument("--manifest", required=True)
    integrate_cmd = sub.add_parser("integrate")
    integrate_cmd.add_argument("--manifest", required=True)
    integrate_cmd.add_argument("--target", required=True)
    integrate_cmd.add_argument("--expected-head", required=True)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.command == "create":
        _emit(create(Path(args.repo), args.base))
        return EXIT_OK
    if args.command == "serve":
        return serve(Path(args.manifest))
    if args.command == "inspect":
        _emit(inspect(Path(args.manifest)))
        return EXIT_OK
    if args.command == "integrate":
        result = integrate(Path(args.manifest), Path(args.target), args.expected_head)
        _emit(result)
        return EXIT_OK if result.get("ok") else EXIT_CONFLICT
    result = cleanup(Path(args.manifest))
    _emit(result)
    return EXIT_OK if result.get("ok") else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.  Requires no API configuration except for ``serve``."""
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except WorktreeError as error:
        _emit({"ok": False, "error": str(error)})
        print(str(error), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
