"""v1.2.0: inline review comments + repo-wide audit sweep.

The three design references (Codecov / Sourcery / Dosu) distilled into
testable behaviours: honest line anchoring (no pseudo-precision), bounded
sweeps with stable ranking, and signature dedupe against the previous audit.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot.audit import (
    audit_signature,
    build_audit_issue,
    collect_audit_files,
    filter_new_findings,
    previous_signatures,
)
from updatepilot.inline import (
    _location_line,
    build_inline_comments,
    changed_lines_by_file,
)


# ------------------------------------------------------------------ inline anchoring
def test_changed_lines_maps_b_side_of_hunks() -> None:
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -10,4 +10,5 @@\n"
        " context\n"
        "-removed\n"
        "+added at 11\n"
        "+added at 12\n"
        " context at 13\n"
    )
    lines = changed_lines_by_file(diff)["src/app.py"]
    # b 侧：context(10) + added(11,12) + context(13)；removed 不在 b 侧
    assert lines == {10, 11, 12, 13}


def test_location_line_accepts_only_bare_numbers() -> None:
    assert _location_line("12") == 12
    assert _location_line("12 行") == 12
    assert _location_line("line 12") == 12
    assert _location_line("L12") == 12
    # 函数名/描述性 location 不硬锚（伪精确比不精确更糟）
    for bad in ("act()", "订单处理函数", "somewhere in the middle", ""):
        assert _location_line(bad) is None, bad


def test_inline_comments_only_anchor_changed_lines() -> None:
    findings = [{
        "file": "src/app.py",
        "findings": [
            {"severity": "high", "location": "11", "why": "变更行上的发现"},
            {"severity": "low", "location": "5000", "why": "行不在 diff 内"},
            {"severity": "medium", "location": "act()", "why": "非行号"},
        ],
    }]
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -10,2 +11,2 @@\n"
        "+added at 11\n"
    )
    inline = build_inline_comments(findings, diff)
    assert len(inline) == 1
    assert inline[0].line == 11 and "变更行上的发现" in inline[0].body
    assert "不构成批准" in inline[0].body  # 每条内联都带 advisory 标注


def test_inline_comments_are_bounded() -> None:
    from updatepilot.inline import MAX_INLINE_COMMENTS

    diff = "".join(f"diff --git a/f{i}.py b/f{i}.py\n@@ -0,1 +1,2 @@\n+new\n"
                   for i in range(MAX_INLINE_COMMENTS + 5))
    findings = [
        {"file": f"f{i}.py",
         "findings": [{"severity": "low", "location": "1", "why": f"n{i}"}]}
        for i in range(MAX_INLINE_COMMENTS + 5)
    ]
    inline = build_inline_comments(findings, diff)
    assert len(inline) == MAX_INLINE_COMMENTS  # 超出的留在汇总，不硬塞


# ------------------------------------------------------------------ audit sweep
def test_collect_audit_files_ranks_critical_first_and_skips_generated(tmp_path: Path) -> None:
    (tmp_path / "src/soapbot/security").mkdir(parents=True)
    (tmp_path / "src/soapbot/security/auth.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "src/app.py").write_text("y = 2", encoding="utf-8")
    pkg = tmp_path / "node_modules/pkg"
    pkg.mkdir(parents=True)
    (pkg / "index.js").write_text("z", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("d", encoding="utf-8")
    wf = tmp_path / ".github/workflows/ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("on: push", encoding="utf-8")

    files = collect_audit_files(
        tmp_path,
        critical_paths=("src/soapbot/security/",),
        extra_skip=(),
    )
    names = [f.as_posix() for f in files]
    assert names[0] == "src/soapbot/security/auth.py"  # 关键面优先
    assert ".github/workflows/ci.yml" in names  # workflow 是注入面，纳入巡检
    assert all("node_modules" not in n for n in names)
    assert all(not n.endswith(".md") for n in names)  # 文档不在巡检面


def test_audit_budget_caps_the_file_list(tmp_path: Path) -> None:
    from updatepilot.audit import MAX_AUDIT_CALLS

    for i in range(100):
        (tmp_path / f"m{i}.py").write_text("x", encoding="utf-8")
    files = collect_audit_files(tmp_path, critical_paths=())
    assert len(files) == MAX_AUDIT_CALLS * 4  # 100 个只取预算内的前 80


def test_signature_dedupe_against_previous_issue() -> None:
    why = "发现：数据外传"
    sig = audit_signature("src/f.py", why)
    entries = [{"file": "src/f.py", "findings": [
        {"severity": "high", "location": "1", "why": why},
        {"severity": "medium", "location": "2", "why": "全新的一条"},
    ]}]
    previous_body = f"- 旧的发现…… sig:{sig}\n"
    fresh = filter_new_findings(entries, previous_body)
    assert [f["why"] for f in fresh[0]["findings"]] == ["全新的一条"]
    # 无上一份 → 全部保留
    assert len(filter_new_findings(entries, None)[0]["findings"]) == 2


def test_previous_signatures_ignores_empty_body() -> None:
    assert previous_signatures(None) == set()
    assert previous_signatures("") == set()


def test_audit_issue_carries_dedupe_signatures_and_provenance() -> None:
    findings = [{"file": "src/f.py", "findings": [
        {"severity": "high", "location": "1", "why": "外传"}]}]
    body = build_audit_issue(
        "2026-11-20T10:00:00", "一句话结论", None, findings,
        previous_url="#12", scanned=80,
    )
    assert "sig:" in body  # 下一轮巡检靠它去重
    assert "#12" in body
    assert "不触发任何自动处置" in body  # 草稿纪律写进每个 issue
