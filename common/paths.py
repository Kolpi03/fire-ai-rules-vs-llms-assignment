"""Repository-relative paths. Nothing in the codebase hardcodes an absolute path."""
from __future__ import annotations

from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
LOG_DIR = ROOT / "logs"
REPORTS_DIR = ROOT / "reports"
BENCH_A = ROOT / "benchmarks" / "project_a"
BENCH_B = ROOT / "benchmarks" / "project_b"

PA_DIR = ROOT / "project_a"
PA_DATA = PA_DIR / "data"
PA_RULE_TEXTS = PA_DIR / "rule_texts"
PA_COMPILED = PA_DIR / "compiled_rules"

PB_DIR = ROOT / "project_b"
PB_DATA = PB_DIR / "data"
PB_RAW = PB_DATA / "raw"
PB_PROCESSED = PB_DATA / "processed"
PB_CORRUPTED = PB_DATA / "corrupted"
PB_REPORTS = PB_DATA / "reports"


def ensure_dirs() -> None:
    """Create every output directory used by the pipelines (idempotent)."""
    for d in (LOG_DIR, REPORTS_DIR, BENCH_A, BENCH_B, PA_DATA, PA_COMPILED,
              PB_RAW, PB_PROCESSED, PB_CORRUPTED, PB_REPORTS):
        d.mkdir(parents=True, exist_ok=True)
