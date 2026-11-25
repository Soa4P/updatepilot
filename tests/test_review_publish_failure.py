"""Publishing the verdict is the deliverable, so failing to publish fails the run.

Audit S-UP-04: `comment_on_pr` / `label_pr` errors were printed and swallowed. The job
stayed green, and a PR whose body says "pre-reviewed" had no comment and no label — the
one state a maintainer cannot detect, because the workflow is the only thing they check.

These tests pin the exit code, not the message: a green run must mean the verdict exists
on the PR.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot import cli
from updatepilot.gh import GitError


class _Config:
    """Only what `_review` reads before the publish step."""

    dry_run = False
    review = object()


def _patch_up_to_publish(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PR_NUMBER", "12")
    monkeypatch.setattr(cli, "load_config", lambda root: _Config())
    monkeypatch.setattr(cli, "resolve_api_key", lambda review: "k")
    monkeypatch.setattr(cli, "_review_options", lambda cfg, key: object())
    monkeypatch.setattr(cli, "_pr_diff_and_files", lambda root, number: ("+x", ["src/app.py"]))
    monkeypatch.setattr(cli, "_pr_tree", lambda root, number, dest: tmp_path)
    monkeypatch.setattr(
        cli,
        "review_target",
        lambda target, options: {
            "verdict": "safe",
            "risk": "low",
            "summary": "looks fine",
            "concerns": [],
            "degraded": False,
            "findings": [],
        },
    )


def test_failed_publish_exits_non_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    _patch_up_to_publish(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise GitError("gh pr comment failed (exit 1): HTTP 422")

    monkeypatch.setattr(cli.gh, "post_review", boom)

    assert cli._review(tmp_path) == 1
    out = capsys.readouterr().out
    # The verdict JSON is printed before the failure so the evidence survives the red run.
    assert '"published": false' in out
    assert "评论/打标失败" in out


def test_successful_publish_exits_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    _patch_up_to_publish(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.gh, "post_review", lambda *a, **k: None)
    monkeypatch.setattr(cli.gh, "label_pr", lambda *a, **k: None)

    assert cli._review(tmp_path) == 0
    assert '"published": true' in capsys.readouterr().out
