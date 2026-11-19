"""Runs evals/cases.json against a running instance of the API.

Usage:
    python -m evals.run_eval [--base-url http://127.0.0.1:8000]
                             [--mode labelled|contract]
                             [--require-llm-mode live|stub|disabled|any]
                             [--report-dir reports] [--no-report]

Modes:
    labelled  (default) Scores each case against its hand-written "expected"
              fields. Only meaningful against a real model, so by default it
              first checks GET /health and refuses to run unless the server
              reports llm_mode == "live". This is what stops a CI job from
              "evaluating" stub responses. Reports:
                - overall pass count (a case passes when every expected field matches)
                - accuracy per field: vendor, date, total, currency
                - needs_review precision and recall (positive class = needs_review true)
                - results per category
                - the prompt version and model the server reported
              and writes a Markdown report to reports/eval-YYYY-MM-DD.md.
    contract  Deterministic, no model needed: every case must return HTTP 200
              with a schema-valid ExtractResult, and one background job must
              round-trip through POST /jobs/extract -> GET /jobs/{id}. Used by
              CI against a server in stub mode. Writes no report.

Scoring rules:
    - A field is scored only for cases whose "expected" contains it. Fields
      whose correct value is debatable are deliberately left out of a case.
    - vendor: compared case-insensitively with whitespace collapsed.
    - total: numeric, within 0.005.
    - date, currency, needs_review: exact.
    - A case whose request failed (non-200) counts as wrong for every field it
      expected, and as a missed flag if it expected needs_review true.

Prints a JSON summary and exits non-zero on any failure.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.llm.schema import ExtractResult

CASES_PATH = Path(__file__).parent / "cases.json"
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"
RESULT_FIELDS = set(ExtractResult.model_fields)
EXIT_FAILED = 1
EXIT_WRONG_MODE = 2

# Reported metric name -> ExtractResult field.
SCORED_FIELDS = {"vendor": "vendor", "date": "date", "total": "total_amount", "currency": "currency"}
TOTAL_TOLERANCE = 0.005
UNCATEGORIZED = "uncategorized"


# --- server metadata ------------------------------------------------------------

def _server_health(client) -> dict:
    try:
        resp = client.get("/health")
        return resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return {}


# --- comparison -------------------------------------------------------------------

def _normalize_text(value):
    return " ".join(value.split()).casefold() if isinstance(value, str) else value


def field_matches(field: str, expected, actual) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    if field == "total_amount":
        try:
            return abs(float(actual) - float(expected)) <= TOTAL_TOLERANCE
        except (TypeError, ValueError):
            return False
    if field == "vendor":
        return _normalize_text(actual) == _normalize_text(expected)
    return actual == expected


def _check_labelled(case: dict, actual: dict) -> list[str]:
    return [
        f"{k}: expected {v!r}, got {actual.get(k)!r}"
        for k, v in case["expected"].items()
        if not field_matches(k, v, actual.get(k))
    ]


def _check_contract(actual: dict) -> list[str]:
    problems = []
    if set(actual) != RESULT_FIELDS:
        problems.append(f"fields: expected {sorted(RESULT_FIELDS)}, got {sorted(actual)}")
    try:
        ExtractResult.model_validate(actual)
    except ValidationError as exc:
        problems.append(f"schema: {exc.error_count()} validation error(s)")
    return problems


# --- scoring ----------------------------------------------------------------------

def _ratio(numerator: int, denominator: int):
    return numerator / denominator if denominator else None


def _new_field_counts() -> dict:
    return {name: {"correct": 0, "total": 0} for name in SCORED_FIELDS}


def _finish_field_counts(counts: dict) -> dict:
    return {name: {**c, "accuracy": _ratio(c["correct"], c["total"])} for name, c in counts.items()}


def score_labelled(cases: list[dict], responses: list[dict]) -> dict:
    """Pure scoring: no I/O. `responses[i]` is {"status": int, "body": dict | None,
    "text": str} for `cases[i]`."""
    if len(cases) != len(responses):
        raise ValueError("cases and responses must have the same length")

    fields = _new_field_counts()
    nr = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "correct": 0, "cases": 0}
    categories: dict[str, dict] = {}
    failures = []
    passed = errors = 0

    for case, resp in zip(cases, responses):
        category = case.get("category") or UNCATEGORIZED
        cat = categories.setdefault(category, {"cases": 0, "passed": 0, "errors": 0, "fields": _new_field_counts()})
        cat["cases"] += 1

        expected = case["expected"]
        ok = resp.get("status") == 200 and isinstance(resp.get("body"), dict)
        actual = resp["body"] if ok else {}

        for name, field in SCORED_FIELDS.items():
            if field not in expected:
                continue
            hit = ok and field_matches(field, expected[field], actual.get(field))
            for bucket in (fields[name], cat["fields"][name]):
                bucket["total"] += 1
                bucket["correct"] += int(hit)

        if "needs_review" in expected:
            nr["cases"] += 1
            want = bool(expected["needs_review"])
            got = actual.get("needs_review") if ok else None
            if got is True:
                nr["tp" if want else "fp"] += 1
            elif got is False:
                nr["fn" if want else "tn"] += 1
            elif want:
                nr["fn"] += 1  # failed request: the flag we needed was not raised
            nr["correct"] += int(got is not None and got == want)

        if not ok:
            errors += 1
            cat["errors"] += 1
            reason = f"HTTP {resp.get('status')}: {str(resp.get('text', ''))[:200]}"
            failures.append({"name": case["name"], "category": category, "reason": reason})
            continue

        mismatches = _check_labelled(case, actual)
        if mismatches:
            failures.append({"name": case["name"], "category": category, "reason": "; ".join(mismatches)})
        else:
            passed += 1
            cat["passed"] += 1

    total = len(cases)
    return {
        "passed": passed,
        "total": total,
        "failed": total - passed,
        "errors": errors,
        "field_accuracy": _finish_field_counts(fields),
        "needs_review": {
            **nr,
            "accuracy": _ratio(nr["correct"], nr["cases"]),
            "precision": _ratio(nr["tp"], nr["tp"] + nr["fp"]),
            "recall": _ratio(nr["tp"], nr["tp"] + nr["fn"]),
        },
        "categories": {
            name: {
                "cases": c["cases"],
                "passed": c["passed"],
                "pass_rate": _ratio(c["passed"], c["cases"]),
                "errors": c["errors"],
                "fields": _finish_field_counts(c["fields"]),
            }
            for name, c in sorted(categories.items())
        },
        "failures": failures,
    }


# --- report -----------------------------------------------------------------------

def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _fraction(counts: dict) -> str:
    return "—" if not counts["total"] else f"{counts['correct']}/{counts['total']}"


def render_report(summary: dict, run_at: datetime, base_url: str) -> str:
    nr = summary["needs_review"]
    lines = [
        f"# Labelled evaluation — {run_at:%Y-%m-%d}",
        "",
        "| | |",
        "|---|---|",
        f"| Run at (UTC) | {run_at:%Y-%m-%d %H:%M:%S} |",
        f"| Server | `{base_url}` |",
        f"| LLM mode | {summary['llm_mode']} |",
        f"| Prompt version | {summary['prompt_version']} |",
        f"| Model | {summary['model']} |",
        f"| Cases | {summary['total']} |",
        f"| Passed (every expected field correct) | {summary['passed']}/{summary['total']} "
        f"({_pct(_ratio(summary['passed'], summary['total']))}) |",
        f"| Request errors | {summary['errors']} |",
        "",
        "## Field accuracy",
        "",
        "Each field is scored only on cases that label it.",
        "",
        "| Field | Correct | Scored cases | Accuracy |",
        "|---|---|---|---|",
    ]
    for name, c in summary["field_accuracy"].items():
        lines.append(f"| {name} | {c['correct']} | {c['total']} | {_pct(c['accuracy'])} |")
    lines += [
        "",
        "## needs_review",
        "",
        "Positive class: `needs_review: true` (the model flagged the receipt for a human).",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Precision | {_pct(nr['precision'])} |",
        f"| Recall | {_pct(nr['recall'])} |",
        f"| Accuracy | {_pct(nr['accuracy'])} ({nr['correct']}/{nr['cases']}) |",
        f"| TP / FP / FN / TN | {nr['tp']} / {nr['fp']} / {nr['fn']} / {nr['tn']} |",
        "",
        "## By category",
        "",
        "| Category | Cases | Passed | Pass rate | Vendor | Date | Total | Currency | Errors |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, c in summary["categories"].items():
        f = c["fields"]
        lines.append(
            f"| {name} | {c['cases']} | {c['passed']} | {_pct(c['pass_rate'])} | "
            f"{_fraction(f['vendor'])} | {_fraction(f['date'])} | {_fraction(f['total'])} | "
            f"{_fraction(f['currency'])} | {c['errors']} |"
        )
    lines += ["", "## Failures", ""]
    if summary["failures"]:
        lines += [f"- `{f['name']}` ({f['category']}): {f['reason'].replace('|', '/')}" for f in summary["failures"]]
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def write_report(content: str, report_dir: Path, run_at: datetime) -> Path:
    """reports/eval-YYYY-MM-DD.md; a second run on the same day gets -2, -3, ...
    instead of overwriting the first."""
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"eval-{run_at:%Y-%m-%d}"
    path = report_dir / f"{stem}.md"
    n = 2
    while path.exists():
        path = report_dir / f"{stem}-{n}.md"
        n += 1
    path.write_text(content, encoding="utf-8")
    return path


# --- runner -----------------------------------------------------------------------

def _check_job_roundtrip(client, text: str, timeout_s: float = 30.0) -> list[str]:
    resp = client.post("/jobs/extract", json={"text": text})
    if resp.status_code != 202:
        return [f"POST /jobs/extract: HTTP {resp.status_code}"]
    job_id = resp.json()["job_id"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = client.get(f"/jobs/{job_id}").json()
        if status["status"] == "succeeded":
            return _check_contract(status["result"])
        if status["status"] == "failed":
            return [f"job failed: {status.get('error_category')}"]
        time.sleep(0.2)
    return [f"job {job_id} did not finish within {timeout_s}s"]


def _post_case(client, case: dict) -> dict:
    resp = client.post("/extract", json={"text": case["input"]})
    try:
        body = resp.json()
    except ValueError:
        body = None
    return {"status": resp.status_code, "body": body, "text": resp.text}


def _run_contract(client, cases: list[dict], check_jobs: bool) -> dict:
    passed, failures = 0, []
    for case in cases:
        resp = _post_case(client, case)
        if resp["status"] != 200 or not isinstance(resp["body"], dict):
            failures.append({"name": case["name"], "reason": f"HTTP {resp['status']}: {resp['text']}"})
            continue
        problems = _check_contract(resp["body"])
        if problems:
            failures.append({"name": case["name"], "reason": "; ".join(problems)})
        else:
            passed += 1
    total = len(cases)
    if check_jobs:
        total += 1
        problems = _check_job_roundtrip(client, cases[0]["input"])
        if problems:
            failures.append({"name": "background_job_roundtrip", "reason": "; ".join(problems)})
        else:
            passed += 1
    return {"passed": passed, "total": total, "failed": len(failures), "failures": failures}


def run(base_url: str = "http://127.0.0.1:8000", mode: str = "labelled",
        require_llm_mode: str | None = None, client=None, check_jobs: bool = True,
        report_dir: Path | None = DEFAULT_REPORT_DIR, cases_path: Path = CASES_PATH,
        now: datetime | None = None) -> int:
    """report_dir=None disables the Markdown report (labelled mode only)."""
    if require_llm_mode is None:
        require_llm_mode = "live" if mode == "labelled" else "any"

    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    owns_client = client is None
    if owns_client:
        client = httpx.Client(base_url=base_url, timeout=60.0)

    try:
        health = _server_health(client)
        server_mode = health.get("llm_mode", "unknown")
        if require_llm_mode != "any" and server_mode != require_llm_mode:
            print(json.dumps({
                "error": "server is not in the required LLM mode; refusing to evaluate",
                "required_llm_mode": require_llm_mode,
                "server_llm_mode": server_mode,
            }, indent=2))
            return EXIT_WRONG_MODE

        meta = {
            "mode": mode,
            "llm_mode": server_mode,
            "prompt_version": health.get("prompt_version") or "unknown",
            "model": health.get("model") or "unknown",
        }
        if mode == "labelled":
            responses = [_post_case(client, case) for case in cases]
            summary = {**meta, **score_labelled(cases, responses)}
        else:
            summary = {**meta, **_run_contract(client, cases, check_jobs)}
    finally:
        if owns_client:
            client.close()

    if mode == "labelled" and report_dir is not None:
        run_at = now or datetime.now(timezone.utc)
        path = write_report(render_report(summary, run_at, base_url), Path(report_dir), run_at)
        summary["report_path"] = str(path)

    print(json.dumps(summary, indent=2))
    return 0 if not summary["failures"] else EXIT_FAILED


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mode", choices=["labelled", "contract"], default="labelled")
    parser.add_argument("--require-llm-mode", choices=["live", "stub", "disabled", "any"], default=None,
                        help="defaults to 'live' for labelled mode and 'any' for contract mode")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR),
                        help="where labelled mode writes eval-YYYY-MM-DD.md (default: reports/)")
    parser.add_argument("--no-report", action="store_true", help="don't write the Markdown report")
    args = parser.parse_args()
    sys.exit(run(args.base_url, args.mode, args.require_llm_mode,
                 report_dir=None if args.no_report else Path(args.report_dir)))
