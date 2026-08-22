"""Benchmark a running Receipt Extractor API using the labelled cases.

Usage: python benchmarks/bench_extract.py --base-url http://127.0.0.1:8000 --runs 2
"""
from __future__ import annotations
import argparse, json, statistics, time
from pathlib import Path
import httpx

CASES = Path(__file__).parents[1] / "evals" / "cases.json"

def main() -> None:
    p = argparse.ArgumentParser(); p.add_argument("--base-url", default="http://127.0.0.1:8000"); p.add_argument("--runs", type=int, default=1); args = p.parse_args()
    cases = json.loads(CASES.read_text())
    latencies=[]; errors=0; reviews=0; total=0
    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        for _ in range(args.runs):
            for case in cases:
                started=time.perf_counter(); response=client.post("/extract", json={"text":case["input"]}); latencies.append((time.perf_counter()-started)*1000); total+=1
                if response.status_code != 200: errors += 1; continue
                if response.json().get("needs_review") is True: reviews += 1
    result={"requests":total,"errors":errors,"error_rate":errors/total if total else None,"needs_review_rate":reviews/total if total else None,"latency_ms":{"p50":statistics.median(latencies) if latencies else None,"p95":sorted(latencies)[max(0, int(len(latencies)*.95)-1)] if latencies else None,"max":max(latencies) if latencies else None}}
    print(json.dumps(result, indent=2))

if __name__ == "__main__": main()
