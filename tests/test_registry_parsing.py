"""Registries: the parsing that decides "is there a newer version".

The action lookup had a bug that no test could see, because the tests fed the planner a
ready-made lookup dict and the registry function was never exercised against the shape
GitHub actually returns. ``GET /repos/{o}/{r}/tags`` answers ``{"name": "v7.0.1"}`` — it
has no ``ref`` field. The code read ``item["ref"]``, got ``""`` for every tag, and
returned ``None``: **action updates never happened**, and the scan reported them as
"没有可用的新版本信息" (indistinguishable from being up to date).

These tests parse a real response shape so the same mistake cannot come back.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from updatepilot import registries
from updatepilot.registries import RegistryError, github_action_latest


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class _FakeOpener:
    def __init__(self, payload):
        self._payload = payload

    def open(self, _request, timeout=None):  # signature mirror
        return _FakeResponse(json.dumps(self._payload).encode())


def _patch(monkeypatch, payload):
    monkeypatch.setattr(
        registries.urllib.request, "build_opener", lambda *_a, **_k: _FakeOpener(payload)
    )
    # The host-resolution guard runs before the request; keep it out of the unit test.
    monkeypatch.setattr(registries, "_assert_routable_hostname", lambda _host: None)


def test_tags_endpoint_shape_is_parsed(monkeypatch) -> None:
    """The real shape: ``name`` only, no ``ref``."""
    _patch(
        monkeypatch,
        [
            {"name": "v6.0.1", "commit": {"sha": "a"}},
            {"name": "v7.0.1", "commit": {"sha": "b"}},
            {"name": "v5.2.0", "commit": {"sha": "c"}},
        ],
    )
    assert github_action_latest("actions/checkout", None) == "v7"


def test_git_refs_shape_is_still_accepted(monkeypatch) -> None:
    """``/git/refs/tags`` answers ``ref``; accepting both keeps the parser honest if the
    endpoint is ever switched."""
    _patch(
        monkeypatch,
        [
            {"ref": "refs/tags/v3"},
            {"ref": "refs/tags/v4"},
        ],
    )
    assert github_action_latest("owner/action", None) == "v4"


def test_non_release_tags_do_not_count(monkeypatch) -> None:
    """A trailing number in a *suffixed* tag is not a major.

    Measured: ``v3.2.2-node20`` (a node-runtime tag) was read as major 20 by a trailing
    ``search``, and the bot opened "upload-artifact v7 → v20" on a real repository.
    """
    _patch(
        monkeypatch,
        [
            {"name": "nightly"},
            {"name": "v2.0.0-rc1"},
            {"name": "v3.2.2-node20"},
            {"name": "v1.9"},
        ],
    )
    assert github_action_latest("owner/action", None) == "v1"


def test_a_malformed_body_is_no_answer(monkeypatch) -> None:
    _patch(monkeypatch, {"message": "Not Found"})
    assert github_action_latest("owner/action", None) is None


def test_a_slug_without_repo_is_an_error() -> None:
    with pytest.raises(RegistryError):
        github_action_latest("checkout", None)


# ------------------------------------------------------------- dry-run safety switch


def test_dry_run_env_is_honoured_without_a_config_file(tmp_path: Path, monkeypatch) -> None:
    """``UPDATEPILOT_DRY_RUN`` must survive the *default* path.

    It used to be read only where a ``.github/updatepilot.yml`` existed. An adopter with
    no config file — the common first run — set the variable, got ``dry_run=False``, and
    the "dry run" opened real PRs (measured: two on Soap4P/soapbot).
    """
    from updatepilot.config import load_config

    monkeypatch.setenv("UPDATEPILOT_DRY_RUN", "1")
    assert load_config(tmp_path).dry_run is True  # no config file at all

    config_dir = tmp_path / ".github"
    config_dir.mkdir()
    (config_dir / "updatepilot.yml").write_text("version: 1\n", encoding="utf-8")
    assert load_config(tmp_path).dry_run is True  # with one


@pytest.mark.parametrize("value", ["1", "true", "yes", "YES", "on", "anything"])
def test_dry_run_is_on_for_anything_not_clearly_off(value, tmp_path, monkeypatch) -> None:
    """A typo must not silently enable PR creation."""
    from updatepilot.config import load_config

    monkeypatch.setenv("UPDATEPILOT_DRY_RUN", value)
    assert load_config(tmp_path).dry_run is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_dry_run_is_off_only_for_explicit_off(value, tmp_path, monkeypatch) -> None:
    from updatepilot.config import load_config

    monkeypatch.setenv("UPDATEPILOT_DRY_RUN", value)
    assert load_config(tmp_path).dry_run is False
