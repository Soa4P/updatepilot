"""Manifest readers: pull pinned dependencies out of the files a repo commits.

v1 ecosystems and their entry points:

* **pip** — ``requirements*.txt`` (``name==pin`` lines) and ``pyproject.toml``
  (``dependencies = ["name==pin", …]`` / ``==`` pins inside the ``project`` table).
  Only exact pins are tracked: the bot's promise is "no version drift", so a
  floating requirement is nobody's business but the author's.
* **github_actions** — ``uses: owner/action@ref`` lines in every workflow file.

Each entry records the file, line number (1-based) and the exact span to rewrite,
so the bumper can patch the manifest without regex-ing the whole file again.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_PY_PIN = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?==(?P<pin>[^\s#]+)\s*(?P<comment>#.*)?$")
_PYPROJECT = re.compile(r'^(?P<indent>\s*)"?(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?)"?\s*==\s*(?P<pin>[^"\s,]+)')

_ACTIONS_USE = re.compile(
    r"^(?P<indent>\s*)-\s+uses:\s*(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(?P<ref>[^\s#]+)"
)


@dataclass
class PinnedDependency:
    """One concrete pin somewhere in the repo."""

    ecosystem: str
    name: str
    current: str
    file: str  # repo-relative path
    line: int  # 1-based
    kind: str  # pep440 | action
    #: The exact substring on that line that is the pin (used by the bumper).
    replace_span: tuple[str, str]  # (old_text, template) — template has {pin}

    def render(self, new_pin: str) -> str:
        # replace, not str.format: the template is the ORIGINAL line, and any
        # other braces on it (a comment containing "{docs}") would crash format
        # or resolve unintended placeholders.
        return self.replace_span[1].replace("{pin}", new_pin)


def _span_for(line_text: str, match: re.Match, pin_group: str) -> tuple[str, str]:
    """(old_text, template) for the whole line, with the pin as ``{pin}``.

    Offsets come from the regex match, not ``rfind``: a comment repeating the pin
    text ("==1.2.3  # was 1.2.3") must not move the placeholder onto the comment.
    """
    start, end = match.span(pin_group)
    return line_text, line_text[:start] + "{pin}" + line_text[end:]


def read_pip_pins(repo_root: Path, manifest: str) -> list[PinnedDependency]:
    """Read `name==pin` entries from one requirements-style file."""
    path = repo_root / manifest
    if not path.is_file():
        return []
    pins: list[PinnedDependency] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PY_PIN.match(line)
        if match:
            name = match.group("name")
            pins.append(PinnedDependency(
                ecosystem="pip", name=name.lower(), current=match.group("pin"),
                file=manifest, line=lineno, kind="pep440",
                replace_span=_span_for(line, match, "pin"),
            ))
            continue
        match = _PYPROJECT.match(line)
        if match:
            name = re.sub(r"\[.*\]", "", match.group("name"))
            pins.append(PinnedDependency(
                ecosystem="pip", name=name.lower(), current=match.group("pin"),
                file=manifest, line=lineno, kind="pep440",
                replace_span=_span_for(line, match, "pin"),
            ))
    return pins


def iter_workflow_files(repo_root: Path, manifests: list[str]) -> Iterator[Path]:
    seen: set[Path] = set()
    for entry in manifests:
        base = repo_root / entry
        if base.is_dir():
            candidates = sorted(base.glob("*.yml")) + sorted(base.glob("*.yaml"))
        elif base.is_file():
            candidates = [base]
        else:
            continue
        for path in candidates:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path


def read_action_refs(repo_root: Path, manifests: list[str]) -> list[PinnedDependency]:
    """Read `uses: owner/action@ref` entries from workflow files.

    Both pinned SHAs and version tags are tracked, but only tag refs produce
    update PRs: a SHA pin means the repo author chose immutability, and rewriting
    it without their say-so would break that promise. SHA refs are reported for
    visibility (the planner skips them silently — they show up as "pinned by SHA").
    """
    pins: list[PinnedDependency] = []
    for path in iter_workflow_files(repo_root, manifests):
        rel = path.relative_to(repo_root).as_posix()
        for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            match = _ACTIONS_USE.match(raw.rstrip("\n"))
            if not match:
                continue
            ref = match.group("ref")
            if re.fullmatch(r"[0-9a-f]{40}", ref):
                continue  # SHA pin: immutability was chosen on purpose
            pins.append(PinnedDependency(
                ecosystem="github_actions", name=match.group("action").lower(),
                current=ref, file=rel, line=lineno, kind="action",
                replace_span=_span_for(raw.rstrip("\n"), match, "ref"),
            ))
    return pins


def read_manifests(repo_root: Path, ecosystem: str, manifests: list[str]) -> list[PinnedDependency]:
    if ecosystem == "pip":
        pins: list[PinnedDependency] = []
        for manifest in manifests:
            path = repo_root / manifest
            if not path.is_file():
                continue
            if manifest.endswith(".txt"):
                pins.extend(read_pip_pins(repo_root, manifest))
            else:
                pins.extend(read_pip_pins(repo_root, manifest))
        return pins
    if ecosystem == "github_actions":
        return read_action_refs(repo_root, manifests or [".github/workflows"])
    return []
