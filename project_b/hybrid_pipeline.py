"""Task B5: hybrid data-quality pipeline built from the measured winners.

Stages (each stage only does what the measurements say it is best at):

0. **Schema gate (rules, per batch)** — required columns (Pandera contract). A batch with a missing or
   renamed column is QUARANTINED: nothing downstream can be trusted, so no further checks run.
   Numeric columns that arrive as strings are flagged, then coerced so later checks still work.
1. **Row rules (every row)** — cheap deterministic checks for problems with a crisp definition.
2. **Batch statistics** — tests that need the whole batch (null spikes, duplicate rate).
3. **ML** — only for problems where a learned model measurably beat the rule (fuzzy near-duplicates,
   descriptor/type, resolution/status, PII novelty).
4. **LLM (optional, budget-capped, OFF by default)** — reviews rows that stage 3 flagged for the semantic
   problems, to confirm or veto them. Measured with real calls (``measure_stage4``): its vetoes removed far
   more true detections than false alarms, so ``use_llm`` defaults to False. Stages 0-3 never depend on it.

Which detector serves each problem in stages 1-3 is not hard-coded: :func:`select_winners` reads
``benchmarks/project_b/detector_results.json`` and picks, per problem, the deterministic detector with the
best F1 among those whose clean-batch row false-alarm rate is <= 0.1%; on ties (within 0.01 F1) it prefers
the simpler family (rule, then statistical, then ML), then the cheaper detector. The selection and its reasons are written into the results.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from common.config import settings
from common.paths import BENCH_B
from project_b.corruption_injector import inject, inject_many
from project_b.detection_types import BATCH_LEVEL_PROBLEMS, PROBLEMS, DetectionResult
from project_b.evaluation import Confusion, cost_per_million_rows
from project_b.reference import Reference

STAGE_OF_FAMILY = {"rule": 1, "statistical": 2, "ml": 3}
SEMANTIC = ("complaint_descriptor_mismatch", "resolution_status_contradiction", "pii_in_text")
MAX_CLEAN_ROW_FA = 0.001
FAMILY_RANK = {"rule": 0, "statistical": 1, "ml": 2}
F1_TIE = 0.01


def select_winners(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    winners: dict[str, dict[str, Any]] = {}
    for p in PROBLEMS:
        cands = [r for r in results if r["problem"] == p and r["family"] != "llm" and r.get("status") == "measured"]
        ok = [r for r in cands if (r.get("clean_row_false_alarm_rate") or 0) <= MAX_CLEAN_ROW_FA] or cands
        best_f1 = max((r.get("f1") or 0) for r in ok)
        near = [r for r in ok if (r.get("f1") or 0) >= best_f1 - F1_TIE]
        # F1 ties: prefer the simpler, more auditable family, then the cheaper detector. Ranking by family first
        # keeps the choice deterministic: measured cost differences between sub-microsecond detectors are timing noise.
        pick = min(near, key=lambda r: (FAMILY_RANK.get(r["family"], 9), r["cost_per_million_rows_usd"]))
        reason = (f"F1={pick.get('f1'):.3f}, clean row FA={pick['clean_row_false_alarm_rate']:.4%}, "
                  f"${pick['cost_per_million_rows_usd']:.4f}/M rows")
        others = [f"{r['detector']}: F1={r.get('f1') or 0:.3f}, FA={r['clean_row_false_alarm_rate']:.4%}"
                  for r in cands if r is not pick]
        winners[p] = {"detector": pick["detector"], "family": pick["family"], "reason": reason, "rejected": others}
    return winners


@dataclass
class PipelineResult:
    quarantined: bool
    flags: pd.DataFrame                     # rows x problems
    alerts: list[Any] = field(default_factory=list)
    stage_seconds: dict[int, float] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)


class HybridPipeline:
    def __init__(self, families: dict[str, dict[str, Any]], winners: dict[str, dict[str, Any]],
                 llm_budget_rows: int = 20, use_llm: bool = False):
        by_name = {d.name: d for fam in ("rule", "stat_ml") for d in families[fam].values()}
        self.detectors = {p: by_name[w["detector"]] for p, w in winners.items()}
        self.schema_gate = families["rule"]["schema_drift"]
        self.llm = families["llm"]
        self.winners = winners
        self.llm_budget_rows = llm_budget_rows
        self.use_llm = use_llm

    def run(self, batch: pd.DataFrame) -> PipelineResult:
        flags = pd.DataFrame(False, index=batch.index, columns=list(PROBLEMS))
        stage_s = {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
        alerts: list[Any] = []
        # ---- stage 0: schema gate ----
        t0 = time.perf_counter()
        gate = self.schema_gate.detect(batch)
        stage_s[0] += time.perf_counter() - t0
        if gate.batch_flag:
            alerts += gate.alerts
            return PipelineResult(True, flags, alerts, stage_s, {"stage4": "not run (quarantined)"})
        work = batch
        tc = self.detectors["type_change"].detect(batch)
        stage_s[STAGE_OF_FAMILY[self.detectors["type_change"].family]] += tc.seconds
        if tc.batch_flag:
            alerts += tc.alerts
            flags["type_change"] = tc.row_flags.reindex(batch.index).fillna(False) if tc.row_flags is not None else True
            work = batch.copy()
            for col in ("latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"):
                work[col] = pd.to_numeric(work[col].astype(str).str.replace(",", ""), errors="coerce")
        # date repair so a format switch does not blind the temporal checks downstream
        df_res = self.detectors["date_format_switch"].detect(work)
        stage_s[STAGE_OF_FAMILY[self.detectors["date_format_switch"].family]] += df_res.seconds
        flags["date_format_switch"] = df_res.row_flags.reindex(work.index).fillna(False)
        if df_res.row_flags is not None and df_res.row_flags.any():
            alerts += df_res.alerts[:20]
            work = work.copy()
            for col in ("created_date", "closed_date"):
                alt = pd.to_datetime(work[col], format="%m/%d/%Y %I:%M:%S %p", errors="coerce")
                fix = alt.notna()
                work.loc[fix, col] = alt[fix].dt.strftime("%Y-%m-%dT%H:%M:%S.000")
        # ---- stages 1-3: winners ----
        for p in PROBLEMS:
            if p in ("schema_drift", "type_change", "date_format_switch"):
                continue
            det = self.detectors[p]
            r: DetectionResult = det.detect(work)
            stage_s[STAGE_OF_FAMILY[det.family]] += r.seconds
            if r.row_flags is not None:
                flags[p] = r.row_flags.reindex(work.index).fillna(False).astype(bool)
            alerts += r.alerts[:50]
        # ---- stage 4: LLM on borderline semantic flags ----
        llm_info: dict[str, Any] = {"stage4": "skipped (disabled)"}
        if self.use_llm:
            llm_info = self._llm_stage(work, flags, stage_s)
        return PipelineResult(False, flags, alerts, stage_s, llm_info)

    def _llm_stage(self, work: pd.DataFrame, flags: pd.DataFrame, stage_s: dict[int, float]) -> dict[str, Any]:
        info: dict[str, Any] = {"rows_sent": 0, "vetoed": 0, "calls": 0, "cost_usd": 0.0}
        for p in SEMANTIC:
            flagged = flags.index[flags[p]]
            if not len(flagged):
                continue
            rows = flagged[: self.llm_budget_rows]
            r = self.llm[p].detect(work, rows=rows)
            stage_s[4] += r.seconds
            if not r.available:
                return {"stage4": f"skipped: {r.note}"}
            info["rows_sent"] += len(rows)
            info["calls"] += r.llm_calls
            info["cost_usd"] += r.llm_cost_usd
            info["llm_latency_ms"] = info.get("llm_latency_ms", 0.0) + r.llm_latency_ms
            veto = rows[~r.row_flags.loc[rows].fillna(False).astype(bool).to_numpy()]
            flags.loc[veto, p] = False
            info["vetoed"] += len(veto)
        info["stage4"] = "ran"
        return info


def measure_stage4(families: dict[str, dict[str, Any]], winners: dict[str, Any], ref: Reference,
                   eval_pool: pd.DataFrame, n_batches: int, seed: int) -> dict[str, Any]:
    """Measure the LLM confirm/veto stage on the same mixed batches as the deterministic evaluation.

    For each semantic problem, compares stage-3 flags with the flags after the LLM review: a veto of a
    labelled-clean row is a *good veto* (false alarm removed); a veto of a labelled-corrupt row is a *bad veto*
    (detection lost). Real calls only; a stored real measurement is reused unless LLM_REMEASURE=1.
    """
    import os
    from common.llm_client import llm_status
    from project_b.benchmark import sample_batch
    prev_path = BENCH_B / "hybrid_results.json"
    if os.environ.get("LLM_REMEASURE") != "1" and prev_path.exists():
        prev = json.loads(prev_path.read_text()).get("llm_stage", {})
        if prev.get("stage4") == "measured":
            return {**prev, "reused_from_run": prev.get("reused_from_run") or prev.get("measured_at")}
    if llm_status()["mode"] != "real":
        return {"stage4": "NOT MEASURED - no GEMINI_API_KEY (offline); stages 0-3 unaffected"}
    size = settings()["project_b"]["batch_size"]
    row_problems = [p for p in PROBLEMS if p not in BATCH_LEVEL_PROBLEMS]
    order = ["exact_duplicates", "near_duplicates"] + [p for p in row_problems if p not in ("exact_duplicates", "near_duplicates")]
    rates = {p: 0.01 for p in order}
    rates.update(date_format_switch=0.1, null_spike=0.5)
    det = HybridPipeline(families, winners, use_llm=False)
    llm = HybridPipeline(families, winners, use_llm=True)
    before = {p: Confusion() for p in SEMANTIC}
    after = {p: Confusion() for p in SEMANTIC}
    good = {p: 0 for p in SEMANTIC}
    bad = {p: 0 for p in SEMANTIC}
    calls = rows_sent = 0
    cost = llm_s = 0.0
    errors: list[str] = []
    for i in range(n_batches):
        b = sample_batch(eval_pool, size, seed + i)
        mixed, labels, _ = inject_many(b, order, ref, seed=seed + i, rates=rates)
        r0 = det.run(mixed.copy())
        r1 = llm.run(mixed.copy())
        info = r1.llm
        if info.get("stage4") != "ran":
            errors.append(str(info.get("stage4")))
            continue
        calls += info["calls"]
        rows_sent += info["rows_sent"]
        cost += info["cost_usd"]
        llm_s += r1.llm.get("llm_latency_ms", 0.0) / 1000
        for p in SEMANTIC:
            before[p].add(r0.flags[p], labels[p])
            after[p].add(r1.flags[p], labels[p])
            vetoed = r0.flags[p] & ~r1.flags[p]
            good[p] += int((vetoed & ~labels[p]).sum())
            bad[p] += int((vetoed & labels[p]).sum())
    if not calls:
        return {"stage4": "FAILED - no successful LLM review", "errors": errors[:5]}
    return {"stage4": "measured", "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"), "model": llm_status()["model"],
            "budget_rows_per_problem_per_batch": llm.llm_budget_rows, "n_batches": n_batches,
            "llm_calls": calls, "rows_reviewed": rows_sent, "cost_usd_list_price": cost,
            "llm_seconds_total": llm_s, "latency_ms_per_call": llm_s / calls * 1000,
            "good_vetoes_false_alarms_removed": good, "bad_vetoes_detections_lost": bad,
            "before_llm": {p: before[p].as_dict() for p in SEMANTIC},
            "after_llm": {p: after[p].as_dict() for p in SEMANTIC},
            "batches_skipped": errors}


def evaluate_hybrid(families: dict[str, dict[str, Any]], ref: Reference, eval_pool: pd.DataFrame,
                    results: list[dict[str, Any]], n_batches: int = 6, seed: int = 4242) -> dict[str, Any]:
    from project_b.benchmark import sample_batch
    winners = select_winners(results)
    # Stages 0-3 only: the reproducible, deterministic numbers. Stage 4 (LLM) is measured separately below
    # so that LLM latency and non-determinism never leak into these metrics.
    pipe = HybridPipeline(families, winners, use_llm=False)
    size = settings()["project_b"]["batch_size"]
    row_problems = [p for p in PROBLEMS if p not in BATCH_LEVEL_PROBLEMS]
    order = ["exact_duplicates", "near_duplicates"] + [p for p in row_problems if p not in ("exact_duplicates", "near_duplicates")]
    rates = {p: 0.01 for p in order}
    rates.update(date_format_switch=0.1, null_spike=0.5)
    per_p = {p: Confusion() for p in PROBLEMS}
    any_conf = Confusion()
    rows_total, secs_total = 0, 0.0
    stage_tot = {k: 0.0 for k in range(5)}
    llm_runs = []
    for i in range(n_batches):
        b = sample_batch(eval_pool, size, seed + i)
        mixed, labels, _ = inject_many(b, order, ref, seed=seed + i, rates=rates)
        t0 = time.perf_counter()
        res = pipe.run(mixed)
        secs_total += time.perf_counter() - t0
        rows_total += len(mixed)
        for k, v in res.stage_seconds.items():
            stage_tot[k] += v
        llm_runs.append(res.llm)
        for p in row_problems:
            per_p[p].add(res.flags[p], labels[p])
        any_conf.add(res.flags[row_problems].any(axis=1), labels[row_problems].any(axis=1))
    # batch-level problems and clean batches
    batch_conf = {p: Confusion() for p in BATCH_LEVEL_PROBLEMS}
    clean_rows = clean_flagged = clean_alarm = 0
    for i in range(n_batches):
        for p in BATCH_LEVEL_PROBLEMS:
            inj = inject(sample_batch(eval_pool, size, seed + 100 + i), p, ref, seed=seed + 100 + i)
            res = pipe.run(inj.batch)
            hit = res.quarantined if p == "schema_drift" else bool(res.flags["type_change"].any())
            batch_conf[p].tp += int(hit)
            batch_conf[p].fn += int(not hit)
        cb = sample_batch(eval_pool, size, seed + 200 + i)
        t0 = time.perf_counter()
        res = pipe.run(cb)
        secs_total += time.perf_counter() - t0
        rows_total += len(cb)
        n_flag = int(res.flags.any(axis=1).sum())
        clean_rows += len(cb)
        clean_flagged += n_flag
        clean_alarm += int(n_flag > 0 or res.quarantined)
        for p in BATCH_LEVEL_PROBLEMS:
            fa = res.quarantined if p == "schema_drift" else bool(res.flags["type_change"].any())
            batch_conf[p].fp += int(fa)
            batch_conf[p].tn += int(not fa)
    out = {
        "winners": winners,
        "per_problem": {p: (batch_conf[p] if p in BATCH_LEVEL_PROBLEMS else per_p[p]).as_dict() for p in PROBLEMS},
        "any_problem_row_level": any_conf.as_dict(),
        "clean_row_false_alarm_rate": clean_flagged / max(clean_rows, 1),
        "clean_batch_alarm_rate": clean_alarm / n_batches,
        "throughput_rows_per_s": rows_total / secs_total,
        "latency_ms_per_5k_batch": secs_total / (rows_total / size) * 1000,
        "stage_seconds_share": {f"stage{k}": v / max(sum(stage_tot.values()), 1e-9) for k, v in stage_tot.items()},
        "cost_per_million_rows_usd_compute": cost_per_million_rows(secs_total, rows_total),
        "llm_stage": measure_stage4(families, winners, ref, eval_pool, n_batches, seed),
        "design": {"n_mixed_batches": n_batches, "batch_size": size, "injection_rates": rates,
                   "n_batch_level_batches_per_problem": n_batches, "n_clean_batches": n_batches},
    }
    BENCH_B.mkdir(parents=True, exist_ok=True)
    (BENCH_B / "hybrid_results.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def compare_variants(families: dict[str, dict[str, Any]], ref: Reference, eval_pool: pd.DataFrame,
                     results: list[dict[str, Any]], n_batches: int = 4, seed: int = 777) -> list[dict[str, Any]]:
    """End-to-end comparison: rules-only vs stat/ML-only vs measured winners (same mixed batches)."""
    from project_b.benchmark import sample_batch
    base = {p: {"detector": families["rule"][p].name, "family": "rule"} for p in PROBLEMS}
    stat = {p: {"detector": families["stat_ml"][p].name, "family": families["stat_ml"][p].family} for p in PROBLEMS}
    variants = {"rules_only": base, "stat_ml_only": stat, "hybrid_winners": select_winners(results)}
    size = settings()["project_b"]["batch_size"]
    row_problems = [p for p in PROBLEMS if p not in BATCH_LEVEL_PROBLEMS]
    order = ["exact_duplicates", "near_duplicates"] + [p for p in row_problems if p not in ("exact_duplicates", "near_duplicates")]
    rates = {p: 0.01 for p in order}
    rates.update(date_format_switch=0.1, null_spike=0.5)
    batches = []
    for i in range(n_batches):
        mixed, labels, _ = inject_many(sample_batch(eval_pool, size, seed + i), order, ref, seed=seed + i, rates=rates)
        batches.append((mixed, labels, sample_batch(eval_pool, size, seed + 50 + i)))
    out = []
    for name, w in variants.items():
        pipe = HybridPipeline(families, w, use_llm=False)
        conf, secs, rows, cflag, crows = Confusion(), 0.0, 0, 0, 0
        f1s = []
        per = {p: Confusion() for p in row_problems}
        for mixed, labels, cb in batches:
            t0 = time.perf_counter()
            r = pipe.run(mixed)
            rc = pipe.run(cb)
            secs += time.perf_counter() - t0
            rows += len(mixed) + len(cb)
            conf.add(r.flags[row_problems].any(axis=1), labels[row_problems].any(axis=1))
            for p in row_problems:
                per[p].add(r.flags[p], labels[p])
            cflag += int(rc.flags.any(axis=1).sum())
            crows += len(cb)
        f1s = [per[p].f1 or 0 for p in row_problems]
        out.append({"variant": name, **{f"any_{k}": v for k, v in conf.as_dict().items() if k in ("precision", "recall", "f1")},
                    "macro_f1_row_problems": float(np.mean(f1s)), "clean_row_false_alarm_rate": cflag / crows,
                    "throughput_rows_per_s": rows / secs, "cost_per_million_rows_usd": cost_per_million_rows(secs, rows)})
    (BENCH_B / "hybrid_variants.json").write_text(json.dumps(out, indent=1))
    return out
