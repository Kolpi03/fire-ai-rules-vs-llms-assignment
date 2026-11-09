"""Family A — declarative rules and schema checks (one detector per problem type).

All checks are explicit and data-contract style: they encode what the data MUST look like (columns,
types, formats, ranges, lookups, allowed pairs, keyword lexicons). They are fit on nothing except the
frozen reference tables (``Reference``), cost microseconds per row, and every alert states the rule.
"""
from __future__ import annotations

import re

import pandas as pd
import pandera.pandas as pa

from project_b.detection_types import Alert, Detector
from project_b.pii_detector import regex_pii
from project_b.reference import NULL_SPIKE_COLUMNS, NYC_BBOX, Reference, parse_dates

ISO_RE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}"
NUMERIC_COLS = ["latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"]
_MAX_ALERTS = 200


def _row_alerts(problem: str, mask: pd.Series, batch: pd.DataFrame, column: str, reason: str,
                expected: str, observed_cols: list[str] | None = None) -> list[Alert]:
    out = []
    for i in mask[mask].index[:_MAX_ALERTS]:
        obs = {c: batch.at[i, c] for c in (observed_cols or [column]) if c in batch.columns}
        out.append(Alert(problem, "row", reason, row=int(i), column=column, observed=obs, expected=expected))
    return out


class SchemaContract(Detector):
    """Pandera DataFrameSchema: required columns must exist (no silent rename/drop)."""
    name, family, problem, algorithm = "pandera_column_contract", "rule", "schema_drift", "Pandera required-column contract"

    def fit(self, ref: Reference) -> "SchemaContract":
        self.ref = ref
        self.schema = pa.DataFrameSchema({c: pa.Column(nullable=True, required=True) for c in ref.columns}, strict=True)
        return self

    def _detect(self, batch):
        missing = [c for c in self.ref.columns if c not in batch.columns]
        extra = [c for c in batch.columns if c not in self.ref.columns]
        alerts = [Alert(self.problem, "column", f"required column '{c}' is missing from the batch", column=c,
                        observed=sorted(batch.columns), expected=f"column '{c}' present") for c in missing]
        alerts += [Alert(self.problem, "column", f"unexpected column '{c}' not in the contract", column=c,
                         observed=c, expected=f"one of {len(self.ref.columns)} contract columns") for c in extra]
        return self.result(batch_flag=bool(missing or extra), alerts=alerts)


class DtypeContract(Detector):
    """Pandera dtype checks: coordinate columns must be numeric (float64)."""
    name, family, problem, algorithm = "pandera_dtype_contract", "rule", "type_change", "Pandera column dtype check"

    def fit(self, ref):
        self.schema = pa.DataFrameSchema({c: pa.Column(float, nullable=True, required=False) for c in NUMERIC_COLS})
        return self

    def _detect(self, batch):
        alerts, flags = [], pd.Series(False, index=batch.index)
        try:
            self.schema.validate(batch, lazy=True)
        except pa.errors.SchemaErrors as exc:
            fc = exc.failure_cases
            for col in fc["column"].dropna().unique():
                alerts.append(Alert(self.problem, "column", f"column '{col}' failed dtype check (expected float64)",
                                    column=str(col), observed=str(batch[col].dtype), expected="float64"))
                flags |= batch[col].map(lambda v: isinstance(v, str))
        return self.result(row_flags=flags, batch_flag=bool(alerts), alerts=alerts)


class DateFormatRule(Detector):
    name, family, problem, algorithm = "iso8601_regex", "rule", "date_format_switch", "regex: ISO-8601 with millis"

    def _detect(self, batch):
        col = batch["created_date"] if "created_date" in batch.columns else pd.Series(dtype=str)
        mask = ~col.astype("string").str.fullmatch(ISO_RE).fillna(False)
        return self.result(row_flags=mask, alerts=_row_alerts(self.problem, mask, batch, "created_date",
                           "created_date is not in the contract format", "YYYY-MM-DDThh:mm:ss.000"))


