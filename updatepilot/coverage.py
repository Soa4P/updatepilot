"""Coverage annotation (Codecov-inspired, self-hosted): the bot reads a
``coverage.json`` that the adopter's own CI already produced — it never runs
tests itself and never uploads anything. For each changed Python file that the
coverage file knows about, the review comment gets a small block: how many of
the changed lines are uncovered, and which.

Missing coverage file / missing entries / coverage for a different tree layout
all degrade to "no annotation" — a comment without the section is still a
complete review.
"""

from __future__ import annotations

import json
from pathlib import Path

from .inline import changed_lines_by_file


def load_coverage(path: Path) -> dict[str, dict]:
    """Parse a pytest-cov ``coverage.json`` into ``{file: {missing: set, summary}}``.

    Returns {} for anything unreadable/malformed — annotation is best-effort by
    contract. Paths in coverage.json are relative to the coverage run's cwd;
    keys are normalised to posix.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    out: dict[str, dict] = {}
    for raw_path, info in files.items():
        if not isinstance(info, dict):
            continue
        norm = raw_path.replace("\\", "/")
        missing = set(info.get("missing_lines") or [])
        summary = info.get("summary") if isinstance(info.get("summary"), dict) else {}
        out[norm] = {"missing": missing, "summary": summary}
    return out


def annotate_changed(
    coverage: dict[str, dict],
    diff: str,
    *,
    max_files: int = 12,
) -> list[str]:
    """Render the coverage section lines for changed files, or [] when there is
    nothing to say (no coverage data / no overlaps). Files the coverage run did
    not see are not listed — absence of data is stated once by the caller."""
    if not coverage:
        return []
    changed = changed_lines_by_file(diff)
    lines_out: list[str] = []
    for path in sorted(changed):
        info = coverage.get(path)
        if not info:
            continue
        touched = changed[path]
        if not touched:
            continue
        missing = info.get("missing", set())
        uncovered = sorted(touched & missing)
        if uncovered:
            shown = ", ".join(str(n) for n in uncovered[:10])
            more = f" 等 {len(uncovered)} 行" if len(uncovered) > 10 else ""
            lines_out.append(
                f"- `{path}`：变更 {len(touched)} 行，其中未覆盖 {len(uncovered)} 行（{shown}{more}）"
            )
        else:
            lines_out.append(f"- `{path}`：变更 {len(touched)} 行，全部覆盖 ✓")
        if len(lines_out) >= max_files:
            break
    return lines_out


def coverage_section(workspace: Path, diff: str) -> str:
    """Full section (or "") for the review comment. Reads ``coverage.json`` from
    the workspace root if present — read-only, by design."""
    coverage = load_coverage(workspace / "coverage.json")
    if not coverage:
        return ""
    rows = annotate_changed(coverage, diff)
    if not rows:
        return ""
    head = "### 覆盖标注（coverage.json 由你的 CI 生成，本 bot 只读）"
    return head + "\n" + "\n".join(rows)
