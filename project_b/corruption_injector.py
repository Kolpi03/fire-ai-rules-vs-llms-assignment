"""Corruption injector: 12 problem types, configurable rates, seeded, with ground-truth labels.

Contract
--------
``inject(batch, problem, rate, seed) -> InjectionResult``
* ``batch`` is never modified (we copy); the clean original is kept alongside for audit.
* ``labels`` is a boolean Series over the CORRUPTED batch's index: True for rows that carry the problem.
  Rows appended by duplicate injectors are labelled True; their source rows stay False.
* ``meta`` records exactly what was done (columns touched, agency chosen, PII types, etc.).
* Batch-level problems (schema drift, type change) set ``batch_label=True`` and label the affected
  rows/column in ``meta``.

Each injector mimics how the problem arises in production feeds (e.g. the date switch uses the
"MM/DD/YYYY hh:mm:ss AM" format of NYC Open Data's own CSV export; PII is appended the way an agent
might paste caller details into the resolution note).
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from project_b.detection_types import PROBLEMS
from project_b.reference import Reference, parse_dates

warnings.filterwarnings("ignore", message="The behavior of DataFrame concatenation with empty or all-NA entries")

DEFAULT_RATES: dict[str, float] = {
    "schema_drift": 1.0, "type_change": 1.0, "date_format_switch": 0.3, "lat_lon_swap": 0.03,
    "exact_duplicates": 0.03, "near_duplicates": 0.03, "null_spike": 0.6, "closed_before_created": 0.03,
    "zip_borough_mismatch": 0.03, "complaint_descriptor_mismatch": 0.03, "resolution_status_contradiction": 0.03,
    "pii_in_text": 0.03,
}

FIRST_NAMES = ["Maria", "James", "Priya", "Wei", "Fatima", "John", "Aisha", "Carlos", "Olga", "Daniel", "Grace",
               "Mohammed", "Elena", "Kwame", "Sofia", "Ravi", "Hannah", "Luis", "Mei", "Patrick"]
LAST_NAMES = ["Gonzalez", "Smith", "Patel", "Chen", "Khan", "Murphy", "Okafor", "Rossi", "Ivanova", "Cohen",
              "Nguyen", "Rodriguez", "Sharma", "Kim", "Brown", "Haddad", "Silva", "Mensah", "Lopez", "Walsh"]


@dataclass
class InjectionResult:
    problem: str
    batch: pd.DataFrame
    labels: pd.Series
    batch_label: bool
    meta: dict[str, Any] = field(default_factory=dict)


def _n(rate: float, n: int) -> int:
    return max(1, int(round(rate * n)))


def _pick(rng: np.random.Generator, idx: pd.Index, k: int) -> pd.Index:
    return pd.Index(rng.choice(np.asarray(idx), size=min(k, len(idx)), replace=False))


def schema_drift(df, rate, rng, ref):
    candidates = ["incident_zip", "borough", "created_date", "complaint_type", "descriptor", "latitude", "status"]
    col = str(rng.choice(candidates))
    if rng.random() < 0.5:
        new = {"incident_zip": "zip_code", "borough": "Borough", "created_date": "created_dt",
               "complaint_type": "complaint", "descriptor": "descriptor_1", "latitude": "lat", "status": "sr_status"}[col]
        out = df.rename(columns={col: new})
        meta = {"action": "rename", "column": col, "new_name": new}
    else:
        out = df.drop(columns=[col])
        meta = {"action": "drop", "column": col}
    return out, pd.Series(False, index=out.index), True, meta


def type_change(df, rate, rng, ref):
    col = str(rng.choice(["latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"]))
    out = df.copy()
    rows = _pick(rng, out.index, _n(rate, len(out)))
    as_str = out[col].astype(object)
    fmt = (lambda v: f"{v:,.0f}") if col.startswith(("x_", "y_")) else (lambda v: f"{v:.6f}")
    as_str.loc[rows] = [fmt(v) if pd.notna(v) else v for v in out.loc[rows, col]]
    out[col] = as_str
    labels = pd.Series(False, index=out.index)
    labels.loc[rows] = True
    return out, labels, True, {"column": col, "rows_as_strings": len(rows)}


def date_format_switch(df, rate, rng, ref):
    out = df.copy()
    start = int(len(out) * (1 - rate))
    rows = out.index[start:]
    for col in ("created_date", "closed_date"):
        parsed = parse_dates(out.loc[rows, col])
        out.loc[rows, col] = parsed.dt.strftime("%m/%d/%Y %I:%M:%S %p").where(parsed.notna(), out.loc[rows, col])
    labels = pd.Series(False, index=out.index)
    labels.loc[rows] = True
    return out, labels, False, {"switch_position": start, "format": "%m/%d/%Y %I:%M:%S %p"}


def lat_lon_swap(df, rate, rng, ref):
    out = df.copy()
    rows = _pick(rng, out.index[out["latitude"].notna()], _n(rate, len(out)))
    out.loc[rows, ["latitude", "longitude"]] = out.loc[rows, ["longitude", "latitude"]].to_numpy()
    labels = pd.Series(False, index=out.index)
    labels.loc[rows] = True
    return out, labels, False, {"rows": len(rows)}


def _merge_chronologically(df: pd.DataFrame, extra: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Append rows and restore feed order (stable sort on created_date): an exact copy lands right after its
    original, so "the duplicate" is unambiguously the later occurrence. Returns (batch, labels)."""
    extra = extra.astype({c: df[c].dtype for c in df.columns if c in extra.columns}, errors="ignore")
    both = pd.concat([df.assign(_new=False), extra.assign(_new=True)], ignore_index=True)
    order = np.argsort(parse_dates(both["created_date"]).to_numpy(), kind="stable")
    both = both.iloc[order].reset_index(drop=True)
    return both.drop(columns="_new"), both["_new"].astype(bool)


