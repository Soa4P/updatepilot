"""Version comparison and change classification.

Four version worlds live here:

* **PEP 440** (pip): parsed with ``packaging.version`` — the ecosystem standard.
* **GitHub action refs** (``v4``, ``v7``): plain major integers. Action tags are
  just moving majors in practice; anything fancier is guessed structure.
* **npm semver** (``1.2.3``, ``1.2.3-beta.1``): the common subset of semver that
  exact-pinned ``package.json`` entries use. Prerelease comparison follows
  semver's rule (prerelease < release, identifiers compared left to right,
  numeric before alphanumeric); build metadata is ignored for precedence, per
  semver. Ranges (``^1.0.0``) are never parsed — floating entries are not the
  bot's business.
* **cargo semver** (``1.2.3``, ``0.8.5``): exactly the same grammar as npm, with
  one policy difference that matters — **under ``0.x`` a minor bump is a breaking
  change** (semver §4: "major version zero … anything MAY change at any time").
  Cargo's own caret rule agrees: ``^0.8`` allows only ``0.8.z``. Classifying
  ``0.8 → 0.9`` as "minor" would let it into a routine grouped PR and skip the
  changelog read that majors get, so ``change_level`` reports ``major`` for it.

Classification (major/minor/patch/other) drives the policy engine: majors are
isolated PRs by default, minor+patch group together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

CHANGE_LEVELS = ("major", "minor", "patch", "other")

_ACTION_TAG = re.compile(r"^v?(\d+)$")
#: strict semver core + optional prerelease/build; a leading "v" is tolerated
#: because some maintainers tag npm releases that way.
_NPM_SEMVER = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


@dataclass(frozen=True)
class VersionInfo:
    """A version we can compare and classify, whatever its ecosystem."""

    raw: str
    kind: str  # pep440 | action | npm | cargo
    pep: Version | None = None
    action_major: int | None = None
    npm: tuple | None = None  # (major, minor, patch, prerelease_tuple | ())
    #: cargo reuses the semver tuple; the kind stays distinct so classification
    #: can apply cargo's 0.x rule without a second parser.
    _SEMVER_KINDS = ("npm", "cargo")

    @classmethod
    def parse(cls, raw: str, kind: str = "pep440") -> VersionInfo | None:
        raw = raw.strip()
        if kind == "action":
            match = _ACTION_TAG.match(raw)
            if not match:
                return None
            return cls(raw=raw, kind="action", action_major=int(match.group(1)))
        if kind in ("npm", "cargo"):
            match = _NPM_SEMVER.match(raw)
            if not match:
                return None
            prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
            return cls(
                raw=raw, kind=kind,
                npm=(int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease),
            )
        try:
            return cls(raw=raw, kind="pep440", pep=Version(raw))
        except InvalidVersion:
            return None

    def __lt__(self, other: VersionInfo) -> bool:
        if self.kind != other.kind:
            raise ValueError(f"cannot compare {self.kind} with {other.kind}")
        if self.kind == "action":
            return (self.action_major or 0) < (other.action_major or 0)
        if self.kind in self._SEMVER_KINDS:
            return self._npm_precedence() < other._npm_precedence()
        assert self.pep is not None and other.pep is not None
        return self.pep < other.pep

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VersionInfo):
            return NotImplemented
        if self.kind != other.kind:
            return False
        if self.kind == "action":
            return self.action_major == other.action_major
        if self.kind in self._SEMVER_KINDS:
            return self._npm_precedence() == other._npm_precedence()
        return self.pep == other.pep

    def _npm_precedence(self) -> tuple:
        """semver precedence tuple: (M, m, p, …prerelease identifiers…).

        semver rule 11: a release sorts BEFORE any prerelease of the same
        triple — the prerelease slot is a release-sentinel ``(2,)`` that sorts
        above every identifier key. Rule 12: numeric identifiers compare
        numerically and rank below alphanumeric ones, hence the
        ``(rank, text, number)`` shape with rank 0 = alphanumeric, 1 = numeric.
        """
        assert self.npm is not None
        major, minor, patch, pre = self.npm
        if not pre:
            pre_key: tuple = ((2,),)
        else:
            pre_key = tuple(
                (1, "", int(part)) if part.isdigit() else (0, part, 0) for part in pre
            )
        return (major, minor, patch, pre_key)

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
        if self.kind in self._SEMVER_KINDS:
            assert self.npm is not None and other.npm is not None
            if self.npm[0] > other.npm[0]:
                return "major"
            if self.kind == "cargo" and other.npm[0] == 0 and self.npm[1] > other.npm[1]:
                # 0.x is pre-stability: cargo's caret rule (^0.8 → only 0.8.z) and
                # semver §4 both say a 0.x minor move can break. Route it through
                # the major path so it needs an explicit allow_major + changelog.
                return "major"
            if self.npm[0] == other.npm[0] and self.npm[1] > other.npm[1]:
                return "minor"
            if (self.npm[0], self.npm[1]) == (other.npm[0], other.npm[1]) and self.npm[2] > other.npm[2]:
                return "patch"
            return "other"
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
        if self.kind in self._SEMVER_KINDS:
            assert self.npm is not None
            return bool(self.npm[3])
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
    elif kind in ("npm", "cargo"):
        if new.is_prerelease():
            return None
        if not (new._npm_precedence() > cur._npm_precedence()):
            return None
    else:
        if new.action_major is None or cur.action_major is None:
            return None
        if new.action_major <= cur.action_major:
            return None
    return new.change_level(cur), cur, new
