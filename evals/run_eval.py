"""Runs evals/cases.json against a running instance of the API.

Usage:
    python -m evals.run_eval [--base-url http://127.0.0.1:8000]
                             [--mode labelled|contract]
                             [--require-llm-mode live|stub|disabled|any]

Modes:
    labelled  (default) Scores each case on whether every key present in
              "expected" matches the endpoint's response. Only meaningful
              against a real model, so by default it first checks GET /health
              and refuses to run unless the server reports llm_mode == "live".
              This is what stops a CI job from "evaluating" stub responses.
    contract  Deterministic, no model needed: every case must return HTTP 200
              with a schema-valid ExtractResult, and one background job must
              round-trip through POST /jobs/extract -> GET /jobs/{id}. Used by
              CI against a server in stub mode.

Prints a JSON summary and exits non-zero on any failure.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.llm.schema import ExtractResult

CASES_PATH = Path(__file__).parent / "cases.json"
RESULT_FIELDS = set(ExtractResult.model_fields)
EXIT_FAILED = 1
EXIT_WRONG_MODE = 2


def _server_llm_mode(client) -> str:
    try:
        resp = client.get("/health")
        return resp.json().get("llm_mode", "unknown") if resp.status_code == 200 else "unknown"
    except (httpx.HTTPError, ValueError):
        return "unknown"


def _check_labelled(case: dict, actual: dict) -> list[str]:
    return [
        f"{k}: expected {v!r}, got {actual.get(k)!r}"
        for k, v in case["expected"].items()
        if actual.get(k) != v
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


def run(base_url: str = "http://127.0.0.1:8000", mode: str = "labelled",
        require_llm_mode: str | None = None, client=None, check_jobs: bool = True) -> int:
    if require_llm_mode is None:
        require_llm_mode = "live" if mode == "labelled" else "any"

    cases = json.loads(CASES_PATH.read_text())
    owns_client = client is None
    if owns_client:
        client = httpx.Client(base_url=base_url, timeout=60.0)

    try:
        server_mode = _server_llm_mode(client)
        if require_llm_mode != "any" and server_mode != require_llm_mode:
            print(json.dumps({
                "error": "server is not in the required LLM mode; refusing to evaluate",
                "required_llm_mode": require_llm_mode,
                "server_llm_mode": server_mode,
            }, indent=2))
            return EXIT_WRONG_MODE

        passed = 0
        failures = []
        for case in cases:
            resp = client.post("/extract", json={"text": case["input"]})
            if resp.status_code != 200:
                failures.append((case["name"], f"HTTP {resp.status_code}: {resp.text}"))
                continue
            actual = resp.json()
            problems = _check_labelled(case, actual) if mode == "labelled" else _check_contract(actual)
            if problems:
                failures.append((case["name"], "; ".join(problems)))
            else:
                passed += 1

        total = len(cases)
        if mode == "contract" and check_jobs:
            total += 1
            problems = _check_job_roundtrip(client, cases[0]["input"])
            if problems:
                failures.append(("background_job_roundtrip", "; ".join(problems)))
            else:
                passed += 1
    finally:
        if owns_client:
            client.close()

    summary = {
        "mode": mode,
        "llm_mode": server_mode,
        "passed": passed,
        "total": total,
        "failed": len(failures),
        "failures": [{"name": name, "reason": reason} for name, reason in failures],
    }
    print(json.dumps(summary, indent=2))
    return 0 if not failures else EXIT_FAILED


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mode", choices=["labelled", "contract"], default="labelled")
    parser.add_argument("--require-llm-mode", choices=["live", "stub", "disabled", "any"], default=None,
                        help="defaults to 'live' for labelled mode and 'any' for contract mode")
    args = parser.parse_args()
    sys.exit(run(args.base_url, args.mode, args.require_llm_mode))
