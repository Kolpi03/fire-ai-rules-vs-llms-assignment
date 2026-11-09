"""Task A5: measure how each engine absorbs the Finance Act 2025 (FY 2025-26) change.

What is measured (all reproducible except the wall-clock implementation times, which were recorded
by hand while doing the migration and live in ``benchmarks/project_a/migration_log.json``):

1. ``probe_fy`` — what each engine does when asked for a financial year: accuracy on that year's test
   cases, or the exception it raises. Run BEFORE migration it shows whether failures are loud.
2. ``silent_failure_probe`` — the classic silent failure: a system that ignores the requested year and
   answers FY 2025-26 questions with FY 2024-25 law. We run the FY 2024-25 rules on the FY 2025-26 cases
   and count how many answers would have been wrong *without any error being raised*.
3. ``mutation_test`` — seeded, realistic migration mistakes applied to the FY 2025-26 rule set
   (e.g. slabs updated but rebate not). For each mutant: how many FY 2025-26 test cases fail. A mutant
   that no test catches would ship a silent failure.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Callable

from common.paths import BENCH_A
from project_a import llm_compiled_engine, llm_query_engine, rules_engine
from project_a.benchmark import is_exact, load_cases
from project_a.models import EngineStatus, TaxpayerProfile
from project_a.rule_registry import RuleSet, get_rule_set

ENGINES: dict[str, Callable] = {"rules_engine": rules_engine.compute,
                                "llm_compiled_engine": llm_compiled_engine.compute,
                                "llm_query_engine": llm_query_engine.compute}


def _benchmark_llm_measurement(fy: str) -> dict[str, Any]:
    path = BENCH_A / "benchmark_results.json"
    e = json.loads(path.read_text()).get("by_fy", {}).get(fy, {}).get("llm_query_engine", {}) if path.exists() else {}
    if str(e.get("status", "")).startswith("measured") and e.get("n_cases"):
        n = e["n_cases"]
        return {"outcome": "answered", "rupee_exact": round(e["rupee_exact_accuracy"] * n), "of": n,
                "accuracy": e["rupee_exact_accuracy"], "model": e.get("model"),
                "source": "benchmarks/project_a/benchmark_results.json (real Gemini calls on a random sample of this "
                          "FY's cases); set MIGRATION_LLM_PROBE=1 for a full per-case probe"}
    return {"outcome": "not measured (no real Engine B measurement for this FY)"}


def _stored_llm_probe(fy: str) -> dict[str, Any] | None:
    """A stored *real* Engine B probe for this FY (reused unless LLM_REMEASURE=1; 1 real call per case otherwise)."""
    import os
    path = BENCH_A / "migration_report.json"
    if os.environ.get("LLM_REMEASURE") == "1" or not path.exists():
        return None
    prev = json.loads(path.read_text())
    for key in ("after_migration", "fy2024_25_still_correct"):
        sec = prev.get(key) or {}
        e = sec.get("engines", {}).get("llm_query_engine", {})
        if sec.get("fy") == fy and e.get("outcome") == "answered":
            return {**e, "reused_from_stored_report": True}
    return None


def probe_fy(fy: str) -> dict[str, Any]:
    cases = load_cases(fy)
    out = {}
    for name, fn in ENGINES.items():
        if name == "llm_query_engine" and (stored := _stored_llm_probe(fy)):
            out[name] = stored
            continue
        if name == "llm_query_engine" and os.environ.get("MIGRATION_LLM_PROBE") != "1":
            # No silent quota spend: a full per-case probe is 1 real call per case (307 calls for both years).
            # Default: cite the real Engine B measurement of this FY from the benchmark (sampled cases).
            out[name] = _benchmark_llm_measurement(fy)
            continue
        try:
            ok = 0
            status = None
            for c in cases:
                r = fn(TaxpayerProfile(**c["input"]))
                status = r.status
                if r.status == EngineStatus.UNAVAILABLE:
                    break
                ok += is_exact(r, c["expected"])
            if status == EngineStatus.UNAVAILABLE:
                out[name] = {"outcome": "unavailable (no GEMINI_API_KEY)"}
            else:
                out[name] = {"outcome": "answered", "rupee_exact": ok, "of": len(cases), "accuracy": ok / len(cases)}
        except Exception as exc:  # noqa: BLE001 - we are measuring exactly this
            out[name] = {"outcome": "loud_error", "error_type": type(exc).__name__, "error": str(exc)[:200]}
    return {"fy": fy, "n_cases": len(cases), "engines": out}


def silent_failure_probe() -> dict[str, Any]:
    """Answer FY 2025-26 cases with FY 2024-25 law (what a year-blind system would do)."""
    cases = load_cases("2025-26")
    wrong, wrong_reco, examples = 0, 0, []
    for c in cases:
        p = TaxpayerProfile(**{**c["input"], "financial_year": "2024-25"})
        r = rules_engine.compute(p)
        if not is_exact(r, c["expected"]):
            wrong += 1
            if len(examples) < 5:
                examples.append({"case_id": c["case_id"], "expected_new": c["expected"]["new"]["tax_payable"],
                                 "fy2024_25_law_new": int(r.new.tax_payable)})
        wrong_reco += r.recommended_regime != c["expected"]["recommended_regime"]
    return {"n_cases": len(cases), "silently_wrong": wrong, "silently_wrong_share": wrong / len(cases),
            "wrong_regime_recommendation": wrong_reco, "errors_raised": 0, "examples": examples}


def _run_ruleset(rs: RuleSet, cases: list[dict]) -> int:
    failures = 0
    for c in cases:
        p = TaxpayerProfile(**c["input"])
        old = rules_engine.compute_regime(p, rs, "old")
        new = rules_engine.compute_regime(p, rs, "new")
        e = c["expected"]
        if int(old.tax_payable) != e["old"]["tax_payable"] or int(new.tax_payable) != e["new"]["tax_payable"]:
            failures += 1
    return failures


MUTANTS: dict[str, Callable[[dict], None]] = {
    "slabs updated, 87A limit/max left at FY24-25 values":
        lambda r: r["regimes"]["new"]["rebate_87a"].update(income_limit=700000, max_rebate=25000),
    "87A limit raised to 12L but max rebate left at 25,000":
        lambda r: r["regimes"]["new"]["rebate_87a"].update(max_rebate=25000),
    "87A max raised to 60,000 but limit left at 7L":
        lambda r: r["regimes"]["new"]["rebate_87a"].update(income_limit=700000),
    "87A marginal relief dropped":
        lambda r: r["regimes"]["new"]["rebate_87a"].update(marginal_relief=False),
    "new 25% slab (20-24L) omitted (30% from 20L)":
        lambda r: r["regimes"]["new"]["slabs"]["by_age_band"].update(
            all=[s for s in r["regimes"]["new"]["slabs"]["by_age_band"]["all"] if s[0] != 2000000][:-1]
            + [[2000000, None, "0.30"]]),
    "slab boundary typo 16L -> 15L":
        lambda r: r["regimes"]["new"]["slabs"]["by_age_band"].update(
            all=[[lo, (1500000 if hi == 1600000 else hi), rt] if True else None for lo, hi, rt in
                 [(x[0] if x[0] != 1600000 else 1500000, x[1], x[2]) for x in r["regimes"]["new"]["slabs"]["by_age_band"]["all"]]]),
    "EQUIVALENT MUTANT: old-regime 87A max changed to 60,000 (old tax at TI<=5L never exceeds 12,500)":
        lambda r: r["regimes"]["old"]["rebate_87a"].update(max_rebate=60000),
    "old-regime 87A limit wrongly raised to 7L (new-regime change applied to old regime)":
        lambda r: r["regimes"]["old"]["rebate_87a"].update(income_limit=700000),
    "standard deduction (new) reverted to 50,000":
        lambda r: r["regimes"]["new"]["standard_deduction"].update(amount=50000),
}


def mutation_test() -> dict[str, Any]:
    base = get_rule_set("2025-26").raw
    cases = load_cases("2025-26")
    out = {}
    for name, mutate in MUTANTS.items():
        raw = copy.deepcopy(base)
        mutate(raw)
        rs = RuleSet(raw)
        fails = _run_ruleset(rs, cases)
        out[name] = {"failing_cases": fails, "of": len(cases), "caught": fails > 0}
    return {"n_mutants": len(MUTANTS), "caught": sum(v["caught"] for v in out.values()), "mutants": out}


def _compiled_mutants() -> dict[str, Callable[[dict], None]]:
    def step(art: dict, sid: str) -> dict:
        return next(s for s in art["regimes"]["new"]["steps"] if s["id"] == sid)

    def partial_threshold(art: dict) -> None:  # only the FIRST 1200000 literal updated, others left at 700000
        st = step(art, "NEW.87A")
        head, tail = st["expr"].split("1200000", 1)
        st["expr"] = head + "1200000" + tail.replace("1200000", "700000")

    def stale_max(art: dict) -> None:
        st = step(art, "NEW.87A")
        st["expr"] = st["expr"].replace("60000", "25000")

    def stale_table(art: dict) -> None:
        art["tables"]["new_115bac"] = [[0, 300000, "0"], [300000, 700000, "0.05"], [700000, 1000000, "0.10"],
                                       [1000000, 1200000, "0.15"], [1200000, 1500000, "0.20"], [1500000, None, "0.30"]]

    def stale_passages(art: dict) -> None:
        for st in art["regimes"]["new"]["steps"]:
            st["passage_id"] = st["passage_id"].replace("P25-", "P24-")

    return {"87A threshold updated in 1 of 3 places": partial_threshold,
            "87A max rebate left at 25,000": stale_max,
            "slab table not recompiled": stale_table,
            "passage citations left pointing at FY 2024-25 text": stale_passages}


def compiled_mutation_test() -> dict[str, Any]:
    from project_a.rule_compiler import validate_artifact, verify_against_suite
    base = llm_compiled_engine.load_artifact("2025-26")
    out = {}
    for name, mutate in _compiled_mutants().items():
        art = copy.deepcopy(base)
        mutate(art)
        problems = validate_artifact(art, "2025-26")
        ver = verify_against_suite(art, "2025-26")
        out[name] = {"static_validation_problems": len(problems), "failing_cases": ver["failed"],
                     "of": ver["cases"], "caught": bool(problems) or ver["failed"] > 0,
                     "caught_by": "static validation" if problems else ("test suite" if ver["failed"] else "NOTHING")}
    return {"n_mutants": len(out), "caught": sum(v["caught"] for v in out.values()), "mutants": out}


def build_report(save: bool = True) -> dict[str, Any]:
    log_path = BENCH_A / "migration_log.json"
    report: dict[str, Any] = {
        "manual_log": json.loads(log_path.read_text()) if log_path.exists() else None,
        "before_migration": json.loads((BENCH_A / "migration_before.json").read_text())
        if (BENCH_A / "migration_before.json").exists() else None,
        "after_migration": probe_fy("2025-26"),
        "fy2024_25_still_correct": probe_fy("2024-25"),
        "silent_failure_probe": silent_failure_probe(),
        "mutation_test": mutation_test(),
        "compiled_mutation_test": compiled_mutation_test(),
        "differential_fuzz_fy2025_26": differential_fuzz("2025-26", 5000),
    }
    if save:
        (BENCH_A / "migration_report.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def differential_fuzz(fy: str, n: int = 5000, seed: int = 7) -> dict[str, Any]:
    """Random-profile differential test: Engine C (approved artifact) vs Engine A."""
    from project_a.differential import random_profile
    import random
    rng = random.Random(seed)
    mism = 0
    for _ in range(n):
        p = random_profile(rng, fy)
        a, c = rules_engine.compute(p), llm_compiled_engine.compute(p)
        mism += any(getattr(a, r).key_figures() != getattr(c, r).key_figures() for r in ("old", "new"))
    return {"fy": fy, "profiles": n, "mismatches": mism}


def record_before() -> dict[str, Any]:
    """Run once, BEFORE migrating, to capture how each engine reacts to an FY 2025-26 request."""
    res = {"fy2025_26_probe": probe_fy("2025-26"), "silent_failure_probe": silent_failure_probe()}
    BENCH_A.mkdir(parents=True, exist_ok=True)
    (BENCH_A / "migration_before.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    print(json.dumps(build_report(), indent=1, default=str))
