"""Sanity checks on the labelled dataset itself, so a malformed case fails CI
instead of silently skewing a live evaluation."""
import json
import re
from collections import Counter
from datetime import date

import pytest

from app.llm.schema import Currency, ExtractRequest, ExtractResult
from evals.run_eval import CASES_PATH

CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))
CATEGORIES = {
    "currency", "date_format", "total_selection", "ocr_noise",
    "discount_refund", "multi_page", "non_receipt", "ambiguous",
}
ORIGINAL_EIGHT = {
    "clean_pkr_receipt", "clean_usd_receipt", "no_vendor_name", "missing_total",
    "eur_invoice", "not_a_receipt", "ambiguous_currency", "gbp_receipt_no_date",
}


def test_case_count_and_original_cases_kept():
    assert len(CASES) == 50
    assert ORIGINAL_EIGHT <= {c["name"] for c in CASES}


def test_case_names_unique():
    names = [c["name"] for c in CASES]
    assert [n for n, k in Counter(names).items() if k > 1] == []


def test_every_category_is_known_and_covered():
    used = Counter(c["category"] for c in CASES)
    assert set(used) == CATEGORIES
    assert all(n >= 4 for n in used.values()), used


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_case_shape_is_valid(case):
    assert set(case) == {"name", "category", "input", "expected"}
    ExtractRequest(text=case["input"])                     # 1-4000 chars, not blank
    expected = case["expected"]
    assert expected, "a case must label at least one field"
    assert set(expected) <= set(ExtractResult.model_fields) - {"confidence"}
    if expected.get("date") is not None:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", expected["date"])
        date.fromisoformat(expected["date"])
    if expected.get("currency") is not None:
        Currency(expected["currency"])
    if expected.get("total_amount") is not None:
        assert expected["total_amount"] >= 0
    if "needs_review" in expected:
        assert isinstance(expected["needs_review"], bool)


@pytest.mark.parametrize("case", [c for c in CASES if c["expected"].get("needs_review") is False],
                         ids=lambda c: c["name"])
def test_needs_review_false_only_when_all_fields_present(case):
    # The prompt says needs_review must be true if any field is missing.
    exp = case["expected"]
    for field in ("vendor", "date", "total_amount", "currency"):
        assert exp.get(field) is not None, f"{case['name']}: {field} must be labelled and non-null"


@pytest.mark.parametrize("case", [c for c in CASES if c["category"] == "non_receipt"], ids=lambda c: c["name"])
def test_non_receipts_expect_all_null_and_review(case):
    assert case["expected"] == {"vendor": None, "date": None, "total_amount": None,
                                "currency": None, "needs_review": True}


@pytest.mark.parametrize("case", [c for c in CASES if c["category"] == "ambiguous"], ids=lambda c: c["name"])
def test_ambiguous_cases_expect_review(case):
    assert case["expected"].get("needs_review") is True
