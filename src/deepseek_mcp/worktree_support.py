"""Low-level git, lease, manifest, and location plumbing for the worktree CLI.

Split out of :mod:`deepseek_mcp.worktrees` so each production module stays within
the repository's source limits.  Internal, host-side code: no MCP tool exposes
it, and every git call uses an argv list with a sanitised environment.

Layout and safety invariants enforced here (all under the ``--repo`` top):
* ``<top>/.deepseek-as-subagent/`` is the controller parent.
* ``<top>/.deepseek-as-subagent/run-<12hex>/`` is the assigned workspace checkout.
* ``<top>/.deepseek-as-subagent/.state/run-<12hex>/`` holds host-only metadata:
  the manifest and a private empty hooks dir, outside the checkout.
* Controller/state/checkout/metadata roots are private (0700) real directories;
  symlinks and manifest repointing are refused.  The metadata and checkout
  instance names must match and derive from the live workspace top.
* ``GIT_*`` overrides are stripped before every command.
* Every mutating git command validates a private, empty hooks directory first
  (so an injected hook cannot run) and disables commit/tag signing.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .execution_lock import (
    WorkspaceLockBusy,
    WorkspaceLockError,
    acquire_workspace_lease,
    workspace_identity,
)

MANIFEST_NAME = "manifest.json"
HOOKS = "hooks"
HIDDEN_NAME = ".deepseek-as-subagent"
STATE_NAME = ".state"
INSTANCE_PREFIX = "run-"
BRANCH_PREFIX = "deepseek-worktree-"
VERSION = 1
MAX_MANIFEST = 64 * 1024
MODE_ENV = "DEEPSEEK_WORKTREE_MODE"
MANIFEST_ENV = "DEEPSEEK_WORKTREE_MANIFEST"
LOCK_ENV = "DEEPSEEK_WORKTREE_LOCK_DIR"
EXIT_OK, EXIT_ERROR, EXIT_CONFLICT = 0, 1, 3
_HEX = re.compile(r"[0-9a-f]{40,64}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_INSTANCE = re.compile(re.escape(INSTANCE_PREFIX) + r"[0-9a-f]{12}")


class WorktreeError(RuntimeError):
    """Base class for worktree lifecycle failures."""


class WorktreeValidationError(WorktreeError):
    """A manifest or repository invariant did not hold."""


class WorktreeStateError(WorktreeError):
    """The worktree or target is in a state that forbids the operation."""


class WorktreeBusy(WorktreeError):
    """Another lifecycle operation or active execution owns the resource."""


def _lock_dir() -> Path | None:
    raw = os.environ.get(LOCK_ENV)
    return Path(raw) if raw else None


def _controller_parent(top) -> Path:
    return Path(top) / HIDDEN_NAME


def _state_parent(top) -> Path:
    return _controller_parent(top) / STATE_NAME


def _git_env() -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", GIT_OPTIONAL_LOCKS="0")
    if os.name != "nt":
        environment["LC_ALL"] = "C"
    return environment


def _stat(path: Path, label: str):
    try:
        return path.lstat()
    except OSError as error:
        raise WorktreeValidationError(f"cannot stat {label}: {error}") from error


def _validate_hooks(hooks: Path) -> None:
    """A mutating git command may only run when hooks stay private and empty."""
    info = _stat(hooks, "git hooks")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorktreeStateError("git hooks path is not a real directory")
    if os.name == "posix":
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise WorktreeStateError("git hooks path is not private")
    elif os.name == "nt":
        from . import windows_file_io

        try:
            windows_file_io.validate_private_path(hooks, directory=True)
        except windows_file_io.WindowsPathError as error:
            raise WorktreeStateError("git hooks path is not private") from error
    try:
        if any(hooks.iterdir()):
            raise WorktreeStateError("git hooks path is not empty")
    except OSError as error:
        raise WorktreeStateError(f"cannot read git hooks path: {error}") from error


def _git(cwd, args, *, mutating=False, hooks=None, check=True):
    argv = ["git", "-C", os.fspath(cwd)]
    if mutating:
        if hooks is None:
            raise WorktreeError("mutating git command requires a private hooks path")
        _validate_hooks(hooks)
        argv += ["-c", f"core.hooksPath={os.fspath(hooks)}", "-c", "commit.gpgsign=false",
                 "-c", "tag.gpgsign=false"]
    argv += list(args)
    try:
        done = subprocess.run(
            argv, shell=False, env=_git_env(), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="surrogateescape", check=False,
        )
    except OSError as error:
        raise WorktreeError(f"failed to run git: {error}") from error
    if check and done.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed ({done.returncode}): "
            f"{(done.stderr or done.stdout or '').strip()}"
        )
    return done


def _rev(cwd: Path, rev: str) -> str:
    done = _git(cwd, ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], check=False)
    value = done.stdout.strip()
    if done.returncode != 0 or not _HEX.fullmatch(value):
        raise WorktreeError(f"cannot resolve commit: {rev}")
    return value


def _top(repo: Path) -> Path:
    done = _git(repo, ["rev-parse", "--show-toplevel"])
    return Path(os.path.realpath(done.stdout.strip()))


def _git_path(cwd: Path, arg: str) -> Path:
    raw = Path(_git(cwd, ["rev-parse", arg]).stdout.strip())
    return Path(os.path.realpath(raw if raw.is_absolute() else Path(cwd) / raw))


def _common(cwd: Path) -> Path:
    return _git_path(cwd, "--git-common-dir")


def _gitdir(cwd: Path) -> Path:
    return _git_path(cwd, "--git-dir")


def _branch(cwd: Path) -> str:
    done = _git(cwd, ["symbolic-ref", "--short", "-q", "HEAD"], check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def _porcelain(cwd: Path) -> str:
    done = _git(cwd, ["status", "--porcelain=v1", "--untracked-files=all"], check=False)
    if done.returncode != 0:
        raise WorktreeError("cannot read git status")
    return done.stdout


def _ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    argv = ["merge-base", "--is-ancestor", ancestor, descendant]
    return _git(cwd, argv, check=False).returncode == 0


def _in_progress(cwd: Path) -> list[str]:
    git_dir = _gitdir(cwd)
    markers = {"MERGE_HEAD": "merge", "CHERRY_PICK_HEAD": "cherry-pick", "REVERT_HEAD": "revert",
               "BISECT_LOG": "bisect", "rebase-merge": "rebase", "rebase-apply": "rebase"}
    return sorted({label for name, label in markers.items() if (git_dir / name).exists()})


def _reject_in_progress(cwd: Path, label: str) -> None:
    states = _in_progress(cwd)
    if states:
        raise WorktreeStateError(f"{label} has an in-progress {'/'.join(states)} operation")


def _identity(path: Path) -> str:
    try:
        return workspace_identity(Path(path)).hex()
    except WorkspaceLockError as error:
        raise WorktreeValidationError(str(error)) from error


def _check(path: Path, label: str, *, directory: bool, private: bool = False) -> None:
    info = _stat(path, label)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if stat.S_ISLNK(info.st_mode) or not expected(info.st_mode):
        kind = "directory" if directory else "regular file"
        raise WorktreeValidationError(f"{label} must be a real {kind}")
    if not private:
        return
    if os.name == "posix":
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise WorktreeValidationError(f"{label} is not private to the current user")
    elif os.name == "nt":
        from . import windows_file_io

        try:
            windows_file_io.validate_private_path(path, directory=directory)
        except windows_file_io.WindowsPathError as error:
            raise WorktreeValidationError(f"{label} is not private") from error


def _field(data: dict, name: str, *, limit: int = 4096) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise WorktreeValidationError(f"manifest field {name!r} is invalid")
    return value


def _load(path: Path) -> tuple[Path, dict]:
    manifest = Path(os.path.abspath(os.fspath(path)))
    _check(manifest, "manifest", directory=False, private=True)
    if manifest.name != MANIFEST_NAME or _stat(manifest, "manifest").st_size > MAX_MANIFEST:
        raise WorktreeValidationError(f"manifest must be a private {MANIFEST_NAME}")
    _check(manifest.parent, "metadata root", directory=True, private=True)
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise WorktreeValidationError(f"invalid manifest: {error}") from error
    if not isinstance(document, dict) or document.get("version") != VERSION:
        raise WorktreeValidationError("unsupported manifest version")
    sections = [document.get(name) for name in ("repo", "root", "checkout")]
    if not all(isinstance(section, dict) for section in sections):
        raise WorktreeValidationError("manifest sections must be objects")
    if _BRANCH.fullmatch(_field(document, "branch", limit=255)) is None:
        raise WorktreeValidationError("manifest branch is invalid")
    if not _HEX.fullmatch(_field(document, "base", limit=64)):
        raise WorktreeValidationError("manifest base is invalid")
    if document.get("integration") is not None and not isinstance(document["integration"], dict):
        raise WorktreeValidationError("manifest integration section is invalid")
    return manifest, document


def _validate_workspace(document: dict) -> None:
    """Tie the recorded workspace top to the live repository that owns it."""
    repo = document["repo"]
    top = Path(_field(repo, "top"))
    common = Path(_field(repo, "common_dir"))
    if os.path.realpath(top) != os.fspath(top) or not top.is_dir():
        raise WorktreeValidationError("workspace top is not the recorded directory")
    try:
        if _top(top) != Path(os.fspath(top)):
            raise WorktreeValidationError("workspace top is not a repository top")
        if _common(top) != Path(os.path.realpath(common)):
            raise WorktreeValidationError("workspace top is not the recorded repository")
    except WorktreeError as error:
        raise WorktreeValidationError(str(error)) from error
    _check(common, "repository common dir", directory=True)
    if _identity(common) != _field(repo, "common_identity", limit=256):
        raise WorktreeValidationError("repository common dir identity changed")


def _validate_root(root: Path, document: dict) -> None:
    """Validate the host-only metadata root under ``.state``."""
    recorded = document["root"]
    top = _field(document["repo"], "top")
    realpath = _field(recorded, "realpath")
    parent = _field(recorded, "parent_realpath")
    identity = _field(recorded, "identity", limit=256)
    controller = os.path.realpath(os.fspath(_controller_parent(top)))
    state = os.path.realpath(os.fspath(_state_parent(top)))
    if os.path.realpath(root) != realpath:
        raise WorktreeValidationError("metadata root realpath changed")
    if os.path.dirname(realpath) != parent:
        raise WorktreeValidationError("metadata root is not in its recorded parent")
    if os.path.basename(parent) != STATE_NAME or os.path.dirname(parent) != controller:
        raise WorktreeValidationError("metadata parent is not the state parent")
    if state != parent:
        raise WorktreeValidationError("metadata parent does not derive from workspace top")
    if controller != os.path.join(top, HIDDEN_NAME):
        raise WorktreeValidationError("controller parent does not derive from workspace top")
    if realpath in (top, controller, state):
        raise WorktreeValidationError("refusing to operate on the workspace or shared parents")
    if _INSTANCE.fullmatch(root.name) is None:
        raise WorktreeValidationError("metadata root is not a random agent instance")
    _check(Path(controller), "controller parent", directory=True, private=True)
    _check(Path(state), "state parent", directory=True, private=True)
    _check(root, "metadata root", directory=True, private=True)
    if _identity(root) != identity:
        raise WorktreeValidationError("metadata root identity changed")


def _validate_repo(root: Path, document: dict) -> Path:
    """Validate the assigned checkout under the controller parent."""
    top = _field(document["repo"], "top")
    controller = os.path.realpath(os.fspath(_controller_parent(top)))
    checkout = Path(_field(document["checkout"], "path"))
    realpath = _field(document["checkout"], "realpath")
    _check(checkout, "checkout", directory=True, private=True)
    if os.path.realpath(checkout) != realpath:
        raise WorktreeValidationError("checkout realpath changed")
    if os.path.dirname(realpath) != controller:
        raise WorktreeValidationError("checkout is not in the controller parent")
    if checkout.name != root.name or _INSTANCE.fullmatch(checkout.name) is None:
        raise WorktreeValidationError("checkout does not match the metadata instance")
    if _identity(checkout) != _field(document["checkout"], "identity", limit=256):
        raise WorktreeValidationError("checkout identity changed")
    common = Path(_field(document["repo"], "common_dir"))
    if _common(checkout) != Path(os.path.realpath(common)):
        raise WorktreeValidationError("checkout does not belong to recorded repository")
    if _branch(checkout) != document["branch"]:
        raise WorktreeValidationError("checkout is not on the recorded branch")
    if not _git(common, ["cat-file", "-e", f"{document['base']}^{{commit}}"], check=False).returncode == 0:
        raise WorktreeValidationError("recorded base commit no longer exists")
    if not _ancestor(common, document["base"], _rev(checkout, "HEAD")):
        raise WorktreeValidationError("recorded base is not an ancestor of the branch")
    return checkout


def _validate(path: Path) -> tuple[Path, Path, dict]:
    manifest, document = _load(path)
    root = manifest.parent
    _validate_workspace(document)
    _validate_root(root, document)
    return root, _validate_repo(root, document), document


def _document(root, checkout, branch, base, common, top) -> dict:
    return {
        "version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "branch": branch, "base": base,
        "repo": {"top": os.fspath(top), "common_dir": os.path.realpath(common), "common_identity": _identity(common)},
        "root": {"path": os.fspath(root), "realpath": os.path.realpath(root),
                 "parent_realpath": os.path.realpath(root.parent), "identity": _identity(root)},
        "checkout": {"path": os.fspath(checkout), "realpath": os.path.realpath(checkout),
                     "parent_realpath": os.path.realpath(checkout.parent), "identity": _identity(checkout)},
        "integration": None,
    }


def _write(path: Path, document: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", dir=str(path.parent))
    with os.fdopen(descriptor, "wb") as handle:
        if os.name == "posix":
            os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(document, indent=2, sort_keys=True).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _acquire(path: Path, what: str, lock_directory: Path | None):
    try:
        return acquire_workspace_lease(Path(path), lock_directory)
    except WorkspaceLockBusy as error:
        raise WorktreeBusy(f"{what} is in use by another process") from error
    except WorkspaceLockError as error:
        raise WorktreeError(f"cannot lock {what}: {error}") from error


@contextmanager
def hold_lease(path: Path, what: str, lock_directory: Path | None):
    lease = _acquire(path, what, lock_directory)
    try:
        yield
    finally:
        lease.release()


@contextmanager
def common_lock(common: Path, lock_directory: Path | None):
    lease = _acquire(common, "repository", lock_directory)
    try:
        yield
    finally:
        lease.release()


def require_clean(cwd: Path, label: str) -> None:
    if _porcelain(cwd).strip():
        raise WorktreeStateError(f"{label} is not clean")


def attached_branch(cwd: Path, label: str) -> str:
    branch = _branch(cwd)
    if not branch:
        raise WorktreeStateError(f"{label} HEAD is detached; a branch is required")
    return branch
