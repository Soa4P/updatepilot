"""Repository-side configuration (``.github/updatepilot.yml``) with sane defaults.

Everything an adopter might want to tune lives here; everything security-relevant
(model egress, review budgets) has a default that fails closed. The config file is
optional — the defaults below are the documented behaviour.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = ".github/updatepilot.yml"

#: ecosystems shipped in v1. npm and cargo are on the roadmap; unknown keys are
#: reported as errors rather than silently ignored (a typo'd ecosystem name must
#: not look like "nothing to update").
SUPPORTED_ECOSYSTEMS = ("pip", "github_actions")
ROADMAP_ECOSYSTEMS = ("npm", "cargo")

#: Per-run ceilings. The scan job holds the user's API key; these bounds exist so a
#: pathological manifest cannot turn into 500 registry calls or an open wallet.
MAX_PRS_PER_RUN = 5
MAX_REVIEW_CALLS = 10
MAX_REVIEW_GROUP_BYTES = 120_000
MAX_REVIEW_GROUP_FILES = 4
MAX_FILE_BYTES = 60_000
MAX_FILE_LINES = 2_000


class ConfigError(ValueError):
    """The config file is malformed; the run refuses instead of guessing."""


@dataclass
class EcosystemConfig:
    enabled: bool = True
    #: Manifest entry points, relative to repo root. Empty list = v1 defaults.
    manifests: list[str] = field(default_factory=list)
    #: Exact dependency names (or action slugs) that must never be bumped.
    ignore: list[str] = field(default_factory=list)
    #: Major bumps are isolated PRs that need a changelog read first; most repos
    #: keep this False and upgrade majors by hand.
    allow_major: bool = False
    #: Group minor+patch bumps of the same ecosystem into one PR.
    group_minor_patch: bool = True


@dataclass
class ReviewConfig:
    #: Any OpenAI-compatible chat/completions endpoint. The key comes from an env
    #: var name (not the value!) so secrets never sit in the committed YAML.
    model: str = "deepseek-chat"
    base_url_env: str = "AI_REVIEW_BASE_URL"
    default_base_url: str = "https://api.deepseek.com"
    api_key_env: str = "AI_REVIEW_API_KEY"
    max_tokens: int = 8000
    retry_max_tokens: int = 16000
    file_max_tokens: int = 6000
    file_retry_max_tokens: int = 12000
    max_calls: int = MAX_REVIEW_CALLS
    #: Path prefixes that get security-critical treatment (reviewed first, never
    #: skipped, and force needs_review when unread). The soapbot defaults were
    #: repo-specific; adopters declare their own.
    critical_paths: list[str] = field(default_factory=list)
    #: Generated/binary globs the reviewer should skip (extra to the built-ins).
    generated_prefixes: list[str] = field(default_factory=list)
    language: str = "zh"


@dataclass
class BotConfig:
    ecosystems: dict[str, EcosystemConfig]
    review: ReviewConfig
    max_prs_per_run: int = MAX_PRS_PER_RUN
    #: Bot PR titles use the conventional `chore(deps):` prefix so existing
    #: tooling (title gates, changelog tooling) keeps working.
    branch_prefix: str = "updatepilot"
    dry_run: bool = False

    @property
    def enabled_ecosystems(self) -> list[str]:
        # Only explicitly configured ecosystems run: a BotConfig built in tests
        # with a single ecosystem must not silently grow the other default.
        return [
            name for name in SUPPORTED_ECOSYSTEMS
            if (eco := self.ecosystems.get(name)) is not None and eco.enabled
        ]


def default_config() -> BotConfig:
    return BotConfig(
        ecosystems={
            "pip": EcosystemConfig(
                manifests=["requirements.txt", "requirements-dev.txt"],
            ),
            "github_actions": EcosystemConfig(
                allow_major=True,  # action majors are runtime updates; skipping them misses security fixes
                group_minor_patch=False,
                manifests=[".github/workflows"],
            ),
        },
        review=ReviewConfig(),
    )


def _merge_ecosystem(name: str, raw: Any, errors: list[str]) -> EcosystemConfig:
    base = default_config().ecosystems.get(name, EcosystemConfig())
    if raw is False:
        return EcosystemConfig(enabled=False, manifests=base.manifests)
    if raw is True or raw is None:
        return base
    if not isinstance(raw, dict):
        errors.append(f"ecosystems.{name}: 应是映射或 true/false，收到 {type(raw).__name__}")
        return base
    known = {"enabled", "manifests", "ignore", "allow_major", "group_minor_patch"}
    for key in raw:
        if key not in known:
            errors.append(f"ecosystems.{name}.{key}: 未知字段（可用：{', '.join(sorted(known))}）")
    merged = EcosystemConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        manifests=[str(m) for m in raw.get("manifests", base.manifests)],
        ignore=[str(i) for i in raw.get("ignore", base.ignore)],
        allow_major=bool(raw.get("allow_major", base.allow_major)),
        group_minor_patch=bool(raw.get("group_minor_patch", base.group_minor_patch)),
    )
    return merged


def _merge_review(raw: Any, errors: list[str]) -> ReviewConfig:
    base = ReviewConfig()
    if raw is None:
        return base
    if not isinstance(raw, dict):
        errors.append(f"review: 应是映射，收到 {type(raw).__name__}")
        return base
    known = {
        "model", "base_url_env", "default_base_url", "api_key_env", "max_tokens",
        "retry_max_tokens", "file_max_tokens", "file_retry_max_tokens", "max_calls",
        "critical_paths", "generated_prefixes", "language",
    }
    for key in raw:
        if key not in known:
            errors.append(f"review.{key}: 未知字段（可用：{', '.join(sorted(known))}）")
    cfg = ReviewConfig(
        model=str(raw.get("model", base.model)),
        base_url_env=str(raw.get("base_url_env", base.base_url_env)),
        default_base_url=str(raw.get("default_base_url", base.default_base_url)),
        api_key_env=str(raw.get("api_key_env", base.api_key_env)),
        max_tokens=int(raw.get("max_tokens", base.max_tokens)),
        retry_max_tokens=int(raw.get("retry_max_tokens", base.retry_max_tokens)),
        file_max_tokens=int(raw.get("file_max_tokens", base.file_max_tokens)),
        file_retry_max_tokens=int(raw.get("file_retry_max_tokens", base.file_retry_max_tokens)),
        max_calls=int(raw.get("max_calls", base.max_calls)),
        critical_paths=[str(p) for p in raw.get("critical_paths", [])],
        generated_prefixes=[str(p) for p in raw.get("generated_prefixes", [])],
        language=str(raw.get("language", base.language)),
    )
    if cfg.max_tokens <= 0 or cfg.file_max_tokens <= 0:
        errors.append("review.max_tokens / review.file_max_tokens 必须为正数")
    return cfg


def load_config(repo_root: Path) -> BotConfig:
    """Parse the adopter's config, or return defaults when the file is absent.

    Unknown keys are errors, not warnings: a typo like ``critcial_paths`` must fail
    the run loudly instead of silently falling back to defaults the author believed
    they had overridden.
    """
    path = repo_root / CONFIG_PATH
    if not path.is_file():
        return default_config()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{CONFIG_PATH} 不是合法 YAML：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_PATH} 顶层应是映射")
    errors: list[str] = []
    known_top = {"version", "ecosystems", "review", "max_prs_per_run", "branch_prefix"}
    for key in raw:
        if key not in known_top:
            errors.append(f"{key}: 未知顶层字段（可用：{', '.join(sorted(known_top))}）")

    eco_raw = raw.get("ecosystems") or {}
    if not isinstance(eco_raw, dict):
        errors.append("ecosystems: 应是映射")
        eco_raw = {}
    ecosystems: dict[str, EcosystemConfig] = {}
    for name in SUPPORTED_ECOSYSTEMS:
        ecosystems[name] = _merge_ecosystem(name, eco_raw.get(name), errors)
    for name in eco_raw:
        if name not in SUPPORTED_ECOSYSTEMS:
            hint = "（在路线图上，尚未支持）" if name in ROADMAP_ECOSYSTEMS else ""
            errors.append(f"ecosystems.{name}: 不支持的 ecosystem{hint}，当前支持 {SUPPORTED_ECOSYSTEMS}")

    review = _merge_review(raw.get("review"), errors)
    max_prs = int(raw.get("max_prs_per_run", MAX_PRS_PER_RUN))
    if max_prs < 0:
        errors.append("max_prs_per_run 不能为负")

    if errors:
        raise ConfigError(f"{CONFIG_PATH} 有 {len(errors)} 处问题：\n- " + "\n- ".join(errors))

    return BotConfig(
        ecosystems=ecosystems,
        review=review,
        max_prs_per_run=max_prs,
        branch_prefix=str(raw.get("branch_prefix", "updatepilot")),
        dry_run=bool(os.environ.get("UPDATEPILOT_DRY_RUN", "")),
    )


def resolve_api_key(review: ReviewConfig) -> str:
    """Read the API key from the configured env-var name. Empty = not configured."""
    return os.environ.get(review.api_key_env, "").strip()


def resolve_base_url(review: ReviewConfig) -> str:
    return os.environ.get(review.base_url_env, "").strip() or review.default_base_url
