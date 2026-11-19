"""Thin wrappers over the ``gh`` and ``git`` CLIs.

The bot deliberately talks to GitHub through ``gh`` (preinstalled on Actions
runners) instead of another HTTP client: the caller's ``GITHUB_TOKEN`` flows in
via ``GH_TOKEN``, and PR creation stays one auditable command. Every wrapper
raises :class:`GitError` with the command's stderr — a failed PR must be loud,
not a silently skipped update.

Injection posture: every subprocess call passes an argv **list** with
``shell=False`` — no shell ever parses these strings, so metacharacters in
dependency names cannot become commands. The remaining risk is *argument*
injection (a value starting with ``-`` parsed as an option by git/gh), which is
closed at the source: branch names must match a strict slug regex before they
reach a subprocess, and every git invocation puts ``--`` before path arguments.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: Branch names reach ``git checkout -B`` / ``git push`` as argv elements. A
#: dependency name (registry-controlled text) flows into them, so the slug is
#: validated to this charset — no leading dash (argument injection), no ``..``
#: (path-ish tricks), bounded length.
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,180}$")


def validate_branch(branch: str) -> str:
    if not BRANCH_RE.match(branch) or ".." in branch:
        raise GitError(f"refusing unsafe branch name: {branch!r}")
    return branch


class GitError(RuntimeError):
    """A git/gh subprocess failed; the message carries stderr."""


def _run(args: list[str], *, cwd: Path, check: bool = True,
         input_text: str | None = None) -> subprocess.CompletedProcess:
    """argv-list only, ``shell=False`` explicitly — see the module docstring."""
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=False, check=False,
        input=input_text,
    )
    if check and result.returncode != 0:
        raise GitError(
            f"{' '.join(args[:3])}… failed (exit {result.returncode}): {result.stderr.strip()[:500]}"
        )
    return result


def repo_slug(cwd: Path) -> str:
    """``owner/name`` of the checkout's origin, via gh's own resolution."""
    result = _run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], cwd=cwd)
    return result.stdout.strip()


def default_branch(cwd: Path) -> str:
    result = _run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
        cwd=cwd,
    )
    return result.stdout.strip() or "main"


def checkout_base(cwd: Path) -> None:
    """Detach onto the default branch's remote head — never commit to a local branch."""
    _run(["git", "fetch", "origin", "--quiet"], cwd=cwd)
    branch = validate_branch(default_branch(cwd))
    _run(["git", "checkout", "--detach", f"origin/{branch}", "--quiet"], cwd=cwd)


def create_branch_and_commit(
    cwd: Path,
    branch: str,
    message: str,
    edited_files: list[str],
) -> None:
    """Validate the branch name, apply edits, commit on a fresh branch, push."""
    validate_branch(branch)
    _run(["git", "config", "user.name", "updatepilot[bot]"], cwd=cwd)
    _run(["git", "config", "user.email", "updatepilot[bot]@users.noreply.github.com"], cwd=cwd)
    _run(["git", "checkout", "-B", validate_branch(branch), "--quiet"], cwd=cwd)
    _run(["git", "add", "--", *edited_files], cwd=cwd)
    _run(["git", "commit", "-m", message, "--quiet"], cwd=cwd)
    _run(["git", "push", "origin", validate_branch(branch), "--quiet"], cwd=cwd)


def open_pr(cwd: Path, *, title: str, body: str, branch: str, base: str | None = None) -> int:
    """Create the PR and return its number."""
    branch = validate_branch(branch)
    base = validate_branch(base or default_branch(cwd))
    result = _run(
        ["gh", "pr", "create", "--title", title, "--body", body,
         "--head", branch, "--base", base],
        cwd=cwd,
    )
    # gh prints the PR URL; the number is its last path segment.
    for token in reversed(result.stdout.strip().split("/")):
        if token.isdigit():
            return int(token)
    raise GitError(f"could not parse PR number from gh output: {result.stdout!r}")


def comment_on_pr(cwd: Path, number: int, body: str) -> None:
    _run(["gh", "pr", "comment", str(number), "--body", body], cwd=cwd)


def label_pr(cwd: Path, number: int, label: str, color: str) -> None:
    _run(["gh", "label", "create", label, "--color", color, "--force"], cwd=cwd, check=False)
    _run(["gh", "pr", "edit", str(number), "--add-label", label], cwd=cwd)


def pr_head_sha(cwd: Path, number: int) -> str:
    result = _run(
        ["gh", "pr", "view", str(number), "--json", "headRefOid", "--jq", ".headRefOid"],
        cwd=cwd,
    )
    return result.stdout.strip()


def post_review(cwd: Path, number: int, body: str, inline: list[dict]) -> None:
    """Post a COMMENT review with optional inline comments (one API call).

    ``inline`` items are ``{path, line, body}``; the caller guarantees every
    line is a changed line of the PR diff — GitHub rejects review comments on
    unchanged lines with 422, so the filter lives with the diff parser.
    Payload goes to gh via stdin (JSON), never through a shell.
    """
    import json

    slug = repo_slug(cwd)
    payload: dict = {"event": "COMMENT", "body": body}
    if inline:
        payload["commit_id"] = pr_head_sha(cwd, number)
        payload["comments"] = [
            {"path": c["path"], "line": int(c["line"]), "body": c["body"]} for c in inline
        ]
    _run(
        ["gh", "api", f"repos/{slug}/pulls/{number}/reviews", "--input", "-"],
        cwd=cwd, input_text=json.dumps(payload, ensure_ascii=False),
    )


def latest_audit_issue(cwd: Path, label: str) -> tuple[int, str] | None:
    """The most recent closed-or-open issue carrying ``label``, or None."""
    result = _run(
        ["gh", "issue", "list", "--label", label, "--state", "all",
         "--limit", "1", "--json", "number,body"],
        cwd=cwd,
    )
    import json as _json

    try:
        items = _json.loads(result.stdout or "[]")
    except ValueError:
        return None
    if not items:
        return None
    first = items[0]
    return int(first["number"]), str(first.get("body") or "")


def create_issue(cwd: Path, *, title: str, body: str, label: str, color: str) -> int:
    _run(["gh", "label", "create", label, "--color", color, "--force"], cwd=cwd, check=False)
    result = _run(["gh", "issue", "create", "--title", title, "--body", body,
                   "--label", label], cwd=cwd)
    for token in reversed(result.stdout.strip().split("/")):
        if token.isdigit():
            return int(token)
    raise GitError(f"could not parse issue number from gh output: {result.stdout!r}")
