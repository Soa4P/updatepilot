"""Manifest readers: exact pins only, spans that round-trip, SHA pins respected."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot.manifests import read_action_refs, read_pip_pins


def test_requirements_pins_are_read_with_line_numbers(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "# comment\n"
        "pytest==9.2.0  # testing\n"
        "\n"
        "requests[socks]==2.31.0\n",
        encoding="utf-8",
    )
    pins = read_pip_pins(tmp_path, "requirements.txt")
    assert [p.name for p in pins] == ["pytest", "requests"]
    assert pins[0].current == "9.2.0" and pins[0].line == 2
    assert pins[1].name == "requests"  # extras stripped from the name


def test_pin_spans_survive_a_trailing_comment(tmp_path: Path) -> None:
    """The placeholder must land on the pin, not on a comment repeating it."""
    (tmp_path / "requirements.txt").write_text(
        "pytest==9.2.0  # was 9.2.0\n", encoding="utf-8"
    )
    pin = read_pip_pins(tmp_path, "requirements.txt")[0]
    rendered = pin.render("9.3.0")
    assert rendered == "pytest==9.3.0  # was 9.2.0", rendered


def test_floating_requirements_are_not_tracked(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "pytest>=9.2.0\nrequests~=2.31\nflask\n", encoding="utf-8"
    )
    assert read_pip_pins(tmp_path, "requirements.txt") == []


def test_sha_pinned_actions_are_skipped_version_tags_kept(tmp_path: Path) -> None:
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
        "      - uses: actions/setup-python@v5\n",
        encoding="utf-8",
    )
    pins = read_action_refs(tmp_path, [".github/workflows"])
    assert [p.current for p in pins] == ["v5"]
    assert pins[0].name == "actions/setup-python"
