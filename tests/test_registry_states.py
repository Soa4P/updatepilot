"""Three registry states must not collapse into one line.

Before this, ``latest is None`` meant all of:

* the registry has no such package (404) — nothing to do, and it is honest to say so;
* the lookup **failed** (network, rate limit, non-JSON) — we do not know anything;
* it answered and everything is current — reported by *absence* of a line.

The third is fine; the first two were both rendered as "没有可用的新版本信息", which reads
like "up to date". That is precisely how action bumps silently never happened: the scan
said nothing was available and nobody could tell the API call had been failing.

"A dependency we could not check" is an error, not a skip — a scan that can see nothing
must not report success.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot.config import BotConfig, EcosystemConfig, ReviewConfig
from updatepilot.planner import plan_scan


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text(
        "requests==2.31.0\nlocal-only==1.0.0\n", encoding="utf-8"
    )
    return tmp_path


def _config() -> BotConfig:
    return BotConfig(
        ecosystems={"pip": EcosystemConfig(manifests=["requirements.txt"])},
        review=ReviewConfig(),
        max_prs_per_run=5,
    )


def test_a_failed_lookup_is_an_error_not_a_skip(tmp_path: Path) -> None:
    report = plan_scan(
        _repo(tmp_path),
        _config(),
        {
            "pip:requests": None,
            "pip:requests:error": "GitHub API rate limited",
            "pip:local-only": None,  # 404: the registry answered, and has no such package
        },
    )
    assert any("查询失败" in line and "rate limited" in line for line in report.errors), report.errors
    assert any("注册表里没有这个包" in line for line in report.skipped), report.skipped
    # The two must never be conflated.
    assert not any("requests" in line for line in report.skipped)


def test_an_up_to_date_package_produces_no_line_at_all(tmp_path: Path) -> None:
    report = plan_scan(
        _repo(tmp_path),
        _config(),
        {"pip:requests": "2.31.0", "pip:local-only": "1.0.0"},
    )
    assert report.planned == []
    assert report.skipped == []
    assert report.errors == []


def test_the_summary_warns_when_something_could_not_be_checked(tmp_path: Path, capsys) -> None:
    """The line a human reads when skimming a scheduled run."""
    from updatepilot.cli import _render_report

    report = plan_scan(
        _repo(tmp_path),
        _config(),
        {"pip:requests": None, "pip:requests:error": "timed out", "pip:local-only": "1.0.0"},
    )
    _render_report(report)
    out = capsys.readouterr().out
    assert "没查成" in out
    assert "timed out" in out