def exact_duplicates(df, rate, rng, ref):
    src = _pick(rng, df.index, _n(rate, len(df)))
    out, labels = _merge_chronologically(df, df.loc[src])
    return out, labels, False, {"duplicates_added": len(src)}


def _typo(s: str, rng: np.random.Generator) -> str:
    if not isinstance(s, str) or len(s) < 4:
        return s
    i = int(rng.integers(1, len(s) - 1))
    op = rng.integers(0, 3)
    if op == 0:
        return s[:i] + s[i + 1:]                      # deletion
    if op == 1:
        return s[:i] + s[i] + s[i:]                   # duplication
    return s[:i - 1] + s[i] + s[i - 1] + s[i + 1:]    # transposition


def near_duplicates(df, rate, rng, ref):
    src = _pick(rng, df.index[df["incident_address"].notna()], _n(rate, len(df)))
    nd = df.loc[src].copy()
    shift = pd.to_timedelta(rng.integers(30, 20 * 60, size=len(nd)), unit="s")
    created = parse_dates(nd["created_date"]) + shift.to_numpy()
    nd["created_date"] = created.dt.strftime("%Y-%m-%dT%H:%M:%S.000")
    nd["unique_key"] = (df["unique_key"].max() + 1 + np.arange(len(nd))).astype("int64")
    which = rng.random(len(nd))
    nd["incident_address"] = [(_typo(a, rng) if w < 0.5 else a) for a, w in zip(nd["incident_address"], which)]
    nd["descriptor"] = [(d.upper() if (w >= 0.5 and isinstance(d, str)) else d) for d, w in zip(nd["descriptor"], which)]
    closed = parse_dates(nd["closed_date"]) + shift.to_numpy()  # keep closed >= created; status/text unchanged
    nd["closed_date"] = closed.dt.strftime("%Y-%m-%dT%H:%M:%S.000").where(closed.notna(), nd["closed_date"])
    out, labels = _merge_chronologically(df, nd)
    return out, labels, False, {"near_duplicates_added": len(nd), "time_shift_s": [30, 1200]}


