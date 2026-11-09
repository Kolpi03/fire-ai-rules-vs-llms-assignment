"""Family B (statistical part) — distribution / profile based detectors fit on the reference period.

Used where a problem shows up as a *shift in a distribution* rather than a violated rule:

* schema_drift         — column-profile fingerprints; an unknown column whose profile matches a missing
                         contract column is diagnosed as a rename (otherwise: drop).
* type_change          — per-column share of non-numeric Python values vs the reference share (0%).
* date_format_switch   — "shape" frequency (digits -> d) vs the reference shape + change-point position.
* exact_duplicates     — batch duplicate-rate z-test against the reference rate.
* null_spike           — one-sided binomial z-test per (agency, column) null count vs the reference rate.
* closed_before_created— robust tail test on resolution duration (outside the reference [0.1%, 99.9%]
                         quantiles, with a margin); negative durations are the extreme lower tail.
"""
from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd

from project_b.detection_types import Alert, Detector
from project_b.reference import NULL_SPIKE_COLUMNS, Reference, parse_dates
from project_b.schema_detector import NUMERIC_COLS, _row_alerts


def _profile(s: pd.Series) -> dict[str, float]:
    vals = s.dropna().astype(str).head(2000)
    num = pd.to_numeric(vals.str.replace(",", ""), errors="coerce")
    return {
        "null": float(s.isna().mean()),
        "numeric": float(num.notna().mean()) if len(vals) else 0.0,
        "zip5": float(vals.str.fullmatch(r"\d{5}").mean()) if len(vals) else 0.0,
        "iso_date": float(vals.str.match(r"\d{4}-\d{2}-\d{2}T").mean()) if len(vals) else 0.0,
        "upper": float(vals.str.isupper().mean()) if len(vals) else 0.0,
        "mean_len": float(vals.str.len().mean() / 100) if len(vals) else 0.0,
        "uniq": float(vals.nunique() / max(len(vals), 1)),
        "num_mean": float(np.tanh(num.mean() / 1e6)) if num.notna().any() else 0.0,
    }


class ColumnProfileMatcher(Detector):
    name, family, problem, algorithm = "column_profile_matcher", "statistical", "schema_drift", "profile fingerprint distance"

    def fit(self, ref_df: pd.DataFrame) -> "ColumnProfileMatcher":
        self.columns = list(ref_df.columns)
        self.profiles = {c: _profile(ref_df[c]) for c in self.columns}
        return self

    def _detect(self, batch):
        missing = [c for c in self.columns if c not in batch.columns]
        extra = [c for c in batch.columns if c not in self.columns]
        alerts = []
        for m in missing:
            best, best_d = None, 9e9
            for e in extra:
                p, q = _profile(batch[e]), self.profiles[m]
                d = math.sqrt(sum((p[k] - q[k]) ** 2 for k in p))
                if d < best_d:
                    best, best_d = e, d
            if best is not None and best_d < 0.15:
                alerts.append(Alert(self.problem, "column", f"column '{m}' appears renamed to '{best}' "
                                    f"(profile distance {best_d:.3f})", column=m, observed=best, expected=m, score=best_d))
            else:
                alerts.append(Alert(self.problem, "column", f"column '{m}' missing and no column matches its profile "
                                    "(dropped)", column=m, observed=sorted(batch.columns)[:5], expected=m))
        return self.result(batch_flag=bool(missing), alerts=alerts)


class TypeShareTest(Detector):
    name, family, problem, algorithm = "type_share_test", "statistical", "type_change", "non-numeric share vs reference"

    def _detect(self, batch):
        flags, alerts = pd.Series(False, index=batch.index), []
        for col in NUMERIC_COLS:
            if col not in batch.columns:
                continue
            is_str = batch[col].map(lambda v: isinstance(v, str))
            share = float(is_str.mean())
            if share > 0:  # reference share is exactly 0, so any string value is significant
                flags |= is_str
                alerts.append(Alert(self.problem, "column", f"{share:.1%} of '{col}' values are strings "
                                    "(reference: 0%)", column=col, observed=round(share, 4), expected=0.0))
        return self.result(row_flags=flags, batch_flag=bool(alerts), alerts=alerts)


def _shape(v) -> str:
    return re.sub(r"\d", "d", str(v)) if isinstance(v, str) else "<null>"


