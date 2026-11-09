"""Common types for every detector: structured alerts, results, and the detector interface.

An :class:`Alert` is deliberately structured (problem, scope, column, observed, expected, reason) instead of
free text, so that (1) alert explanation quality can be scored consistently across families, and (2) a
downstream dashboard can group and route alerts without parsing prose.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

PROBLEMS: tuple[str, ...] = (
    "schema_drift",                # a. renamed or dropped column
    "type_change",                 # b. column type change
    "date_format_switch",          # c. date format switch partway through a batch
    "lat_lon_swap",                # d. latitude and longitude swapped
    "exact_duplicates",            # e. exact duplicates
    "near_duplicates",             # f. near-duplicates
    "null_spike",                  # g. null spike in one agency's records
    "closed_before_created",       # h. closed date earlier than created date
    "zip_borough_mismatch",        # i. ZIP code inconsistent with borough
    "complaint_descriptor_mismatch",  # j. complaint type inconsistent with descriptor
    "resolution_status_contradiction",  # k. resolution description contradicting status
    "pii_in_text",                 # l. personal data inside free-text fields
)
BATCH_LEVEL_PROBLEMS = {"schema_drift", "type_change"}  # judged per batch; others per row
FAMILIES = ("rule", "statistical", "ml", "llm")


@dataclass
class Alert:
    problem: str
    scope: str                      # "row" | "batch" | "column"
    reason: str
    row: int | None = None
    column: str | None = None
    observed: Any = None
    expected: Any = None
    score: float | None = None

    def explanation_quality(self) -> int:
        """0-5 rubric: names a reason (>=20 chars), locates it (row/column), shows the observed value,
        states what was expected, and carries the problem type. Automatic proxy, documented in RESULTS_B."""
        return sum([
            bool(self.problem),
            bool(self.reason and len(self.reason) >= 20),
            self.row is not None or self.column is not None,
            self.observed is not None,
            self.expected is not None,
        ])


@dataclass
class DetectionResult:
    detector: str
    family: str                     # rule | statistical | ml | llm
    problem: str
    mode: str = "deterministic"     # deterministic | statistical | ml | llm-real | llm-cached | llm-unavailable
    row_flags: pd.Series | None = None   # bool per row index (None for batch-only detectors)
    batch_flag: bool = False
    alerts: list[Alert] = field(default_factory=list)
    seconds: float = 0.0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: float = 0.0
    llm_latency_ms: float = 0.0          # sum of measured API latencies (cached replays keep the original)
    rows_reviewed: int = 0          # LLM: rows actually sent to the model
    available: bool = True
    note: str = ""


class Detector:
    name: str = "detector"
    family: str = "rule"
    problem: str = ""
    algorithm: str = ""

    def fit(self, reference: "Any") -> "Detector":  # noqa: D401 - optional
        return self

    def detect(self, batch: pd.DataFrame) -> DetectionResult:
        t0 = time.perf_counter()
        res = self._detect(batch)
        res.seconds = time.perf_counter() - t0
        if res.row_flags is not None and not res.batch_flag:
            res.batch_flag = bool(res.row_flags.any())
        return res

    def _detect(self, batch: pd.DataFrame) -> DetectionResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def result(self, **kw: Any) -> DetectionResult:
        mode = {"rule": "deterministic", "statistical": "statistical", "ml": "ml"}.get(self.family, "llm-real")
        return DetectionResult(detector=self.name, family=self.family, problem=self.problem, mode=kw.pop("mode", mode),
                               **kw)


def flags_from_mask(batch: pd.DataFrame, mask: pd.Series | None) -> pd.Series:
    if mask is None:
        return pd.Series(False, index=batch.index)
    return mask.reindex(batch.index).fillna(False).astype(bool)
