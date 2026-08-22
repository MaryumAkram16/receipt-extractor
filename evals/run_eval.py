"""Runs evals/cases.json against a running instance of the API and prints a score.

Usage:
    python -m evals.run_eval [--base-url http://127.0.0.1:8000]

Scores each case on whether every key present in "expected" matches the
endpoint's response. Prints a pass/fail line per case and a summary count.
"""
import argparse
import json
import sys
from pathlib import Path

import httpx

CASES_PATH = Path(__file__).parent / "cases.json"


def run(base_url: str) -> int:
    cases = json.loads(CASES_PATH.read_text())
    passed = 0
    failures = []

    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        for case in cases:
            resp = client.post("/extract", json={"text": case["input"]})
            if resp.status_code != 200:
                failures.append((case["name"], f"HTTP {resp.status_code}: {resp.text}"))
                continue
            actual = resp.json()
            expected = case["expected"]
            mismatches = [
                f"{k}: expected {v!r}, got {actual.get(k)!r}"
                for k, v in expected.items()
                if actual.get(k) != v
            ]
            if mismatches:
                failures.append((case["name"], "; ".join(mismatches)))
            else:
                passed += 1

    total = len(cases)
    summary = {"passed": passed, "total": total, "failed": len(failures), "failures": [{"name": name, "reason": reason} for name, reason in failures]}
    print(json.dumps(summary, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    raise SystemExit(run(args.base_url))
