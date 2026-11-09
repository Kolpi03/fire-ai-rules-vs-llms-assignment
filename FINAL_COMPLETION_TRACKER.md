# Final completion tracker

Every requirement of *Rules vs LLMs Take-Home Assignment* (Fire AI), its status, and the evidence. Statuses:
COMPLETE · INCOMPLETE · BLOCKED — EXTERNAL DEPENDENCY · FAILED · NOT APPLICABLE. Updated 2026-11-09.

## Project A — income-tax engine

| # | Requirement | Status | Evidence | Remaining action |
|---|---|---|---|---|
| A1a | ≥ 200 test cases with expected tax, both regimes, edge cases at every threshold | COMPLETE | `project_a/data/tax_test_cases.json` (307 cases: 179 FY 2024-25, 128 FY 2025-26); RESULTS_A §3 | — |
| A1b | ≥ 30 cases checked against the official calculator | COMPLETE | 30/30 match in both regimes: `reports/official_calculator_validation.md`, `project_a/data/official_calculator_results.json`, 60 screenshots + raw capture in `evidence/official_calculator/` | Portal cannot test HRA / 80D sub-limits (entered as amounts) — documented |
| A1c | 50 plain-language descriptions incl. Hinglish, incomplete, contradictory | COMPLETE | `project_a/data/natural_language_cases.json` (+20 held-out) | — |
| A2a | Hand-coded rules | COMPLETE | `project_a/rules_engine.py` + `config/tax_rules_fy*.yaml`; 100% rupee-exact | — |
| A2b | LLM at query time | COMPLETE | `project_a/llm_query_engine.py`; measured with real Gemini calls (gemini-3.5-flash-lite): 25% / 30% rupee-exact on 40 cases/year | Small sample (±14 pts) — stated |
| A2c | LLM-compiled rules, reviewed, run without LLM | COMPLETE (human sign-off pending) | `project_a/compiled_rules/fy*.compiled.json` (compiled by Claude, test-gated, 100% exact). Gemini recompile: 116/179 and 90/128 cases, blocked by the gate (`benchmarks/project_a/gemini_compile_verification.json`) | Submitter should read and sign off the artifacts (AI_USAGE.md) |
| A3 | Plain language -> structured input; ask instead of guess | COMPLETE | `natural_language_parser.py`: 50/50 dev, held-out first run 17/20 (2 unsafe, fixed). Raw Gemini extractor measured: 24/29 right, guessed on 9/21 (`benchmarks/project_a/llm_extractor_eval.json`) | — |
| A4 | Accuracy, 10x determinism, latency, cost/1k, clause traceability per engine | COMPLETE | `benchmarks/project_a/benchmark_results.json`; RESULTS_A §1 | — |
| A5 | FY 2025-26 change applied to all three; time, breakage, silent failures | COMPLETE | `benchmarks/project_a/migration_report.json`, `migration_log.json`; 101/128 silent if year-blind; 8/9 + 4/4 mutants caught | — |
| A6 | Final design ≤ 2 pages, components labelled, justified by measurements | COMPLETE | `reports/FINAL_DESIGN_A.md` (1144 words ≤ 1,150) | — |
| A-del | RESULTS_A.md: comparison table, change log, final design | COMPLETE | `reports/RESULTS_A.md` (generated) | — |

## Project B — data-quality layer

| # | Requirement | Status | Evidence | Remaining action |
|---|---|---|---|---|
| B0 | NYC 311, calendar 2023 | COMPLETE | 37/37 day files verified, 328,529 rows (`project_b/data/processed/raw_manifest.json`); reference Jan–Sep (203,918 clean rows), evaluation Oct–Dec (68,790); split verified by validator | — |
| B1 | Profile raw data: real problems with counts and examples | COMPLETE | `project_b/data/reports/profile.{md,json}`; RESULTS_B §2. Full-year API counts: 5 of 6 exact | `missing_latitude`: 11/12 months exact, July unavailable (NYC API HTTP 503) — **BLOCKED — EXTERNAL DEPENDENCY** for the one supplementary count; rerun `python scripts/fetch_missing_latitude.py --split-failed` when the API recovers |
| B2 | Injector for all 12 problem types at a controlled rate | COMPLETE | `project_b/corruption_injector.py`; tests in `project_b/tests/test_injector.py` | — |
| B3 | Rules, statistical/ML and LLM detectors for every problem | COMPLETE | 36 measured results in `benchmarks/project_b/detector_results.json` (12 LLM rows from real calls) | — |
| B4 | Detection rate, clean false-alarm rate, cost/M rows, throughput, explanation | COMPLETE | `benchmarks/project_b/detector_results.{json,csv}`; RESULTS_B §3 | LLM rows are small samples — stated |
| B5 | Hybrid pipeline, catches and costs end to end | COMPLETE | `benchmarks/project_b/hybrid_results.json`: P 0.996 / R 0.988 / F1 0.992; stage 4 LLM measured and disabled (85 true detections lost vs 5 false alarms removed) | — |
| B6 | Scale design ≤ 2 pages: 50M rows/day, 120 customers, different schemas | COMPLETE | `reports/FINAL_DESIGN_B.md` (1149 words ≤ 1,150), `benchmarks/project_b/scale_estimates.json` | — |
| B-del | RESULTS_B.md: per-problem table, real issues, scale design | COMPLETE | `reports/RESULTS_B.md` (generated) | — |

## Cross-cutting deliverables and rules

| # | Requirement | Status | Evidence | Remaining action |
|---|---|---|---|---|
| D1 | DECISION_MATRIX.md, each choice tied to a measurement | COMPLETE | `reports/DECISION_MATRIX.md` (generated) | — |
| D2 | AI_USAGE.md: tools, uses, where the AI was wrong | COMPLETE | `reports/AI_USAGE.md` (15 caught errors) | — |
| D3 | One command reproduces every number | COMPLETE | `python run_all.py --skip-download`: 13/13 steps ok, exit code 0 (`logs/run_all.exitcode`), 0 new LLM calls, 51/51 counts and hashes identical to the previous run (timings vary) | — |
| D4 | Python 3.10+, clean structure, tests | COMPLETE | Python 3.13.9; 681 tests pass (612 Project A + 69 Project B; integration: hybrid pipeline, run_all; regression: `test_regression.py`; mutation tests in the A5 report) | — |
| D5 | LLM budget: Gemini free tier | COMPLETE | All calls on the free tier ($0 spent); list-price costs reported | — |
| D6 | Official sources only | COMPLETE | `project_a/rule_texts/_snapshots/manifest.json` (SHA-256 per page) | — |
| D7 | Private GitHub repo | FINAL_GIT_STATUS | FINAL_GIT_EVIDENCE | Share with the hiring team (GitHub → Settings → Collaborators) |
| D8 | Secrets never committed | COMPLETE | `.env` git-ignored; scan of every file for the actual key and common token patterns: 0 hits | — |
| D9 | Validator | COMPLETE | `scripts/validate_submission.py`: 85/85 checks pass | — |
