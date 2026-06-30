"""Cross-filesystem migration data-loss safety (Phase 1 fix 1.6).

A cross-FS ``shutil.move`` copies ``legacy`` → ``target`` (``copytree``) first,
then deletes ``legacy`` (``rmtree``). If a prior run was killed *mid-rmtree* —
after the copy completed — ``target`` holds the COMPLETE data while ``legacy``
is a partially-deleted remnant, and the ``.migrating`` flag is still present.

The completeness check in ``_target_is_completed_copy`` originally compared only
the *top-level* entry-name sets. When the interrupting kill landed while
``rmtree`` was deleting a NESTED file (so the top-level directories were still
present in both legacy and target), ``legacy_names == target_names`` — not a
strict subset — so the check returned ``False``. The resume path then did
``rmtree(target)`` + re-``move``, permanently losing the nested files ``rmtree``
had already removed from ``legacy``.

These tests pin the whole-tree (recursive) completeness check: the nested-delete
case must KEEP target, and a genuinely incomplete copy must still be re-moved.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.exceptions import MigrationError
from claude_swap.paths import (
    LEGACY_BACKUP_DIRNAME,
    _target_is_completed_copy,
    migrate_legacy_backup_dir,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Temp HOME with CLAUDE_CONFIG_DIR unset (mirrors tests/test_paths.py)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    with patch("pathlib.Path.home", return_value=home):
        yield home


def _flag_for(target: Path) -> Path:
    return target.parent / f".{target.name}.migrating"


class TestTargetIsCompletedCopyRecursive:
    """The completeness check must compare the WHOLE tree, not just top level."""

    def test_nested_deletion_keeps_top_level_equal_but_target_is_superset(
        self, isolated_home: Path
    ):
        """rmtree deleted only a NESTED file → top-level names still equal.

        Pre-fix this returned False (legacy_names == target_names, not a strict
        subset), so the destructive resume would re-move the partial legacy.
        """
        target = isolated_home / "target"
        (target / "credentials").mkdir(parents=True)
        (target / "credentials" / "cred1.enc").write_text("real-creds-1")
        (target / "credentials" / "cred2.enc").write_text("real-creds-2")

        legacy = isolated_home / "legacy"
        (legacy / "credentials").mkdir(parents=True)
        # rmtree already removed the nested cred2.enc from legacy; the
        # "credentials" directory (a top-level entry) is still present.
        (legacy / "credentials" / "cred1.enc").write_text("real-creds-1")

        assert _target_is_completed_copy(legacy, target) is True

    def test_identical_tree_is_not_a_completed_superset(self, isolated_home: Path):
        """Identical whole trees → no positive evidence legacy is a strict subset."""
        target = isolated_home / "target"
        (target / "credentials").mkdir(parents=True)
        (target / "credentials" / "cred1.enc").write_text("data")

        legacy = isolated_home / "legacy"
        (legacy / "credentials").mkdir(parents=True)
        (legacy / "credentials" / "cred1.enc").write_text("data")

        assert _target_is_completed_copy(legacy, target) is False

    def test_legacy_has_nested_file_target_lacks_is_not_completed(
        self, isolated_home: Path
    ):
        """legacy ⊄ target (legacy has a nested file target lacks) → not complete.

        Top-level names are equal, but legacy is NOT a subset of target, so this
        is not a finished post-copy state.
        """
        target = isolated_home / "target"
        (target / "credentials").mkdir(parents=True)
        (target / "credentials" / "cred1.enc").write_text("data")

        legacy = isolated_home / "legacy"
        (legacy / "credentials").mkdir(parents=True)
        (legacy / "credentials" / "cred1.enc").write_text("data")
        (legacy / "credentials" / "cred2.enc").write_text("legacy-only")

        assert _target_is_completed_copy(legacy, target) is False


class TestMigrateNestedDeletionSafety:
    def test_resume_does_not_lose_target_after_nested_rmtree(
        self, isolated_home: Path
    ):
        """Kill landed mid-rmtree(legacy) on a NESTED file after copy finished.

        Top-level entry names are IDENTICAL between legacy and target (only a
        nested cred was deleted from legacy), so the original top-level-only
        subset check returned False and the resume path would rmtree(target) and
        re-move the partial legacy — permanently losing cred2.enc. The recursive
        check must instead keep the complete target and drop the legacy remnant.
        """
        # target = the complete, post-copy copy.
        target = isolated_home / ".local" / "share" / "claude-swap"
        target.mkdir(parents=True)
        (target / "sequence.json").write_text('{"complete": true}')
        (target / "credentials").mkdir()
        (target / "credentials" / "cred1.enc").write_text("real-creds-1")
        (target / "credentials" / "cred2.enc").write_text("real-creds-2")

        # legacy = partial remnant: rmtree removed the NESTED cred2.enc and
        # sequence.json, but the top-level "credentials" dir is still present so
        # the top-level name sets are NOT a strict subset relationship.
        legacy = isolated_home / LEGACY_BACKUP_DIRNAME
        legacy.mkdir()
        (legacy / "credentials").mkdir()
        (legacy / "credentials" / "cred1.enc").write_text("real-creds-1")

        flag = _flag_for(target)
        flag.touch()

        result = migrate_legacy_backup_dir(target)

        # The complete data in target must be fully preserved.
        assert (target / "sequence.json").read_text() == '{"complete": true}'
        assert (target / "credentials" / "cred1.enc").read_text() == "real-creds-1"
        assert (target / "credentials" / "cred2.enc").read_text() == "real-creds-2"
        # The legacy remnant and flag are cleaned up; no second move ran.
        assert not legacy.exists()
        assert not flag.exists()
        assert result is False

    def test_resume_does_not_lose_target_when_only_nested_dir_diff(
        self, isolated_home: Path
    ):
        """Both share every top-level name; legacy is missing a whole nested dir.

        sequence.json identical at top level; legacy lost configs/extra.json
        (nested) — target is the authoritative complete copy.
        """
        target = isolated_home / ".local" / "share" / "claude-swap"
        target.mkdir(parents=True)
        (target / "sequence.json").write_text('{"complete": true}')
        (target / "configs").mkdir()
        (target / "configs" / "a.json").write_text('{"a": 1}')
        (target / "configs" / "b.json").write_text('{"b": 2}')

        legacy = isolated_home / LEGACY_BACKUP_DIRNAME
        legacy.mkdir()
        (legacy / "sequence.json").write_text('{"complete": true}')
        (legacy / "configs").mkdir()
        (legacy / "configs" / "a.json").write_text('{"a": 1}')
        # b.json already rmtree'd from legacy.

        flag = _flag_for(target)
        flag.touch()

        result = migrate_legacy_backup_dir(target)

        assert (target / "configs" / "a.json").read_text() == '{"a": 1}'
        assert (target / "configs" / "b.json").read_text() == '{"b": 2}'
        assert not legacy.exists()
        assert not flag.exists()
        assert result is False


class TestMigrateGenuinelyIncompleteCopyIsReMoved:
    def test_partial_copy_with_equal_top_level_is_re_moved(
        self, isolated_home: Path
    ):
        """Inverse: target is a genuinely INCOMPLETE copy → must be re-moved.

        Here legacy is the authoritative complete source and target is a partial
        copy from an interrupted copytree (legacy has a nested file target
        lacks). Top-level names happen to be equal, but legacy is NOT a subset of
        target, so we must NOT keep target — we re-move legacy.
        """
        legacy = isolated_home / LEGACY_BACKUP_DIRNAME
        legacy.mkdir()
        (legacy / "sequence.json").write_text('{"src": "legacy"}')
        (legacy / "credentials").mkdir()
        (legacy / "credentials" / "cred1.enc").write_text("legacy-cred-1")
        (legacy / "credentials" / "cred2.enc").write_text("legacy-cred-2")

        # target = partial copy: copytree got cred1 but died before cred2 and
        # before writing the real sequence.json contents.
        target = isolated_home / ".local" / "share" / "claude-swap"
        target.mkdir(parents=True)
        (target / "sequence.json").write_text("partial-garbage")
        (target / "credentials").mkdir()
        (target / "credentials" / "cred1.enc").write_text("partial")

        flag = _flag_for(target)
        flag.touch()

        result = migrate_legacy_backup_dir(target)

        # legacy was the authoritative source → it is what ends up at target.
        assert result is True
        assert not legacy.exists()
        assert not flag.exists()
        assert (target / "sequence.json").read_text() == '{"src": "legacy"}'
        assert (target / "credentials" / "cred1.enc").read_text() == "legacy-cred-1"
        assert (target / "credentials" / "cred2.enc").read_text() == "legacy-cred-2"

    def test_stale_partial_target_subset_of_legacy_is_re_moved(
        self, isolated_home: Path
    ):
        """Classic resume: target is a strict subset of legacy → re-move legacy."""
        legacy = isolated_home / LEGACY_BACKUP_DIRNAME
        legacy.mkdir()
        (legacy / "sequence.json").write_text('{"src": "legacy"}')
        (legacy / "extra.json").write_text('{"extra": true}')

        target = isolated_home / ".local" / "share" / "claude-swap"
        target.mkdir(parents=True)
        (target / "sequence.json").write_text("stale-partial")

        flag = _flag_for(target)
        flag.touch()

        result = migrate_legacy_backup_dir(target)

        assert result is True
        assert not legacy.exists()
        assert not flag.exists()
        assert (target / "sequence.json").read_text() == '{"src": "legacy"}'
        assert (target / "extra.json").read_text() == '{"extra": true}'
