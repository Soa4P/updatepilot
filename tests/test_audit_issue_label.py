"""The audit issue's label is load-bearing: dedup looks the previous issue up BY it.

Audit S-UP-13, in two halves:

* `gh label create` ran with `check=False`, so a missing `issues: write` permission
  surfaced only as a baffling parse error from the *create* call. The reason is now
  printed before the louder failure.
* An issue that lands without its label is invisible to `latest_audit_issue` — dedup
  would silently degrade to "everything is new" on every run. The label is verified
  after creation and attached explicitly; an unattachable label fails the run.

These tests drive `create_issue` through a fake `_run`, asserting on the command
sequences rather than on gh itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot import gh
from updatepilot.gh import GitError

CREATE_URL = "https://github.example/owner/repo/issues/77"


def _fake_run(monkeypatch, responses: dict[tuple[str, ...], subprocess.CompletedProcess]):
    calls: list[list[str]] = []

    def run(args, *, cwd, check=True, input_text=None):
        calls.append(args)
        key = tuple(args[:3])
        result = responses.get(key)
        if result is None:
            result = subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if check and result.returncode != 0:
            raise gh.GitError(f"{args[0]} failed: {result.stderr.strip()[:200]}")
        return result

    monkeypatch.setattr(gh, "_run", run)
    return calls


def test_a_failed_label_create_is_reported_before_the_louder_failure(
    monkeypatch, tmp_path, capsys
) -> None:
    """The swallow hid *why* the next step would fail."""
    calls = _fake_run(
        monkeypatch,
        {
            ("gh", "label", "create"): subprocess.CompletedProcess(
                [], 1, stdout="", stderr="must have issues:write"
            ),
            ("gh", "issue", "create"): subprocess.CompletedProcess(
                [], 0, stdout=CREATE_URL, stderr=""
            ),
            ("gh", "issue", "view"): subprocess.CompletedProcess(
                [], 0, stdout='["updatepilot:audit"]', stderr=""
            ),
        },
    )

    number = gh.create_issue(
        tmp_path, title="t", body="b", label="updatepilot:audit", color="FBCA04"
    )

    assert number == 77
    assert "::warning::label create failed" in capsys.readouterr().out
    assert any(args[0] == "gh" and args[1] == "label" for args in calls)


def test_a_missing_label_is_attached_after_creation(monkeypatch, tmp_path) -> None:
    """An unlabelled issue is invisible to the next run's dedup lookup."""
    calls = _fake_run(
        monkeypatch,
        {
            ("gh", "issue", "create"): subprocess.CompletedProcess(
                [], 0, stdout=CREATE_URL, stderr=""
            ),
            # The created issue carries no label (creation with a missing label is
            # exactly the case gh can produce).
            ("gh", "issue", "view"): subprocess.CompletedProcess(
                [], 0, stdout="[]", stderr=""
            ),
        },
    )

    number = gh.create_issue(
        tmp_path, title="t", body="b", label="updatepilot:audit", color="FBCA04"
    )

    assert number == 77
    edits = [args for args in calls if args[:2] == ["gh", "issue"] and "edit" in args]
    assert edits, "未带标签的 issue 必须补挂标签"


def test_an_unattachable_label_fails_the_run(monkeypatch, tmp_path) -> None:
    """Silent degradation is the failure mode; loud is the only acceptable shape."""
    _fake_run(
        monkeypatch,
        {
            ("gh", "issue", "create"): subprocess.CompletedProcess(
                [], 0, stdout=CREATE_URL, stderr=""
            ),
            ("gh", "issue", "view"): subprocess.CompletedProcess(
                [], 0, stdout="[]", stderr=""
            ),
            ("gh", "issue", "edit"): subprocess.CompletedProcess(
                [], 1, stdout="", stderr="403"
            ),
        },
    )

    with pytest.raises(GitError):
        gh.create_issue(
            tmp_path, title="t", body="b", label="updatepilot:audit", color="FBCA04"
        )


def test_an_already_labelled_issue_needs_no_edit(monkeypatch, tmp_path) -> None:
    calls = _fake_run(
        monkeypatch,
        {
            ("gh", "issue", "create"): subprocess.CompletedProcess(
                [], 0, stdout=CREATE_URL, stderr=""
            ),
            ("gh", "issue", "view"): subprocess.CompletedProcess(
                [], 0, stdout='["updatepilot:audit"]', stderr=""
            ),
        },
    )

    assert gh.create_issue(
        tmp_path, title="t", body="b", label="updatepilot:audit", color="FBCA04"
    ) == 77
    assert not [
        args for args in calls if args[:2] == ["gh", "issue"] and "edit" in args
    ]
