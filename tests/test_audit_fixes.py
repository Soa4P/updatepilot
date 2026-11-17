"""Regression tests for the v1.0.1 audit fixes.

Each test pins one finding from the security review: deleted-detection false
positive, brace-safe pin rendering, branch validation coverage, bumper path
containment, and the registry egress guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot.bumper import BumpError, apply_edits
from updatepilot.gh import GitError, validate_branch
from updatepilot.manifests import PinnedDependency
from updatepilot.registries import RegistryError, _get_json
from updatepilot.review import collect_file_entries


def test_real_diff_header_does_not_falsify_deletion(tmp_path: Path) -> None:
    """A REAL `gh pr diff` section always contains "\\n--- a/…" — that must not
    classify a tree-missing LIVE file as deleted (fully_read=True), or the
    not-fully-read downgrade is defeated."""
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "index abc..def 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    entries = collect_file_entries(["src/app.py"], tree_root=tmp_path, diff=diff, critical_paths=())
    assert entries[0].deleted is False
    assert entries[0].fully_read is False  # 树缺失 = 未完整读取 = 强制降级


def test_true_deletion_is_recognized_structurally(tmp_path: Path) -> None:
    diff = (
        "diff --git a/gone.py b/gone.py\n"
        "index abc..000\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-body\n"
    )
    entries = collect_file_entries(["gone.py"], tree_root=tmp_path, diff=diff, critical_paths=())
    assert entries[0].deleted is True and entries[0].fully_read is True


def test_pin_render_is_brace_safe() -> None:
    """A comment containing "{docs}" must not crash str.format on the template."""
    pin = PinnedDependency(
        ecosystem="pip", name="pytest", current="9.2.0", file="requirements.txt",
        line=1, kind="pep440",
        replace_span=("pytest==9.2.0  # see {docs}", "pytest=={pin}  # see {docs}"),
    )
    assert pin.render("9.3.0") == "pytest==9.3.0  # see {docs}"


def test_validate_branch_rejects_injection_shapes() -> None:
    for hostile in (
        "-oProxyCommand=evil", "--upload-pack=evil", "a/../..b", "", "x" * 200,
        "分支名", "with space", "with;semicolon",
    ):
        with pytest.raises(GitError):
            validate_branch(hostile)
    assert validate_branch("updatepilot/pip/pytest-9.3.0") == "updatepilot/pip/pytest-9.3.0"


def test_bumper_refuses_paths_that_escape_the_repo(tmp_path: Path) -> None:
    from updatepilot.planner import PlannedPR

    (tmp_path / "requirements.txt").write_text("pytest==9.2.0\n", encoding="utf-8")
    pr = PlannedPR(ecosystem="pip", branch="b", title="t", body_lines=[], updates=[])
    pr.edits["../../outside.txt"] = {1: "pwned"}
    with pytest.raises(BumpError, match="越出仓库根"):
        apply_edits(tmp_path, pr)
    assert not (tmp_path.parent / "outside.txt").exists()


def test_registry_get_json_refuses_non_https(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="https"):
        _get_json("http://pypi.org/pypi/x/json")
    with pytest.raises(RegistryError, match="https"):
        _get_json("file:///etc/passwd")
