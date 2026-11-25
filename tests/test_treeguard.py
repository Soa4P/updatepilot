"""Caps on the review tree (external audit item #1 for this repo).

Review mode builds an archive from a *fetched PR ref* — content the PR author controls —
and unpacks it. Nothing bounded the size, so a PR with a hundred thousand files or one
5 GB file could exhaust the runner before review began. These tests pin each cap and the
property that matters most: the check happens on tar **headers**, before extraction.
"""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from updatepilot.treeguard import (
    MAX_ARCHIVE_BYTES,
    MAX_MEMBER_BYTES,
    MAX_MEMBERS,
    TreeTooLargeError,
    assert_tree_within_limits,
)


def _tar(path: Path, members: dict[str, int] | list[tuple[str, int]]) -> Path:
    """Write a tar with the given {name: size} (contents are zero bytes, never extracted)."""
    items = members.items() if isinstance(members, dict) else members
    with tarfile.open(path, "w") as tar:
        for name, size in items:
            info = tarfile.TarInfo(name)
            info.size = size
            tar.addfile(info, io.BytesIO(b"\0" * size) if size else io.BytesIO(b""))
    return path


def test_a_normal_repository_passes(tmp_path: Path) -> None:
    archive = _tar(tmp_path / "ok.tar", {"pyproject.toml": 200, "src/app.py": 4000})
    report = assert_tree_within_limits(archive)
    assert report["members"] == 2
    assert report["unpacked_bytes"] == 4200


def test_an_oversized_single_file_is_refused(tmp_path: Path) -> None:
    archive = _tar(tmp_path / "big.tar", {"huge.bin": MAX_MEMBER_BYTES + 1})
    with pytest.raises(TreeTooLargeError, match="per-file cap"):
        assert_tree_within_limits(archive)


def test_too_many_members_is_refused(tmp_path: Path) -> None:
    members = [(f"f{i}.txt", 0) for i in range(MAX_MEMBERS + 1)]
    archive = _tar(tmp_path / "many.tar", members)
    with pytest.raises(TreeTooLargeError, match="members"):
        assert_tree_within_limits(archive)


def test_an_oversized_archive_is_refused_before_opening(tmp_path: Path) -> None:
    archive = tmp_path / "huge.tar"
    archive.write_bytes(b"\0" * 64)
    with pytest.raises(TreeTooLargeError, match="cap"):
        assert_tree_within_limits(archive, max_archive_bytes=16)
    assert MAX_ARCHIVE_BYTES > 16  # the parameter exists for the test, not for policy


def test_an_escaping_member_is_refused(tmp_path: Path) -> None:
    archive = _tar(tmp_path / "evil.tar", {"../../etc/passwd": 10})
    with pytest.raises(TreeTooLargeError, match="escapes"):
        assert_tree_within_limits(archive)


def test_an_absolute_member_is_refused(tmp_path: Path) -> None:
    archive = _tar(tmp_path / "abs.tar", {"/root/.ssh/authorized_keys": 10})
    with pytest.raises(TreeTooLargeError, match="escapes"):
        assert_tree_within_limits(archive)


def test_a_non_tar_is_refused_with_a_readable_message(tmp_path: Path) -> None:
    archive = tmp_path / "not.tar"
    archive.write_bytes(b"PK\x03\x04 this is a zip")
    with pytest.raises(TreeTooLargeError, match="not a readable tar"):
        assert_tree_within_limits(archive)


def test_extraction_gets_a_wall_clock_ceiling() -> None:
    """A cap on bytes does not bound time: 120k small files still cost minutes."""
    from updatepilot import cli

    source = (Path(cli.__file__)).read_text(encoding="utf-8")
    assert source.count("timeout=EXTRACT_TIMEOUT_SECONDS") == 2, (
        "both tree builds (PR and branch) must carry the timeout"
    )
