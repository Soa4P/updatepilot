"""cargo ecosystem support: manifest reader, version policy, registry wiring.

cargo is not "npm with a different filename", and the three places that differ are
pinned here:

* ``Cargo.toml`` has several syntactically different dependency forms (bare string,
  inline table, sub-table, per-target table) and several non-registry forms
  (``path``, ``git``, ``workspace = true``) that must not be bumped;
* under ``0.x`` a *minor* bump is breaking (semver §4, and cargo's own caret rule),
  so it has to route through the major path instead of a routine group;
* a ``Cargo.toml`` version is a requirement, not a resolution — merging the PR
  changes nothing until ``Cargo.lock`` is refreshed, and the PR body has to say so.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot.config import BotConfig, EcosystemConfig, ReviewConfig
from updatepilot.manifests import read_cargo_pins, read_manifests
from updatepilot.planner import plan_scan
from updatepilot.versions import classify_update

CARGO_TOML = """\
[package]
name = "demo"
version = "0.1.0"

[dependencies]
serde = "1.0.200"
tokio = { version = "1.38.0", features = ["full"] }
local-crate = { path = "../local" }
git-crate = { git = "https://example.com/x.git" }
loose = { version = "1", features = [] }
wildcard = "*"

[dev-dependencies]
tempfile = "=3.10.1"

[dependencies.regex]
version = "1.10.4"

[target.'cfg(unix)'.dependencies]
libc = "0.2.155"

[workspace.dependencies]
anyhow = "1.0.86"
"""


def _write_cargo(root: Path, text: str = CARGO_TOML) -> None:
    (root / "Cargo.toml").write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ manifest reader


def test_cargo_forms_are_parsed_and_rewritten_in_place(tmp_path: Path) -> None:
    _write_cargo(tmp_path)
    pins = {pin.name: pin for pin in read_cargo_pins(tmp_path, "Cargo.toml")}

    assert set(pins) == {"serde", "tokio", "tempfile", "regex", "libc", "anyhow"}
    # Each render must change the version text and nothing else: formatting,
    # features and the exact-pin "=" all survive.
    assert pins["serde"].render("1.0.219") == 'serde = "1.0.219"'
    assert pins["tokio"].render("1.44.0") == 'tokio = { version = "1.44.0", features = ["full"] }'
    assert pins["tempfile"].render("3.11.0") == 'tempfile = "=3.11.0"'
    assert pins["regex"].render("1.11.0") == 'version = "1.11.0"'
    assert pins["libc"].render("0.2.160") == 'libc = "0.2.160"'
    assert pins["anyhow"].render("1.0.90") == 'anyhow = "1.0.90"'


def test_path_git_workspace_and_loose_requirements_are_skipped(tmp_path: Path) -> None:
    """Only a comparable, rewritable version is a pin.

    ``path``/``git``/``workspace = true`` have no registry version to query, and
    ``"1"`` / ``"*"`` carry no patch level — rewriting them would be guessing.
    """
    _write_cargo(tmp_path)
    names = {pin.name for pin in read_cargo_pins(tmp_path, "Cargo.toml")}
    assert not names & {"local-crate", "git-crate", "loose", "wildcard"}


def test_manifest_dispatch_uses_the_cargo_reader(tmp_path: Path) -> None:
    _write_cargo(tmp_path)
    pins = read_manifests(tmp_path, "cargo", ["Cargo.toml"])
    assert {pin.kind for pin in pins} == {"cargo"}
    assert read_manifests(tmp_path, "cargo", ["missing.toml"]) == []


# ----------------------------------------------------------------- version policy


def test_cargo_semver_levels() -> None:
    assert classify_update("1.2.3", "1.2.4", "cargo")[0] == "patch"
    assert classify_update("1.2.3", "1.3.0", "cargo")[0] == "minor"
    assert classify_update("1.2.3", "2.0.0", "cargo")[0] == "major"


def test_a_zero_major_minor_bump_is_breaking() -> None:
    """0.8 → 0.9 can break; grouping it as "minor" skips the changelog read."""
    level, _cur, _new = classify_update("0.8.5", "0.9.0", "cargo")
    assert level == "major"
    # …while 0.8.5 → 0.8.6 stays an ordinary patch.
    assert classify_update("0.8.5", "0.8.6", "cargo")[0] == "patch"


def test_prereleases_are_not_updates() -> None:
    assert classify_update("1.2.3", "1.3.0-rc.1", "cargo") is None
    assert classify_update("1.2.3", "1.2.3", "cargo") is None


# ------------------------------------------------------------------- planning


def _cargo_only_config(*, allow_major: bool = False) -> BotConfig:
    return BotConfig(
        ecosystems={
            "cargo": EcosystemConfig(
                manifests=["Cargo.toml"], allow_major=allow_major, group_minor_patch=True
            ),
        },
        review=ReviewConfig(),
        max_prs_per_run=5,
    )


def test_cargo_group_pr_carries_the_lockfile_instruction(tmp_path: Path) -> None:
    _write_cargo(tmp_path)
    report = plan_scan(
        tmp_path,
        _cargo_only_config(),
        {"cargo:serde": "1.0.219", "cargo:tokio": "1.44.0", "cargo:regex": "1.11.0"},
    )
    assert len(report.planned) == 1
    pr = report.planned[0]
    assert pr.ecosystem == "cargo"
    body = "\n".join(pr.body_lines)
    assert "cargo update" in body and "Cargo.lock" in body
    assert pr.edits["Cargo.toml"] == {
        6: 'serde = "1.0.219"',
        7: 'tokio = { version = "1.44.0", features = ["full"] }',
        17: 'version = "1.11.0"',
    }


def test_zero_major_bump_needs_allow_major(tmp_path: Path) -> None:
    """0.2.155 → 0.3.0 is a major by cargo policy, so the default config drops it."""
    _write_cargo(tmp_path)
    lookup = {"cargo:libc": "0.3.0"}
    refused = plan_scan(tmp_path, _cargo_only_config(), lookup)
    assert refused.planned == []
    assert any("major" in line for line in refused.skipped)

    allowed = plan_scan(tmp_path, _cargo_only_config(allow_major=True), lookup)
    assert len(allowed.planned) == 1
    assert "0.3.0" in allowed.planned[0].title
