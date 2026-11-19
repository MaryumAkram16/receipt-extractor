"""The evaluation runner: mode guard, scoring, and the Markdown report.

Same style as before: the FastAPI TestClient plus the FakeLLM from conftest,
so nothing here touches the network. Reports go to pytest's tmp_path, never
the repository's reports/ directory.
"""
import json
from datetime import datetime, timezone

import pytest

from evals import run_eval

CASES = json.loads(run_eval.CASES_PATH.read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 19, 12, 30, tzinfo=timezone.utc)


def _files(path):
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


def _run(client, capsys, tmp_path, **kwargs):
    # Reports get their own subdirectory: conftest already puts the temp database under tmp_path.
    kwargs.setdefault("report_dir", tmp_path / "eval-reports")
    code = run_eval.run(client=client, check_jobs=False, now=NOW, **kwargs)
    return code, json.loads(capsys.readouterr().out)


def _answer(expected: dict, **overrides) -> dict:
    """A schema-valid model answer that matches `expected`, with overrides."""
    return {"vendor": None, "date": None, "total_amount": None, "currency": None,
            "confidence": 0.5, "needs_review": True, **expected, **overrides}


def _ok(body: dict) -> dict:
    return {"status": 200, "body": body, "text": json.dumps(body)}


# --- mode guard (unchanged rule) ---------------------------------------------------

