"""The CLI the GitHub Action invokes: ``updatepilot scan`` / ``updatepilot review``.

``scan`` is the full loop: read manifests, ask registries, plan PRs under the
repo's policy; then for each planned PR: create branch, commit, push, open PR,
and immediately run the full-file review on its own PR. The human sees every PR
with its review already attached.

``review`` is the standalone review mode, for adopters who only want the
reviewer. It needs ``PR_NUMBER`` in the environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import gh
from .audit import (
    AUDIT_LABEL,
    MAX_AUDIT_CALLS,
    audit_issue_title,
    build_audit_issue,
    collect_audit_files,
    filter_new_findings,
)
from .bumper import BumpError, apply_edits
from .config import (
    MAX_FILE_BYTES,
    MAX_FILE_LINES,
    BotConfig,
    load_config,
    resolve_api_key,
    resolve_base_url,
)
from .coverage import coverage_section
from .gh import GitError
from .inline import build_inline_comments
from .planner import ScanReport, attach_release_notes, fetch_latest_versions, plan_scan
from .review import (
    FileEntry,
    ReviewOptions,
    ReviewTarget,
    plan_groups,
    review_file_groups,
    review_target,
    truncate,
)
from .treeguard import EXTRACT_TIMEOUT_SECONDS, TreeTooLargeError, assert_tree_within_limits

LABEL_COLORS = {
    "ai:safe": "0E8A16",
    "ai:needs-review": "FBCA04",
    "ai:malicious": "B60205",
}


def _print(message: str) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(message)


def _pr_tree(cwd: Path, number: int, temp_dir: Path) -> Path:
    """Materialise the PR head tree as text (git archive then tar -x)."""
    tree = temp_dir / "pr-tree"
    tree.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "fetch", "--depth=1", "origin", f"refs/pull/{number}/head"],
        cwd=cwd, capture_output=True, text=True, shell=False, check=True,
    )
    archive = tree / "_archive.tar"
    with archive.open("wb") as handle:
        subprocess.run(
            ["git", "archive", "FETCH_HEAD"], cwd=cwd, check=True, stdout=handle,
        )
    assert_tree_within_limits(archive)
    subprocess.run(
        ["tar", "-xf", str(archive), "-C", str(tree)],
        cwd=cwd, capture_output=True, text=True, shell=False, check=True,
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    archive.unlink()
    return tree


def _pr_diff_and_files(cwd: Path, number: int) -> tuple[str, list[str]]:
    diff = subprocess.run(
        ["gh", "pr", "diff", str(number)], cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace", shell=False, check=True,
    ).stdout
    names_raw = subprocess.run(
        ["gh", "pr", "diff", str(number), "--name-only"], cwd=cwd,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        shell=False, check=True,
    ).stdout
    names = [line.strip() for line in names_raw.splitlines() if line.strip()]
    return diff, names


def _diff_of_edits(repo_root: Path, planned, base: str) -> str:
    """The diff of the bot's own commit — identical to the PR diff."""
    result = subprocess.run(
        ["git", "diff", f"origin/{base}", planned.branch, "--"],
        cwd=repo_root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", shell=False, check=True,
    )
    return result.stdout


def verdict_label(verdict: str) -> str:
    return {"safe": "ai:safe", "needs_review": "ai:needs-review",
            "malicious": "ai:malicious"}[verdict]


def render_verdict(review: dict) -> str:
    from .review import render_comment

    return render_comment(review, language="zh")


def _scan_report(repo_root: Path, config: BotConfig) -> ScanReport:
    from .manifests import read_manifests

    pins: list = []
    for eco in config.enabled_ecosystems:
        pins.extend(read_manifests(repo_root, eco, config.ecosystems[eco].manifests))
    token = os.environ.get("GITHUB_TOKEN")
    lookup = fetch_latest_versions(pins, github_token=token)
    return plan_scan(repo_root, config, lookup, github_token=token)


def _render_report(report: ScanReport) -> None:
    _print(f"计划开 {len(report.planned)} 个 PR")
    for pr in report.planned:
        _print(f"  - {pr.title}")
    for line in report.deferred:
        _print(f"⏸ {line}")
    for line in report.skipped[:20]:
        _print(f"⏭ {line}")
    for line in report.errors:
        _print(f"✗ {line}")
    if report.errors:
        # The summary line is what a human reads when they skim a scheduled run. Without
        # this, "0 PRs" from a rate-limited run is indistinguishable from "everything is
        # current" — the failure mode that made an action bump silently never happen.
        _print(
            f"⚠ {len(report.errors)} 个依赖**没查成**（不是已最新）；这些坐标本轮没有结论，详见上面的 ✗ 行。"
        )


