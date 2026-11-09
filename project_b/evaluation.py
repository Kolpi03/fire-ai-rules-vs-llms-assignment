"""Metrics for detectors against injected ground truth.

Units of evaluation
-------------------
* Row-level problems (10 of 12): every row of every corrupted batch is a TP/FP/TN/FN against the
  injected labels. Precision, recall (= detection rate) and F1 come from those counts.
* Batch-level problems (schema_drift, type_change): the unit is the batch; corrupted batches are
  positives, clean batches are negatives.
* False-alarm rate on CLEAN batches is reported two ways: row FPR (flagged rows / rows) and batch alarm
  rate (share of clean batches with at least one alert) — the second is what an on-call team feels.
* Cost per million rows: measured wall-clock seconds per row (single thread, this laptop) x the compute
  price in settings.yaml; LLM: measured tokens x list price, or a projection from the exact prompts.
* Explanation quality: mean of the 0-5 structured rubric in ``Alert.explanation_quality``.

LLM sampling (free-tier budget): positives and negatives are sampled separately, so recall and FPR are
unbiased within each stratum; precision is then computed at the true batch prevalence pi:
precision = R*pi / (R*pi + FPR*(1-pi)).
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
import pandas as pd

from common.config import settings
from project_b.detection_types import BATCH_LEVEL_PROBLEMS, DetectionResult


@dataclass
class Confusion:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    def add(self, flags: pd.Series, labels: pd.Series) -> None:
        f = flags.reindex(labels.index).fillna(False).astype(bool).to_numpy()
        y = labels.astype(bool).to_numpy()
        self.tp += int((f & y).sum())
        self.fp += int((f & ~y).sum())
        self.tn += int((~f & ~y).sum())
        self.fn += int((~f & y).sum())

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)

    @property
    def fpr(self) -> float | None:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "precision": self.precision, "recall": self.recall, "f1": self.f1, "fpr": self.fpr}


def cost_per_million_rows(seconds: float, rows: int) -> float:
    price = settings()["llm"]["compute_usd_per_cpu_hour"]
    return seconds / max(rows, 1) * 1e6 / 3600 * price


def explanation_score(results: list[DetectionResult]) -> float | None:
    scores = [a.explanation_quality() for r in results for a in r.alerts[:200]]
    return statistics.fmean(scores) if scores else None


def evaluate_deterministic(problem: str, detector: Any, corrupted: list[Any], clean: list[pd.DataFrame]) -> dict[str, Any]:
    """Evaluate a rule/statistical/ML detector on corrupted (InjectionResult) and clean batches."""
    batch_level = problem in BATCH_LEVEL_PROBLEMS
    rows_conf, batch_conf = Confusion(), Confusion()
    secs, rows = 0.0, 0
    c_results, clean_results = [], []
    for inj in corrupted:
        r = detector.detect(inj.batch)
        secs += r.seconds
        rows += len(inj.batch)
        c_results.append(r)
        if r.row_flags is not None and not batch_level:
            rows_conf.add(r.row_flags, inj.labels)
        batch_conf.tp += int(r.batch_flag)
        batch_conf.fn += int(not r.batch_flag)
    clean_rows_flagged = clean_rows = clean_batches_alarmed = 0
    for b in clean:
        r = detector.detect(b)
        secs += r.seconds
        rows += len(b)
        clean_results.append(r)
        n_flag = int(r.row_flags.sum()) if r.row_flags is not None else 0
        clean_rows_flagged += n_flag
        clean_rows += len(b)
        alarm = bool(r.batch_flag or n_flag)
        clean_batches_alarmed += alarm
        batch_conf.fp += int(alarm)
        batch_conf.tn += int(not alarm)
        if not batch_level and r.row_flags is not None:
            rows_conf.add(r.row_flags, pd.Series(False, index=b.index))
    primary = batch_conf if batch_level else rows_conf
    return {
        "problem": problem, "detector": detector.name, "family": detector.family, "algorithm": detector.algorithm,
        "status": "measured", "unit": "batch" if batch_level else "row",
        **{k: v for k, v in primary.as_dict().items()},
        "batch_detection_rate": batch_conf.tp / max(batch_conf.tp + batch_conf.fn, 1),
        "clean_row_false_alarm_rate": clean_rows_flagged / max(clean_rows, 1),
        "clean_batch_alarm_rate": clean_batches_alarmed / max(len(clean), 1),
        "latency_ms_per_batch": secs / max(len(corrupted) + len(clean), 1) * 1000,
        "throughput_rows_per_s": rows / secs if secs else None,
        "cost_per_million_rows_usd": cost_per_million_rows(secs, rows),
        "explanation_quality_0_5": explanation_score(c_results),
        "n_corrupted_batches": len(corrupted), "n_clean_batches": len(clean),
    }


def evaluate_llm(problem: str, detector: Any, corrupted: list[Any], clean: list[pd.DataFrame],
                 rows_per_stratum: int, seed: int) -> dict[str, Any]:
    """LLM evaluation on a stratified sample, or a projection-only row when no key is available."""
    rng = np.random.default_rng(seed)
    inj, cb = corrupted[0], clean[0]
    # The LLM reviewer for null_spike answers per batch (one call over the per-agency null table), so it is
    # scored per batch even though the deterministic null-spike detectors are scored per row.
    from project_b.llm_detector import BATCH_PROBLEMS as LLM_BATCH_PROBLEMS
    batch_level = problem in BATCH_LEVEL_PROBLEMS or problem in LLM_BATCH_PROBLEMS
    price_note = "projected from the exact prompts (chars/4 tokens) at list price; NOT a measurement"
    pos = inj.labels[inj.labels].index
    neg = inj.labels[~inj.labels].index
    samp_pos = pd.Index(rng.choice(pos, min(rows_per_stratum, len(pos)), replace=False)) if len(pos) else pos
    samp_neg = pd.Index(rng.choice(neg, min(rows_per_stratum, len(neg)), replace=False))
    samp_clean = pd.Index(rng.choice(cb.index, min(rows_per_stratum, len(cb)), replace=False))
    # projection: per-row review of EVERY row (rows mode), or one call per batch / candidate pairs
    calls, tin, tout = detector.projected_tokens(inj.batch, None if batch_level or problem == "near_duplicates"
                                                 else inj.batch.index[:200])
    scale = (len(inj.batch) / 200) if not (batch_level or problem == "near_duplicates") else 1
    from common.llm_client import cost_usd, model_name
    proj_cost_batch = cost_usd(model_name(), int(tin * scale), int(tout * scale))
    base = {"problem": problem, "detector": detector.name, "family": "llm", "algorithm": detector.algorithm,
            "unit": "batch" if batch_level else "row", "model": model_name(),
            "projected_cost_per_million_rows_usd": proj_cost_batch / len(inj.batch) * 1e6,
            "projected_calls_per_million_rows": calls * scale / len(inj.batch) * 1e6,
            "projection_basis": price_note}
    probe = detector.detect(cb.head(5)) if not batch_level else detector.detect(cb)
    if not probe.available:
        return {**base, "status": "NOT MEASURED - no GEMINI_API_KEY (offline)", "precision": None, "recall": None,
                "f1": None, "clean_row_false_alarm_rate": None, "clean_batch_alarm_rate": None,
                "tp": None, "fp": None, "tn": None, "fn": None}
    if batch_level:
        conf = Confusion()
        runs_c, runs_k = [], [probe]
        for c in corrupted:
            r = detector.detect(c.batch)
            runs_c.append(r)
            conf.tp += int(bool(r.batch_flag))
            conf.fn += int(not r.batch_flag)
        for k in clean[1:]:
            runs_k.append(detector.detect(k))
        for r in runs_k:
            conf.fp += int(bool(r.batch_flag))
            conf.tn += int(not r.batch_flag)
        allr = runs_c + runs_k
        cost = sum(r.llm_cost_usd for r in allr)
        n_rows = sum(len(c.batch) for c in corrupted) + sum(len(k) for k in clean)
        return {**base, "unit": "batch",
                "status": f"measured ({len(runs_c)} corrupted + {len(runs_k)} clean batches)"
                          + (" WITH LLM ERRORS" if any(r.note for r in allr) else ""), **conf.as_dict(),
                "batch_detection_rate": conf.tp / max(conf.tp + conf.fn, 1),
                "clean_batch_alarm_rate": conf.fp / max(conf.fp + conf.tn, 1), "clean_row_false_alarm_rate": None,
                "llm_calls": sum(r.llm_calls for r in allr),
                "llm_input_tokens": sum(r.llm_input_tokens for r in allr),
                "llm_output_tokens": sum(r.llm_output_tokens for r in allr),
                "measured_cost_usd": cost, "measured_cost_per_million_rows_usd": cost / max(n_rows, 1) * 1e6,
                "latency_ms_per_call": sum(r.llm_latency_ms for r in allr) / max(sum(r.llm_calls for r in allr), 1),
                "errors": [r.note for r in allr if r.note][:5],
                "explanation_quality_0_5": explanation_score(runs_c)}
    rows = samp_pos.append(samp_neg)
    r_c = detector.detect(inj.batch, rows=rows) if problem != "near_duplicates" else detector.detect(inj.batch)
    r_k = detector.detect(cb, rows=samp_clean) if problem != "near_duplicates" else detector.detect(cb)
    flags = r_c.row_flags.astype("boolean")
    rec_conf, neg_conf, clean_conf = Confusion(), Confusion(), Confusion()
    rec_conf.add(flags.loc[samp_pos].fillna(False), inj.labels.loc[samp_pos])
    neg_conf.add(flags.loc[samp_neg].fillna(False), inj.labels.loc[samp_neg])
    kf = r_k.row_flags.astype("boolean")
    clean_conf.add(kf.loc[samp_clean].fillna(False), pd.Series(False, index=samp_clean))
    R, F = rec_conf.recall or 0.0, neg_conf.fpr or 0.0
    pi = len(pos) / len(inj.batch)
    prec = (R * pi) / (R * pi + F * (1 - pi)) if (R * pi + F * (1 - pi)) else None
    reviewed = r_c.rows_reviewed + r_k.rows_reviewed
    cost = r_c.llm_cost_usd + r_k.llm_cost_usd
    return {**base, "status": f"measured on stratified sample ({len(samp_pos)} pos, {len(samp_neg)} neg, "
                              f"{len(samp_clean)} clean rows)" + (" WITH LLM ERRORS" if (r_c.note or r_k.note) else ""),
            "tp": rec_conf.tp, "fn": rec_conf.fn, "fp": neg_conf.fp, "tn": neg_conf.tn,
            "recall": R, "fpr": F, "precision": prec,
            "f1": (2 * prec * R / (prec + R)) if prec and R else 0.0,
            "clean_row_false_alarm_rate": clean_conf.fpr, "clean_batch_alarm_rate": None,
            "measured_cost_per_million_rows_usd": cost / max(reviewed, 1) * 1e6,
            "llm_calls": r_c.llm_calls + r_k.llm_calls,
            "llm_input_tokens": r_c.llm_input_tokens + r_k.llm_input_tokens,
            "llm_output_tokens": r_c.llm_output_tokens + r_k.llm_output_tokens, "measured_cost_usd": cost,
            "latency_ms_per_call": (r_c.llm_latency_ms + r_k.llm_latency_ms) / max(r_c.llm_calls + r_k.llm_calls, 1),
            "errors": [r.note for r in (r_c, r_k) if r.note][:5],
            "explanation_quality_0_5": explanation_score([r_c])}


def fmt(x: Any, pct: bool = False, digits: int = 3) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    if pct:
        return f"{x * 100:.1f}%"
    if isinstance(x, float):
        return f"{x:.{digits}g}" if abs(x) < 1e-2 or abs(x) >= 1e4 else f"{x:.{digits}f}"
    return str(x)
