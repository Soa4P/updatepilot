"""The policy engine: pins + registry answers → planned PRs.

Pinned here is what makes UpdatePilot different from a dumb "bump everything"
bot: majors are isolated, ignored names win, downgrades never happen, the
per-run ceiling defers honestly (into the report, not into silence), and every
planned PR carries its own exact edits.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot.config import BotConfig, EcosystemConfig, ReviewConfig
from updatepilot.manifests import PinnedDependency
from updatepilot.planner import plan_scan


def _pin(name: str, current: str, *, eco: str = "pip", file: str = "requirements.txt",
         line: int = 1, kind: str = "pep440") -> PinnedDependency:
    return PinnedDependency(
        ecosystem=eco, name=name, current=current, file=file, line=line, kind=kind,
        replace_span=(f"{name}=={current}", f"{name}=={{pin}}"),
    )


def _config(**eco_overrides) -> BotConfig:
    return BotConfig(
        ecosystems={
            "pip": EcosystemConfig(
                manifests=["requirements.txt"], group_minor_patch=True, **eco_overrides
            ),
        },
        review=ReviewConfig(),
        max_prs_per_run=5,
    )


def _lookup(*pairs: tuple[str, str | None]) -> dict[str, str | None]:
    return {f"pip:{name}": version for name, version in pairs}


def test_minor_and_patch_group_into_one_pr(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "pytest==9.2.0\nrequests==2.31.0\n", encoding="utf-8"
    )
    report = plan_scan(tmp_path, _config(), _lookup(("pytest", "9.2.1"), ("requests", "2.32.0")))
    assert len(report.planned) == 1
    pr = report.planned[0]
    assert pr.ecosystem == "pip" and len(pr.updates) == 2
    assert pr.edits["requirements.txt"] == {1: "pytest==9.2.1", 2: "requests==2.32.0"}
    assert pr.title.startswith("chore(deps): group")


def test_majors_get_their_own_pr_and_are_dropped_when_disallowed(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest==9.2.0\n", encoding="utf-8")
    lookup = _lookup(("pytest", "10.0.0"))

    allowed = plan_scan(tmp_path, _config(allow_major=True), lookup)
    assert len(allowed.planned) == 1
    assert "10.0.0" in allowed.planned[0].title
    assert allowed.planned[0].edits["requirements.txt"] == {1: "pytest==10.0.0"}

    refused = plan_scan(tmp_path, _config(), lookup)
    assert refused.planned == []
    assert any("major" in line for line in refused.skipped)


def test_ignored_names_win_and_downgrades_are_dropped(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "pytest==9.2.0\nrequests==2.31.0\nflask==3.0.0\n", encoding="utf-8"
    )
    config = _config()
    config.ecosystems["pip"].ignore = ["flask"]
    report = plan_scan(
        tmp_path, config,
        _lookup(("pytest", "9.2.1"), ("requests", "2.30.0"), ("flask", "3.1.0")),
    )
    assert any("ignore" in line for line in report.skipped)  # flask 在名单
    assert not any(u.pin.name == "requests" for pr in report.planned for u in pr.updates)
    # pytest（patch）仍在计划里
    assert any(u.pin.name == "pytest" for pr in report.planned for u in pr.updates)


def test_ceiling_defers_into_the_report_not_silence(tmp_path: Path) -> None:
    names = [f"pkg{i}" for i in range(8)]
    (tmp_path / "requirements.txt").write_text(
        "\n".join(f"{n}==1.0.0" for n in names) + "\n", encoding="utf-8"
    )
    config = _config()
    config.max_prs_per_run = 2
    lookup = _lookup(*[(n, "1.0.1") for n in names])
    report = plan_scan(tmp_path, config, lookup)
    # 分组模式：8 个 minor/patch 打成 1 个 PR —— 上限不打它
    assert len(report.planned) == 1
    assert report.deferred == []
    # 关掉分组后，8 个 PR 上限 2 → 6 个进 deferred
    config.ecosystems["pip"].group_minor_patch = False
    split = plan_scan(tmp_path, config, lookup)
    assert len(split.planned) == 2
    assert len(split.deferred) == 6
    assert any("max_prs_per_run=2" in line for line in split.deferred)


def test_action_pins_plan_with_major_isolation(tmp_path: Path) -> None:
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(
        "jobs:\n  test:\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n",
        encoding="utf-8",
    )
    config = _config()
    config.ecosystems["pip"].enabled = False
    config.ecosystems["github_actions"] = EcosystemConfig(
        manifests=[".github/workflows"], allow_major=True, group_minor_patch=False,
    )
    lookup = {
        "github_actions:actions/checkout": "v4",
        "github_actions:actions/setup-python": "v7",
    }
    report = plan_scan(tmp_path, config, lookup)
    # checkout 已是最新（v4→v4 不动）；setup-python v5→v7 是 major，allow_major=True 开 PR
    assert len(report.planned) == 1
    assert report.planned[0].updates[0].pin.name == "actions/setup-python"
    assert report.planned[0].updates[0].latest == "v7"
    assert report.planned[0].edits[".github/workflows/ci.yml"] == {5: "      - uses: actions/setup-python@v7"}


def test_sha_pinned_actions_are_silently_left_alone(tmp_path: Path) -> None:
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(
        "jobs:\n  test:\n    steps:\n"
        "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n",
        encoding="utf-8",
    )
    config = _config()
    config.ecosystems["pip"].enabled = False
    config.ecosystems["github_actions"] = EcosystemConfig(manifests=[".github/workflows"])
    report = plan_scan(tmp_path, config, {"github_actions:actions/checkout": "v7"})
    assert report.planned == []  # SHA 钉住 = 作者选了不可变，bot 不碰