def _review_options(config: BotConfig, api_key: str) -> ReviewOptions:
    review = config.review
    return ReviewOptions(
        api_key=api_key,
        model=review.model,
        base_url=resolve_base_url(review),
        max_tokens=review.max_tokens,
        retry_max_tokens=review.retry_max_tokens,
        file_max_tokens=review.file_max_tokens,
        file_retry_max_tokens=review.file_retry_max_tokens,
        max_calls=review.max_calls,
        critical_paths=tuple(review.critical_paths),
        extra_generated=tuple(review.generated_prefixes),
        language=review.language,
    )


def _branch_tree(cwd: Path, branch: str, temp_dir: Path) -> Path:
    """Materialise a pushed branch as text (git archive | tar -x) — the review
    reads exactly what was committed, not whatever the working tree later
    becomes. Same text-only contract as _pr_tree."""
    tree = temp_dir / "branch-tree"
    tree.mkdir(parents=True, exist_ok=True)
    archive = tree / "_archive.tar"
    with archive.open("wb") as handle:
        subprocess.run(
            ["git", "archive", gh.validate_branch(branch)], cwd=cwd, check=True, stdout=handle,
        )
    assert_tree_within_limits(archive)
    subprocess.run(
        ["tar", "-xf", str(archive), "-C", str(tree)],
        cwd=cwd, capture_output=True, text=True, shell=False, check=True,
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    archive.unlink()
    return tree


def _scan(repo_root: Path) -> int:
    config = load_config(repo_root)
    report = _scan_report(repo_root, config)
    attach_release_notes(report, github_token=os.environ.get("GITHUB_TOKEN"))
    _render_report(report)
    api_key = resolve_api_key(config.review)
    options = _review_options(config, api_key)
    # Dry-run must not need the network: `default_branch` fetches, and a scan that
    # cannot reach GitHub should still show the plan it *would* open (measured — it
    # printed "git fetch origin… failed" and then "0 PRs", which reads like success).
    base_branch = "" if config.dry_run else gh.default_branch(repo_root)
    opened: list[tuple[int, str]] = []

    for planned in report.planned:
        if config.dry_run:
            _print(f"[dry-run] would open: {planned.title}")
            continue
        try:
            gh.checkout_base(repo_root)
            touched = apply_edits(repo_root, planned)
            gh.create_branch_and_commit(repo_root, planned.branch, planned.title, touched)
            number = gh.open_pr(
                repo_root,
                title=planned.title,
                body="\n".join(planned.body_lines),
                branch=planned.branch,
                base=base_branch,
            )
        except (GitError, BumpError) as exc:
            _print(f"⚠ {planned.title}: {exc}")
            continue
        verdict = "needs_review"
        if api_key:
            diff = _diff_of_edits(repo_root, planned, base_branch)
            # Review the PUSHED branch content, not the working tree: between
            # this PR's commit and the review, nothing may silently drift — and
            # the next loop iteration's checkout_base would reset the tree.
            with tempfile.TemporaryDirectory() as temp:
                try:
                    tree = _branch_tree(repo_root, planned.branch, Path(temp))
                except TreeTooLargeError as exc:
                    # A tree we refuse to unpack is still a PR we opened: say so on the
                    # PR rather than leaving it silently unreviewed.
                    _print(f"⚠ {planned.title}: 审查树超出限额，已跳过审查（{exc}）")
                    tree = None
                if tree is not None:
                    review = review_target(
                        ReviewTarget(
                            diff=diff,
                            changed_files=sorted(planned.edits),
                            tree_root=tree,
                            title=planned.title,
                            body="\n".join(planned.body_lines),
                        ),
                        options,
                    )
                    verdict = review["verdict"]
                    comment = render_verdict(review)
                else:
                    comment = (
                        "本 PR 未经过 AI 审查：待审代码树超出审查限额（文件数/单文件/总量）。"
                        "请人工审查，或拆分本 PR。"
                    )
        else:
            comment = "未配置审查模型 key；本 PR 未经过 AI 审查，请人工审查。"
        try:
            gh.comment_on_pr(repo_root, number, comment)
            gh.label_pr(repo_root, number, verdict_label(verdict),
                        LABEL_COLORS[verdict_label(verdict)])
        except GitError as exc:
            _print(f"⚠ PR #{number} 评论/打标失败：{exc}")
        opened.append((number, verdict))
    _print(f"完成：{len(opened)} 个 PR 已开出并附审查。")
    return 0


def _review(repo_root: Path) -> int:
    """Standalone review mode for adopters who only want the reviewer."""
    raw_number = os.environ.get("PR_NUMBER", "")
    if not raw_number.isdigit():
        _print("review 模式需要 PR_NUMBER 环境变量")
        return 2
    number = int(raw_number)
    config = load_config(repo_root)
    api_key = resolve_api_key(config.review)
    if not api_key:
        _print(f"未配置 {config.review.api_key_env}；没有进行审查，请人工审查。")
        return 0
    diff, names = _pr_diff_and_files(repo_root, number)
    options = _review_options(config, api_key)
    inline = []
    with tempfile.TemporaryDirectory() as temp:
        try:
            tree = _pr_tree(repo_root, number, Path(temp))
        except TreeTooLargeError as exc:
            _print(f"审查树超出限额，拒绝解包：{exc}")
            return 1
        review = review_target(
            ReviewTarget(
                diff=diff,
                changed_files=names,
                tree_root=tree,
                title=os.environ.get("PR_TITLE", ""),
                body=os.environ.get("PR_BODY", ""),
            ),
            options,
        )
        # 行级内联评论：location 是纯行号且落在变更行内才锚——其余留在汇总里。
        inline = build_inline_comments(review.get("findings", []), diff)
    body = render_verdict(review)
    # coverage.json 由调用方的 CI 生成（bot 读不跑）；存在即标注变更行的未覆盖。
    coverage_block = coverage_section(repo_root, diff)
    if coverage_block:
        body += "\n\n" + coverage_block
    if inline:
        _print(f"内联评论 {len(inline)} 条（其余留在汇总）。")
    try:
        gh.post_review(repo_root, number, body, [
            {"path": c.path, "line": c.line, "body": c.body} for c in inline
        ])
        label = verdict_label(review["verdict"])
        gh.label_pr(repo_root, number, label, LABEL_COLORS[label])
    except GitError as exc:
        _print(f"⚠ PR #{number} 评论/打标失败：{exc}")
    _print(json.dumps({"verdict": review["verdict"], "degraded": review["degraded"],
                       "inline": len(inline)}))
    return 0


def _audit(repo_root: Path) -> int:
    """Repo-wide sweep (draft-only): bounded groups → labelled issue, deduped
    against the previous audit issue by signature. Disposal stays human."""
    config = load_config(repo_root)
    api_key = resolve_api_key(config.review)
    if not api_key:
        _print(f"未配置 {config.review.api_key_env}；巡检需要模型调用，未执行。")
        return 0
    options = _review_options(config, api_key)
    files = collect_audit_files(
        repo_root,
        critical_paths=tuple(config.review.critical_paths),
        extra_skip=tuple(config.review.generated_prefixes),
    )
    entries: list[FileEntry] = []
    capped = 0
    for rel in files:
        content, was_capped, _dropped = truncate(
            (repo_root / rel).read_text(encoding="utf-8", errors="replace"),
            max_bytes=MAX_FILE_BYTES, max_lines=MAX_FILE_LINES,
        )
        capped += was_capped
        entries.append(FileEntry(
            path=rel.as_posix(), mode="full", content=content,
            hunks="", fully_read=True,
        ))
    groups, _leftovers = plan_groups(entries, max_calls=MAX_AUDIT_CALLS)
    _print(f"巡检 {len(entries)} 个文件，分 {len(groups)} 组（预算 {MAX_AUDIT_CALLS}）。")
    reviewed = review_file_groups(
        groups, api_key=api_key, model=options.model, base_url=options.base_url,
    )
    findings = [{"file": e.path, "findings": e.findings} for e in reviewed if e.findings]
    previous = gh.latest_audit_issue(repo_root, AUDIT_LABEL)
    fresh = filter_new_findings(findings, previous[1] if previous else None)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    body = build_audit_issue(
        now, "本轮巡检未做整体裁决——发现列表即产出（草稿，处置归维护者）。",
        None, fresh, previous_url=f"#{previous[0]}" if previous else None,
        scanned=len(entries),
    )
    note = f"，另 {capped} 个文件仅读了部分内容" if capped else ""
    _print(f"新发现 {sum(len(g['findings']) for g in fresh)} 条（去重后）{note}。")
    number = gh.create_issue(
        repo_root, title=audit_issue_title(now), body=body,
        label=AUDIT_LABEL, color="FBCA04",
    )
    _print(f"巡检 issue #{number} 已创建。")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ""
    repo_root = Path(os.environ.get("UPDATEPILOT_REPO", ".")).resolve()
    if command == "scan":
        return _scan(repo_root)
    if command == "review":
        return _review(repo_root)
    if command == "audit":
        return _audit(repo_root)
    _print("用法：updatepilot scan | review（需 PR_NUMBER）| audit（仓库巡检）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
