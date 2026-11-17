"""Version comparison and change classification.

Two version worlds live here:

* **PEP 440** (pip): parsed with ``packaging.version`` — the ecosystem standard.
* **GitHub action refs** (``v4``, ``v7``): plain major integers. Action tags are
  just moving majors in practice; anything fancier is guessed structure.

Classification (major/minor/patch/other) drives the policy engine: majors are
isolated PRs by default, minor+patch group together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

CHANGE_LEVELS = ("major", "minor", "patch", "other")

_ACTION_TAG = re.compile(r"^v?(\d+)$")


@dataclass(frozen=True)
class VersionInfo:
    """A version we can compare and classify, whatever its ecosystem."""

    raw: str
    kind: str  # pep440 | action
    pep: Version | None = None
    action_major: int | None = None

    @classmethod
    def parse(cls, raw: str, kind: str = "pep440") -> VersionInfo | None:
        raw = raw.strip()
        if kind == "action":
            match = _ACTION_TAG.match(raw)
            if not match:
                return None
            return cls(raw=raw, kind="action", action_major=int(match.group(1)))
        try:
            return cls(raw=raw, kind="pep440", pep=Version(raw))
        except InvalidVersion:
            return None

    def __lt__(self, other: VersionInfo) -> bool:
        if self.kind != other.kind:
            raise ValueError(f"cannot compare {self.kind} with {other.kind}")
        if self.kind == "action":
            return (self.action_major or 0) < (other.action_major or 0)
        assert self.pep is not None and other.pep is not None
        return self.pep < other.pep

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VersionInfo):
            return NotImplemented
        if self.kind != other.kind:
            return False
        if self.kind == "action":
            return self.action_major == other.action_major
        return self.pep == other.pep

    def change_level(self, other: VersionInfo) -> str:
        """How far ``self`` (new) is from ``other`` (current): major/minor/patch/other.

        PEP 440 has no clean notion of "minor" across pre/post/dev releases, so
        anything that is not a clean release triple lands in ``other`` — and the
        policy treats ``other`` like a minor (groupable, not isolated). The levels
        exist to route PRs, not to render changelogs.
        """
        if self.kind == "action":
            assert self.action_major is not None and other.action_major is not None
            if self.action_major > other.action_major:
                return "major"
            if self.action_major < other.action_major:
                return "other"  # downgrade; the planner refuses these
            return "patch"  # same major, tag bump = runtime update
        assert self.pep is not None and other.pep is not None
        if not (self.pep > other.pep):
            return "other"
        if self.pep.epoch != other.pep.epoch:
            return "other"
        if self.pep.major > other.pep.major:
            return "major"
        if self.pep.minor > other.pep.minor:
            return "minor"
        if self.pep.micro > other.pep.micro:
            return "patch"
        return "other"  # pre/post/dev-only moves are neither of the three

    def is_prerelease(self) -> bool:
        if self.kind == "action":
            return False
        assert self.pep is not None
        return self.pep.is_prerelease or self.pep.is_devrelease


def classify_update(current: str, latest: str, kind: str = "pep440") -> tuple[str, VersionInfo, VersionInfo] | None:
    """Classify one candidate update, or None when it is not an upgrade.

    Pre-releases never count as updates: a pinned ``==1.2.3`` should not chase
    ``1.3.0rc1`` unless the pin asked for it.
    """
    cur = VersionInfo.parse(current, kind)
    new = VersionInfo.parse(latest, kind)
    if cur is None or new is None:
        return None
    if kind == "pep440":
        if new.is_prerelease():
            return None
        if not (new.pep > cur.pep):
            return None
    else:
        if new.action_major is None or cur.action_major is None:
            return None
        if new.action_major <= cur.action_major:
            return None
    return new.change_level(cur), cur, new
