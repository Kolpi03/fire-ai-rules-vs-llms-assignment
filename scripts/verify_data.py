"""Verify the downloaded NYC 311 day chunks and the temporal split; write a committed manifest.

Checks per expected sampled day: file present, row count, created_date range (all rows must fall on that day),
SHA-256 of the raw file. Across chunks: no duplicate unique_key, no unexpected extra files. Split: the months
present in the reference pool and the evaluation pool must be exactly the configured ones.

Exit code 0 only if every check passes. Output: project_b/data/processed/raw_manifest.json
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import settings  # noqa: E402
from common.paths import PB_PROCESSED, PB_RAW  # noqa: E402
from project_b.data_loader import sampled_days  # noqa: E402


def main() -> int:
    check_split = "--no-split" not in sys.argv
    problems: list[str] = []
    chunks = []
    keys: dict[str, str] = {}
    dup_keys = 0
    expected = sampled_days()
    for day in expected:
        f = PB_RAW / f"day_{day.isoformat()}.csv"
        if not f.exists():
            problems.append(f"missing chunk {f.name}")
            chunks.append({"day": day.isoformat(), "status": "MISSING"})
            continue
        df = pd.read_csv(f, dtype=str, keep_default_na=False, na_values=[""])
        created = pd.to_datetime(df["created_date"], format="%Y-%m-%dT%H:%M:%S.%f", errors="coerce")
        off_day = int((created.dt.date != day).sum())
        if off_day:
            problems.append(f"{f.name}: {off_day} rows not created on {day}")
        if len(df) == 0:
            problems.append(f"{f.name}: empty")
        for k in df["unique_key"]:
            if k in keys:
                dup_keys += 1
            keys[k] = f.name
        chunks.append({"day": day.isoformat(), "file": f.name, "rows": len(df),
                       "created_min": str(created.min()), "created_max": str(created.max()),
                       "sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "status": "ok"})
    extra = sorted({p.name for p in PB_RAW.glob("day_*.csv")} - {f"day_{d.isoformat()}.csv" for d in expected})
    if extra:
        problems.append(f"unexpected extra chunks: {extra}")
    if dup_keys:
        problems.append(f"{dup_keys} unique_keys appear in more than one chunk")

    split: dict = {}
    cfg = settings()["project_b"]
    for name, path, want in (("reference", PB_PROCESSED / "clean_reference_pool.parquet", cfg["reference_months"]),
                             ("evaluation", PB_PROCESSED / "clean_eval_pool.parquet", cfg["evaluation_months"])):
        if not check_split or not path.exists():
            split[name] = "not checked (pools not rebuilt yet)" if path.exists() else "not built yet"
            continue
        months = sorted(pd.to_datetime(pd.read_parquet(path, columns=["created_date"])["created_date"].str[:10]).dt.month.unique().tolist())
        rows = len(pd.read_parquet(path, columns=["unique_key"]))
        split[name] = {"months_present": months, "configured": want, "rows": rows}
        if set(months) != set(want):
            problems.append(f"{name} pool months {months} != configured {want}")
    from project_b.data_loader import SAMPLE_PARQUET
    snapshot = None
    if SAMPLE_PARQUET.exists():
        snapshot = {"file": SAMPLE_PARQUET.name, "rows": len(pd.read_parquet(SAMPLE_PARQUET, columns=["unique_key"])),
                    "sha256": hashlib.sha256(SAMPLE_PARQUET.read_bytes()).hexdigest()}
        total = sum(c.get("rows", 0) for c in chunks)
        if snapshot["rows"] != total:
            problems.append(f"processed snapshot has {snapshot['rows']} rows but raw chunks have {total}")
    manifest = {"processed_snapshot": snapshot, "expected_chunks": len(expected), "present": sum(c["status"] == "ok" for c in chunks),
                "total_rows": sum(c.get("rows", 0) for c in chunks), "duplicate_unique_keys_across_chunks": dup_keys,
                "split": split, "chunks": chunks, "problems": problems}
    PB_PROCESSED.mkdir(parents=True, exist_ok=True)
    (PB_PROCESSED / "raw_manifest.json").write_text(json.dumps(manifest, indent=1))
    for c in chunks:
        print(f"{c['day']}  {c['status']:<7} rows={c.get('rows', '-'):>6}  {c.get('created_min', '')[:19]} .. "
              f"{c.get('created_max', '')[:19]}  {c.get('sha256', '')[:12]}")
    print(json.dumps({k: v for k, v in manifest.items() if k != "chunks"}, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