class DateShapeChangePoint(Detector):
    name, family, problem, algorithm = "date_shape_changepoint", "statistical", "date_format_switch", "shape frequency + CUSUM change point"

    def fit(self, ref: Reference):
        self.ref_shape = ref.date_shape
        return self

    def _detect(self, batch):
        if "created_date" not in batch.columns:
            return self.result(row_flags=pd.Series(False, index=batch.index))
        shapes = batch["created_date"].map(_shape)
        off = (shapes != self.ref_shape).astype(int)
        flags = off.astype(bool)
        alerts = []
        if flags.any():
            dev = (off - off.mean()).cumsum().to_numpy()
            cp = int(np.argmin(dev)) + 1  # change point: CUSUM minimum
            alerts.append(Alert(self.problem, "column", f"created_date format changes at row {cp}: shape "
                                f"'{shapes.iloc[min(cp, len(shapes) - 1)]}' replaces '{self.ref_shape}'",
                                column="created_date", row=cp, observed=shapes[flags].value_counts().index[0],
                                expected=self.ref_shape))
            alerts += _row_alerts(self.problem, flags, batch, "created_date", "date shape differs from reference",
                                  self.ref_shape)[:50]
        return self.result(row_flags=flags, alerts=alerts)


class DuplicateRateTest(Detector):
    name, family, problem, algorithm = "duplicate_rate_ztest", "statistical", "exact_duplicates", "binomial z-test on duplicate rate"

    def fit(self, ref: Reference):
        self.p0 = max(ref.dup_rate, 1e-5)
        return self

    def _detect(self, batch):
        dup = batch.astype(str).duplicated(keep="first")
        n, k = len(batch), int(dup.sum())
        z = (k - n * self.p0) / math.sqrt(n * self.p0 * (1 - self.p0))
        alarm = z > 5
        alerts = [Alert(self.problem, "batch", f"duplicate rate {k / n:.2%} vs reference {self.p0:.3%} (z={z:.1f})",
                        observed=k / n, expected=self.p0, score=z, column="*")] if alarm else []
        return self.result(row_flags=dup if alarm else pd.Series(False, index=batch.index), batch_flag=alarm,
                           alerts=alerts)


class NullRateBinomial(Detector):
    name, family, problem, algorithm = "null_rate_binomial", "statistical", "null_spike", "binomial z-test per agency/column"
    Z = 5.0

    def fit(self, ref: Reference):
        self.rates, self.overall = ref.null_rates, ref.null_rates_overall
        return self

    def _detect(self, batch):
        flags, alerts = pd.Series(False, index=batch.index), []
        for agency, g in batch.groupby("agency"):
            n = len(g)
            if n < 30:
                continue
            for col in NULL_SPIKE_COLUMNS:
                if col not in g.columns:
                    continue
                p0 = min(max(self.rates.get(agency, {}).get(col, self.overall[col]), 0.002), 0.98)
                k = int(g[col].isna().sum())
                z = (k - n * p0) / math.sqrt(n * p0 * (1 - p0))
                if z > self.Z and k / n > 2 * p0 + 0.05:
                    flags.loc[g.index[g[col].isna()]] = True
                    alerts.append(Alert(self.problem, "column", f"{agency}: '{col}' null rate {k / n:.1%} vs "
                                        f"reference {p0:.1%} (z={z:.1f}, n={n})", column=col, observed=k / n,
                                        expected=p0, score=z))
        return self.result(row_flags=flags, alerts=alerts)


class DurationTailTest(Detector):
    name, family, problem, algorithm = "duration_tail_test", "statistical", "closed_before_created", "reference quantile tails"

    def fit(self, ref: Reference):
        q = ref.duration_q
        self.lo = min(q["0.001"], 0.0) - 1e-6
        self.hi = q["0.999"] * 1.5
        return self

    def _detect(self, batch):
        hours = (parse_dates(batch["closed_date"]) - parse_dates(batch["created_date"])).dt.total_seconds() / 3600
        mask = hours.notna() & ((hours < self.lo) | (hours > self.hi))
        return self.result(row_flags=mask, alerts=_row_alerts(self.problem, mask, batch, "closed_date",
                           "resolution duration is outside the reference distribution",
                           f"{self.lo:.2f}h .. {self.hi:.0f}h", ["created_date", "closed_date"]))
