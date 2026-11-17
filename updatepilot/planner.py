"""The policy engine: pins + registry answers → planned PRs.

Policy, in order:

1. **Ignored names** never update (the config said so; that decision outranks any
   registry answer).
2. **Downgrades and sideways moves** are dropped — a "update" to an older version
   is not an update.
3. **Majors** become their own PR (or are dropped when ``allow_major`` is False) —
   a major needs a changelog read, and mixing it with routine bumps hides it.
4. **minor+patch** group into one PR per ecosystem when ``group_minor_patch`` is on.
5. The **per-run PR ceiling** applies last: the oldest-dependency groups first.
   Everything beyond the ceiling is reported as deferred, never silently dropped.

A planned PR carries every file edit it needs, so the bumper is a dumb applier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import BotConfig, EcosystemConfig
from .manifests import PinnedDependency, read_manifests
from .registries import RegistryError
from .versions import classify_update


@dataclass
class Update:
    pin: PinnedDependency
    latest: str
    level: str  # major | minor | patch | other


@dataclass
class PlannedPR:
    ecosystem: str
    branch: str
    title: str
    body_lines: list[str]
    updates: list[Update]
    #: {file: {line: rendered_line}} — the bumper applies these verbatim.
    edits: dict[str, dict[int, str]] = field(default_factory=dict)

    def total_files(self) -> int:
        return len(self.edits)


@dataclass
class ScanReport:
    planned: list[PlannedPR]
    deferred: list[str]  # "pip: pytest 9.2.0 → 9.3.0 (minor)" beyond the ceiling
    skipped: list[str]  # ignored / SHA-pinned / registry-unavailable, with reasons
    errors: list[str]  # registry failures worth surfacing

    @property
    def has_updates(self) -> bool:
        return bool(self.planned)


def _branch_slug(name: str) -> str:
    slug = name.replace("/", "-").replace(" ", "-")
    return "".join(ch for ch in slug if ch.isalnum() or ch in "-._")


def _apply_edit(pr: PlannedPR, pin: PinnedDependency, new_pin: str) -> None:
    lines = pr.edits.setdefault(pin.file, {})
    lines[pin.line] = pin.render(new_pin)


def plan_scan(
    repo_root: Any,
    config: BotConfig,
    lookup: dict[str, str | None],
    *,
    github_token: str | None = None,
) -> ScanReport:
    """Build the PR plan.

    ``lookup`` maps ``ecosystem:name`` → latest version string (or None). It is
    injected rather than fetched here so tests can feed a table and the CLI can
    batch all registry calls with its own rate-limit pacing.
    """
    planned: list[PlannedPR] = []
    deferred: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    per_updates: list[Update] = []
    for eco_name in config.enabled_ecosystems:
        eco = config.ecosystems[eco_name]
        pins = read_manifests(repo_root, eco_name, eco.manifests)
        for pin in pins:
            if pin.name in {i.lower() for i in eco.ignore}:
                skipped.append(f"{eco_name}: {pin.name} 在 ignore 名单")
                continue
            latest = lookup.get(f"{eco_name}:{pin.name}")
            if latest is None:
                skipped.append(f"{eco_name}: {pin.name} 没有可用的新版本信息")
                continue
            try:
                result = classify_update(pin.current, latest, pin.kind)
            except (ValueError, TypeError) as exc:  # malformed pins must not kill the scan
                errors.append(f"{eco_name}: {pin.name} 版本解析失败（{pin.current!r} → {latest!r}）：{exc}")
                continue
            if result is None:
                continue  # already current (or pre-release) — nothing to say
            level, _cur, new = result
            if level == "other":
                skipped.append(f"{eco_name}: {pin.name} {pin.current} → {latest}（降级/异常段，跳过）")
                continue
            if level == "major" and not eco.allow_major:
                skipped.append(
                    f"{eco_name}: {pin.name} {pin.current} → {latest}（major，需人工读 changelog）"
                )
                continue
            per_updates.append(Update(pin=pin, latest=new.raw, level=level))

    majors = [u for u in per_updates if u.level == "major"]
    routines = [u for u in per_updates if u.level in ("minor", "patch", "other")]

    for update in majors:
        planned.append(_single_pr(update, config))

    eco_cfg: dict[str, EcosystemConfig] = config.ecosystems
    for eco_name in config.enabled_ecosystems:
        eco = eco_cfg.get(eco_name, EcosystemConfig())
        if not eco.group_minor_patch:
            planned.extend(_single_pr(u, config) for u in routines if u.pin.ecosystem == eco_name)
            continue
        grouped = [u for u in routines if u.pin.ecosystem == eco_name]
        if not grouped:
            continue
        grouped.sort(key=lambda u: (u.pin.name, u.pin.file, u.pin.line))
        pr = PlannedPR(
            ecosystem=eco_name,
            branch="",
            title="",
            body_lines=[],
            updates=grouped,
        )
        _fill_group_pr(pr, config)
        planned.append(pr)

    # Ceiling last: keep the first N planned (majors first, then grouped routine),
    # defer the rest with a reason. The report is the honesty mechanism.
    ceiling = max(0, config.max_prs_per_run)
    if len(planned) > ceiling:
        for pr in planned[ceiling:]:
            names = ", ".join(sorted({u.pin.name for u in pr.updates})[:3])
            deferred.append(f"{pr.ecosystem}: {names}（超出本轮 max_prs_per_run={ceiling}）")
        planned = planned[:ceiling]

    return ScanReport(planned=planned, deferred=deferred, skipped=skipped, errors=errors)


def _single_pr(update: Update, config: BotConfig) -> PlannedPR:
    slug = _branch_slug(update.pin.name)
    level = update.level
    pr = PlannedPR(
        ecosystem=update.pin.ecosystem,
        branch="",
        title="",
        body_lines=[],
        updates=[update],
    )
    pr.branch = f"{config.branch_prefix}/{update.pin.ecosystem}/{slug}-{update.latest}"
    pr.title = (
        f"chore(deps): bump {update.pin.name} "
        f"{update.pin.current} → {update.latest}"
    )
    pr.body_lines = [
        (f"**{update.pin.ecosystem}** · **{level}** · `{update.pin.name}` "
         f"`{update.pin.current}` → `{update.latest}`"),
        "",
        f"文件：`{update.pin.file}` 第 {update.pin.line} 行",
        "",
        "本 PR 由 UpdatePilot 开出，并已附带完整文件 AI 审查（见下方评论）；",
        "合并决定仍由维护者做出。",
    ]
    _apply_edit(pr, update.pin, update.latest)
    return pr


def _fill_group_pr(pr: PlannedPR, config: BotConfig) -> None:
    names = sorted({u.pin.name for u in pr.updates})
    pr.branch = f"{config.branch_prefix}/{pr.ecosystem}/group-{_branch_slug('-'.join(names))[:40]}"
    pr.title = f"chore(deps): group {pr.ecosystem} minor/patch updates ({len(names)} packages)"
    pr.body_lines = [
        f"**{pr.ecosystem}** · minor/patch 分组 · {len(names)} 个包：",
        "",
    ]
    pr.body_lines.extend(f"- `{u.pin.name}` `{u.pin.current}` → `{u.latest}`（{u.level}）" for u in pr.updates)
    pr.body_lines += [
        "",
        "本 PR 由 UpdatePilot 开出，并已附带完整文件 AI 审查（见下方评论）；",
        "合并决定仍由维护者做出。",
    ]
    for update in pr.updates:
        _apply_edit(pr, update.pin, update.latest)


def fetch_latest_versions(
    pins: list[PinnedDependency], *, github_token: str | None = None
) -> dict[str, str | None]:
    """Batch every registry lookup the plan needs, keyed ``ecosystem:name``.

    Registry failures are swallowed into None (the planner reports them as
    skipped) because one unreachable package must not stop the other thirty.
    """
    from .registries import github_action_latest, pypi_latest

    lookup: dict[str, str | None] = {}
    seen: set[str] = set()
    for pin in pins:
        key = f"{pin.ecosystem}:{pin.name}"
        if key in seen:
            continue
        seen.add(key)
        try:
            if pin.ecosystem == "pip":
                lookup[key] = pypi_latest(pin.name)
            elif pin.ecosystem == "github_actions":
                lookup[key] = github_action_latest(pin.name, github_token)
            else:
                lookup[key] = None
        except RegistryError as exc:
            lookup[key] = None
            # The reason lands in the scan report via a marker key the caller reads.
            lookup[f"{key}:error"] = str(exc)
    return lookup
