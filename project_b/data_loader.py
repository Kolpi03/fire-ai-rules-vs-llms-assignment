"""NYC 311 data acquisition (official NYC Open Data Socrata API) with on-disk caching.

Sampling design
---------------
The 2023 slice of dataset ``erm2-nwe9`` has ~3.22 million rows (count verified via the API).
Downloading it fully takes ~40 minutes at the throughput we measured (~1.4k rows/s),
and server-side ``LIKE`` filters for a hash sample time out (full scans). We therefore take a
**day-cluster sample**: every ``day_step``-th calendar day of 2023 (all rows of each sampled
day). Because 10 is not a multiple of 7, the sampled days rotate through every weekday and
cover every month. Only ``created_date`` is filtered, which the API serves efficiently.

The raw API response (CSV, all values as text) is cached untouched under ``data/raw`` — the
original data is never modified. A typed, concatenated Parquet copy goes to
``data/processed``. Re-running never re-downloads a day that is already cached.

Full-year exact counts for a few profiling checks are fetched with SoQL aggregate queries
(:func:`fetch_full_year_aggregates`). Those are best-effort: the endpoint is slow and
sometimes returns 503/timeouts, so failures are recorded as ``unavailable``, never guessed.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from common.config import env, settings
from common.logging_utils import get_logger, log_event
from common.paths import PB_PROCESSED, PB_RAW

log = get_logger("project_b.data_loader")

NUMERIC_COLUMNS = ["unique_key", "x_coordinate_state_plane", "y_coordinate_state_plane",
                   "latitude", "longitude"]
DATE_COLUMNS = ["created_date", "closed_date", "due_date", "resolution_action_updated_date"]
SAMPLE_PARQUET = PB_PROCESSED / "nyc311_2023_sample.parquet"


def _cfg() -> dict[str, Any]:
    return settings()["nyc311"]


def sampled_days(year: int | None = None) -> list[dt.date]:
    cfg = _cfg()
    year = year or cfg["year"]
    start = dt.date(year, 1, 1)
    n_days = (dt.date(year + 1, 1, 1) - start).days
    return [start + dt.timedelta(days=d) for d in range(cfg["day_offset"] - 1, n_days, cfg["day_step"])]


def _headers() -> dict[str, str]:
    headers = {"User-Agent": "rules-vs-llms-assignment/1.0 (data-quality research)"}
    token = env("NYC_APP_TOKEN")  # optional Socrata app token raises rate limits
    if token:
        headers["X-App-Token"] = token
    return headers


def _get(url: str, params: dict[str, Any], timeout: int, retries: int = 5) -> requests.Response:
    """GET with exponential backoff on 429/5xx/timeouts."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=_headers(), timeout=timeout)
            if resp.status_code == 200:
                return resp
            last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
        except requests.RequestException as exc:  # timeout / connection error
            last_err = exc
        wait = 2 ** attempt * 3
        log_event(log, "retrying request", url=url, attempt=attempt + 1, wait_s=wait, error=str(last_err))
        time.sleep(wait)
    raise RuntimeError(f"request failed after {retries} attempts: {last_err}")


def download_day(day: dt.date, force: bool = False) -> Path:
    """Download every 2023 row created on ``day`` into ``data/raw/day_YYYY-MM-DD.csv``."""
    cfg = _cfg()
    out = PB_RAW / f"day_{day.isoformat()}.csv"
    if out.exists() and not force:
        return out
    url = f"{cfg['base_url']}/resource/{cfg['dataset_id']}.csv"
    nxt = day + dt.timedelta(days=1)
    frames: list[str] = []
    offset = 0
    while True:
        params = {
            "$select": ",".join(cfg["columns"]),
            "$where": f"created_date >= '{day.isoformat()}T00:00:00' AND created_date < '{nxt.isoformat()}T00:00:00'",
            "$order": "unique_key",
            "$limit": cfg["page_size"],
            "$offset": offset,
        }
        text = _get(url, params, cfg["request_timeout_seconds"]).text
        n_rows = max(text.count("\n") - 1, 0)  # approximate (free text can contain newlines)
        frames.append(text if offset == 0 else text.split("\n", 1)[1])
        if n_rows < cfg["page_size"]:
            break
        offset += cfg["page_size"]
    tmp = out.with_suffix(".part")
    tmp.write_text("".join(frames), encoding="utf-8")
    tmp.replace(out)
    log_event(log, "downloaded day", day=day.isoformat(), bytes=out.stat().st_size)
    return out