class BBoxSwapRule(Detector):
    name, family, problem, algorithm = "nyc_bbox_rule", "rule", "lat_lon_swap", "range check: NYC bounding box"

    def _detect(self, batch):
        lat = pd.to_numeric(batch.get("latitude"), errors="coerce")
        lon = pd.to_numeric(batch.get("longitude"), errors="coerce")
        swapped = lat.between(*NYC_BBOX["lon"]) & lon.between(*NYC_BBOX["lat"])
        outside = lat.notna() & ~(lat.between(*NYC_BBOX["lat"]) & lon.between(*NYC_BBOX["lon"]))
        mask = swapped | outside
        return self.result(row_flags=mask, alerts=_row_alerts(self.problem, mask, batch, "latitude",
                           "coordinates fall outside NYC; values fit the box when swapped",
                           f"lat in {NYC_BBOX['lat']}, lon in {NYC_BBOX['lon']}", ["latitude", "longitude"]))


class ExactDuplicateRule(Detector):
    name, family, problem, algorithm = "exact_duplicate_rule", "rule", "exact_duplicates", "pandas duplicated(all columns)"

    def _detect(self, batch):
        mask = batch.astype(str).duplicated(keep="first")
        return self.result(row_flags=mask, alerts=_row_alerts(self.problem, mask, batch, "unique_key",
                           "row is an exact copy of an earlier row", "unique rows"))


class NearDuplicateRule(Detector):
    """Same complaint type + same normalised address + same agency within 30 minutes, different key."""
    name, family, problem, algorithm = "blocking_key_rule", "rule", "near_duplicates", "exact blocking key + 30-min window"

    def _detect(self, batch):
        c = parse_dates(batch["created_date"])
        addr = batch["incident_address"].astype("string").str.upper().str.replace(r"\s+", " ", regex=True)
        key = batch["complaint_type"].astype(str) + "|" + addr.fillna("") + "|" + batch["agency"].astype(str)
        tmp = pd.DataFrame({"k": key, "c": c, "has": addr.notna()}).sort_values("c")
        gap = tmp.groupby("k")["c"].diff().dt.total_seconds()
        mask = (gap.notna() & (gap <= 1800) & tmp["has"]).reindex(batch.index, fill_value=False)
        return self.result(row_flags=mask, alerts=_row_alerts(self.problem, mask, batch, "incident_address",
                           "same complaint type, address and agency as another request within 30 minutes",
                           "one request per incident", ["complaint_type", "incident_address", "created_date"]))


class NullThresholdRule(Detector):
    """Data contract: per agency, each monitored column's null share must stay below a fixed limit."""
    name, family, problem, algorithm = "null_contract_rule", "rule", "null_spike", "fixed max-null-share per agency/column"
    LIMIT = 0.25
    MIN_ROWS = 50

    def _detect(self, batch):
        flags, alerts = pd.Series(False, index=batch.index), []
        for agency, g in batch.groupby("agency"):
            if len(g) < self.MIN_ROWS:
                continue
            for col in NULL_SPIKE_COLUMNS:
                if col in g.columns and col != "location_type":
                    share = g[col].isna().mean()
                    if share > self.LIMIT:
                        flags.loc[g.index[g[col].isna()]] = True
                        alerts.append(Alert(self.problem, "column", f"{agency}: {share:.0%} of '{col}' is null",
                                            column=col, observed=round(float(share), 3), expected=f"<= {self.LIMIT:.0%}"))
        return self.result(row_flags=flags, alerts=alerts)


class ClosedBeforeCreatedRule(Detector):
    name, family, problem, algorithm = "date_order_rule", "rule", "closed_before_created", "closed_date >= created_date"

    def _detect(self, batch):
        mask = parse_dates(batch["closed_date"]) < parse_dates(batch["created_date"])
        return self.result(row_flags=mask, alerts=_row_alerts(self.problem, mask, batch, "closed_date",
                           "closed_date is earlier than created_date", "closed_date >= created_date",
                           ["created_date", "closed_date"]))


