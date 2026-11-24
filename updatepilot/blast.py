"""Blast radius: how much of the repository refers to a dependency.

Why the order is a decision and not cosmetics
---------------------------------------------
``max_prs_per_run`` makes the plan's order behaviour: whatever sorts first is what
this run actually opens, and everything after it is deferred to the next run. That
order used to be whatever the manifests happened to list first, which means the
ceiling was spent on alphabetically or historically lucky dependencies.

The measure here is deliberately crude: the number of source files that mention the
dependency. The honest alternative — a static import graph per ecosystem — is three
language parsers and a pile of guessing about dynamic imports, and a wrong graph
would be worse than an honest proxy. A dependency referenced from forty files has
more places to break than one referenced from two, and that is enough to order a
queue that a human then reviews.

What is deliberately excluded: lock files (they name every dependency, so they would
rank everything equally and swamp the signal), vendored trees, build output, and
anything binary. The walk is capped in both file count and file size, and matching
is a single token scan per file, so a pathological repository cannot turn the scan
into the slow part of the run.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

#: Directories that never carry first-party references worth counting.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", ".tox", "dist", "build",
    "target", "site-packages", "vendor", "third_party",
})

#: Lock files list every dependency by definition. Counting them would rank every
#: package identically and drown the signal the ordering exists for.
SKIP_BASENAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "poetry.lock", "Pipfile.lock", "requirements.lock", "uv.lock",
    "composer.lock", "Gemfile.lock", "go.sum",
})

#: Caps. A big monorepo is the case this feature is for, so the ceiling is a few
#: thousand files; beyond that the ordering falls back to name order for whatever
#: was not reached, which is what it did before this module existed.
MAX_SCAN_FILES = 1500
MAX_FILE_BYTES = 200_000

#: Identifiers as they appear in source. Underscores and dots are part of a token,
#: so ``serde_json::Value`` yields ``serde_json`` rather than ``serde``.
_TOKEN = re.compile(r"[A-Za-z0-9_.-]+")


def _variants(name: str) -> set[str]:
    """Spellings of one dependency name that source files might use.

    * actions are referenced as ``owner/action`` in a workflow but imported or
      named by the bare action elsewhere — the slug's last segment is the token;
    * cargo dependency keys may be written ``serde-json`` while Rust code says
      ``serde_json``; both spellings are the same crate.
    """
    base = name.rsplit("/", 1)[-1]
    out = {base}
    if "-" in base:
        out.add(base.replace("-", "_"))
    return {token.lower() for token in out if token}


def blast_radii(repo_root: Path, names: Iterable[str]) -> dict[str, int]:
    """Map each dependency name to the number of files that mention it.

    Names absent from the result scored zero. The walk stops after
    ``MAX_SCAN_FILES`` readable files, so the answer is "at least this many" for
    enormous repositories — the ordering only needs a magnitude.
    """
    wanted = [name for name in names if name]
    if not wanted:
        return {}
    variant_owner: dict[str, str] = {}
    for name in wanted:
        for variant in _variants(name):
            variant_owner.setdefault(variant, name)
    counts: dict[str, int] = dict.fromkeys(wanted, 0)
    seen_files = 0
    for path in _walk(repo_root):
        if seen_files >= MAX_SCAN_FILES:
            break
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue  # binary
        seen_files += 1
        hits: set[str] = set()
        for token in _TOKEN.findall(raw.decode("utf-8", "replace").lower()):
            owner = variant_owner.get(token)
            if owner is not None and owner not in hits:
                hits.add(owner)
        for owner in hits:
            counts[owner] += 1
    return counts


def _walk(repo_root: Path) -> Iterable[Path]:
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name.lower() in SKIP_BASENAMES:
            continue
        yield path
