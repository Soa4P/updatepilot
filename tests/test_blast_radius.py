"""Blast-radius ordering: the ceiling spends its slots on the widest surface.

``max_prs_per_run`` makes the plan's order behaviour rather than presentation —
whatever sorts first is what the run actually opens. Before this, the order was
manifest order, so a dependency pinned three lines earlier won the slot. These
tests pin the measure, its deliberate exclusions (lock files, vendored trees), and
its effect on both isolated majors and the grouped routine PRs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot.blast import blast_radii
from updatepilot.config import BotConfig, EcosystemConfig, ReviewConfig
from updatepilot.planner import plan_scan


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "requirements.txt").write_text(
        "requests==2.31.0\nclick==8.1.0\n", encoding="utf-8"
    )
    return tmp_path


def test_mentions_are_counted_per_file_not_per_occurrence(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "src" / "a.py").write_text("import requests\nrequests.get('x')\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("from requests import Session\n", encoding="utf-8")
    (root / "src" / "c.py").write_text("import click\n", encoding="utf-8")

    radii = blast_radii(root, {"requests", "click"})
    # Two source files plus the manifest that pins it (which names every dependency,
    # so it contributes the same +1 to each — an offset, not a ranking distortion).
    assert radii["requests"] == 3, "two files with three mentions still counts as two"
    assert radii["click"] == 2


def test_lockfiles_and_vendored_trees_are_ignored(tmp_path: Path) -> None:
    """A lock file names every dependency; counting it would flatten the ranking."""
    root = _repo(tmp_path)
    (root / "src" / "a.py").write_text("import requests\n", encoding="utf-8")
    (root / "requirements.lock").write_text("requests==2.31.0\nclick==8.1.0\n", encoding="utf-8")
    vendor = root / "node_modules" / "pkg"
    vendor.mkdir(parents=True)
    (vendor / "index.js").write_text("require('requests')", encoding="utf-8")

    radii = blast_radii(root, {"requests", "click"})
    assert radii["requests"] == 2, "source file + requirements.txt, not the lock or vendor tree"
    assert radii["click"] == 1, "requirements.txt only"


def test_action_slugs_match_their_bare_name(tmp_path: Path) -> None:
    """A workflow writes ``owner/action``; code and prose write ``action``."""
    root = _repo(tmp_path)
    (root / "src" / "ci.py").write_text("# setup-python is pinned for speed\n", encoding="utf-8")
    radii = blast_radii(root, {"actions/setup-python"})
    assert radii["actions/setup-python"] == 1


def test_cargo_dash_and_underscore_spellings_both_count(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "src" / "a.rs").write_text("use serde_json::Value;\n", encoding="utf-8")
    radii = blast_radii(root, {"serde-json"})
    assert radii["serde-json"] == 1


def test_binary_files_are_skipped(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "src" / "blob.bin").write_bytes(b"\x00\x01requests\x00")
    assert blast_radii(root, {"requests"})["requests"] == 1  # requirements.txt only


# ------------------------------------------------------------- effect on the plan


def _config(max_prs: int) -> BotConfig:
    return BotConfig(
        ecosystems={
            "pip": EcosystemConfig(
                manifests=["requirements.txt"], allow_major=True, group_minor_patch=False
            ),
        },
        review=ReviewConfig(),
        max_prs_per_run=max_prs,
    )


def test_ceiling_keeps_the_widest_major_first(tmp_path: Path) -> None:
    """With one slot, the dependency used in more files gets it — not the first one
    listed in the manifest."""
    root = _repo(tmp_path)
    # `click` is listed second but is referenced from three files; `requests` from one.
    for index in range(3):
        (root / "src" / f"c{index}.py").write_text("import click\n", encoding="utf-8")
    (root / "src" / "r.py").write_text("import requests\n", encoding="utf-8")

    lookup = {"pip:requests": "3.0.0", "pip:click": "9.0.0"}
    report = plan_scan(root, _config(max_prs=1), lookup)

    assert len(report.planned) == 1
    assert report.planned[0].updates[0].pin.name == "click"
    assert report.planned[0].updates[0].radius == 4  # 3 source files + requirements.txt
    assert any("requests" in line for line in report.deferred)


def test_radius_is_reported_in_the_pr_body(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "src" / "r.py").write_text("import requests\n", encoding="utf-8")
    report = plan_scan(root, _config(max_prs=5), {"pip:requests": "3.0.0", "pip:click": None})
    body = "\n".join(report.planned[0].body_lines)
    assert "影响面" in body and "2 个文件" in body


def test_grouped_pr_lists_widest_first(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    for index in range(2):
        (root / "src" / f"c{index}.py").write_text("import click\n", encoding="utf-8")
    (root / "src" / "r.py").write_text("import requests\n", encoding="utf-8")

    config = _config(max_prs=5)
    config.ecosystems["pip"].group_minor_patch = True
    report = plan_scan(
        root, config, {"pip:requests": "2.32.0", "pip:click": "8.1.8"}
    )
    assert [u.pin.name for u in report.planned[0].updates] == ["click", "requests"]
