"""Repo-wide audit mode (``updatepilot audit``): a bounded, deduplicated sweep.

Dosu-inspired but draft-only: the sweep produces a labelled issue with findings;
nothing is auto-triaged, auto-closed or auto-fixed — disposal stays with the
maintainer. Bounds make "whole repository" a bounded job, not an open wallet:

* generated/binary files are skipped by the same classification as PR review;
* files are ranked security-critical first, then by path, and packed into the
  same group calls the PR review uses — budget ``MAX_AUDIT_CALLS``;
* findings are deduplicated against the previous audit issue by signature
  (file + normalised why-prefix): only new findings are reported, and the
  previous issue is linked so known ones stay findable.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

#: 巡检预算：一次巡检最多多少个分组模型调用（×4 文件/组 = ≤80 文件）。
MAX_AUDIT_CALLS = 20
#: issue 正文的字节上限（超预算的发现截断并在尾部说明）。
MAX_ISSUE_BYTES = 60_000
AUDIT_LABEL = "updatepilot:audit"
AUDIT_TITLE_PREFIX = "UpdatePilot 代码库巡检"

_CODE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs", ".vue", ".rs", ".dart")


def _is_source(path: Path, extra_skip: tuple[str, ...] = ()) -> bool:
    """What the sweep reads: code suffixes, plus workflow YAML (shell-injection
    surface). Generated dirs, docs and lockfiles are skipped by the same
    classification the PR review uses."""
    rel = path.as_posix().lower()
    skip_prefixes = ("node_modules/", "__pycache__/", "dist/", "build/", ".venv/",
                     "vendor/", "docs/", "mobile/build/") + tuple(
        p.lower().rstrip("/") + "/" for p in extra_skip
    )
    if any(rel.startswith(prefix) for prefix in skip_prefixes):
        return False
    if rel.endswith(_CODE_SUFFIXES):
        return True
    return rel.endswith((".yml", ".yaml")) and rel.startswith(".github/workflows")


def collect_audit_files(
    repo_root: Path,
    *,
    critical_paths: tuple[str, ...] = (),
    extra_skip: tuple[str, ...] = (),
    budget: int = MAX_AUDIT_CALLS * 4,
) -> list[Path]:
    """Rank and bound the files one audit sweep will read.

    Ranking: security-critical paths first (the config's critical_paths), then
    everything else by path — stable so consecutive sweeps read the same order
    and the dedupe signature stays meaningful.

    Only regular files **inside** the repository are listed. ``is_file()`` follows
    symlinks, so a committed ``notes.py -> /home/ops/.ssh/id_rsa`` used to be read and
    pasted into the model prompt — and from there into a public issue (audit S-UP-01).
    The resolved path must stay under the repo root; a link is skipped, not read
    through.
    """
    root = repo_root.resolve()
    files: list[Path] = []
    for path in sorted(repo_root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if not path.resolve().is_relative_to(root):
                continue
        except OSError:
            continue
        rel = path.relative_to(repo_root)
        if _is_source(rel, extra_skip):
            files.append(rel)
    ranked = sorted(
        files,
        key=lambda rel: (0 if any(rel.as_posix().startswith(c) for c in critical_paths) else 1,
                         rel.as_posix()),
    )
    return ranked[: max(0, budget)]


def audit_signature(file_path: str, why: str) -> str:
    """Stable dedupe key: file + normalised why prefix, hashed to 16 hex chars
    (the file name is inside the hash, so the bare value is what gets compared
    and what the previous issue stores)."""
    norm = re.sub(r"\s+", " ", why or "").strip().lower()[:80]
    payload = f"{file_path}::{norm}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def previous_signatures(previous_body: str | None) -> set[str]:
    """Recover the dedupe keys recorded in the previous audit issue body."""
    if not previous_body:
        return set()
    return set(re.findall(r"sig:([0-9a-f]{16})", previous_body))


def filter_new_findings(
    entries: list[dict],
    previous_body: str | None,
) -> list[dict]:
    """Drop findings whose signature appeared in the previous audit issue.

    ``entries`` is the aggregated ``[{"file": …, "findings": […]}]`` shape from
    the review flow; a group with zero surviving findings is dropped entirely.
    """
    known = previous_signatures(previous_body)
    if not known:
        return entries
    out: list[dict] = []
    for group in entries:
        fresh = []
        for finding in group.get("findings", []):
            sig = audit_signature(str(group.get("file") or ""), str(finding.get("why") or ""))
            if sig not in known:
                fresh.append(finding)
        if fresh:
            out.append({"file": group.get("file", ""), "findings": fresh})
    return out


def audit_issue_title(now_iso: str) -> str:
    return f"{AUDIT_TITLE_PREFIX} · {now_iso[:10]}"


def build_audit_issue(
    now_iso: str,
    verdict_summary: str,
    coverage_block: str | None,
    findings: list[dict],
    *,
    previous_url: str | None = None,
    scanned: int,
) -> str:
    """Render the audit issue body: verdict, new findings (with dedupe sigs so
    the next sweep can skip them), and provenance."""
    lines = [
        f"## {AUDIT_TITLE_PREFIX} · {now_iso[:10]}",
        "",
        verdict_summary or "(无结论)",
        "",
        f"本轮扫描 {scanned} 个源码文件（预算 {MAX_AUDIT_CALLS * 4}）；"
        "发现为**草稿**，处置由维护者决定。已知的旧发现不在此重复"
        + (f"——上一份：{previous_url}" if previous_url else "") + "。",
    ]
    if coverage_block:
        lines += ["", "### 覆盖面", "", coverage_block]
    if findings:
        lines += ["", "### 新发现", ""]
        for group in findings[:12]:
            lines.append(f"**`{group['file']}`**")
            for finding in group["findings"][:6]:
                where = f"（{finding.get('location')}）" if finding.get("location") else ""
                lines.append(
                    f"- {finding.get('severity', 'medium')}{where}：{finding.get('why', '')}  "
                    f"sig:{audit_signature(str(group.get('file')), str(finding.get('why', '')))}"
                )
    over = sum(len(g["findings"]) for g in findings[:12]) - sum(
        len(g["findings"]) for g in findings
    )
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > MAX_ISSUE_BYTES:
        body = body[:MAX_ISSUE_BYTES] + "\n\n（超出 issue 上限，已截断——下一轮巡检去重后会再报增量。）"
    if over:
        body += f"\n\n（另有 {over} 条发现超出展示上限，见审查引擎日志。）"
    body += "\n\n---\n\n由 UpdatePilot 自动巡检生成；本 issue 不触发任何自动处置。"
    return body
