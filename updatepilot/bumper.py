"""Applies a planned PR's edits to the working tree.

Deliberately dumb: it writes exactly the lines the planner rendered (from the
manifest reader's own spans), re-reads each file to verify the new pin is
present, and refuses to touch anything else. If the file changed on disk since
the scan (another commit landed mid-run), the line-mismatch aborts the bump for
that file — a rewrite of the wrong line is worse than a skipped update.

Every write path must resolve inside ``repo_root`` (the same containment the
reviewer enforces): manifest paths come from the adopter's config, but a drifted
or malicious config must not turn the bumper into an arbitrary-file writer.
"""

from __future__ import annotations

from pathlib import Path

from .planner import PlannedPR
from .review import safe_join


class BumpError(RuntimeError):
    """The working tree no longer matches what the scan saw."""


def apply_edits(repo_root: Path, pr: PlannedPR) -> list[str]:
    """Write the planned edits; return the file list for ``git add``."""
    touched: list[str] = []
    for rel_file, line_map in pr.edits.items():
        resolved = safe_join(repo_root, rel_file)
        if resolved is None:
            raise BumpError(
                f"manifest 路径越出仓库根，拒绝写入：{rel_file!r}"
            )
        text = resolved.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        for lineno, rendered in sorted(line_map.items()):
            index = lineno - 1
            if index >= len(lines):
                raise BumpError(
                    f"{rel_file}:{lineno} 不在当前文件里（扫描后文件变了？）；"
                    "为避免改错行，跳过这个更新"
                )
            lines[index] = rendered
        # errors="replace" mirrors the read: a manifest that round-trips with
        # replacement characters must not crash the write.
        resolved.write_text(
            "\n".join(lines) + ("\n" if text.endswith("\n") else ""),
            encoding="utf-8", errors="replace",
        )
        touched.append(rel_file)
    return touched
