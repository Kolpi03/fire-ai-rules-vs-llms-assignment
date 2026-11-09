"""Task A4 benchmark: accuracy, determinism, latency, cost and traceability for the three engines.

Definitions (also printed in RESULTS_A.md)
* **Rupee-exact accuracy** — share of test cases where, for BOTH regimes, total income and tax payable
  equal the expected values exactly AND the recommended regime matches.
* **Component accuracy** — share of cases where every component (slab tax, rebate, surcharge, cess) is
  within Rs 0.01 of the expected value.
* **Determinism** — each sampled case is executed ``determinism_repeats`` times; an engine is
  deterministic on a case when all runs produce an identical hash of the key figures.
* **Latency** — wall-clock per request (both regimes + recommendation), avg / median / p95.
* **Cost per 1,000 requests** — deterministic engines: measured CPU seconds x compute price;
  query-time LLM: measured tokens x list price (or a clearly labelled projection when no key).
* **Traceability** — share of results in which every explanation line carries a clause and a rule id
  that resolves (rule-set ids for Engine A, approved artifact step ids for Engine C, retrieved passage
  ids for Engine B) AND every non-zero tax component is covered by such a line.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import statistics
import time
from typing import Any, Callable

from common.config import load_yaml, settings
from common.llm_client import estimate_tokens, llm_status, model_name, price_for
from common.paths import BENCH_A, PA_DATA
from project_a import llm_compiled_engine, llm_query_engine, rules_engine
from project_a.models import EngineStatus, TaxComputation, TaxpayerProfile
from project_a.rule_registry import get_rule_set

COMPONENT_RULES_A = {"tax_on_income": "SLABS", "rebate_87a": "87A", "surcharge": "SURCHARGE", "cess": "CESS",
                     "tax_payable": "288B"}


def load_cases(fy: str | None = None) -> list[dict[str, Any]]:
    cases = json.loads((PA_DATA / "tax_test_cases.json").read_text())["cases"]
    return [c for c in cases if fy is None or c["input"]["financial_year"] == fy]


def fingerprint(c: TaxComputation) -> str:
    if c.old is None or c.new is None:
        return f"status:{c.status.value}"
    payload = json.dumps({"old": c.old.key_figures(), "new": c.new.key_figures(), "rec": c.recommended_regime},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def is_exact(c: TaxComputation, exp: dict[str, Any]) -> bool:
    if c.old is None or c.new is None:
        return False
    for reg in ("old", "new"):
        got, e = getattr(c, reg), exp[reg]
        if int(round(got.tax_payable)) != e["tax_payable"] or int(round(got.total_income)) != e["total_income"]:
            return False
    return c.recommended_regime == exp["recommended_regime"]


def components_ok(c: TaxComputation, exp: dict[str, Any]) -> bool:
    if c.old is None or c.new is None:
        return False
    return all(abs(float(getattr(getattr(c, reg), k)) - float(exp[reg][k])) < 0.01
               for reg in ("old", "new") for k in ("tax_on_income", "rebate_87a", "surcharge", "cess"))


def traceable(c: TaxComputation, valid_ids: set[str], component_map: dict[str, str] | None) -> bool:
    if c.old is None or c.new is None:
        return False
    for r in (c.old, c.new):
        if any(not ln.clause or ln.rule_id not in valid_ids for ln in r.lines):
            return False
        if component_map is not None:
            for comp, token in component_map.items():
                if getattr(r, comp) != 0 and not any(token in ln.rule_id for ln in r.lines):
                    return False
    return True


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else float("nan")


def _latency_stats(ms: list[float]) -> dict[str, float]:
    return {"avg_ms": statistics.fmean(ms), "median_ms": statistics.median(ms), "p95_ms": _pct(ms, 0.95),
            "n": len(ms)}


def _deterministic_engine_report(name: str, fn: Callable[[TaxpayerProfile], TaxComputation], fy: str,
                                 cases: list[dict], cfg: dict, valid_ids: set[str],
                                 component_map: dict[str, str] | None) -> dict[str, Any]:
    profiles = [TaxpayerProfile(**c["input"]) for c in cases]
    exact = comp = trace = 0
    failures = []
    for c, p in zip(cases, profiles):
        r = fn(p)
        if is_exact(r, c["expected"]):
            exact += 1
        else:
            failures.append(c["case_id"])
        comp += components_ok(r, c["expected"])
        trace += traceable(r, valid_ids, component_map)
    # latency: several full passes, per-request timing; CPU time for cost
    lat: list[float] = []
    cpu0 = time.process_time()
    for _ in range(cfg["latency_repeats"]):
        for p in profiles:
            t0 = time.perf_counter()
            fn(p)
            lat.append((time.perf_counter() - t0) * 1000)
    cpu_per_req = (time.process_time() - cpu0) / (cfg["latency_repeats"] * len(profiles))
    # determinism
    rng = random.Random(cfg["seed"])
    sample = rng.sample(profiles, min(cfg["determinism_sample"], len(profiles)))
    det = sum(len({fingerprint(fn(p)) for _ in range(cfg["determinism_repeats"])}) == 1 for p in sample)
    price_cpu_hour = settings()["llm"]["compute_usd_per_cpu_hour"]
    return {"engine": name, "fy": fy, "status": "measured", "n_cases": len(cases),
            "rupee_exact_accuracy": exact / len(cases), "component_accuracy": comp / len(cases),
            "failed_cases": failures[:20], "traceability": trace / len(cases),
            "determinism": {"cases": len(sample), "repeats": cfg["determinism_repeats"], "deterministic_cases": det},
            "latency": _latency_stats(lat),
            "cost_per_1000_usd": cpu_per_req * 1000 / 3600 * price_cpu_hour,
            "cost_basis": f"measured CPU {cpu_per_req * 1000:.3f} ms/request x ${price_cpu_hour}/CPU-hour"}


def _llm_engine_report(fy: str, cases: list[dict], cfg: dict) -> dict[str, Any]:
    status = llm_status()
    model = model_name()
    price = price_for(model)
    # Projection from the real prompt sizes (always computable, clearly labelled)
    prompt_tokens = [estimate_tokens(llm_query_engine.SYSTEM + llm_query_engine.build_prompt(
        p, llm_query_engine.retrieve(p))) for p in (TaxpayerProfile(**c["input"]) for c in cases)]
    avg_in = statistics.fmean(prompt_tokens)
    out_tok = cfg["projected_llm_output_tokens"]
    projected = (avg_in * price["input"] + out_tok * price["output"]) / 1e6 * 1000
    base = {"engine": "llm_query_engine", "fy": fy, "model": model,
            "projected_cost_per_1000_usd": projected,
            "projection_basis": f"avg prompt {avg_in:.0f} tokens (chars/4 estimate of the real prompts) + "
                                f"{out_tok} output tokens (assumed, thinking tokens excluded) at "
                                f"${price['input']}/${price['output']} per M tokens"}
    if status["mode"] != "real":
        return {**base, "status": "NOT MEASURED - no GEMINI_API_KEY (offline mode)", "n_cases": 0,
                "rupee_exact_accuracy": None, "determinism": None, "latency": None, "traceability": None}
    rng = random.Random(cfg["seed"])
    sample = rng.sample(cases, min(cfg["llm_accuracy_sample"], len(cases)))
    exact = raw_exact = trace = 0
    lat, cost, outcomes = [], 0.0, {}
    for c in sample:
        p = TaxpayerProfile(**c["input"])
        r = llm_query_engine.compute(p, use_cache=True)
        outcomes[r.status.value] = outcomes.get(r.status.value, 0) + 1
        meta = r.llm_meta or {}
        if meta.get("mode") == "real":
            lat.append(meta.get("latency_ms", 0.0))
        cost += meta.get("cost_usd", 0.0)
        raw_exact += is_exact(r, c["expected"])
        exact += is_exact(r, c["expected"]) and r.status == EngineStatus.OK
        valid = set((meta.get("retrieved_passages") or []))
        trace += traceable(r, valid, None)
    det_cases = sample[: cfg["llm_determinism_sample"]]
    det = 0
    for c in det_cases:
        p = TaxpayerProfile(**c["input"])
        fps = set()
        for _ in range(cfg["determinism_repeats"]):
            r = llm_query_engine.compute(p, use_cache=False, shadow_verify=False)
            fps.add(fingerprint(r))
            if (r.llm_meta or {}).get("mode") == "real":
                lat.append(r.llm_meta.get("latency_ms", 0.0))
                cost += r.llm_meta.get("cost_usd", 0.0)
        det += len(fps) == 1
    n_calls = len(sample) + len(det_cases) * cfg["determinism_repeats"]
    return {**base, "status": "measured (real Gemini calls; cached replays excluded from latency)",
            "n_cases": len(sample), "rupee_exact_accuracy_raw_llm": raw_exact / len(sample),
            "rupee_exact_accuracy": exact / len(sample), "validation_outcomes": outcomes,
            "traceability": trace / len(sample),
            "determinism": {"cases": len(det_cases), "repeats": cfg["determinism_repeats"], "deterministic_cases": det},
            "latency": _latency_stats(lat) if lat else None,
            "cost_per_1000_usd": cost / max(n_calls, 1) * 1000, "cost_basis": "measured tokens x list price"}


def _previous_llm_measurement(fy: str) -> dict[str, Any] | None:
    """A stored *real* Engine B measurement for this FY, or None.

    Real LLM calls cost money/quota and are non-deterministic, so a reproducibility run must not silently
    replace a real measurement (with NOT MEASURED when offline, or with a fresh sample when online).
    Set LLM_REMEASURE=1 to force new calls.
    """
    path = BENCH_A / "benchmark_results.json"
    if os.environ.get("LLM_REMEASURE") == "1" or not path.exists():
        return None
    prev = json.loads(path.read_text())
    e = prev.get("by_fy", {}).get(fy, {}).get("llm_query_engine", {})
    if str(e.get("status", "")).startswith("measured"):
        return {**e, "reused_from_run": e.get("reused_from_run") or prev.get("generated_at"),
                "reuse_note": "real Gemini measurement reused (set LLM_REMEASURE=1 to re-measure)"}
    return None


def run(fys: list[str] | None = None) -> dict[str, Any]:
    cfg = {**load_yaml("benchmark_config.yaml")["project_a"]}
    fys = fys or ["2024-25", "2025-26"]
    prev_llm = {fy: _previous_llm_measurement(fy) for fy in fys}
    status = llm_status()
    if any(prev_llm.values()) and status["mode"] != "real":
        status = {**status, "mode": "real (reused stored measurement)"}
    results: dict[str, Any] = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "llm": status, "by_fy": {}}
    for fy in fys:
        cases = load_cases(fy)
        rs = get_rule_set(fy)
        art = llm_compiled_engine.load_artifact(fy)
        comp_ids = {s["id"] for reg in ("old", "new") for s in art["regimes"][reg]["steps"]}
        comp_map = {k: v for k, v in {"tax_on_income": "SLABS", "rebate_87a": "87A", "surcharge": "SURCHARGE",
                                      "cess": "CESS", "tax_payable": "PAYABLE"}.items()}
        results["by_fy"][fy] = {
            "rules_engine": _deterministic_engine_report("rules_engine", rules_engine.compute, fy, cases, cfg,
                                                         rs.rule_ids() | {"INPUT.SALARY", "OLD.80A2"},
                                                         COMPONENT_RULES_A),
            "llm_compiled_engine": _deterministic_engine_report("llm_compiled_engine", llm_compiled_engine.compute, fy,
                                                                cases, cfg, comp_ids, comp_map),
            "llm_query_engine": prev_llm[fy] or _llm_engine_report(fy, cases, cfg),
        }
    BENCH_A.mkdir(parents=True, exist_ok=True)
    (BENCH_A / "benchmark_results.json").write_text(json.dumps(results, indent=1, default=str))
    return results


if __name__ == "__main__":
    print(json.dumps(run(), indent=1, default=str))
