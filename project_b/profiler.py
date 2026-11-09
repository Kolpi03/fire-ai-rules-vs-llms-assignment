"""Task B1: profile the RAW 2023 NYC 311 sample for data-quality problems that already exist.

Methodology
-----------
* Population: the day-cluster sample (every 10th day of 2023, all rows of each sampled day; see
  data_loader). Counts are exact for the sample; percentages are of sample rows. A full-year projection
  (share x 3,224,723 rows) is given separately and labelled as an estimate. Where the SoQL aggregate
  endpoint answered, an exact full-year count is reported next to it.
* Checks are deterministic and listed in ``CHECKS`` with their definition, so every count is
  reproducible; examples are the first 5 matching rows (unique_key + the relevant fields).
* Free-text PII examples are masked before being written to the report.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import pandas as pd

from common.paths import BENCH_B, PB_REPORTS
from project_b.data_loader import fetch_full_year_aggregates, load_sample, sampled_days
from project_b.pii_detector import phonenumbers_pii
from project_b.reference import issue_masks, parse_dates, zip_majority_map

FULL_YEAR_ROWS = 3_224_723  # verified via SoQL count(*) on 2026-11-08

DEFINITIONS = {
    "closed_before_created": ("temporal", "closed_date < created_date"),
    "closed_equals_created": ("temporal", "closed_date == created_date to the second (auto-closed or placeholder)"),
    "status_closed_without_closed_date": ("consistency", "status == 'Closed' but closed_date is null"),
    "not_closed_but_has_closed_date": ("consistency", "status != 'Closed' but closed_date is set"),
    "missing_coordinates": ("completeness", "latitude or longitude null"),
    "coordinates_outside_nyc": ("validity", "lat/lon outside the NYC bounding box"),
    "missing_or_invalid_zip": ("validity", "incident_zip null or not 5 digits"),
    "borough_unspecified": ("completeness", "borough == 'Unspecified' or null"),
    "missing_descriptor": ("completeness", "descriptor null"),
    "missing_resolution_description": ("completeness", "resolution_description null"),
    "mojibake_in_text": ("encoding", "UTF-8 bytes decoded as Latin-1 in resolution text (e.g. 'Transportationâs')"),
    "created_at_exact_midnight": ("temporal", "created_date exactly 00:00:00 (date-only timestamps)"),
    "closed_more_than_365_days_after_created": ("temporal", "resolution took over a year"),
    "due_date_mostly_null": ("completeness", "due_date null (column effectively deprecated)"),
    "zip_borough_mismatch": ("consistency", "incident_zip's majority borough (>=95% of >=20 rows) differs from borough"),
    "exact_duplicate_rows": ("uniqueness", "row identical to another row in every column"),
    "duplicate_unique_key": ("uniqueness", "unique_key appears more than once"),
    "near_duplicate_requests": ("uniqueness", "same complaint_type + incident_address within 60 minutes of an earlier request"),
    "city_case_or_spelling_variants": ("consistency", "city values that differ only by case/whitespace from another value"),
    "resolution_updated_before_created": ("temporal", "resolution_action_updated_date < created_date"),
    "latlon_inconsistent_with_xy": ("consistency", "lat/lon more than 0.01 deg from the value implied by x/y state plane"),
    "possible_pii_in_resolution_text": ("privacy", "non-agency phone/e-mail or out-of-vocabulary proper-noun pair in resolution text"),
    "community_board_unspecified": ("completeness", "community_board starts with '0 Unspecified'"),
}


def _mask_pii(text: str) -> str:
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<EMAIL>", text)
    return re.sub(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", "<PHONE>", text)


def run_profile() -> dict[str, Any]:
    df = load_sample()
    n = len(df)
    zip_major = zip_majority_map(df)
    masks = issue_masks(df, zip_major)
    masks["exact_duplicate_rows"] = df.astype(str).duplicated(keep=False)
    masks["duplicate_unique_key"] = df["unique_key"].duplicated(keep=False)
    c = parse_dates(df["created_date"])
    key = df["complaint_type"].astype(str) + "|" + df["incident_address"].fillna("")
    tmp = pd.DataFrame({"k": key, "c": c, "has": df["incident_address"].notna()}).sort_values("c")
    gap = tmp.groupby("k")["c"].diff().dt.total_seconds()
    masks["near_duplicate_requests"] = (gap.notna() & (gap <= 3600) & tmp["has"]).reindex(df.index, fill_value=False)
    city = df["city"].dropna()
    canon = city.str.upper().str.strip()
    variants = city.groupby(canon).nunique()
    masks["city_case_or_spelling_variants"] = df["city"].str.upper().str.strip().isin(variants[variants > 1].index) & \
        (df["city"] != df["city"].str.upper())
    masks["resolution_updated_before_created"] = parse_dates(df["resolution_action_updated_date"]) < c
    from sklearn.linear_model import LinearRegression
    d = df.dropna(subset=["latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"])
    X = d[["x_coordinate_state_plane", "y_coordinate_state_plane"]].to_numpy(float)
    res_lat = (d["latitude"] - LinearRegression().fit(X, d["latitude"]).predict(X)).abs()
    res_lon = (d["longitude"] - LinearRegression().fit(X, d["longitude"]).predict(X)).abs()
    masks["latlon_inconsistent_with_xy"] = ((res_lat > 0.01) | (res_lon > 0.01)).reindex(df.index, fill_value=False)
    vocab: set[str] = set()
    texts = df["resolution_description"].dropna()
    tc = texts.value_counts()
    for t in tc[tc >= 20].index:  # vocabulary of recurring templates
        vocab.update(re.findall(r"[a-z']+", t.lower()))
    from project_b.pii_detector import EMAIL, PHONE
    from project_b.reference import _digits_or_lower
    contact_freq = df["resolution_description"].dropna().map(
        lambda t: [_digits_or_lower(m.group(0)) for m in list(PHONE.finditer(t)) + list(EMAIL.finditer(t))]).explode().value_counts()
    allow = set(contact_freq[contact_freq >= 5].index)
    cache: dict[str, bool] = {}
    masks["possible_pii_in_resolution_text"] = df["resolution_description"].map(
        lambda t: cache.setdefault(t, bool(phonenumbers_pii(t, vocab, allow))) if isinstance(t, str) else False)
    masks["community_board_unspecified"] = df["community_board"].fillna("").str.startswith("0 Unspecified")

    example_cols = {
        "closed_before_created": ["created_date", "closed_date", "agency"],
        "closed_equals_created": ["created_date", "closed_date", "agency", "complaint_type"],
        "not_closed_but_has_closed_date": ["status", "closed_date"],
        "status_closed_without_closed_date": ["status", "closed_date"],
        "mojibake_in_text": ["resolution_description"],
        "zip_borough_mismatch": ["incident_zip", "borough"],
        "latlon_inconsistent_with_xy": ["latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"],
        "possible_pii_in_resolution_text": ["resolution_description"],
        "near_duplicate_requests": ["created_date", "complaint_type", "incident_address"],
        "city_case_or_spelling_variants": ["city"],
    }
    issues = []
    for name, (itype, definition) in DEFINITIONS.items():
        m = masks[name].fillna(False).astype(bool)
        k = int(m.sum())
        cols = ["unique_key"] + example_cols.get(name, ["agency", "complaint_type"])
        ex = df.loc[m, [c for c in cols if c in df.columns]].head(5).astype(str)
        if "resolution_description" in ex.columns:
            ex["resolution_description"] = ex["resolution_description"].map(lambda t: _mask_pii(t)[:160])
        issues.append({"issue": name, "issue_type": itype, "definition": definition, "affected_rows": k,
                       "pct_of_sample": k / n, "projected_full_year_rows": round(k / n * FULL_YEAR_ROWS),
                       "examples": ex.to_dict(orient="records")})
    issues.sort(key=lambda r: -r["affected_rows"])
    by_agency_closed_before = df[masks["closed_before_created"]]["agency"].value_counts().head(5).to_dict()
    try:
        agg = fetch_full_year_aggregates(timeout=90, offline=os.environ.get("NYC_OFFLINE") == "1")
    except Exception as exc:  # noqa: BLE001
        agg = {"error": str(exc)}
    out = {"sample_rows": n, "sampled_days": len(list(PB_REPORTS.parent.joinpath("raw").glob("day_*.csv"))),
           "design_days": len(sampled_days()), "full_year_rows": FULL_YEAR_ROWS,
           "methodology": __doc__, "issues": issues, "full_year_aggregates": agg,
           "closed_before_created_by_agency": by_agency_closed_before,
           "column_null_rates": {c: float(df[c].isna().mean()) for c in df.columns}}
    PB_REPORTS.mkdir(parents=True, exist_ok=True)
    BENCH_B.mkdir(parents=True, exist_ok=True)
    (PB_REPORTS / "profile.json").write_text(json.dumps(out, indent=1, default=str))
    pd.DataFrame([{k: v for k, v in i.items() if k != "examples"} for i in issues]).to_csv(PB_REPORTS / "profile.csv", index=False)
    (PB_REPORTS / "profile.md").write_text(to_markdown(out))
    return out


def to_markdown(out: dict[str, Any]) -> str:
    lines = ["# NYC 311 (2023) raw-data profile", "",
             f"Sample: {out['sample_rows']:,} rows from {out['sampled_days']} of {out['design_days']} planned sampled days "
             f"(full year: {out['full_year_rows']:,} rows).", "",
             "| Issue | Type | Definition | Rows (sample) | % of sample | Projected full year* |",
             "|---|---|---|---:|---:|---:|"]
    for i in out["issues"]:
        lines.append(f"| {i['issue']} | {i['issue_type']} | {i['definition']} | {i['affected_rows']:,} | "
                     f"{i['pct_of_sample'] * 100:.2f}% | {i['projected_full_year_rows']:,} |")
    lines += ["", "*Projection = sample share x full-year rows (estimate, not a count).", "",
              "## Exact full-year counts (SoQL aggregates, where the API answered)", ""]
    for k, v in out["full_year_aggregates"].items():
        if isinstance(v, dict):
            lines.append(f"- {k}: {v.get('count', 'unavailable - ' + str(v.get('error', ''))[:80])}")
    lines += ["", "## Examples (first 5 rows per issue; PII masked)", ""]
    for i in out["issues"]:
        if i["affected_rows"]:
            lines.append(f"**{i['issue']}**")
            for e in i["examples"][:3]:
                lines.append(f"- `{json.dumps(e)[:300]}`")
            lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    r = run_profile()
    for i in r["issues"]:
        print(f"{i['issue']:<42} {i['affected_rows']:>8,} {i['pct_of_sample'] * 100:6.2f}%")