def null_spike(df, rate, rng, ref):
    counts = df["agency"].value_counts()
    agency = str(rng.choice(counts[counts >= 100].index[:6]))
    col = str(rng.choice(["incident_zip", "incident_address", "latitude", "descriptor", "location_type"]))
    out = df.copy()
    pool = out.index[(out["agency"] == agency) & out[col].notna()]
    rows = _pick(rng, pool, int(round(rate * len(pool))))
    out.loc[rows, col] = np.nan if col == "latitude" else pd.NA
    if col == "latitude":
        out.loc[rows, "longitude"] = np.nan
    labels = pd.Series(False, index=out.index)
    labels.loc[rows] = True
    return out, labels, False, {"agency": agency, "column": col, "rows_nulled": len(rows)}


def closed_before_created(df, rate, rng, ref):
    out = df.copy()
    rows = _pick(rng, out.index[out["closed_date"].notna()], _n(rate, len(out)))
    created = parse_dates(out.loc[rows, "created_date"])
    back = pd.to_timedelta(rng.integers(60, 30 * 24 * 3600, size=len(rows)), unit="s")
    out.loc[rows, "closed_date"] = (created - back.to_numpy()).dt.strftime("%Y-%m-%dT%H:%M:%S.000")
    labels = pd.Series(False, index=out.index)
    labels.loc[rows] = True
    return out, labels, False, {"rows": len(rows)}


def zip_borough_mismatch(df, rate, rng, ref):
    out = df.copy()
    by_borough: dict[str, list[str]] = {}
    for z, b in ref.zip_borough.items():
        by_borough.setdefault(b, []).append(z)
    rows = _pick(rng, out.index, _n(rate, len(out)))
    new = []
    for r in rows:
        own = out.at[r, "borough"]
        others = [b for b in by_borough if b != own]
        new.append(str(rng.choice(by_borough[str(rng.choice(others))])))
    out.loc[rows, "incident_zip"] = new
    labels = pd.Series(False, index=out.index)
    labels.loc[rows] = True
    return out, labels, False, {"rows": len(rows)}


def complaint_descriptor_mismatch(df, rate, rng, ref):
    out = df.copy()
    types = list(ref.allowed_pairs)
    rows = _pick(rng, out.index, _n(rate, len(out)))
    new = []
    for r in rows:
        own = out.at[r, "complaint_type"]
        allowed_own = set(ref.allowed_pairs.get(own, []))
        while True:
            other = str(rng.choice(types))
            cands = [d for d in ref.allowed_pairs[other] if d not in allowed_own]
            if other != own and cands:
                new.append(str(rng.choice(cands)))
                break
    out.loc[rows, "descriptor"] = new
    labels = pd.Series(False, index=out.index)
    labels.loc[rows] = True
    return out, labels, False, {"rows": len(rows)}


def resolution_status_contradiction(df, rate, rng, ref):
    """Closed rows get an open status (and closed_date removed, so only the TEXT contradicts the status);
    open rows get a closure-style resolution text."""
    out = df.copy()
    closed = out.index[out["status"] == "Closed"]
    rows = _pick(rng, closed, _n(rate, len(out)))
    out.loc[rows, "status"] = rng.choice(["Open", "In Progress", "Pending", "Assigned"], size=len(rows))
    out.loc[rows, "closed_date"] = pd.NA
    out.loc[rows, "resolution_action_updated_date"] = pd.NA
    labels = pd.Series(False, index=out.index)
    labels.loc[rows] = True
    return out, labels, False, {"rows": len(rows), "mode": "closed text with open status"}