def test_labelled_mode_refuses_stub_server(client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LLM_STUB", "1")
    code, out = _run(client, capsys, tmp_path, mode="labelled")
    assert code == run_eval.EXIT_WRONG_MODE
    assert out["server_llm_mode"] == "stub"
    assert _files(tmp_path / "eval-reports") == [], "no report may be written when refusing"


def test_labelled_mode_refuses_disabled_server(client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LLM_ENABLED", "false")
    code, _ = _run(client, capsys, tmp_path, mode="labelled")
    assert code == run_eval.EXIT_WRONG_MODE


def test_labelled_mode_refuses_server_without_llm_mode(capsys, tmp_path):
    class OldServer:  # pre-llm_mode /health
        def get(self, path):
            return type("R", (), {"status_code": 200, "json": lambda self: {"status": "ok"}})()

    code = run_eval.run(client=OldServer(), report_dir=tmp_path)
    assert code == run_eval.EXIT_WRONG_MODE
    assert json.loads(capsys.readouterr().out)["server_llm_mode"] == "unknown"


# --- contract mode (stub) ------------------------------------------------------------

def test_contract_mode_passes_against_stub(client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LLM_STUB", "1")
    code, out = _run(client, capsys, tmp_path, mode="contract", require_llm_mode="stub")
    assert code == 0
    assert out["passed"] == out["total"] == len(CASES)
    assert out["llm_mode"] == "stub"
    assert _files(tmp_path / "eval-reports") == [], "contract mode writes no report"


def test_contract_mode_fails_on_server_errors(client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LLM_STUB", "0")  # live mode with no provider -> guard raises -> 500s
    code, out = _run(client, capsys, tmp_path, mode="contract")
    assert code == run_eval.EXIT_FAILED
    assert out["failed"] == len(CASES)


# --- labelled mode end to end -------------------------------------------------------

def test_labelled_mode_perfect_run(client, fake_llm, capsys, tmp_path):
    fake_llm.script = [json.dumps(_answer(c["expected"])) for c in CASES]
    code, out = _run(client, capsys, tmp_path, mode="labelled")
    assert code == 0
    assert (out["passed"], out["total"], out["failed"], out["errors"]) == (len(CASES), len(CASES), 0, 0)
    assert out["prompt_version"] == "extract-v2"
    assert out["model"] == "test-model"
    for name, stats in out["field_accuracy"].items():
        assert stats["accuracy"] == 1.0, name
        assert stats["total"] > 0, name
    assert out["needs_review"]["precision"] == out["needs_review"]["recall"] == 1.0
    assert set(out["categories"]) == {c["category"] for c in CASES}
    assert out["report_path"] == str(tmp_path / "eval-reports" / "eval-2026-11-19.md")


def test_labelled_mode_writes_dated_markdown_report(client, fake_llm, capsys, tmp_path):
    fake_llm.script = [json.dumps(_answer(c["expected"])) for c in CASES]
    _run(client, capsys, tmp_path, mode="labelled")
    report = (tmp_path / "eval-reports" / "eval-2026-11-19.md").read_text()
    assert report.startswith("# Labelled evaluation — 2026-11-19")
    for needle in ("| Prompt version | extract-v2 |", "| Model | test-model |",
                   "## Field accuracy", "| vendor |", "| total |",
                   "## needs_review", "| Precision | 100.0% |", "| Recall | 100.0% |",
                   "## By category", "| multi_page |", "## Failures", "None."):
        assert needle in report, needle


def test_second_report_same_day_does_not_overwrite(client, fake_llm, capsys, tmp_path):
    fake_llm.script = [json.dumps(_answer(c["expected"])) for c in CASES] * 2
    _run(client, capsys, tmp_path, mode="labelled")
    _, out = _run(client, capsys, tmp_path, mode="labelled")
    assert out["report_path"] == str(tmp_path / "eval-reports" / "eval-2026-11-19-2.md")
    assert _files(tmp_path / "eval-reports") == ["eval-2026-11-19-2.md", "eval-2026-11-19.md"]


def test_report_can_be_disabled(client, fake_llm, capsys, tmp_path):
    fake_llm.script = [json.dumps(_answer(c["expected"])) for c in CASES]
    _, out = _run(client, capsys, tmp_path, mode="labelled", report_dir=None)
    assert "report_path" not in out
    assert _files(tmp_path / "eval-reports") == []


def test_labelled_mode_reports_failures_and_exits_nonzero(client, fake_llm, capsys, tmp_path):
    # Every answer is the all-null "not a receipt" response.
    nulls = _answer({}, needs_review=True)
    fake_llm.script = [json.dumps(nulls) for _ in CASES]
    code, out = _run(client, capsys, tmp_path, mode="labelled")
    assert code == run_eval.EXIT_FAILED
    assert out["categories"]["non_receipt"]["pass_rate"] == 1.0
    assert out["needs_review"]["recall"] == 1.0          # flagged everything...
    assert out["needs_review"]["precision"] < 1.0        # ...including clean receipts
    assert out["field_accuracy"]["vendor"]["accuracy"] < 0.5
    report = (tmp_path / "eval-reports" / "eval-2026-11-19.md").read_text()
    assert "`clean_pkr_receipt` (total_selection): vendor: expected 'Cafe Aroma', got None" in report


# --- pure scoring -------------------------------------------------------------------

def _case(name, expected, category="c"):
    return {"name": name, "category": category, "input": "x", "expected": expected}


def test_field_accuracy_only_counts_labelled_fields():
    cases = [
        _case("a", {"vendor": "Shop", "total_amount": 10}),
        _case("b", {"date": "2025-01-01"}),
    ]
    responses = [_ok(_answer({}, vendor="Shop", total_amount=11)), _ok(_answer({}, date="2025-01-01"))]
    s = run_eval.score_labelled(cases, responses)
    fa = s["field_accuracy"]
    assert fa["vendor"] == {"correct": 1, "total": 1, "accuracy": 1.0}
    assert fa["total"] == {"correct": 0, "total": 1, "accuracy": 0.0}
    assert fa["date"] == {"correct": 1, "total": 1, "accuracy": 1.0}
    assert fa["currency"] == {"correct": 0, "total": 0, "accuracy": None}
    assert (s["passed"], s["failed"]) == (1, 1)


@pytest.mark.parametrize("field, expected, actual, match", [
    ("vendor", "GREENLEAF  GROCERY", "Greenleaf Grocery", True),
    ("vendor", "Riverbend Books", "  Riverbend   Books ", True),
    ("vendor", "Riverbend Books", "Riverbend", False),
    ("total_amount", 5.5, 5.50, True),
    ("total_amount", 1082, 1082.004, True),
    ("total_amount", 1082, 1082.02, False),
    ("total_amount", 10, "ten", False),
    ("date", "2025-06-12", "2025-06-12", True),
    ("date", "2025-06-12", "12/06/2025", False),
    ("currency", None, None, True),
    ("currency", None, "PKR", False),
    ("currency", "PKR", None, False),
    ("needs_review", True, True, True),
    ("needs_review", False, True, False),
])
def test_field_matches(field, expected, actual, match):
    assert run_eval.field_matches(field, expected, actual) is match


def test_needs_review_precision_and_recall():
    cases = [
        _case("tp", {"needs_review": True}),
        _case("tp2", {"needs_review": True}),
        _case("fn", {"needs_review": True}),
        _case("fp", {"needs_review": False}),
        _case("tn", {"needs_review": False}),
        _case("tn2", {"needs_review": False}),
        _case("unlabelled", {"vendor": None}),   # no needs_review label: not counted
    ]
    preds = [True, True, False, True, False, False, True]
    responses = [_ok(_answer({}, needs_review=p)) for p in preds]
    nr = run_eval.score_labelled(cases, responses)["needs_review"]
    assert (nr["tp"], nr["fp"], nr["fn"], nr["tn"]) == (2, 1, 1, 2)
    assert nr["cases"] == 6
    assert nr["precision"] == pytest.approx(2 / 3)
    assert nr["recall"] == pytest.approx(2 / 3)
    assert nr["accuracy"] == pytest.approx(4 / 6)


def test_precision_is_none_when_nothing_flagged():
    cases = [_case("a", {"needs_review": False})]
    nr = run_eval.score_labelled(cases, [_ok(_answer({}, needs_review=False))])["needs_review"]
    assert nr["precision"] is None
    assert nr["recall"] is None


def test_request_errors_count_as_wrong_and_as_missed_flags():
    cases = [
        _case("err_flag", {"vendor": None, "needs_review": True}),
        _case("err_clean", {"vendor": "Shop", "needs_review": False}),
    ]
    responses = [{"status": 502, "body": {"detail": "x"}, "text": "upstream"}] * 2
    s = run_eval.score_labelled(cases, responses)
    assert s["errors"] == 2 and s["passed"] == 0
    assert s["field_accuracy"]["vendor"] == {"correct": 0, "total": 2, "accuracy": 0.0}
    nr = s["needs_review"]
    assert (nr["tp"], nr["fp"], nr["fn"], nr["tn"]) == (0, 0, 1, 0)
    assert nr["accuracy"] == 0.0
    assert s["failures"][0]["reason"].startswith("HTTP 502")


def test_categories_are_scored_separately():
    cases = [
        _case("a1", {"vendor": "A"}, "alpha"),
        _case("a2", {"vendor": "A"}, "alpha"),
        _case("b1", {"vendor": "B"}, "beta"),
        {"name": "u", "input": "x", "expected": {"vendor": None}},   # no category
    ]
    responses = [_ok(_answer({}, vendor="A")), _ok(_answer({}, vendor="nope")),
                 _ok(_answer({}, vendor="B")), _ok(_answer({}, vendor=None))]
    cats = run_eval.score_labelled(cases, responses)["categories"]
    assert list(cats) == ["alpha", "beta", "uncategorized"]
    assert cats["alpha"]["pass_rate"] == 0.5
    assert cats["alpha"]["fields"]["vendor"] == {"correct": 1, "total": 2, "accuracy": 0.5}
    assert cats["beta"]["passed"] == 1
    assert cats["uncategorized"]["cases"] == 1


def test_score_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        run_eval.score_labelled([_case("a", {})], [])


def test_render_report_handles_empty_metrics():
    s = {**run_eval.score_labelled([], []), "llm_mode": "live", "prompt_version": "p", "model": "m"}
    report = run_eval.render_report(s, NOW, "http://x")
    assert "| Precision | n/a |" in report
    assert "| vendor | 0 | 0 | n/a |" in report
