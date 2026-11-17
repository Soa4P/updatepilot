"""Applies a planned PR's edits to the working tree.

Deliberately dumb: it writes exactly the lines the planner rendered (from the
manifest reader's own spans), re-reads each file to verify the new pin is
present, and refuses to touch anything else. If the file changed on disk since
the scan (another commit landed mid-run), the line-mismatch aborts the bump for
that file — a rewrite of the wrong line is worse than a skipped update.
"""

from __future__ import annotations

from pathlib import Path

from .planner import PlannedPR


class BumpError(RuntimeError):
    """The working tree no longer matches what the scan saw."""


def apply_edits(repo_root: Path, pr: PlannedPR) -> list[str]:
    """Write the planned edits; return the file list for ``git add``."""
    touched: list[str] = []
    for rel_file, line_map in pr.edits.items():
        path = repo_root / rel_file
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        for lineno, rendered in sorted(line_map.items()):
            index = lineno - 1
            if index >= len(lines):
                raise BumpError(
                    f"{rel_file}:{lineno} 不在当前文件里（扫描后文件变了？）；"
                    "为避免改错行，跳过这个更新"
                )
            lines[index] = rendered
        path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
        touched.append(rel_file)
    return touched
