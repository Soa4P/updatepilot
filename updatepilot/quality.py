"""Measuring review quality against a labelled corpus.

Why this exists
---------------
Every other guarantee in this project is *deterministic* and therefore testable without a
model: the coverage table, the forced downgrades, the publish exit code. The judgement
itself was the one part with no measurement — "is the review good?" was answerable only by
reading comments and forming an impression.

The corpus splits each case into two questions, because they can be answered separately
and only one of them needs a model:

* **deterministic** — the verdict must be non-``safe`` no matter what the model says
  (instructions aimed at the reviewer, a lockfile-only PR, an unread file, a truncated
  diff). These run in CI against a stubbed model that always answers ``safe``, so the
  *real* pipeline executes and a regression in the downgrade rules fails the build. A
  ``safe`` verdict on one of them is a miss.
* **model judgement** — malicious code with no structural tell (a backdoor, an exfil
  call). Only a real model call can catch these, so they are excluded from the CI gate and
  reported by ``updatepilot quality --corpus ... --with-model`` when someone runs the
  measurement.

That split is the honest one: it does not pretend a keyless CI run measures review quality,
and it does not let the part that *can* be measured drift.

Corpus format (JSONL, one object per line)::

    {"id": "...", "expected": "safe|needs_review|malicious", "deterministic": true,
     "title": "...", "body": "...", "diff": "...",
     "files": {"path": "content"}, "critical": ["infra/"]}

The reviewer is injected, so the same corpus works with a different model, a different
prompt, or no model at all.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

VERDICTS = ("safe", "needs_review", "malicious")

#: What a stubbed model answers. ``safe`` is the worst case for the deterministic rules:
#: anything that should not be labelled safe has to be caught by code, not by the model.
STUB_ANSWER = json.dumps(
    {"verdict": "safe", "risk": "low", "summary": "stub: always safe", "concerns": []}
)


@dataclass(slots=True)
class Case:
    """One labelled review input."""

    id: str
    expected: str
    deterministic: bool
    title: str = ""
    body: str = ""
    diff: str = ""
    files: dict[str, str] = field(default_factory=dict)
    critical: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if self.expected not in VERDICTS:
            raise ValueError(f"{self.id}: unknown expected verdict {self.expected!r}")


@dataclass(slots=True)
class CaseResult:
    case: Case
    verdict: str
    summary: str = ""

    @property
    def passed(self) -> bool:
        """Exact match for safe cases; "did not claim safe" for the rest.

        A case labelled ``malicious`` that lands on ``needs_review`` is a partial
        success — a human is still being asked. Counting it as a miss would push the
        metric toward tuning the model's wording instead of its recall.
        """
        if self.case.expected == "safe":
            return self.verdict == "safe"
        return self.verdict != "safe"


@dataclass(slots=True)
class Report:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def misses(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]

    @property
    def deterministic_misses(self) -> list[CaseResult]:
        return [r for r in self.misses if r.case.deterministic]

    def unsafe_recall(self, *, deterministic_only: bool = False) -> float:
        """Share of non-safe cases not labelled safe. The number that matters.

        ``deterministic_only`` is what the stub gate reports: a stubbed model cannot find
        a backdoor, so including the model-only cases would measure the stub's blindness
        and drag a healthy number down for a reason that is not a defect.
        """
        positives = [
            r for r in self.results
            if r.case.expected != "safe" and (not deterministic_only or r.case.deterministic)
        ]
        if not positives:
            return 1.0
        return sum(1 for r in positives if r.verdict != "safe") / len(positives)

    def safe_precision(self, *, deterministic_only: bool = False) -> float:
        """Share of ``safe`` verdicts that were meant to be safe."""
        claimed = [
            r for r in self.results
            if r.verdict == "safe" and (not deterministic_only or r.case.deterministic)
        ]
        if not claimed:
            return 1.0
        return sum(1 for r in claimed if r.case.expected == "safe") / len(claimed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            # Two scopes, because they answer different questions. ``all`` is the real
            # measurement (needs a model); ``deterministic`` is what the keyless CI gate
            # can honestly assert.
            "all": {
                "unsafe_recall": round(self.unsafe_recall(), 4),
                "safe_precision": round(self.safe_precision(), 4),
            },
            "deterministic": {
                "unsafe_recall": round(self.unsafe_recall(deterministic_only=True), 4),
                "safe_precision": round(self.safe_precision(deterministic_only=True), 4),
            },
            "misses": [
                {
                    "id": r.case.id,
                    "expected": r.case.expected,
                    "got": r.verdict,
                    "deterministic": r.case.deterministic,
                    "note": r.case.note,
                }
                for r in self.misses
            ],
        }


def _expand(value: Any) -> str:
    """A string, or ``{"repeat": {"text": ..., "times": N}}``.

    JSONL has no repetition operator, and two cases need one: a diff over the 200 KB cap
    and a file over the 60 KB per-file cap. Building those by hand would put 260 KB of
    ``y`` into the corpus file.
    """
    if isinstance(value, dict) and "repeat" in value:
        spec = value["repeat"] or {}
        return str(spec.get("text", "")) * int(spec.get("times", 1))
    return str(value or "")


def load_corpus(path: Path) -> list[Case]:
    """Read a JSONL corpus. Blank lines and ``#`` comments are ignored."""
    cases: list[Case] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
        files = raw.get("files") or {}
        if not isinstance(files, dict):
            raise TypeError(f"{path}:{number}: 'files' must be an object")
        critical = raw.get("critical") or []
        if not isinstance(critical, list):
            raise TypeError(f"{path}:{number}: 'critical' must be a list")
        resolved = {str(rel): _expand(body) for rel, body in files.items()}
        cases.append(
            Case(
                id=str(raw.get("id") or f"line{number}"),
                expected=str(raw.get("expected") or ""),
                deterministic=bool(raw.get("deterministic", True)),
                title=str(raw.get("title") or ""),
                body=str(raw.get("body") or ""),
                diff=_expand(raw.get("diff")),
                files=resolved,
                critical=tuple(str(c) for c in critical),
                note=str(raw.get("note") or ""),
            )
        )
    ids = [c.id for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"{path}: duplicate case ids {sorted(duplicates)}")
    return cases


