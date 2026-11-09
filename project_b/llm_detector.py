"""Family C — LLM review (Gemini), per row on samples or per batch on summaries.

Review modes (chosen per problem to give the LLM a fair chance at the lowest sensible cost):
* ``batch_summary`` — schema_drift, type_change, null_spike: ONE call per batch with the header, dtypes,
  three example rows and (for null spikes) the per-agency null-rate table.
* ``rows``          — date format, coordinates, closed/created, ZIP/borough, complaint/descriptor,
  resolution/status, PII, exact duplicates: rows are sent in chunks of ``CHUNK`` with only the columns the
  question needs; the model returns the ids of rows that have the problem plus a reason.
* ``pairs``         — near_duplicates: candidate pairs from cheap blocking (same complaint type + ZIP within
  60 min) are sent; the model judges whether each pair is the same incident.

Honesty rules: without GEMINI_API_KEY every detector returns ``available=False`` and NO flags; the
evaluation marks those cells "not measured". Cost per million rows is still *projected* from the exact
prompts these detectors would send (chars/4 token estimate), and labelled as a projection.
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from common.llm_client import LLMResponseError, LLMUnavailable, cost_usd, estimate_tokens, get_client, model_name
from project_b.detection_types import Alert, DetectionResult, Detector, PROBLEMS
from project_b.reference import NULL_SPIKE_COLUMNS, parse_dates

CHUNK = 20
SYSTEM = ("You are a meticulous data-quality reviewer for New York City 311 service-request data. "
          "Answer ONLY with JSON. Flag a row only if you are confident it has the specific problem asked about.")

ROW_SPECS: dict[str, tuple[list[str], str]] = {
    "date_format_switch": (["created_date", "closed_date"],
                           "Which rows have created_date/closed_date in a DIFFERENT format from the majority format "
                           "of this feed (ISO 8601 'YYYY-MM-DDThh:mm:ss.000')?"),
    "lat_lon_swap": (["latitude", "longitude", "borough", "incident_zip"],
                     "Which rows have latitude and longitude swapped or implausible for New York City?"),
    "exact_duplicates": (["unique_key", "created_date", "complaint_type", "descriptor", "incident_address", "status"],
                         "Which rows are exact duplicates of another row in this list (same values in every field)? "
                         "Flag only the later copy."),
    "closed_before_created": (["created_date", "closed_date", "status"],
                              "Which rows have a closed_date earlier than the created_date?"),
    "zip_borough_mismatch": (["incident_zip", "borough", "city"],
                             "Which rows have a ZIP code that is NOT in the stated NYC borough?"),
    "complaint_descriptor_mismatch": (["complaint_type", "descriptor", "agency"],
                                      "Which rows have a descriptor that does not fit the complaint type?"),
    "resolution_status_contradiction": (["status", "resolution_description"],
                                        "Which rows have a resolution description that contradicts the status "
                                        "(e.g. text says the request was handled/closed but status is open)?"),
    "pii_in_text": (["resolution_description"],
                    "Which rows contain personal data (a person's name, phone number or e-mail) in the free text?"),
}
BATCH_PROBLEMS = ("schema_drift", "type_change", "null_spike")


def _shape(key: str):
    """Validator for generate_json: accept {key: [...]} or a bare list (some models return the list itself).
    Anything else raises LLMResponseError so the client retries; nothing is guessed."""
    def check(parsed: Any) -> dict[str, Any]:
        if isinstance(parsed, list):
            return {key: parsed}
        if isinstance(parsed, dict) and isinstance(parsed.get(key, []), list):
            return parsed
        raise LLMResponseError(f"unexpected response shape for '{key}': {str(parsed)[:120]}")
    return check


def _batch_shape(parsed: Any) -> dict[str, Any]:
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        parsed = parsed[0]
    if isinstance(parsed, dict) and "problem_found" in parsed:
        return parsed
    raise LLMResponseError(f"unexpected batch response shape: {str(parsed)[:120]}")


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rows_payload(df: pd.DataFrame, cols: list[str]) -> str:
    present = [c for c in cols if c in df.columns]
    recs = [{"row": int(i), **{c: (None if pd.isna(v) else (v if isinstance(v, (int, float, str)) else str(v)))
                              for c, v in r.items()}} for i, r in df[present].iterrows()]
    return json.dumps(recs, default=str)


def row_prompt(problem: str, chunk: pd.DataFrame) -> str:
    cols, question = ROW_SPECS[problem]
    return (f"{question}\nRows:\n{_rows_payload(chunk, cols)}\n"
            'Return {"flagged": [{"row": <row id>, "reason": "<short reason>"}]} (empty list if none).')


def batch_prompt(problem: str, batch: pd.DataFrame, expected_columns: list[str]) -> str:
    head = json.dumps({c: str(batch[c].dtype) for c in batch.columns})
    sample = batch.head(3).to_json(orient="records", default_handler=str)
    if problem == "null_spike":
        table = batch.groupby("agency")[[c for c in NULL_SPIKE_COLUMNS if c in batch.columns]].agg(
            lambda s: round(float(s.isna().mean()), 3))
        table["rows"] = batch.groupby("agency").size()
        extra = f"Per-agency null share:\n{table.to_csv()}\nIs there an abnormal null spike for one agency? "
    elif problem == "schema_drift":
        extra = f"Expected columns: {expected_columns}\nAre any expected columns missing or renamed? "
    else:
        extra = ("Expected numeric (float) columns: latitude, longitude, x_coordinate_state_plane, "
                 "y_coordinate_state_plane. Has any column's type changed (e.g. numbers as strings)? ")
    return (f"Batch of {len(batch)} NYC 311 rows. Column dtypes: {head}\nFirst rows: {sample}\n{extra}\n"
            'Return {"problem_found": true|false, "column": "<column or agency/column>", "reason": "<short reason>"}.')


def near_dup_candidates(batch: pd.DataFrame, window_s: int = 3600) -> list[tuple[int, int]]:
    c = parse_dates(batch["created_date"])
    d = pd.DataFrame({"t": c, "ct": batch["complaint_type"], "zip": batch["incident_zip"]}).sort_values("t")
    pairs = []
    for _, g in d.groupby(["ct", "zip"]):
        idx, ts = list(g.index), list(g["t"])
        for i in range(1, len(idx)):
            for j in range(i - 1, -1, -1):
                if (ts[i] - ts[j]).total_seconds() > window_s:
                    break
                pairs.append((idx[j], idx[i]))
    return pairs


def pair_prompt(batch: pd.DataFrame, pairs: list[tuple[int, int]]) -> str:
    cols = ["unique_key", "created_date", "complaint_type", "descriptor", "incident_address", "incident_zip"]
    items = [{"pair": k, "a": batch.loc[a, cols].astype(str).to_dict(), "b": batch.loc[b, cols].astype(str).to_dict()}
             for k, (a, b) in enumerate(pairs)]
    return ("Each pair below is two 311 requests with the same complaint type and ZIP filed within an hour. Which pairs "
            "are the SAME request entered twice (slightly altered time or text), not two genuinely separate reports?\n"
            f"{json.dumps(items)}\nReturn {{\"duplicates\": [<pair numbers>]}}.")


class LLMReviewDetector(Detector):
    family = "llm"

    def __init__(self, problem: str, expected_columns: list[str], client: Any | None = None):
        self.problem = problem
        self.name = f"gemini_review_{problem}"
        self.algorithm = ("LLM batch-summary review" if problem in BATCH_PROBLEMS else
                          "LLM candidate-pair review" if problem == "near_duplicates" else "LLM row review")
        self.expected_columns = expected_columns
        self._client = client
        self._client_error: str | None = None

    def client(self):
        if self._client is None and self._client_error is None:
            try:
                self._client = get_client()
            except LLMUnavailable as exc:
                self._client_error = str(exc)
        return self._client

    # ---------------- cost projection (no API call) ----------------
    def projected_tokens(self, batch: pd.DataFrame, rows: pd.Index | None = None) -> tuple[int, int, int]:
        """(calls, input_tokens, output_tokens) the detector WOULD use on ``rows`` of this batch."""
        if self.problem in BATCH_PROBLEMS:
            p = batch_prompt(self.problem, batch, self.expected_columns)
            return 1, estimate_tokens(SYSTEM + p), 60
        if self.problem == "near_duplicates":
            pairs = near_dup_candidates(batch)
            calls = max(1, -(-len(pairs) // CHUNK))
            toks = sum(estimate_tokens(SYSTEM + pair_prompt(batch, pairs[i:i + CHUNK])) for i in range(0, len(pairs), CHUNK))
            return calls, toks, calls * 40
        idx = batch.index if rows is None else rows
        calls, toks = 0, 0
        for i in range(0, len(idx), CHUNK):
            calls += 1
            toks += estimate_tokens(SYSTEM + row_prompt(self.problem, batch.loc[idx[i:i + CHUNK]]))
        return calls, toks, calls * 120

    # ---------------- real review ----------------
    def _detect(self, batch: pd.DataFrame, rows: pd.Index | None = None) -> DetectionResult:
        cl = self.client()
        if cl is None:
            return self.result(mode="llm-unavailable", available=False, row_flags=None,
                               note=f"not measured: {self._client_error}")
        res = self.result(mode="llm-real", row_flags=pd.Series(pd.NA, index=batch.index, dtype="boolean"))
        try:
            if self.problem in BATCH_PROBLEMS:
                r = cl.generate_json(batch_prompt(self.problem, batch, self.expected_columns), system=SYSTEM,
                                     validator=_batch_shape)
                self._account(res, r)
                found = bool(r.parsed.get("problem_found"))
                res.batch_flag = found
                res.row_flags = None
                if found:
                    res.alerts.append(Alert(self.problem, "batch", str(r.parsed.get("reason", ""))[:300],
                                            column=r.parsed.get("column"), observed="LLM judgement", expected=None))
                return res
            if self.problem == "near_duplicates":
                pairs = near_dup_candidates(batch)
                res.row_flags[:] = False
                for i in range(0, len(pairs), CHUNK):
                    chunk = pairs[i:i + CHUNK]
                    r = cl.generate_json(pair_prompt(batch, chunk), system=SYSTEM, validator=_shape("duplicates"))
                    self._account(res, r)
                    for k in r.parsed.get("duplicates", []):
                        k = _as_int(k)
                        if k is not None and 0 <= k < len(chunk):
                            res.row_flags.at[chunk[k][1]] = True
                            res.alerts.append(Alert(self.problem, "row", "LLM: same request entered twice",
                                                    row=int(chunk[k][1]), observed=int(chunk[k][0])))
                res.rows_reviewed = len(batch)
                return res
            idx = batch.index if rows is None else rows
            res.row_flags.loc[idx] = False
            for i in range(0, len(idx), CHUNK):
                chunk = batch.loc[idx[i:i + CHUNK]]
                r = cl.generate_json(row_prompt(self.problem, chunk), system=SYSTEM, validator=_shape("flagged"))
                self._account(res, r)
                for f in r.parsed.get("flagged", []):
                    rid = _as_int(f.get("row") if isinstance(f, dict) else f)
                    if rid is not None and rid in chunk.index:
                        res.row_flags.at[rid] = True
                        res.alerts.append(Alert(self.problem, "row", str(f.get("reason", "LLM flagged"))[:300]
                                                if isinstance(f, dict) else "LLM flagged", row=int(rid),
                                                observed=chunk.loc[rid].to_dict(), expected=None))
            res.rows_reviewed = len(idx)
        except LLMResponseError as exc:
            res.note = f"LLM error: {exc}"[:300]
        return res

    def detect(self, batch: pd.DataFrame, rows: pd.Index | None = None) -> DetectionResult:  # type: ignore[override]
        import time
        t0 = time.perf_counter()
        res = self._detect(batch, rows)
        res.seconds = time.perf_counter() - t0
        return res

    @staticmethod
    def _account(res: DetectionResult, r: Any) -> None:
        res.llm_calls += 1
        res.llm_input_tokens += r.input_tokens
        res.llm_output_tokens += r.output_tokens
        res.llm_latency_ms += float(getattr(r, "latency_ms", 0.0) or 0.0)
        res.llm_cost_usd += r.cost_usd if r.mode == "real" else cost_usd(model_name(), r.input_tokens, r.output_tokens)
        res.mode = "llm-real" if r.mode == "real" else f"llm-{r.mode}"


def build_family_c(expected_columns: list[str], client: Any | None = None) -> dict[str, LLMReviewDetector]:
    return {p: LLMReviewDetector(p, expected_columns, client) for p in PROBLEMS}
