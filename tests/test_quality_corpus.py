"""The quality gate: deterministic downgrades must hold against a stubbed model.

The corpus lives in ``tests/corpus/review_quality.jsonl``. This file is the part that runs
in CI: the *real* pipeline (grouping, coverage, integrity, injection scan) is exercised
with a model stubbed to always answer ``safe``, and any case that should not be safe must
still be caught. That is a measurement of the code, not of a model.

The model-facing half — backdoors, exfiltration, auth bypasses — is measured by
``updatepilot quality --corpus ... --with-model`` with a configured key. It is excluded
here on purpose: a keyless CI run that "measured review quality" by scoring a stub would
be a number that looks like evidence and is not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from updatepilot.quality import (
    evaluate,
    load_corpus,
    render_report,
    reviewer,
    run_quality,
)

CORPUS = Path(__file__).resolve().parent / "corpus" / "review_quality.jsonl"


def _cases():
    return load_corpus(CORPUS)


def test_the_corpus_is_big_enough_and_balanced() -> None:
    cases = _cases()
    assert len(cases) >= 30, "合成语料少于 30 条就不足以做度量"
    deterministic = [c for c in cases if c.deterministic]
    judged = [c for c in cases if not c.deterministic]
    safe = [c for c in cases if c.expected == "safe"]
    assert len(deterministic) >= 12, "确定性用例太少，降级规则没被覆盖"
    assert len(judged) >= 8, "模型判定用例太少，度量没有意义"
    assert len(safe) >= 6, "没有 safe 用例就算不出精确率"
    # Every non-safe deterministic case must be non-safe for a *structural* reason.
    for case in deterministic:
        if case.expected != "safe":
            assert case.note, f"{case.id} 缺少说明：为什么这条不靠模型也能判出来"


def test_no_deterministic_miss_against_a_stub_that_always_says_safe() -> None:
    """The gate. A stub answering `safe` must not be able to earn a safe verdict on a
    case that should not be safe — the downgrade rules are the only thing standing."""
    report = evaluate(_cases(), reviewer(stub_model=True))

    misses = report.deterministic_misses
    assert not misses, "确定性降级失效：\n" + render_report(report)
    assert report.safe_precision(deterministic_only=True) == 1.0, (
        "有 safe 结论落在了本该非 safe 的确定性用例上：\n" + render_report(report)
    )


def test_the_safe_cases_stay_safe_with_a_stub() -> None:
    """The other direction: over-downgrading is a product failure too.

    A bot that answers ``needs_review`` for a one-line docs change is a bot whose label
    means nothing, and the operator learns to ignore it.
    """
    report = evaluate(_cases(), reviewer(stub_model=True))
    for result in report.results:
        if result.case.expected == "safe":
            assert result.verdict == "safe", (
                f"{result.case.id} 被误降级了：{result.summary}"
            )


def test_model_only_cases_are_reported_but_do_not_gate() -> None:
    """A stub cannot find a backdoor; the report still counts it, the gate ignores it."""
    report = evaluate(_cases(), reviewer(stub_model=True))
    judged = [r for r in report.results if not r.case.deterministic]
    assert judged, "语料里没有模型判定用例"
    # With a stub, every model-only non-safe case is a miss — and that is *not* a
    # deterministic miss, which is what keeps CI honest.
    assert all(not r.passed for r in judged if r.case.expected != "safe")
    # ... and none of those misses is a deterministic one.
    assert report.deterministic_misses == []


def test_the_cli_exits_zero_on_the_committed_corpus() -> None:
    assert run_quality(CORPUS) == 0


def test_a_regressed_downgrade_fails_the_cli(tmp_path: Path) -> None:
    """Proves the gate can fail: a lockfile-only case marked deterministically unsafe.

    With the rule in place it passes; the failure mode this pins is the *harness* being
    able to see a violation at all (an always-green gate measures nothing).
    """
    broken = tmp_path / "broken.jsonl"
    broken.write_text(
        json.dumps(
            {
                "id": "not-caught-by-any-rule",
                "expected": "malicious",
                "deterministic": True,
                "title": "chore: tidy",
                "diff": "+x = 1",
                "files": {"src/app.py": "x = 1\n"},
                "note": "nothing structural here: a stub model will say safe",
            }
        ),
        encoding="utf-8",
    )
    assert run_quality(broken) == 1


def test_the_report_carries_numbers_not_just_pass_fail() -> None:
    report = evaluate(_cases(), reviewer(stub_model=True))
    payload = report.to_dict()
    assert payload["total"] == report.total
    assert 0.0 <= payload["all"]["unsafe_recall"] <= 1.0
    assert payload["deterministic"]["safe_precision"] == 1.0
    # The stub cannot catch model-only cases, so the all-cases precision is lower — that
    # gap *is* the measurement a real model run fills in.
    assert payload["all"]["safe_precision"] < payload["deterministic"]["safe_precision"]
    assert isinstance(payload["misses"], list)
