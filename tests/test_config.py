"""Config parsing: unknown keys fail loudly, secrets stay env-var references."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from updatepilot.config import ConfigError, load_config


def test_missing_config_file_yields_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.ecosystems["pip"].enabled
    assert config.ecosystems["github_actions"].allow_major is True
    assert config.review.api_key_env == "AI_REVIEW_API_KEY"


def test_unknown_keys_fail_loudly(tmp_path: Path) -> None:
    """A typo'd key must refuse the run, not silently keep defaults."""
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "updatepilot.yml").write_text(
        "ecosystems:\n  pip:\n    critcial_paths: [x]\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="critcial_paths"):
        load_config(tmp_path)


def test_unsupported_ecosystem_is_an_error(tmp_path: Path) -> None:
    """A typo'd or unimplemented ecosystem must not look like "nothing to update"."""
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "updatepilot.yml").write_text(
        "ecosystems:\n  composer: {enabled: true}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="不支持"):
        load_config(tmp_path)


def test_cargo_is_a_supported_ecosystem_now(tmp_path: Path) -> None:
    """cargo moved off the roadmap: it has a manifest reader and a registry.

    Pinned because the failure modes are quiet — if ``SUPPORTED_ECOSYSTEMS`` ever
    loses it, every adopter's ``ecosystems.cargo`` block becomes a config error, and
    if the default manifest regressed, the scan would silently find no pins.
    """
    config = load_config(tmp_path)  # defaults only, no file at all
    assert "cargo" in config.enabled_ecosystems
    assert config.ecosystems["cargo"].manifests == ["Cargo.toml"]

    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "updatepilot.yml").write_text(
        "ecosystems:\n  cargo: {allow_major: true}\n", encoding="utf-8"
    )
    assert load_config(tmp_path).ecosystems["cargo"].allow_major is True


def test_disable_an_ecosystem_with_false(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "updatepilot.yml").write_text(
        "ecosystems:\n  pip: false\n  npm: false\n  cargo: false\n", encoding="utf-8"
    )
    config = load_config(tmp_path)
    assert config.ecosystems["pip"].enabled is False
    assert config.enabled_ecosystems == ["github_actions"]  # npm and cargo disabled above


def test_api_key_is_read_from_the_env_var_name(tmp_path: Path, monkeypatch) -> None:
    """The key value lives in the environment; the YAML only ever names it."""
    from updatepilot.config import resolve_api_key

    config = load_config(tmp_path)
    monkeypatch.delenv("AI_REVIEW_API_KEY", raising=False)
    assert resolve_api_key(config.review) == ""
    monkeypatch.setenv("AI_REVIEW_API_KEY", " sk-test-123 ")
    assert resolve_api_key(config.review) == "sk-test-123"  # 去空白：BOM/尾随空格是常见事故
