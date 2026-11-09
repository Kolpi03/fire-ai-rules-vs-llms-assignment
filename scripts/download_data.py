"""Download (or reuse cached) NYC 311 2023 sample + full-year aggregate counts.

Usage: python scripts/download_data.py [--force] [--skip-aggregates]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_b import data_loader  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    ap.add_argument("--skip-aggregates", action="store_true")
    args = ap.parse_args()
    print("Downloading NYC 311 2023 day-cluster sample ...")
    data_loader.download_sample(force=args.force)
    path = data_loader.build_processed(force=True)
    print(f"Processed sample: {path}")
    if not args.skip_aggregates:
        print("Fetching full-year aggregate counts (best effort) ...")
        res = data_loader.fetch_full_year_aggregates(force=args.force)
        for k, v in res.items():
            print(f"  {k}: {v.get('count', v.get('status'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