def pii_in_text(df, rate, rng, ref):
    out = df.copy()
    rows = _pick(rng, out.index[out["resolution_description"].notna()], _n(rate, len(out)))
    kinds = rng.choice(["phone", "email", "name", "name_bare"], size=len(rows))
    texts = []
    for r, kind in zip(rows, kinds):
        first, last = str(rng.choice(FIRST_NAMES)), str(rng.choice(LAST_NAMES))
        snippet = {
            "phone": f" Caller callback number {rng.integers(200, 999)}-{rng.integers(200, 999)}-{rng.integers(1000, 9999)}.",
            "email": f" Follow-up sent to {first.lower()}.{last.lower()}{rng.integers(1, 99)}@gmail.com.",
            "name": f" Caller {first} {last} confirmed the condition.",
            "name_bare": f" {first} {last} at the location was informed.",
        }[str(kind)]
        texts.append(str(out.at[r, "resolution_description"]) + snippet)
    out.loc[rows, "resolution_description"] = texts
    labels = pd.Series(False, index=out.index)
    labels.loc[rows] = True
    return out, labels, False, {"rows": len(rows), "kinds": pd.Series(kinds).value_counts().to_dict()}


INJECTORS: dict[str, Callable] = {
    "schema_drift": schema_drift, "type_change": type_change, "date_format_switch": date_format_switch,
    "lat_lon_swap": lat_lon_swap, "exact_duplicates": exact_duplicates, "near_duplicates": near_duplicates,
    "null_spike": null_spike, "closed_before_created": closed_before_created,
    "zip_borough_mismatch": zip_borough_mismatch, "complaint_descriptor_mismatch": complaint_descriptor_mismatch,
    "resolution_status_contradiction": resolution_status_contradiction, "pii_in_text": pii_in_text,
}
assert set(INJECTORS) == set(PROBLEMS)


def inject(batch: pd.DataFrame, problem: str, ref: Reference, rate: float | None = None,
           seed: int = 0) -> InjectionResult:
    rng = np.random.default_rng(seed)
    rate = DEFAULT_RATES[problem] if rate is None else rate
    out, labels, batch_label, meta = INJECTORS[problem](batch.copy(), rate, rng, ref)
    meta.update(problem=problem, rate=rate, seed=seed, n_in=len(batch), n_out=len(out))
    return InjectionResult(problem, out, labels.astype(bool), batch_label or bool(labels.any()), meta)


def inject_many(batch: pd.DataFrame, problems: list[str], ref: Reference, seed: int,
                rates: dict[str, float] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Inject several row-level problems into one batch (end-to-end hybrid evaluation).

    Labels travel WITH the rows as hidden ``_lbl_<problem>`` columns, so row-appending injectors (exact and
    near duplicates) and re-sorting keep them aligned, and a copy of a corrupted row inherits its source's
    labels. Exact duplicates are injected after the other row problems (so a copy duplicates the fully corrupted
    row and stays an exact copy), and the date-format switch last, because it is a formatting layer: injecting
    temporal problems into already re-formatted dates would silently fail to corrupt them. Remaining overlap
    effects (e.g. a null spike erasing a swapped coordinate) are reported, not hidden.
    Returns (batch, label matrix [rows x problems], metas).
    """
    last = ("exact_duplicates", "date_format_switch")  # copy fully-corrupted rows; formatting layer last
    ordered = [p for p in problems if p not in last] + [p for p in last if p in problems]
    cur = batch.copy()
    for p in PROBLEMS:
        cur[f"_lbl_{p}"] = False
    metas = []
    for i, p in enumerate(ordered):
        res = inject(cur, p, ref, (rates or {}).get(p), seed=seed * 100 + i)
        cur = res.batch
        cur[f"_lbl_{p}"] = cur[f"_lbl_{p}"].astype(bool) | res.labels.to_numpy()
        metas.append(res.meta)
    lbl_cols = [f"_lbl_{p}" for p in PROBLEMS]
    labels = cur[lbl_cols].astype(bool)
    labels.columns = list(PROBLEMS)
    return cur.drop(columns=lbl_cols), labels, metas
