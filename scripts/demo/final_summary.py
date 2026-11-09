"""Final results screen for the demo: every figure is read from the project's result files or a live validator run."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text())


def main() -> int:
    bench = load("benchmarks/project_a/benchmark_results.json")
    mig = load("benchmarks/project_a/migration_report.json")
    off = load("project_a/data/official_calculator_results.json")
    diff = load("benchmarks/project_a/differential_80d_per_person.json")
    hyb = load("benchmarks/project_b/hybrid_results.json")
    run = load("benchmarks/submission_summary.json")
    print("FINAL RESULTS (read from benchmarks/*.json and project_a/data/*.json)")
    print("=" * 72)
    for fy, e in bench["by_fy"].items():
        a, c, b = e["rules_engine"], e["llm_compiled_engine"], e["llm_query_engine"]
        print(f"FY {fy}: rules engine {a['rupee_exact_accuracy']:.0%} rupee-exact on {a['n_cases']} cases; "
              f"compiled rules {c['rupee_exact_accuracy']:.0%}; LLM at query time "
              f"{b['rupee_exact_accuracy']:.0%} on {b['n_cases']} sampled cases")
    for fy in ("2024_25", "2025_26"):
        r = load(f"project_a/compiled_rules/fy{fy}.compiled.json")["review"]
        print(f"Compiled rules FY {fy.replace('_', '-')}: {r['status'].upper()} by {r['reviewer']} on "
              f"{r['reviewed_on']}, {r['verification']['passed']}/{r['verification']['cases']} cases, "
              f"sha256 {r['artifact_sha256'][:16]}...")
    print(f"Official calculator: {off['verified_match']}/{off['cases_attempted']} cases match in both regimes")
    sp = mig["silent_failure_probe"]
    print(f"Law change: a year-blind system would be silently wrong on {sp['silently_wrong']}/{sp['n_cases']} "
          f"FY 2025-26 cases; mutants caught {mig['mutation_test']['caught']}/{mig['mutation_test']['n_mutants']} "
          f"(rules), {mig['compiled_mutation_test']['caught']}/{mig['compiled_mutation_test']['n_mutants']} (compiled)")
    profiles = diff["profiles_per_run"] * len(diff["runs"])
    mism = sum(v["engineA_vs_candidate_mismatches"] + v["engineA_vs_oracle_mismatches"] for v in diff["runs"].values())
    print(f"Random-profile comparison (80D): {profiles:,} profiles, {mism} mismatches")
    a = hyb["any_problem_row_level"]
    print(f"NYC 311 hybrid data-quality pipeline: precision {a['precision']:.3f}, recall {a['recall']:.3f}, "
          f"F1 {a['f1']:.3f}")
    tests = [s.get("detail", {}).get("summary") for s in run["steps"] if "tests" in s["step"]]
    print(f"Last full run (run_all.py --skip-download, {run['finished_at']}): " + "; ".join(t for t in tests if t))
    val = subprocess.run([sys.executable, "scripts/validate_submission.py"], cwd=ROOT, capture_output=True, text=True)
    print("Validator (live): " + val.stdout.strip().splitlines()[-1])
    return val.returncode


if __name__ == "__main__":
    raise SystemExit(main())
