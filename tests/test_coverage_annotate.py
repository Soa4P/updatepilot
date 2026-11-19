"""Coverage annotation: read-only, best-effort, data never leaves the repo.

Codecov's capability without Codecov's SaaS — the adopter's CI produces
``coverage.json``, the bot reads it and annotates the review comment. Pins:
parse tolerance (malformed = no annotation), b-side intersection only (removed
lines cannot be "uncovered"), per-file rendering, and the read-only contract
(a missing file is not an error).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot.coverage import annotate_changed, coverage_section, load_coverage

_COVERAGE_JSON = """{
  "files": {
    "src/app.py": {
      "executed_lines": [10, 13],
      "missing_lines": [11, 12],
      "summary": {"covered_lines": 2, "num_statements": 4}
    },
    "src/other.py": {
      "executed_lines": [1],
      "missing_lines": [2],
      "summary": {"covered_lines": 1, "num_statements": 2}
    }
  }
}"""


def test_load_coverage_normalises_and_tolerates_garbage(tmp_path: Path) -> None:
    good = tmp_path / "coverage.json"
    good.write_text(_COVERAGE_JSON, encoding="utf-8")
    cov = load_coverage(good)
    assert cov["src/app.py"]["missing"] == {11, 12}

    bad = tmp_path / "bad.json"
    bad.write_text("<html>404</html>", encoding="utf-8")
    assert load_coverage(bad) == {}
    assert load_coverage(tmp_path / "missing.json") == {}


def test_annotation_counts_context_and_added_as_changed() -> None:
    """removed 行不在 b 侧（不可能"未覆盖"）；context+added 都计入变更行。"""
    coverage = {"src/app.py": {"missing": {11, 12}, "summary": {}}}
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -10,4 +11,4 @@\n"
        " context 11\n"
        "+added 12\n"
        "-removed\n"
        " context 13\n"
    )
    rows = annotate_changed(coverage, diff)
    assert len(rows) == 1
    # changed = {11, 12, 13}（b 侧）；missing = {11, 12} → 未覆盖 2 行
    assert "未覆盖 2 行" in rows[0]
    assert "11" in rows[0] and "12" in rows[0]


def test_fully_covered_changed_lines_render_the_check_mark() -> None:
    coverage = {"src/app.py": {"missing": {99}, "summary": {}}}
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " context\n"
        "+added\n"
    )
    rows = annotate_changed(coverage, diff)
    assert rows == ["- `src/app.py`：变更 2 行，全部覆盖 ✓"]


def test_files_without_coverage_data_are_not_listed() -> None:
    coverage = {"src/known.py": {"missing": set(), "summary": {}}}
    diff = (
        "diff --git a/src/unknown.py b/src/unknown.py\n"
        "+++ b/src/unknown.py\n"
        "+new\n"
    )
    assert annotate_changed(coverage, diff) == []


def test_coverage_section_reads_the_workspace_file(tmp_path: Path) -> None:
    (tmp_path / "coverage.json").write_text(_COVERAGE_JSON, encoding="utf-8")
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -10,3 +10,3 @@\n"
        "+added 11\n"
        "+added 12\n"
    )
    section = coverage_section(tmp_path, diff)
    assert "coverage.json 由你的 CI 生成" in section
    assert "src/app.py" in section
    # 没有文件 → 空节（评论仍然完整）
    assert coverage_section(tmp_path / "nowhere", diff) == ""