class ZipBoroughLookupRule(Detector):
    name, family, problem, algorithm = "zip_borough_lookup", "rule", "zip_borough_mismatch", "frozen ZIP->borough table"

    def fit(self, ref):
        self.map = ref.zip_borough
        return self

    def _detect(self, batch):
        if "incident_zip" not in batch.columns or "borough" not in batch.columns:
            return self.result(row_flags=pd.Series(False, index=batch.index), note="columns missing")
        expected = batch["incident_zip"].map(self.map)
        mask = (expected.notna() & (expected != batch["borough"])) | (batch["incident_zip"].notna() & expected.isna())
        alerts = []
        for i in mask[mask].index[:_MAX_ALERTS]:
            alerts.append(Alert(self.problem, "row", "ZIP code does not belong to the stated borough", row=int(i),
                                column="incident_zip", observed={"zip": batch.at[i, "incident_zip"],
                                                                 "borough": batch.at[i, "borough"]},
                                expected=expected.get(i) or "a known NYC ZIP"))
        return self.result(row_flags=mask, alerts=alerts)


class AllowedPairRule(Detector):
    name, family, problem, algorithm = "allowed_pairs_rule", "rule", "complaint_descriptor_mismatch", "frozen allowed (type, descriptor) pairs"

    def fit(self, ref):
        self.allowed = {k: set(v) for k, v in ref.allowed_pairs.items()}
        return self

    def _detect(self, batch):
        ok = [d in self.allowed.get(t, ()) for t, d in zip(batch["complaint_type"], batch["descriptor"])]
        mask = ~pd.Series(ok, index=batch.index) & batch["descriptor"].notna()
        return self.result(row_flags=mask, alerts=_row_alerts(self.problem, mask, batch, "descriptor",
                           "descriptor never seen with this complaint type in the reference period",
                           "a descriptor allowed for the complaint type", ["complaint_type", "descriptor"]))


CLOSED_CUES = re.compile(r"\b(?:closed|was closed|responded|took action|inspected|issued a summons|no evidence|"
                         r"were gone|not necessary|resolved|completed|corrected|fixed the condition|no violations?|"
                         r"violations were issued|determined that|unable to)\b", re.I)
OPEN_CUES = re.compile(r"\b(?:will be|has been (?:received|submitted|assigned|dispatched)|is being|in progress|"
                       r"pending|scheduled|will respond|awaiting|under review|will review)\b", re.I)


class StatusLexiconRule(Detector):
    """Keyword lexicon: a closure-only phrase with an open status (or open-only phrase with Closed)."""
    name, family, problem, algorithm = "status_lexicon_rule", "rule", "resolution_status_contradiction", "keyword lexicon vs status"

    def _detect(self, batch):
        text = batch["resolution_description"].fillna("")
        closed_txt = text.str.contains(CLOSED_CUES) & ~text.str.contains(OPEN_CUES)
        open_txt = text.str.contains(OPEN_CUES) & ~text.str.contains(CLOSED_CUES)
        is_closed = batch["status"] == "Closed"
        mask = (closed_txt & ~is_closed) | (open_txt & is_closed)
        return self.result(row_flags=mask, alerts=_row_alerts(self.problem, mask, batch, "status",
                           "resolution text describes a closed request but status is not Closed (or vice versa)",
                           "status consistent with resolution text", ["status", "resolution_description"]))


class PIIRegexRule(Detector):
    name, family, problem, algorithm = "pii_regex_rule", "rule", "pii_in_text", "regex: phone, e-mail, cue-word names"

    def _detect(self, batch):
        hits = batch["resolution_description"].map(regex_pii)
        mask = hits.map(bool)
        alerts = [Alert(self.problem, "row", "free text contains personal data: " + ", ".join(k for k, _ in hits[i]),
                        row=int(i), column="resolution_description", observed=[k for k, _ in hits[i]],
                        expected="no phone/e-mail/name in free text") for i in mask[mask].index[:_MAX_ALERTS]]
        return self.result(row_flags=mask, alerts=alerts)


RULE_DETECTORS = [SchemaContract, DtypeContract, DateFormatRule, BBoxSwapRule, ExactDuplicateRule, NearDuplicateRule,
                  NullThresholdRule, ClosedBeforeCreatedRule, ZipBoroughLookupRule, AllowedPairRule,
                  StatusLexiconRule, PIIRegexRule]
