"""Run all benchmarks (Project A, Task A5 report, Project B detectors, hybrid) without the test steps.

Usage: python scripts/run_benchmarks.py [--a-only | --b-only]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-only", action="store_true")
    ap.add_argument("--b-only", action="store_true")
    a = ap.parse_args()
    if not a.b_only:
        from project_a.benchmark import run as run_a
        from project_a.law_change_migration import build_report
        run_a()
        build_report()
        print("Project A benchmark + migration report written to benchmarks/project_a/")
    if not a.a_only:
        from project_b.benchmark import run as run_b
        from project_b.cli import run_hybrid
        run_b()
        h = run_hybrid()
        print("Project B results written to benchmarks/project_b/;", json.dumps(h["any_problem_row_level"]))
