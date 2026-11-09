"""Clean pool, temporal split and the learned reference ("data contract") used by all detectors.

Clean pool
----------
Benchmarks need clean batches whose ONLY problems are the injected ones. We start from the real 2023
sample and remove rows exhibiting the real issues the profiler finds (closed<created, closed/status
inconsistency, missing/invalid coordinates or ZIP, unspecified borough, ZIP-borough disagreement with
the majority mapping, missing descriptor/resolution) and collapse real near-duplicates (same complaint
type and address within 60 minutes). This is stated plainly in RESULTS_B: false-alarm rates on clean
batches are measured on data that passed these filters, so they understate false alarms on raw data;
the raw-data alert counts are reported separately by the profiler.

Temporal split
--------------
Reference (fitting/learning) = sampled days in Jan-Sep 2023; evaluation batches = Oct-Dec 2023. No
detector sees evaluation-period rows during fitting.

Learned reference
-----------------
Built once from the reference period and frozen to ``data/processed/reference.json``: expected columns
and dtypes, ZIP->borough majority map, ZIP centroids, allowed (complaint_type, descriptor) pairs, per
agency/column null rates, duplicate rates, resolution-text vocabulary, and duration quantiles.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from common.config import settings
from common.logging_utils import get_logger, log_event
from common.paths import PB_PROCESSED
from project_b.data_loader import load_sample

log = get_logger("project_b.reference")

NYC_BBOX = {"lat": (40.47, 40.93), "lon": (-74.27, -73.68)}
NULL_SPIKE_COLUMNS = ["incident_zip", "incident_address", "latitude", "longitude", "descriptor", "location_type", "city"]


def parse_dates(s: pd.Series) -> pd.Series:
    """Parse the canonical ISO format strictly (other formats -> NaT)."""
    return pd.to_datetime(s, format="%Y-%m-%dT%H:%M:%S.%f", errors="coerce")


def issue_masks(df: pd.DataFrame, zip_major: dict[str, str] | None = None) -> dict[str, pd.Series]:
    """Boolean masks for real data-quality issues (shared by the profiler and the clean-pool filter)."""
    c, cl = parse_dates(df["created_date"]), parse_dates(df["closed_date"])
    lat, lon = df["latitude"], df["longitude"]
    zip_ok = df["incident_zip"].fillna("").str.fullmatch(r"\d{5}")
    m = {
        "closed_before_created": cl.notna() & (cl < c),
        "closed_equals_created": cl.notna() & (cl == c),
        "status_closed_without_closed_date": (df["status"] == "Closed") & cl.isna(),
        "not_closed_but_has_closed_date": (df["status"] != "Closed") & cl.notna(),
        "missing_coordinates": lat.isna() | lon.isna(),
        "coordinates_outside_nyc": lat.notna() & ~(lat.between(*NYC_BBOX["lat"]) & lon.between(*NYC_BBOX["lon"])),
        "missing_or_invalid_zip": ~zip_ok,
        "borough_unspecified": df["borough"].fillna("Unspecified") == "Unspecified",
        "missing_descriptor": df["descriptor"].isna(),
        "missing_resolution_description": df["resolution_description"].isna(),
        "mojibake_in_text": df["resolution_description"].fillna("").str.contains("â€|â\x80|Ã|â(?=s\\b)", regex=True),
        "created_at_exact_midnight": c.dt.strftime("%H:%M:%S").eq("00:00:00"),
        "closed_more_than_365_days_after_created": (cl - c).dt.days > 365,
        "due_date_mostly_null": df["due_date"].isna(),
    }
    if zip_major:
        mapped = df["incident_zip"].map(zip_major)
        m["zip_borough_mismatch"] = mapped.notna() & (df["borough"] != "Unspecified") & (mapped != df["borough"])
    return m


def zip_majority_map(df: pd.DataFrame, min_support: int = 20, min_share: float = 0.95) -> dict[str, str]:
    z = df.dropna(subset=["incident_zip", "borough"])
    z = z[(z["borough"] != "Unspecified") & z["incident_zip"].str.fullmatch(r"\d{5}")]
    g = z.groupby("incident_zip")["borough"]
    counts, top = g.size(), g.agg(lambda s: s.value_counts().index[0])
    share = g.agg(lambda s: s.value_counts(normalize=True).iloc[0])
    keep = (counts >= min_support) & (share >= min_share)
    return top[keep].to_dict()


def build_clean_pool(df: pd.DataFrame, zip_major: dict[str, str]) -> pd.DataFrame:
    m = issue_masks(df, zip_major)
    bad = (m["closed_before_created"] | m["status_closed_without_closed_date"] | m["not_closed_but_has_closed_date"]
           | m["missing_coordinates"] | m["coordinates_outside_nyc"] | m["missing_or_invalid_zip"]
           | m["borough_unspecified"] | m["missing_descriptor"] | m["missing_resolution_description"]
           | m["zip_borough_mismatch"] | df["incident_zip"].map(zip_major).isna())
    clean = df[~bad].copy()
    # collapse real near-duplicates: same complaint type + address within 60 minutes (keep the first)
    clean["_c"] = parse_dates(clean["created_date"])
    clean = clean.sort_values("_c")
    key = clean["complaint_type"].astype(str) + "|" + clean["incident_address"].fillna("").astype(str)
    gap = clean.groupby(key)["_c"].diff().dt.total_seconds()
    near = gap.notna() & (gap <= 3600) & clean["incident_address"].notna()
    clean = clean[~near].drop(columns="_c").reset_index(drop=True)
    log_event(log, "clean pool", raw=len(df), clean=len(clean), removed_issue=int(bad.sum()), removed_near_dup=int(near.sum()))
    return clean


@dataclass
class Reference:
    """Frozen, JSON-serialisable knowledge learned from the reference period."""
    columns: list[str]
    dtypes: dict[str, str]
    zip_borough: dict[str, str]
    zip_centroid: dict[str, list[float]]        # zip -> [lat, lon, lat_std, lon_std]
    allowed_pairs: dict[str, list[str]]         # complaint_type -> descriptors seen >= 2 times
    null_rates: dict[str, dict[str, float]]     # agency -> column -> null rate
    null_rates_overall: dict[str, float]
    agency_counts: dict[str, int]
    dup_rate: float
    date_shape: str
    vocab: list[str]                            # resolution-text vocabulary (lower-case tokens)
    duration_q: dict[str, float]                # quantiles of closed-created hours
    n_rows: int
    contact_allowlist: list[str]                # phones/e-mails repeated across >= 5 reference requests

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, s: str) -> "Reference":
        return cls(**json.loads(s))


def _digits_or_lower(contact: str) -> str:
    """Normalise a phone (digits only) or e-mail (lower case) for allowlist matching."""
    return contact.lower() if "@" in contact else re.sub(r"\D", "", contact)[-10:]


def _shape(v: Any) -> str:
    import re as _re
    return _re.sub(r"[0-9]", "d", str(v)) if v is not None and v == v else "<null>"


def build_reference(ref: pd.DataFrame, zip_major: dict[str, str]) -> Reference:
    import re as _re
    centroid = ref.groupby("incident_zip").agg(lat=("latitude", "mean"), lon=("longitude", "mean"),
                                               lat_s=("latitude", "std"), lon_s=("longitude", "std"),
                                               n=("latitude", "size"))
    centroid = centroid[centroid["n"] >= 10].fillna(0.01)
    pairs = ref.groupby(["complaint_type", "descriptor"]).size()
    pairs = pairs[pairs >= 2].reset_index()
    allowed = pairs.groupby("complaint_type")["descriptor"].agg(list).to_dict()
    agencies = ref["agency"].value_counts()
    null_rates = {a: {c: float(g[c].isna().mean()) for c in NULL_SPIKE_COLUMNS}
                  for a, g in ref.groupby("agency") if len(g) >= 100}
    vocab: set[str] = set()
    for t in ref["resolution_description"].dropna().unique():
        vocab.update(_re.findall(r"[a-z']+", t.lower()))
    c, cl = parse_dates(ref["created_date"]), parse_dates(ref["closed_date"])
    hours = ((cl - c).dt.total_seconds() / 3600).dropna()
    shapes = ref["created_date"].map(_shape).value_counts()
    from project_b.pii_detector import EMAIL, PHONE
    contacts = ref["resolution_description"].dropna().map(
        lambda t: {m.group(0) for m in PHONE.finditer(t)} | {m.group(0) for m in EMAIL.finditer(t)})
    contact_counts = pd.Series([c for s in contacts for c in s]).value_counts()
    allow = sorted(_digits_or_lower(c) for c in contact_counts[contact_counts >= 5].index)
    return Reference(
        columns=list(ref.columns), dtypes={c: str(ref[c].dtype) for c in ref.columns},
        zip_borough=zip_major,
        zip_centroid={z: [float(r.lat), float(r.lon), float(max(r.lat_s, 0.003)), float(max(r.lon_s, 0.003))]
                      for z, r in centroid.iterrows()},
        allowed_pairs=allowed, null_rates=null_rates,
        null_rates_overall={c: float(ref[c].isna().mean()) for c in NULL_SPIKE_COLUMNS},
        agency_counts={k: int(v) for k, v in agencies.items()},
        dup_rate=float(ref.duplicated().mean()), date_shape=str(shapes.index[0]), vocab=sorted(vocab),
        duration_q={str(q): float(hours.quantile(q)) for q in (0.001, 0.01, 0.5, 0.99, 0.999)},
        n_rows=len(ref), contact_allowlist=allow)


REF_PATH = PB_PROCESSED / "reference.json"
POOL_REF = PB_PROCESSED / "clean_reference_pool.parquet"
POOL_EVAL = PB_PROCESSED / "clean_eval_pool.parquet"


def prepare(force: bool = False) -> tuple[Reference, pd.DataFrame, pd.DataFrame]:
    """Build (or load) the reference, the reference-period clean pool and the evaluation clean pool."""
    if REF_PATH.exists() and POOL_EVAL.exists() and POOL_REF.exists() and not force:
        return Reference.from_json(REF_PATH.read_text()), pd.read_parquet(POOL_REF), pd.read_parquet(POOL_EVAL)
    cfg = settings()["project_b"]
    df = load_sample()
    month = pd.to_datetime(df["created_date"].str[:10], errors="coerce").dt.month
    ref_raw = df[month.isin(cfg["reference_months"])]
    zip_major = zip_majority_map(ref_raw)
    pool = build_clean_pool(df, zip_major)
    pm = pd.to_datetime(pool["created_date"].str[:10]).dt.month
    ref_pool = pool[pm.isin(cfg["reference_months"])].reset_index(drop=True)
    eval_pool = pool[pm.isin(cfg["evaluation_months"])].reset_index(drop=True)
    reference = build_reference(ref_pool, zip_major)
    PB_PROCESSED.mkdir(parents=True, exist_ok=True)
    REF_PATH.write_text(reference.to_json())
    ref_pool.to_parquet(POOL_REF, index=False)
    eval_pool.to_parquet(POOL_EVAL, index=False)
    log_event(log, "reference built", ref_rows=len(ref_pool), eval_rows=len(eval_pool), zips=len(zip_major))
    return reference, ref_pool, eval_pool
