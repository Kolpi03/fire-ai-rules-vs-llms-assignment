"""Exact full-year count of 2023 NYC 311 rows with a null latitude, fetched month by month.

The single year-wide SoQL count(*) timed out (HTTP 504 / read timeout) and later hit HTTP 503. Twelve disjoint
monthly counts are cheaper for the server; their sum is the exact full-year count (the month windows partition
2023 by created_date, the same filter as the year-wide query).

Resumable: months already fetched successfully are never re-requested or overwritten. Requests are sequential
with a pause between them. The full-year total is written ONLY when all 12 months succeeded; otherwise the
aggregate stays "unavailable" with the per-month status recorded. Nothing is estimated.

Usage: python scripts/fetch_missing_latitude.py [--pause 8] [--split-failed]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from project_b import data_loader  # noqa: E402

OUT = data_loader.PB_PROCESSED / "missing_latitude_monthly.json"


def month_where(m: int) -> str:
    a = f"2023-{m:02d}-01T00:00:00"
    b = "2024-01-01T00:00:00" if m == 12 else f"2023-{m + 1:02d}-01T00:00:00"
    return f"created_date >= '{a}' AND created_date < '{b}' AND latitude IS NULL"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pause", type=float, default=8.0)
    ap.add_argument("--timeout", type=int, default=150)
    ap.add_argument("--split-failed", action="store_true", help="retry previously failed months as two half-month queries")
    a = ap.parse_args()
    cfg = data_loader._cfg()
    url = f"{cfg['base_url']}/resource/{cfg['dataset_id']}.json"
    state = json.loads(OUT.read_text()) if OUT.exists() else {"months": {}}
    for m in range(1, 13):
        key = f"2023-{m:02d}"
        if state["months"].get(key, {}).get("status") == "ok":
            continue
        params = {"$select": "count(*) AS n", "$where": month_where(m)}
        attempts = state["months"].get(key, {}).get("attempts", 0) + 1
        try:
            if a.split_failed and attempts > 1:
                # smaller, disjoint half-month windows; their sum is still the exact monthly count
                nxt = "2024-01-01T00:00:00" if m == 12 else f"2023-{m + 1:02d}-01T00:00:00"
                halves = [(f"2023-{m:02d}-01T00:00:00", f"2023-{m:02d}-16T00:00:00"), (f"2023-{m:02d}-16T00:00:00", nxt)]
                parts = []
                for lo, hi in halves:
                    q = {"$select": "count(*) AS n",
                         "$where": f"created_date >= '{lo}' AND created_date < '{hi}' AND latitude IS NULL"}
                    body = data_loader._get(url, q, timeout=a.timeout, retries=2).json()
                    parts.append({"query": q, "raw_response": body, "count": int(body[0]["n"])})
                    time.sleep(a.pause)
                state["months"][key] = {"status": "ok", "count": sum(x["count"] for x in parts), "parts": parts,
                                        "method": "two disjoint half-month queries", "attempts": attempts,
                                        "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
            else:
                resp = data_loader._get(url, params, timeout=a.timeout, retries=2)
                body = resp.json()
                state["months"][key] = {"status": "ok", "count": int(body[0]["n"]), "raw_response": body,
                                        "query": params, "attempts": attempts,
                                        "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            state["months"][key] = {"status": "failed", "error": str(exc)[:300], "query": params, "attempts": attempts,
                                    "last_try": dt.datetime.now().isoformat(timespec="seconds")}
        print(key, state["months"][key]["status"], state["months"][key].get("count", ""), flush=True)
        OUT.write_text(json.dumps(state, indent=1))
        time.sleep(a.pause)
    ok = {k: v for k, v in state["months"].items() if v.get("status") == "ok"}
    state["months_ok"] = sorted(ok)
    state["months_failed"] = sorted(k for k, v in state["months"].items() if v.get("status") != "ok")
    complete = len(ok) == 12
    state["full_year_count"] = sum(v["count"] for v in ok.values()) if complete else None
    state["note"] = ("exact: sum of 12 disjoint monthly counts" if complete else
                     "incomplete: full-year count NOT computed; partial months only (not an estimate)")
    OUT.write_text(json.dumps(state, indent=1))
    agg = json.loads(data_loader.AGG_CACHE.read_text()) if data_loader.AGG_CACHE.exists() else {}
    if complete:
        agg["missing_latitude"] = {"status": "ok", "count": state["full_year_count"],
                                   "method": "sum of 12 monthly SoQL count(*) queries (see missing_latitude_monthly.json)",
                                   "query": data_loader.FULL_YEAR_QUERIES["missing_latitude"],
                                   "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
    else:
        agg.setdefault("missing_latitude", {"status": "unavailable"})
        agg["missing_latitude"]["partial_monthly"] = {k: v["count"] for k, v in sorted(ok.items())}
        agg["missing_latitude"]["months_failed"] = state["months_failed"]
    data_loader.AGG_CACHE.write_text(json.dumps(agg, indent=2))
    print(f"months ok {len(ok)}/12; full-year count: {state['full_year_count']}")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
