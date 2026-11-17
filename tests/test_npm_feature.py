"""v1.1.0 features: npm ecosystem + changelog excerpts + config containment.

Each test is a behaviour from the feature list, written red→green per the tdd
discipline; the semver edge cases (prerelease ordering, build metadata, v-prefix)
are pinned because npm is the ecosystem where hand-rolled comparison goes wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot.config import ConfigError, load_config
from updatepilot.manifests import read_npm_pins
from updatepilot.versions import VersionInfo, classify_update

# ------------------------------------------------------------------ npm manifest
_PACKAGE_JSON = """{
  "name": "myapp",
  "version": "1.0.0",
  "dependencies": {
    "react": "18.2.0",
    "axios": "^1.6.0",
    "lodash": "4.17.21-beta.1",
    "zod": "3.22.4"
  },
  "devDependencies": {
    "typescript": "5.5.4",
    "vitest": "*"
  },
  "engines": {
    "node": "18.2.0"
  }
}
"""


def test_npm_reads_exact_pins_inside_dependency_blocks(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(_PACKAGE_JSON, encoding="utf-8")
    pins = read_npm_pins(tmp_path, "package.json")
    names = {p.name for p in pins}
    assert names == {"react", "lodash", "zod", "typescript"}
    # 范围与通配不跟踪（^/*）；顶层 "version": "1.0.0" 与 engines.node 不在依赖块内
    assert all(p.current == "18.2.0" or p.current in ("4.17.21-beta.1", "3.22.4", "5.5.4")
               for p in pins)
    react = next(p for p in pins if p.name == "react")
    assert react.line == 5
    assert react.render("18.3.1") == '    "react": "18.3.1",'


# ------------------------------------------------------------------ semver
def test_npm_semver_classification_and_prerelease_ordering() -> None:
    assert classify_update("18.2.0", "18.3.1", "npm")[0] == "minor"
    assert classify_update("18.2.0", "19.0.0", "npm")[0] == "major"
    assert classify_update("18.2.0", "18.2.1", "npm")[0] == "patch"
    # semver 规则 11：release > 同三元组的 prerelease
    assert classify_update("1.0.0-beta.1", "1.0.0", "npm") is not None
    # prerelease 永远不算更新
    assert classify_update("1.0.0", "1.1.0-beta.2", "npm") is None
    # build metadata 不参与优先级
    assert classify_update("1.0.0+build.1", "1.0.0", "npm") is None
    # v 前缀容忍
    assert VersionInfo.parse("v18.2.0", "npm") == VersionInfo.parse("18.2.0", "npm")
    # 范围形状不是版本
    assert VersionInfo.parse("^18.2.0", "npm") is None


# ------------------------------------------------------------------ changelog
def _report_with(updates):
    from updatepilot.planner import PlannedPR, ScanReport

    pr = PlannedPR(ecosystem="npm", branch="b", title="t", body_lines=["base"], updates=updates)
    return ScanReport(planned=[pr], deferred=[], skipped=[], errors=[])


def test_attach_notes_only_uses_github_hosted_repositories() -> None:
    from updatepilot.manifests import PinnedDependency
    from updatepilot.planner import Update, attach_release_notes

    pin = PinnedDependency(ecosystem="npm", name="react", current="18.2.0",
                           file="package.json", line=1, kind="npm", replace_span=("x", "x"))
    report = _report_with([Update(pin=pin, latest="18.3.1", level="minor")])
    with mock.patch("updatepilot.registries.npm_repository_slug", return_value="facebook/react"), \
         mock.patch("updatepilot.registries.github_latest_release_notes",
                    return_value="### Bug fixes\n- thing") as notes:
        attach_release_notes(report, github_token=None)
    notes.assert_called_once_with("facebook/react", None)
    body = "\n".join(report.planned[0].body_lines)
    assert "### Bug fixes" in body


def test_attach_notes_skips_non_github_repositories() -> None:
    from updatepilot.manifests import PinnedDependency
    from updatepilot.planner import Update, attach_release_notes

    pin = PinnedDependency(ecosystem="npm", name="private-pkg", current="1.0.0",
                           file="package.json", line=1, kind="npm", replace_span=("x", "x"))
    report = _report_with([Update(pin=pin, latest="1.1.0", level="minor")])
    with mock.patch("updatepilot.registries.npm_repository_slug", return_value=None):
        attach_release_notes(report, github_token=None)
    body = "\n".join(report.planned[0].body_lines)
    assert "上游发布说明" not in body
    assert "npm install" in body  # lockfile 提醒仍在


def test_npm_pr_body_always_carries_the_lockfile_note(tmp_path: Path) -> None:
    """npm bumps touch the manifest only — the body must say who refreshes the lock."""
    from updatepilot.config import BotConfig, EcosystemConfig, ReviewConfig
    from updatepilot.planner import plan_scan

    (tmp_path / "package.json").write_text(_PACKAGE_JSON, encoding="utf-8")
    config = BotConfig(
        ecosystems={"npm": EcosystemConfig(manifests=["package.json"])},
        review=ReviewConfig(), max_prs_per_run=5,
    )
    lookup = {"npm:react": "18.3.1", "npm:zod": "3.22.5"}
    report = plan_scan(tmp_path, config, lookup)
    body = "\n".join(report.planned[0].body_lines)
    assert "npm install" in body


# ------------------------------------------------------------------ config containment
def test_config_rejects_escape_paths_at_load_time(tmp_path: Path) -> None:
    """Audit P2-1: absolute or .. manifest paths must fail at parse, not later."""
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "updatepilot.yml").write_text(
        "ecosystems:\n  pip:\n    manifests: [/etc/requirements.txt]\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="仓库根内"):
        load_config(tmp_path)
    (tmp_path / ".github" / "updatepilot.yml").write_text(
        "ecosystems:\n  pip:\n    manifests: [../../etc/requirements.txt]\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="仓库根内"):
        load_config(tmp_path)


def test_npm_is_now_a_supported_ecosystem(tmp_path: Path) -> None:
    config = load_config(tmp_path)  # 无配置文件 → 默认值
    assert "npm" in config.enabled_ecosystems
    assert config.ecosystems["npm"].manifests == ["package.json"]


import pytest
