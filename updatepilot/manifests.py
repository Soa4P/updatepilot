"""Manifest readers: pull pinned dependencies out of the files a repo commits.

Supported ecosystems and their entry points:

* **pip** — ``requirements*.txt`` (``name==pin`` lines) and ``pyproject.toml``
  (``dependencies = ["name==pin", …]`` / ``==`` pins inside the ``project`` table).
  Only exact pins are tracked: the bot's promise is "no version drift", so a
  floating requirement is nobody's business but the author's.
* **npm** — exact versions in ``package.json`` ``dependencies``/``devDependencies``.
* **github_actions** — ``uses: owner/action@ref`` lines in every workflow file.
* **cargo** — ``Cargo.toml`` dependency tables (see :func:`read_cargo_pins`).

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
    if ecosystem == "npm":
        pins = []
        for manifest in manifests or ["package.json"]:
            if (repo_root / manifest).is_file():
                pins.extend(read_npm_pins(repo_root, manifest))
        return pins
    if ecosystem == "cargo":
        pins = []
        for manifest in manifests or ["Cargo.toml"]:
            if (repo_root / manifest).is_file():
                pins.extend(read_cargo_pins(repo_root, manifest))
        return pins
    return []


def read_npm_pins(repo_root: Path, manifest: str) -> list[PinnedDependency]:
    """Read exact-pinned entries from ``package.json``.

    Only ``dependencies`` / ``devDependencies`` entries with EXACT versions
    (``"react": "18.2.0"``) are tracked — ranges (``^1.0.0``, ``~2.3``, ``*``)
    are the author's floating contract, same policy as pip. Line numbers come
    from a block-scoped scan (depth-tracking braces), not JSON parsing: the
    bumper rewrites by line, and a re-serialized JSON would reformat the file.
    """
    path = repo_root / manifest
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    pins: list[PinnedDependency] = []
    # Block-scope tracking: scope opens when "dependencies"/"devDependencies"
    # opens (depth increases on that line) and closes when depth drops back
    # below the recorded level. JSON has no comments, so depth counting is exact.
    scope: str | None = None
    scope_depth = 0
    depth = 0
    # Exact semver only, encoded in the pin group itself (not a lookahead): the
    # closing quote right after the group enforces the full match, and ranges
    # (^1.2.3, 1.2.x, *, next) simply fail to match.
    line_re = re.compile(
        r'^(\s*)"(?P<name>[^"]+)"\s*:\s*"(?P<pin>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)"\s*,?\s*$'
    )
    block_re = re.compile(r'^"([^"]+)"\s*:\s*\{\s*$')
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        new_depth = depth + stripped.count("{") - stripped.count("}")
        if scope is not None and new_depth < scope_depth:
            scope = None
        key = block_re.match(stripped)
        if key and key.group(1) in ("dependencies", "devDependencies") and new_depth > depth:
            scope = key.group(1)
            scope_depth = new_depth
        depth = new_depth
        if scope is None:
            continue
        match = line_re.match(raw.rstrip("\n"))
        if not match:
            continue
        name = match.group("name")
        if name == scope:
            continue
        pins.append(PinnedDependency(
            ecosystem="npm", name=name.lower(), current=match.group("pin"),
            file=manifest, line=lineno, kind="npm",
            replace_span=_span_for(raw.rstrip("\n"), match, "pin"),
        ))
    return pins


#: A Cargo dependency table header. Covers plain tables, workspace tables and the
#: per-target form (``[target.'cfg(unix)'.dependencies]``) because all of them end
#: in one of the three dependency names.
_CARGO_TABLE = re.compile(r"^\s*\[(?P<table>[^\]]+)\]\s*(?:#.*)?$")
#: ``[dependencies.serde]`` — the entry is the table name, the pin is ``version``.
_CARGO_SUBTABLE = re.compile(
    r"(?:^|\.)(?:dev-|build-)?dependencies\.(?P<name>[A-Za-z0-9_-]+)$"
)
_CARGO_DEP_TABLE = re.compile(r"(?:^|\.)(?:dev-|build-)?dependencies$")
#: ``name = "1.2.3"`` / ``name = { version = "1.2.3", … }`` / ``version = "1.2.3"``
_CARGO_ENTRY = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z0-9_-]+)\s*=\s*(?P<value>.*)$")
_CARGO_VERSION_FIELD = re.compile(r'\bversion\s*=\s*"(?P<pin>[^"]*)"')
#: bare string value: ``serde = "1.2.3"``
_CARGO_STRING_PIN = re.compile(r'^"(?P<pin>[^"]*)"')
#: Only x.y.z (optionally ``=``-prefixed) is a pin we can classify and rewrite.
#: ``"1"``, ``"1.2"``, ``"*"`` and caret/tilde/range strings carry no comparable
#: patch level, and ``path``/``git``/``workspace`` entries carry no version at all.
_CARGO_PIN = re.compile(r"^=?\s*(?P<pin>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")


def read_cargo_pins(repo_root: Path, manifest: str) -> list[PinnedDependency]:
    """Read dependency pins out of a ``Cargo.toml``.

    Which forms are tracked, and why:

    * ``serde = "1.2.3"`` — Cargo's caret requirement. It is the normal thing to
      find in a real manifest, and its *minimum* is the version actually resolved
      on a fresh ``cargo update``, so that is the pin we compare. A caret move is
      a real upgrade for the whole 1.x range, so the PR body tells the maintainer
      to refresh ``Cargo.lock``.
    * ``serde = "=1.2.3"`` — exact pin; only the version text is rewritten, the
      ``=`` stays.
    * ``serde = { version = "1.2.3", features = [...] }`` — inline table; the span
      is the version string inside, so features and formatting survive.
    * ``[dependencies.serde]`` + ``version = "1.2.3"`` — sub-table form.
    * Intentionally skipped: ``path``/``git``/``workspace = true`` entries (no
      registry version to query) and versions without a full triple (``"1"``,
      ``"1.2"``, ``"*"``, ranges).
    """
    path = repo_root / manifest
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    pins: list[PinnedDependency] = []
    table = ""
    sub_entry = ""
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\n")
        header = _CARGO_TABLE.match(line)
        if header:
            table = header.group("table").strip()
            match = _CARGO_SUBTABLE.search(table)
            sub_entry = match.group("name") if match else ""
            continue
        if not table:
            continue
        in_dep_table = bool(_CARGO_DEP_TABLE.search(table))
        if not in_dep_table and not sub_entry:
            continue
        entry = _CARGO_ENTRY.match(line)
        if not entry:
            continue
        if sub_entry:
            # Inside `[dependencies.serde]` only the `version` key is the pin.
            if entry.group("name") != "version":
                continue
            name = sub_entry
            value = entry.group("value").strip()
            match = _CARGO_STRING_PIN.match(value)
        else:
            name = entry.group("name")
            if name == "version":  # a nested key, not a dependency
                continue
            value = entry.group("value").strip()
            match = (
                _CARGO_VERSION_FIELD.search(value)
                if value.startswith("{")
                else _CARGO_STRING_PIN.match(value)
            )
        if not match:
            continue  # path/git/workspace entry, or a table key we do not track
        raw_pin = match.group("pin")
        lead = len(raw_pin) - len(raw_pin.lstrip())
        pin_match = _CARGO_PIN.match(raw_pin.strip())
        if not pin_match:
            continue
        # The span must cover the version text only: an exact pin is written
        # ``=1.2.3`` and the ``=`` is part of the requirement, not the version.
        # ``match`` was run on the value substring, so its offsets are shifted by
        # where the value starts, plus any leading spaces inside the quotes.
        base = entry.start("value") + match.start("pin") + lead
        start = base + pin_match.start("pin")
        end = base + pin_match.end("pin")
        pins.append(PinnedDependency(
            ecosystem="cargo", name=name.lower(), current=pin_match.group("pin"),
            file=manifest, line=lineno, kind="cargo",
            replace_span=(line, line[:start] + "{pin}" + line[end:]),
        ))
    return pins
