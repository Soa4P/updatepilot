"""Inline review comments: anchor findings to real changed lines — or don't anchor.

Sourcery-inspired but honesty-first: a finding goes inline **only when** the
model's location hint contains a bare line number AND that line is a changed
line (b-side) of the PR diff for that file. Function-name hints and descriptive
locations are already rendered in the summary comment; anchoring them to
guessed lines would be pseudo-precision. Bounded at ``MAX_INLINE_COMMENTS`` —
the rest stay in the summary, where the full per-file findings list lives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_INLINE_COMMENTS = 10

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class InlineComment:
    path: str
    line: int
    body: str


def changed_lines_by_file(diff: str) -> dict[str, set[int]]:
    """Map each file (post-image path) to its changed line numbers on the b side.

    A line is "changed" when it is an added line (``+``) or context (`` ``) —
    removed lines do not exist on the b side, so they are skipped.
    """
    result: dict[str, set[int]] = {}
    header = re.compile(r"^diff --git a/(.*) b/(.*)$")
    current: str | None = None
    line_no = 0
    in_hunk = False
    for line in diff.splitlines():
        header_match = header.match(line)
        if header_match:
            current = header_match.group(2).strip()
            in_hunk = False
            continue
        if line.startswith("@@"):
            match = _HUNK.match(line)
            if match and current is not None:
                line_no = int(match.group(1))
                in_hunk = True
            continue
        if not in_hunk or current is None:
            continue
        if line.startswith("+"):
            result.setdefault(current, set()).add(line_no)
            line_no += 1
        elif line.startswith("-"):
            continue  # removed line: not on the b side
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            result.setdefault(current, set()).add(line_no)
            line_no += 1
    return result


def _location_line(location: str) -> int | None:
    """Extract a bare line number from a model-written location hint.

    Accepts "12", "12 行", "line 12", "L12". Function names and prose return
    None — anchoring those would be pseudo-precision.
    """
    hint = (location or "").strip()
    match = re.fullmatch(r"(?:line\s*|L)(\d+)", hint, re.IGNORECASE) or re.fullmatch(
        r"(\d+)\s*行?", hint
    )
    if not match:
        return None
    line = int(match.group(1))
    return line if line > 0 else None


def build_inline_comments(
    findings: list[dict],
    diff: str,
    *,
    max_comments: int = MAX_INLINE_COMMENTS,
) -> list[InlineComment]:
    """Pick findings that can be anchored honestly: integer location + the line
    is a changed line of that file. Everything else stays in the summary.

    ``findings`` items are the aggregated ``{"file": path, "findings": [...]}``
    entries produced by the review flow.
    """
    changed = changed_lines_by_file(diff)
    out: list[InlineComment] = []
    for group in findings:
        path = str(group.get("file") or "")
        lines = changed.get(path, set())
        for finding in group.get("findings", []):
            if len(out) >= max_comments:
                return out
            line = _location_line(str(finding.get("location") or ""))
            if line is None or line not in lines:
                continue
            severity = str(finding.get("severity") or "medium")
            why = str(finding.get("why") or "").strip()
            if not why:
                continue
            out.append(InlineComment(
                path=path, line=line,
                body=f"**[{severity}]** {why}\n\n— UpdatePilot（辅助意见，不构成批准）",
            ))
    return out
