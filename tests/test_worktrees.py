from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp import worktree_support, worktrees
from deepseek_mcp.execution_lock import acquire_workspace_lease
from deepseek_mcp.worktree_support import (
    WorktreeBusy,
    WorktreeError,
    WorktreeStateError,
    WorktreeValidationError,
    _check,
    _validate_hooks,
)

GIT = shutil.which("git")
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


@unittest.skipUnless(GIT, "git is required for worktree lifecycle tests")
class WorktreeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.base = Path(self._temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.lock_directory = self.base / "locks"
        self.environment = patch.dict(
            os.environ,
            {worktrees.LOCK_ENV: str(self.lock_directory)},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self._git(["-c", "init.defaultBranch=main", "init", "-q"])
        self._git(["config", "user.name", "Test"])
        self._git(["config", "user.email", "test@example.com"])
        # The shared nested parent must be ignored so instances never dirty the
        # integration target.
        (self.repo / ".gitignore").write_text(
            worktree_support.HIDDEN_NAME + "/\n", encoding="utf-8"
        )
        (self.repo / "a.txt").write_text("base\n", encoding="utf-8")
        self._git(["add", ".gitignore", "a.txt"])
        self._git(["commit", "-q", "-m", "init"])
        self.workspace_top = Path(os.path.realpath(self.repo))

    # -- helpers ----------------------------------------------------------- #
    def _git(self, args: list[str], cwd: Path | None = None, check: bool = True):
        environment = dict(os.environ)
        environment.update(_IDENTITY)
        return subprocess.run(
            ["git", "-C", str(cwd or self.repo), *args],
            check=check,
            capture_output=True,
            text=True,
            env=environment,
        )

    def _commit(self, cwd: Path, name: str, content: str, message: str) -> str:
        (cwd / name).write_text(content, encoding="utf-8")
        self._git(["add", name], cwd=cwd)
        self._git(["commit", "-q", "-m", message], cwd=cwd)
        return self._git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()

    def _create(self, **kwargs) -> tuple[dict, Path, Path]:
        result = worktrees.create(self.repo, **kwargs)
        self.assertTrue(result["ok"])
        manifest = Path(result["manifest"])
        checkout = Path(result["checkout"])
        return result, manifest, checkout

    def _head(self) -> str:
        return self._git(["rev-parse", "HEAD"]).stdout.strip()

    # -- create ------------------------------------------------------------ #
    def test_parent_cleanup_preserves_nested_managed_instances(self) -> None:
        _, manifest, checkout = self._create()
        inner = worktrees.create(checkout)
        worktrees.integrate(manifest, self.repo, self._head())
        with self.assertRaisesRegex(WorktreeStateError, "nested subagent storage"):
            worktrees.cleanup(manifest)
        self.assertTrue(Path(inner["checkout"]).is_dir())
        self.assertTrue(Path(inner["manifest"]).is_file())
        self.assertTrue(manifest.is_file())

    def test_create_creates_private_checkout_and_separate_metadata(self) -> None:
        result, manifest, checkout = self._create()
        controller = self.workspace_top / worktree_support.HIDDEN_NAME
        state = controller / worktree_support.STATE_NAME
        metadata = manifest.parent
        self.assertEqual(result["base"], self._head())
        self.assertEqual(Path(result["checkout"]), checkout)
        self.assertEqual(Path(result["metadata"]), metadata)
        # The assigned workspace is the run-* directory itself, not a /checkout.
        self.assertEqual(checkout.parent, controller)
        self.assertEqual(metadata.parent, state)
        self.assertEqual(metadata.parent.parent, controller)
        self.assertEqual(metadata.name, checkout.name)
        self.assertRegex(checkout.name, r"run-[0-9a-f]{12}")
        self.assertNotIn("checkout", checkout.parts[-1])
        self.assertFalse((checkout / worktree_support.MANIFEST_NAME).exists())
        self.assertTrue(manifest.is_file())
        self.assertEqual(
            self._git(["symbolic-ref", "--short", "HEAD"], cwd=checkout).stdout.strip(),
            result["branch"],
        )
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
            for path in (controller, state, metadata, checkout):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_create_accepts_explicit_base(self) -> None:
        first = self._git(["rev-parse", "HEAD"]).stdout.strip()
        self._commit(self.repo, "b.txt", "second\n", "second")
        result, _, checkout = self._create(base=first)
        self.assertEqual(result["base"], first)
        self.assertEqual(
            self._git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip(), first
        )

    def test_create_rejects_busy_repository(self) -> None:
        common_dir = worktrees._common(self.repo)
        lease = acquire_workspace_lease(common_dir, self.lock_directory)
        try:
            with self.assertRaises(WorktreeBusy):
                worktrees.create(self.repo)
        finally:
            lease.release()

    # -- validation / tamper ---------------------------------------------- #
    def test_manifest_repo_tamper_is_rejected(self) -> None:
        _, manifest, _ = self._create()
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["repo"]["common_dir"] = str(self.base / "elsewhere")
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(WorktreeValidationError):
            worktrees.inspect(manifest)

    def test_manifest_root_parent_tamper_is_rejected(self) -> None:
        _, manifest, _ = self._create()
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["root"]["parent_realpath"] = str(self.base / "elsewhere")
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(WorktreeValidationError):
            worktrees.inspect(manifest)

    def test_manifest_identity_tamper_is_rejected(self) -> None:
        _, manifest, _ = self._create()
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["root"]["identity"] = "00" * 16
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(WorktreeValidationError):
            worktrees.cleanup(manifest)

    def test_branch_switch_on_checkout_is_rejected(self) -> None:
        _, manifest, checkout = self._create()
        self._git(["branch", "other", "HEAD"])
        self._git(["checkout", "-q", "other"], cwd=checkout)
        with self.assertRaises(WorktreeValidationError):
            worktrees.inspect(manifest)

    def test_manifest_outside_allowed_parent_is_rejected(self) -> None:
        _, manifest, _ = self._create()
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["repo"]["top"] = str(self.base)  # not a repository top
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(WorktreeValidationError):
            worktrees.inspect(manifest)

    def test_create_requires_gitignored_parent(self) -> None:
        other = self.base / "other"
        other.mkdir()
        self._git(["-c", "init.defaultBranch=main", "init", "-q"], cwd=other)
        self._git(["config", "user.name", "Test"], cwd=other)
        self._git(["config", "user.email", "test@example.com"], cwd=other)
        (other / "a.txt").write_text("base\n", encoding="utf-8")
        self._git(["add", "a.txt"], cwd=other)
        self._git(["commit", "-q", "-m", "init"], cwd=other)
        with self.assertRaises(WorktreeStateError):
            worktrees.create(other)

    def test_create_rejects_symlinked_parent(self) -> None:
        target = self.base / "attacker"
        target.mkdir(mode=0o700)
        link = self.repo / worktree_support.HIDDEN_NAME
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        with self.assertRaises(WorktreeValidationError):
            worktrees.create(self.repo)
        self.assertEqual(list(target.iterdir()), [])

    def test_create_rejects_symlinked_state_parent(self) -> None:
        controller = self.repo / worktree_support.HIDDEN_NAME
        controller.mkdir(mode=0o700)
        target = self.base / "state-target"
        target.mkdir(mode=0o700)
        try:
            (controller / worktree_support.STATE_NAME).symlink_to(
                target, target_is_directory=True
            )
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        with self.assertRaises(WorktreeValidationError):
            worktrees.create(self.repo)

    def test_create_on_plain_directory_is_rejected(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(WorktreeError):
            worktrees.create(plain)

    def test_create_collision_never_removes_unknown_checkout(self) -> None:
        controller = self.repo / worktree_support.HIDDEN_NAME
        controller.mkdir(mode=0o700)
        fixed = "deadbeefcafe"
        occupied = controller / (worktree_support.INSTANCE_PREFIX + fixed)
        occupied.mkdir(mode=0o700)
        marker = occupied / "keep.txt"
        marker.write_text("mine\n", encoding="utf-8")
        with patch.object(worktrees.secrets, "token_hex", return_value=fixed):
            with self.assertRaises(WorktreeError):
                worktrees.create(self.repo)
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), "mine\n")

    # -- inspect ----------------------------------------------------------- #
    def test_inspect_includes_staged_and_committed_changes(self) -> None:
        _, manifest, checkout = self._create()
        (checkout / "a.txt").write_text("staged\n", encoding="utf-8")
        self._git(["add", "a.txt"], cwd=checkout)
        self.assertIn("a.txt", worktrees.inspect(manifest)["diff_stat"])
        self._git(["commit", "-qm", "worker"], cwd=checkout)
        report = worktrees.inspect(manifest)
        self.assertTrue(report["clean"])
        self.assertIn("a.txt", report["committed_diff_stat"])

    def test_missing_manifest_field_is_validation_error(self) -> None:
        _, manifest, _ = self._create()
        document = json.loads(manifest.read_text(encoding="utf-8"))
        del document["root"]["identity"]
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(WorktreeValidationError):
            worktrees.inspect(manifest)

    def test_inspect_reports_untracked_and_diff(self) -> None:
        _, manifest, checkout = self._create()
        (checkout / "untracked.txt").write_text("x\n", encoding="utf-8")
        (checkout / "a.txt").write_text("changed\n", encoding="utf-8")
        report = worktrees.inspect(manifest)
        self.assertFalse(report["clean"])
        self.assertIn("untracked.txt", report["untracked"])
        codes = {(entry["code"], entry["path"]) for entry in report["files"]}
        self.assertIn(("??", "untracked.txt"), codes)
        self.assertIn((" M", "a.txt"), codes)

    def test_inspect_clean_branch(self) -> None:
        _, manifest, _ = self._create()
        report = worktrees.inspect(manifest)
        self.assertTrue(report["clean"])
        self.assertEqual(report["untracked"], [])
        self.assertEqual(report["head"], report["base"])
        self.assertEqual(report["commits_ahead_of_base"], 0)

    # -- serve ------------------------------------------------------------- #
    def test_serve_binds_fixed_workspace_environment(self) -> None:
        _, manifest, checkout = self._create()
        captured: dict = {}

        class _Completed:
            returncode = 0

        real_run = subprocess.run

        def fake_run(argv, **kwargs):
            if "cwd" not in kwargs:
                return real_run(argv, **kwargs)
            captured["argv"] = argv
            captured["environment"] = kwargs["env"]
            captured["cwd"] = kwargs["cwd"]
            return _Completed()

        with patch.object(worktrees.subprocess, "run", fake_run):
            code = worktrees.serve(manifest, python="/usr/bin/python3")

        self.assertEqual(code, 0)
        self.assertEqual(captured["cwd"], str(checkout))
        self.assertEqual(captured["argv"][0], "/usr/bin/python3")
        self.assertEqual(captured["argv"][1:], ["-I", "-m", "deepseek_mcp.server"])
        self.assertEqual(
            captured["environment"]["DEEPSEEK_WORKSPACE"], str(checkout)
        )
        self.assertEqual(captured["environment"][worktrees.MODE_ENV], "1")
        self.assertEqual(
            captured["environment"][worktrees.MANIFEST_ENV], str(manifest)
        )

    def test_serve_validates_before_launch(self) -> None:
        _, manifest, _ = self._create()
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["branch"] = "refs/heads/evil"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(WorktreeValidationError):
            worktrees.serve(manifest)

    def test_serve_refuses_while_active(self) -> None:
        _, manifest, _ = self._create()
        lease = acquire_workspace_lease(
            manifest.parent, self.lock_directory
        )
        try:
            with self.assertRaises(WorktreeBusy):
                worktrees.serve(manifest)
        finally:
            lease.release()

    # -- integrate --------------------------------------------------------- #
    def test_integrate_merges_and_records_state(self) -> None:
        result, manifest, checkout = self._create()
        source_head = self._commit(
            checkout, "b.txt", "worker\n", "worker change"
        )
        target_head = self._head()
        outcome = worktrees.integrate(manifest, self.repo, target_head)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["status"], "merged")
        self.assertEqual(outcome["source_head"], source_head)
        parents = self._git(
            ["rev-list", "--parents", "-n", "1", "HEAD"]
        ).stdout.split()
        self.assertEqual(len(parents), 3)  # merge commit has two parents
        document = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(document["integration"]["source_head"], source_head)
        # cleanup now succeeds because the branch is integrated.
        self.assertEqual(worktrees.cleanup(manifest)["status"], "cleaned")

    def test_integrate_rejects_dirty_source(self) -> None:
        _, manifest, checkout = self._create()
        (checkout / "a.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(WorktreeStateError):
            worktrees.integrate(manifest, self.repo, self._head())

    def test_integrate_rejects_dirty_target(self) -> None:
        _, manifest, _ = self._create()
        (self.repo / "a.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(WorktreeStateError):
            worktrees.integrate(manifest, self.repo, self._head())

    def test_integrate_rejects_wrong_expected_head(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        with self.assertRaises(WorktreeStateError):
            worktrees.integrate(manifest, self.repo, "0" * 40)

    def test_integrate_rejects_detached_target(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        head = self._head()
        self._git(["checkout", "-q", "--detach", head])
        with self.assertRaises(WorktreeStateError):
            worktrees.integrate(manifest, self.repo, head)

    def test_integrate_rejects_other_repository(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        other = self.base / "other"
        other.mkdir()
        subprocess.run(
            ["git", "-C", str(other), "-c", "init.defaultBranch=main", "init", "-q"],
            check=True, capture_output=True, text=True, env=dict(os.environ, **_IDENTITY),
        )
        (other / "x.txt").write_text("x\n", encoding="utf-8")
        self._git(["add", "x.txt"], cwd=other)
        self._git(["commit", "-q", "-m", "x"], cwd=other)
        other_head = self._git(["rev-parse", "HEAD"], cwd=other).stdout.strip()
        with self.assertRaises(WorktreeStateError):
            worktrees.integrate(manifest, other, other_head)

    def test_integrate_rejects_non_ancestor_base(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        # Advance the target on a disjoint history so the recorded base is no
        # longer an ancestor of target HEAD.
        self._git(["checkout", "-q", "--orphan", "orphan"])
        (self.repo / "o.txt").write_text("o\n", encoding="utf-8")
        self._git(["add", "o.txt"])
        self._git(["commit", "-q", "-m", "orphan"])
        orphan_head = self._head()
        with self.assertRaises(WorktreeStateError):
            worktrees.integrate(manifest, self.repo, orphan_head)

    def test_integrate_conflict_is_retained(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "a.txt", "worker\n", "worker")
        self._commit(self.repo, "a.txt", "host\n", "host")
        target_head = self._head()
        outcome = worktrees.integrate(manifest, self.repo, target_head)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["status"], "conflict")
        self.assertTrue(outcome["retained"])
        self.assertIn("a.txt", outcome["conflicted"])
        merge_head = worktrees._gitdir(self.repo) / "MERGE_HEAD"
        self.assertTrue(merge_head.exists())

    # -- cleanup ----------------------------------------------------------- #
    def test_cleanup_refuses_dirty_worktree(self) -> None:
        _, manifest, checkout = self._create()
        (checkout / "dirty.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(WorktreeStateError):
            worktrees.cleanup(manifest)

    def test_cleanup_refuses_unintegrated_branch(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        with self.assertRaises(WorktreeStateError):
            worktrees.cleanup(manifest)

    def test_cleanup_refuses_in_progress_operation(self) -> None:
        _, manifest, checkout = self._create()
        (worktrees._gitdir(checkout) / "MERGE_HEAD").write_text(
            self._head() + "\n", encoding="utf-8"
        )
        with self.assertRaises(WorktreeStateError):
            worktrees.cleanup(manifest)

    def test_cleanup_refuses_while_active(self) -> None:
        _, manifest, _ = self._create()
        lease = acquire_workspace_lease(manifest.parent, self.lock_directory)
        try:
            with self.assertRaises(WorktreeBusy):
                worktrees.cleanup(manifest)
        finally:
            lease.release()

    def test_cleanup_removes_checkout_metadata_and_shared_parents(self) -> None:
        result, manifest, checkout = self._create()
        branch = result["branch"]
        metadata = manifest.parent
        state = metadata.parent
        controller = state.parent
        self.assertEqual(worktrees.cleanup(manifest)["status"], "cleaned")
        self.assertFalse(checkout.exists())
        self.assertFalse(manifest.exists())
        self.assertFalse(metadata.exists())
        self.assertFalse(state.exists())
        self.assertFalse(controller.exists())
        self.assertTrue((self.repo / ".git").exists())  # workspace untouched
        verify = self._git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False
        )
        self.assertNotEqual(verify.returncode, 0)

    def test_cleanup_keeps_shared_parents_until_last_instance(self) -> None:
        _, first, _ = self._create()
        _, second, _ = self._create()
        controller = first.parent.parent.parent
        state = first.parent.parent
        self.assertEqual(controller, second.parent.parent.parent)
        self.assertEqual(state, second.parent.parent)
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(worktrees.cleanup(first)["status"], "cleaned")
        self.assertTrue(state.is_dir())
        self.assertTrue(controller.is_dir())
        self.assertEqual(worktrees.cleanup(second)["status"], "cleaned")
        self.assertFalse(state.exists())
        self.assertFalse(controller.exists())
        self.assertTrue((self.repo / ".git").exists())

    def test_cleanup_allows_branch_at_base(self) -> None:
        _, manifest, _ = self._create()
        self.assertEqual(worktrees.cleanup(manifest)["status"], "cleaned")

    # -- lifecycle locking and re-validation ------------------------------- #
    def test_integrate_refuses_while_serving(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        lease = acquire_workspace_lease(manifest.parent, self.lock_directory)
        try:
            with self.assertRaises(WorktreeBusy):
                worktrees.integrate(manifest, self.repo, self._head())
        finally:
            lease.release()

    def test_integrate_refuses_while_source_execution_active(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        lease = acquire_workspace_lease(checkout, self.lock_directory)
        try:
            with self.assertRaises(WorktreeBusy):
                worktrees.integrate(manifest, self.repo, self._head())
        finally:
            lease.release()

    def test_integrate_refuses_while_target_execution_active(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        lease = acquire_workspace_lease(self.repo, self.lock_directory)
        try:
            with self.assertRaises(WorktreeBusy):
                worktrees.integrate(manifest, self.repo, self._head())
        finally:
            lease.release()

    def test_integrate_rejects_in_progress_source_even_when_clean(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        (worktrees._gitdir(checkout) / "MERGE_HEAD").write_text(
            self._head() + "\n", encoding="utf-8"
        )
        with self.assertRaises(WorktreeStateError):
            worktrees.integrate(manifest, self.repo, self._head())

    def test_integrate_rejects_in_progress_target_even_when_clean(self) -> None:
        _, manifest, _ = self._create()
        (worktrees._gitdir(self.repo) / "MERGE_HEAD").write_text(
            self._head() + "\n", encoding="utf-8"
        )
        with self.assertRaises(WorktreeStateError):
            worktrees.integrate(manifest, self.repo, self._head())

    def test_integrate_revalidates_inside_locks(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        real_validate = worktrees._validate
        calls: list = []

        def counting(path):
            calls.append(path)
            return real_validate(path)

        with patch.object(worktrees, "_validate", counting):
            worktrees.integrate(manifest, self.repo, self._head())
        self.assertEqual(len(calls), 1)

    def test_serve_revalidates_after_acquiring_root_lease(self) -> None:
        _, manifest, _ = self._create()
        real_load = worktrees._load
        calls = {"n": 0}

        def counting_load(path):
            manifest_path, document = real_load(path)
            calls["n"] += 1
            if calls["n"] >= 2:
                tampered = json.loads(json.dumps(document))
                tampered["branch"] = "main"
                return manifest_path, tampered
            return manifest_path, document

        with patch.object(worktrees, "_load", counting_load):
            with self.assertRaises(WorktreeValidationError):
                worktrees.serve(manifest)
        self.assertGreaterEqual(calls["n"], 2)

    def test_cleanup_refuses_while_worktree_execution_active(self) -> None:
        _, manifest, checkout = self._create()
        lease = acquire_workspace_lease(checkout, self.lock_directory)
        try:
            with self.assertRaises(WorktreeBusy):
                worktrees.cleanup(manifest)
        finally:
            lease.release()

    # -- integration verification ------------------------------------------ #
    def test_cleanup_rejects_forged_integration_merge(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        worktrees.integrate(manifest, self.repo, self._head())
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["integration"]["merge_head"] = document["base"]
        manifest.write_text(json.dumps(document), encoding="utf-8")
        if os.name == "posix":
            os.chmod(manifest, 0o600)
        with self.assertRaises(WorktreeStateError):
            worktrees.cleanup(manifest)

    def test_cleanup_rejects_rewound_target(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        before = self._head()
        worktrees.integrate(manifest, self.repo, before)
        self._git(["reset", "--hard", before])
        with self.assertRaises(WorktreeStateError):
            worktrees.cleanup(manifest)

    def test_manual_conflict_resolution_then_integrate_and_cleanup(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "a.txt", "worker\n", "worker")
        self._commit(self.repo, "a.txt", "host\n", "host")
        conflict = worktrees.integrate(manifest, self.repo, self._head())
        self.assertEqual(conflict["status"], "conflict")
        (self.repo / "a.txt").write_text("resolved\n", encoding="utf-8")
        self._git(["add", "a.txt"])
        self._git(["commit", "-q", "--no-edit"])
        resolved = self._head()
        outcome = worktrees.integrate(manifest, self.repo, resolved)
        self.assertEqual(outcome["status"], "merged")
        self.assertEqual(worktrees.cleanup(manifest)["status"], "cleaned")

    # -- file kind and hooks injection ------------------------------------- #
    def test_check_requires_regular_file_for_non_directories(self) -> None:
        regular = self.base / "regular.txt"
        regular.write_text("x", encoding="utf-8")
        with self.assertRaises(WorktreeValidationError):
            _check(regular, "passage", directory=True)
        with self.assertRaises(WorktreeValidationError):
            _check(self.base, "passage", directory=False)
        if hasattr(os, "mkfifo"):
            fifo = self.base / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(WorktreeValidationError):
                _check(fifo, "passage", directory=False)

    def test_validate_hooks_requires_private_empty_directory(self) -> None:
        empty = self.base / "empty-hooks"
        empty.mkdir()
        os.chmod(empty, 0o700)
        _validate_hooks(empty)
        nonempty = self.base / "nonempty-hooks"
        nonempty.mkdir()
        os.chmod(nonempty, 0o700)
        (nonempty / "post-merge").write_text("#!/bin/sh\n", encoding="utf-8")
        with self.assertRaises(WorktreeStateError):
            _validate_hooks(nonempty)
        if hasattr(os, "symlink"):
            link = self.base / "linked-hooks"
            try:
                link.symlink_to(empty, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlinks unavailable: {error}")
            with self.assertRaises(WorktreeStateError):
                _validate_hooks(link)

    def test_mutation_rejects_injected_hook(self) -> None:
        _, manifest, checkout = self._create()
        (manifest.parent / worktree_support.HOOKS / "post-merge").write_text(
            "#!/bin/sh\nexit 1\n", encoding="utf-8"
        )
        self._commit(checkout, "b.txt", "worker\n", "worker")
        with self.assertRaises(WorktreeStateError):
            worktrees.integrate(manifest, self.repo, self._head())

    # -- CLI --------------------------------------------------------------- #
    def test_cli_create_and_inspect_without_api_config(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = worktrees.main(["create", "--repo", str(self.repo)])
        self.assertEqual(code, 0)
        created = json.loads(buffer.getvalue())
        self.assertTrue(created["ok"])

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = worktrees.main(["inspect", "--manifest", created["manifest"]])
        self.assertEqual(code, 0)
        report = json.loads(buffer.getvalue())
        self.assertTrue(report["ok"])
        self.assertTrue(report["clean"])

    def test_cli_conflict_returns_conflict_exit_code(self) -> None:
        result, manifest, checkout = self._create()
        self._commit(checkout, "a.txt", "worker\n", "worker")
        self._commit(self.repo, "a.txt", "host\n", "host")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = worktrees.main(
                [
                    "integrate",
                    "--manifest",
                    str(manifest),
                    "--target",
                    str(self.repo),
                    "--expected-head",
                    self._head(),
                ]
            )
        self.assertEqual(code, worktrees.EXIT_CONFLICT)
        self.assertEqual(json.loads(buffer.getvalue())["status"], "conflict")

    def test_cli_reports_error_for_missing_manifest(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = worktrees.main(
                ["inspect", "--manifest", str(self.base / "missing" / "manifest.json")]
            )
        self.assertEqual(code, worktrees.EXIT_ERROR)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])

    def test_cli_cleanup_error_is_nonzero(self) -> None:
        _, manifest, checkout = self._create()
        self._commit(checkout, "b.txt", "worker\n", "worker")
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = worktrees.main(["cleanup", "--manifest", str(manifest)])
        self.assertEqual(code, worktrees.EXIT_ERROR)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])

    def test_cli_cleanup_branch_retained_is_nonzero(self) -> None:
        retained = {"ok": False, "status": "branch_retained"}
        with patch.object(worktrees, "cleanup", return_value=retained):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = worktrees.main(
                    ["cleanup", "--manifest", str(self.base / "manifest.json")]
                )
        self.assertEqual(code, worktrees.EXIT_ERROR)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