def download_sample(force: bool = False) -> list[Path]:
    """Download (or reuse cached) raw CSVs for every sampled day."""
    PB_RAW.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    days = sampled_days()
    failed: list[dt.date] = []
    for attempt_round in range(3):  # a throttled day is retried later instead of aborting the run
        todo = days if attempt_round == 0 else failed
        failed = []
        for i, day in enumerate(todo, 1):
            cached = (PB_RAW / f"day_{day.isoformat()}.csv").exists()
            try:
                p = download_day(day, force=force and attempt_round == 0)
            except RuntimeError as exc:
                failed.append(day)
                print(f"  [{i}/{len(todo)}] {day} FAILED ({exc}); will retry", flush=True)
                continue
            paths.append(p)
            print(f"  [{i}/{len(todo)}] {day} {'cached' if cached and not force else 'downloaded'} "
                  f"({p.stat().st_size / 1e6:.1f} MB)", flush=True)
        if not failed:
            break
        time.sleep(60)
    if failed:
        log_event(log, "days not downloaded", days=[d.isoformat() for d in failed])
        print(f"WARNING: {len(failed)} sampled days could not be downloaded: {failed}. "
              "The sample proceeds without them (recorded in the profile report).")
    return paths


def _read_raw(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])


def build_processed(force: bool = False) -> Path:
    """Concatenate raw day files into one typed Parquet file (raw files stay untouched)."""
    if SAMPLE_PARQUET.exists() and not force:
        return SAMPLE_PARQUET
    raw_files = sorted(PB_RAW.glob("day_*.csv"))
    if not raw_files:
        raise FileNotFoundError("No raw NYC 311 files found. Run `python scripts/download_data.py` first.")
    df = pd.concat([_read_raw(p) for p in raw_files], ignore_index=True)
    df = type_frame(df)
    PB_PROCESSED.mkdir(parents=True, exist_ok=True)
    df.to_parquet(SAMPLE_PARQUET, index=False)
    log_event(log, "built processed sample", rows=len(df), files=len(raw_files))
    return SAMPLE_PARQUET


def type_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the canonical dtypes: numbers as float/int, dates as ISO strings kept verbatim.

    Dates are deliberately kept as *strings* in the source format
    (``2023-03-01T01:24:20.000``) because the data-quality layer must see what the source
    actually sent: a date-format switch is only detectable on the raw representation.
    """
    out = df.copy()
    for c in NUMERIC_COLUMNS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "unique_key" in out.columns:
        out["unique_key"] = out["unique_key"].astype("Int64")
    for c in out.columns:
        if c not in NUMERIC_COLUMNS:
            out[c] = out[c].astype("string")
    return out


def load_sample() -> pd.DataFrame:
    """Load the processed sample, downloading/building it if needed."""
    if not SAMPLE_PARQUET.exists():
        if not list(PB_RAW.glob("day_*.csv")):
            download_sample()
        build_processed()
    return pd.read_parquet(SAMPLE_PARQUET)


# --------------------------------------------------------------------------------------
# Full-year server-side aggregates (exact population counts, best effort)
# --------------------------------------------------------------------------------------
YEAR_WHERE = "created_date >= '2023-01-01T00:00:00' AND created_date < '2024-01-01T00:00:00'"

FULL_YEAR_QUERIES: dict[str, dict[str, str]] = {
    "total_rows": {"$select": "count(*) AS n", "$where": YEAR_WHERE},
    "closed_before_created": {"$select": "count(*) AS n", "$where": f"{YEAR_WHERE} AND closed_date < created_date"},
    "status_closed_without_closed_date": {"$select": "count(*) AS n",
                                          "$where": f"{YEAR_WHERE} AND status = 'Closed' AND closed_date IS NULL"},
    "missing_incident_zip": {"$select": "count(*) AS n", "$where": f"{YEAR_WHERE} AND incident_zip IS NULL"},
    "missing_latitude": {"$select": "count(*) AS n", "$where": f"{YEAR_WHERE} AND latitude IS NULL"},
    "borough_unspecified": {"$select": "count(*) AS n", "$where": f"{YEAR_WHERE} AND borough = 'Unspecified'"},
}

AGG_CACHE = PB_PROCESSED / "full_year_aggregates.json"


def fetch_full_year_aggregates(force: bool = False, timeout: int = 150, offline: bool = False) -> dict[str, Any]:
    """Run each aggregate query once; cache results; record failures as unavailable.

    ``offline=True`` makes no network call: cached counts are returned and anything never fetched
    successfully keeps (or gets) status "unavailable" — counts are never invented.
    """
    cfg = _cfg()
    cache: dict[str, Any] = json.loads(AGG_CACHE.read_text()) if AGG_CACHE.exists() else {}
    if offline:
        for name, params in FULL_YEAR_QUERIES.items():
            if cache.get(name, {}).get("status") != "ok":
                cache.setdefault(name, {"status": "unavailable", "error": "offline run (--skip-download)",
                                        "query": params})
        return cache
    url = f"{cfg['base_url']}/resource/{cfg['dataset_id']}.json"
    for name, params in FULL_YEAR_QUERIES.items():
        if not force and cache.get(name, {}).get("status") == "ok":
            continue
        try:
            resp = _get(url, params, timeout=timeout, retries=2)
            n = int(resp.json()[0]["n"])
            cache[name] = {"status": "ok", "count": n, "query": params,
                           "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            cache[name] = {"status": "unavailable", "error": str(exc)[:300], "query": params}
        AGG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        AGG_CACHE.write_text(json.dumps(cache, indent=2))
        time.sleep(2)
    return cache
