"""`mode: review` must run the *base* commit's code, not the PR's (S-UP-05).

`pull_request_target` exists so a fork PR can reach a secret; it is also the trigger that
lets the PR author decide what runs with that secret. The dangerous configuration is
subtle and reads fine: `actions/checkout` with no `ref` checks out the merge commit of the
PR — i.e. the contributor's code, and their `.github/updatepilot.yml`, which the action
then loads and trusts.

Two things pin it: the recipe in the README, and a guard in the action that refuses to run
`review` when the workspace HEAD *is* the PR head.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _run_script() -> str:
    steps = ACTION["runs"]["steps"]
    for step in steps:
        run = step.get("run") or ""
        if "updatepilot" in run and "UPDATEPILOT_MODE" in run:
            return run
    raise AssertionError("找不到执行 updatepilot 的 step")


def test_the_action_receives_the_pr_head_sha() -> None:
    env = None
    for step in ACTION["runs"]["steps"]:
        if "UPDATEPILOT_MODE" in (step.get("env") or {}):
            env = step["env"]
    assert env is not None
    assert "PR_HEAD_SHA" in env, "没有把 PR head 传进来，守卫无从比较"


def test_review_mode_refuses_to_run_the_prs_own_code() -> None:
    script = _run_script()
    assert 'rev-parse HEAD' in script
    assert '"$head_now" = "$PR_HEAD_SHA"' in script
    assert "exit 1" in script
    # Only review mode: scan/audit legitimately run from the default branch.
    assert '"$UPDATEPILOT_MODE" = "review"' in script


def test_the_readme_recipe_pins_the_base_commit() -> None:
    """The copy-pasteable recipe is the thing people actually deploy."""
    assert "pull_request_target" in README
    section = README[README.index("pull_request_target") :][:1800]
    assert "github.event.pull_request.base.sha" in section, "配方没有钉 base，等于照抄一份不安全的配置"
    assert "contents: read" in section, "review 模式不需要写仓库权限"