def _options_for(case: Case):
    from .review import ReviewOptions

    return ReviewOptions(
        api_key="stub",
        model="stub",
        base_url="https://example.invalid",
        critical_paths=case.critical,
    )


def reviewer(*, stub_model: bool, options=None) -> Callable[[Case], dict]:
    """Build a case runner over the *real* pipeline.

    ``stub_model=True`` patches only ``call_model``: grouping, coverage, integrity and
    the deterministic downgrades all execute for real, which is the point of the CI gate.
    ``stub_model=False`` makes real model calls and needs a configured key.
    """
    from .review import ReviewTarget, review_target

    def run(case: Case) -> dict:
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp)
            for rel, body in case.files.items():
                target = tree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8")
            if options is not None:
                opts = options
            else:
                opts = _options_for(case)
                opts.critical_paths = case.critical
            target_obj = ReviewTarget(
                diff=case.diff,
                changed_files=sorted(case.files) or ["__none__"],
                tree_root=tree if case.files else None,
                title=case.title,
                body=case.body,
            )
            if stub_model:
                # Two patches, not one. Group calls answer with a ``files`` payload, so a
                # single verdict-shaped answer would mark every entry "not fully read"
                # and downgrade *everything* — the gate would then pass on noise. The
                # group stub returns the entries untouched (`fully_read` as collected),
                # which is exactly "the model contributed nothing": what remains is the
                # structural half under test.
                def fake_groups(groups, **_kwargs):
                    return [entry for group in groups for entry in group]

                with mock.patch("updatepilot.review.call_model", return_value=STUB_ANSWER),                         mock.patch("updatepilot.review.review_file_groups", side_effect=fake_groups):
                    return review_target(target_obj, opts)
            return review_target(target_obj, opts)

    return run


def evaluate(cases: list[Case], run: Callable[[Case], dict]) -> Report:
    report = Report()
    for case in cases:
        answer = run(case)
        report.results.append(
            CaseResult(
                case=case,
                verdict=str(answer.get("verdict", "needs_review")),
                summary=str(answer.get("summary", "")),
            )
        )
    return report


def render_report(report: Report, *, language: str = "zh") -> str:
    zh = language != "en"
    lines = [
        ("语料 {n} 条，通过 {p}" if zh else "{n} cases, {p} passed").format(
            n=report.total, p=report.passed
        ),
        ("非安全召回率 {r:.0%}，safe 精确率 {q:.0%}" if zh
         else "unsafe recall {r:.0%}, safe precision {q:.0%}").format(
            r=report.unsafe_recall(deterministic_only=True),
            q=report.safe_precision(deterministic_only=True),
        ),
    ]
    for miss in report.misses:
        if miss.case.deterministic:
            flag = "确定性" if zh else "deterministic"
        else:
            flag = "模型判定" if zh else "model-only"
        lines.append(
            f"- {miss.case.id}: 期望 {miss.case.expected}，得到 {miss.verdict}（{flag}）"
            if zh
            else f"- {miss.case.id}: expected {miss.case.expected}, got {miss.verdict} ({flag})"
        )
    return "\n".join(lines)


def run_quality(corpus_path: Path, *, with_model: bool = False, options=None) -> int:
    """CLI entry. The stub gate exits 1 on any deterministic miss."""
    try:
        cases = load_corpus(corpus_path)
    except (OSError, ValueError) as exc:
        print(f"corpus error: {exc}", file=sys.stderr)
        return 2
    run = reviewer(stub_model=not with_model, options=options)
    report = evaluate(cases, run)
    print(render_report(report))
    print(json.dumps(report.to_dict(), ensure_ascii=False))
    if with_model:
        return 0
    # Only the deterministic half is a gate: a stubbed model cannot find a backdoor, and
    # failing CI for that would be measuring the stub, not the product.
    return 1 if report.deterministic_misses else 0
