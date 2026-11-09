# Rules vs LLMs: take-home assignment (Fire AI)

Two systems, each built three ways, measured, and assembled into a recommended design:

* **Project A: Indian income-tax engine.** Salaried individuals, FY 2024-25, then the Finance Act 2025 change for
  FY 2025-26. Built as (A) hand-coded rules, (B) an LLM at query time, (C) LLM-compiled rules, plus a plain-language
  parser that asks instead of guessing.
* **Project B: NYC 311 data-quality layer.** Real-data profiling of 2023 requests, an injector for 12 corruption
  types, rule / statistical-ML / LLM detectors for each type, and a hybrid pipeline.

**Results:** [`reports/RESULTS_A.md`](reports/RESULTS_A.md), [`reports/RESULTS_B.md`](reports/RESULTS_B.md),
[`reports/DECISION_MATRIX.md`](reports/DECISION_MATRIX.md),
[`reports/FINAL_DESIGN_A.md`](reports/FINAL_DESIGN_A.md), [`reports/FINAL_DESIGN_B.md`](reports/FINAL_DESIGN_B.md),
[`reports/AI_USAGE.md`](reports/AI_USAGE.md),
[`reports/official_calculator_validation.md`](reports/official_calculator_validation.md),
[`FINAL_COMPLETION_TRACKER.md`](FINAL_COMPLETION_TRACKER.md). Every number in RESULTS_* and DECISION_MATRIX is
generated from `benchmarks/**.json` by `scripts/generate_reports.py`.

> **What was measured, and how.** All LLM results (Engine B, the Gemini field extractor, the 12 LLM detectors and
> stage 4 of the hybrid) come from **real Gemini calls** with `gemini-3.5-flash-lite` on the free tier
> (`gemini-2.5-flash` returned 404 "no longer available to new users"). Costs are measured tokens x list price; the
> actual spend was $0. Samples are small and stated next to every figure. Nothing is mocked into a results table.
> The **30 official-calculator cases** were entered on the Income Tax Department's public calculator: 30/30 match in
> both regimes (screenshots in `evidence/official_calculator/`).

## Results at a glance

| | Result | Source |
|---|---|---|
| Engine A (rules) / C (LLM-compiled) | 100% rupee-exact on 179 + 128 cases, deterministic, ~0.04 / 0.75 ms | `benchmarks/project_a/benchmark_results.json` |
| Engine B (LLM at query time) | 25% / 30% rupee-exact (40 cases/year), deterministic on 1/6 cases, median 3.9 s, ~$3.5 per 1k | same |
| Official calculator | 30/30 cases match in both regimes | `project_a/data/official_calculator_results.json` |
| Law change (FY 2025-26) | year-blind answers silently wrong on 101/128 cases; 8/9 rule and 4/4 compiled mutants caught | `benchmarks/project_a/migration_report.json` |
| Hybrid data-quality pipeline | row-level P 0.996 / R 0.988 / F1 0.992; ~16-17k rows/s on one thread | `benchmarks/project_b/hybrid_results.json` |
| LLM detectors / stage 4 | never beat the chosen detector beyond sampling error; 10^3.8-10^7.5x the cost; stage 4 vetoes lost 85 true detections vs 5 false alarms removed | `benchmarks/project_b/detector_results.json` |

## Architecture

```
common/            config + .env loading, JSON-lines logging, Gemini client (retry, pacing, cost, cache, no silent mock)
config/            settings.yaml, tax_rules_fy2024_25.yaml, tax_rules_fy2025_26.yaml, benchmark_config.yaml
project_a/
  models.py                  Pydantic input/output models (every line: clause + rule id + version)
  rule_registry.py           one rule set per FY, fail-loud lookup (no "latest" fallback)
  rules_engine.py            Engine A: deterministic computation (Decimal, s.288A/288B rounding)
  reference_oracle.py        independent Fraction-based implementation that produces expected test values
  rule_texts/                official passages per FY (+ _snapshots/ of the source pages with SHA-256)
  rule_dsl.py, rule_compiler.py, llm_compiled_engine.py
                             Engine C: LLM-compiled JSON artifact -> validate -> verify -> approve -> run (no LLM at runtime)
  llm_query_engine.py        Engine B: retrieve passages -> Gemini -> schema/arithmetic/citation/shadow checks
  natural_language_parser.py text -> profile or clarifying questions; nl_evaluation.py scores it
  tax_explainer.py, cli.py, benchmark.py, law_change_migration.py, test_case_generator.py, differential.py
  data/                      307 tax cases, 50 + 20 plain-language cases, 30-case official-calculator worksheet
project_b/
  data_loader.py             Socrata API download (day-cluster sample, cached, retries), typed Parquet
  profiler.py                Task B1 real-issue profile -> data/reports/profile.{json,csv,md}
  reference.py               clean pool, Jan-Sep reference vs Oct-Dec evaluation split, frozen learned reference
  corruption_injector.py     12 problem types, seeded, labels, original never mutated
  schema_detector.py         family A: Pandera contracts, regex, range, lookup, lexicon rules
  statistical_detector.py    family B (stats): profile matching, binomial tests, change point, tail tests
  ml_detector.py             family B (ML): x/y regression, rapidfuzz blocking, TF-IDF+LogReg, ZIP centroid distance
  llm_detector.py            family C: Gemini batch-summary / row / pair review
  pii_detector.py            regex vs libphonenumber + vocabulary novelty + frequency allowlist
  evaluation.py, benchmark.py, hybrid_pipeline.py, cli.py
benchmarks/        machine-readable results (JSON/CSV) for both projects
reports/           Markdown reports (generated + hand-written design documents)
scripts/           download_data.py, generate_tax_tests.py, run_benchmarks.py, generate_reports.py, validate_submission.py,
                   official_calculator_compare.py, fetch_missing_latitude.py
evidence/          official_calculator/: raw portal capture + 60 screenshots (30 cases x 2 regimes)
run_all.py         one command reproduces everything
```

## Technology
Python 3.10+ (tested on 3.13.9). pandas, numpy, pyarrow, pydantic v2, PyYAML, scikit-learn, pandera, rapidfuzz,
phonenumbers, google-genai, requests, pytest. Presidio was considered for PII and not adopted, because it needs a
large spaCy model and the measured results did not require it.

## Install and environment
```bash
git clone https://github.com/Kolpi03/fire-ai-rules-vs-llms-assignment.git && cd fire-ai-rules-vs-llms-assignment
python -m venv .venv && source .venv/bin/activate     # optional
pip install -r requirements.txt
cp .env.example .env    # optional: add GEMINI_API_KEY (free tier) and GEMINI_MODEL; never commit .env
```
**External APIs.** Gemini (optional; `GEMINI_API_KEY`) and NYC Open Data / Socrata (only for a fresh download;
optional `NYC_APP_TOKEN`). Without a key everything deterministic still runs. The stored real LLM measurements in
`benchmarks/` are **reused** by default (a rerun never replaces a real measurement with NOT MEASURED, and never spends
quota silently); set `LLM_REMEASURE=1` to make new calls.

## Run everything (reproduces every reported number)
```bash
python run_all.py --skip-download # offline: cached/committed data, ~3 min; the command used for the reported numbers
python run_all.py                 # also re-downloads missing NYC day files (~30-60 min the first time)
python run_all.py --skip-download --resume       # continue after the last step recorded "ok"
python run_all.py --skip-download --from-step 10 # rerun steps 10-13 only
```
Each step is checkpointed to `logs/run_all.checkpoint.json`; the exit code goes to `logs/run_all.exitcode`.
Steps: environment check -> tax-source snapshots -> test-case generation -> Project A tests -> Project A benchmark ->
Task A5 migration report -> NYC data -> profile -> Project B tests -> detector benchmark -> hybrid -> reports ->
validation. A summary goes to `benchmarks/submission_summary.json`. A failed external step (network, API) is
reported and the offline steps continue.

## Project A
```bash
python -m project_a.cli ask "I earn 15 lakh annually, pay 25k rent in Bangalore, invest 1.5 lakh in 80C and have medical insurance."
python -m project_a.cli ask "I'm 29, salary 18 LPA, no rent, 80C 1.5 lakh, NPS 50k" -v      # computes + audit trail
python -m project_a.cli calc --json '{"financial_year":"2025-26","age":30,"gross_salary":1275000}' --engine all
python -m project_a.cli benchmark
python -m project_a.cli migration-report
python -m project_a.cli compile run --fy 2025-26        # Gemini recompilation (needs key) -> candidate
python -m project_a.cli compile verify --candidate project_a/compiled_rules/candidates/<file>.json --fy 2025-26
python -m project_a.cli compile approve --candidate <file> --fy 2025-26 --reviewer "Your Name"
make project-a
```
**Official calculator:** the 30 worksheet cases were entered on
https://eportal.incometax.gov.in/iec/foservices/#/TaxCalc/calculator (Advance Calculator, AY 2025-26, both regimes).
Raw portal figures: `evidence/official_calculator/portal_capture_raw.json`; one screenshot per case and regime;
`python scripts/official_calculator_compare.py` recomputes the comparison offline (also run by `generate_reports.py`).
The portal takes net salary and one 80D amount, so it checks everything except our HRA and 80D sub-limit logic.
`generate_tax_tests.py` preserves filled rows.

## Project B
```bash
python -m project_b.cli download      # day-cluster sample of 2023 (cached; optional NYC_APP_TOKEN raises rate limits)
python -m project_b.cli profile       # real issues -> project_b/data/reports/profile.md
python -m project_b.cli inject --problem pii_in_text --rate 0.05 --seed 1 --out /tmp/pii.parquet
python -m project_b.cli benchmark     # 12 problems x 3 families -> benchmarks/project_b/detector_results.{json,csv}
python -m project_b.cli hybrid        # end-to-end hybrid + variants -> benchmarks/project_b/hybrid_results.json
python -m project_b.cli check /tmp/pii.parquet   # run the hybrid pipeline on any batch
make project-b
```

## Tests
```bash
python -m pytest                      # both projects (pyproject sets testpaths)
make test-a ; make test-b
python scripts/validate_submission.py # structural/honesty checks: sizes, 36 results, limits, secrets, TODOs
```

## Reproducibility notes
* Seeds: `config/settings.yaml` (`seed`), `config/benchmark_config.yaml`; injector seeds are derived per batch and stored
  in `data/corrupted/<problem>/meta_*.json`.
* NYC data: the processed sample snapshot `project_b/data/processed/nyc311_2023_sample.parquet` is committed, because
  NYC can edit old records. Raw API CSVs are cached locally under `project_b/data/raw/` and not committed.
* Latency/throughput are single-thread wall-clock measurements on the machine that ran them. Expect variation.

## Known limitations
* LLM results use one small model (`gemini-3.5-flash-lite`) on small samples (40 tax cases/year, 60 rows per
  detector); a stronger model may score higher on the semantic checks. The Engine C artifacts were compiled by
  Claude in the dev session instead of Gemini, and they still need the submitter's human sign-off (see AI_USAGE.md).
* The official calculator confirms 30 FY 2024-25 cases. It cannot check HRA or 80D sub-limits (entered as amounts)
  and there is no FY 2025-26 comparison; those rest on the independent oracle, hand-worked cases and differential tests.
* NYC full-year `missing_latitude` count: exact only if all 12 monthly API queries succeed
  (`scripts/fetch_missing_latitude.py`, resumable); otherwise recorded as unavailable, never estimated.
* Tax scope: salary only. No 80CCD(2), professional tax, let-out property, or 24(b) Rs 30,000 case.
* Parser: rule-based. The held-out first run was 17/20 with 2 unsafe outputs (since fixed). Unseen phrasings will
  cause more over-asking.
* Project B clean batches are filtered real data, so their false-alarm rates are lower than on raw data. Raw-data
  alert volumes are in the profile. The sample is a day-cluster sample (37 days), not the full 3.2M rows.

## Data sources
* Income Tax Department of India: incometaxindia.gov.in (s.87A, s.80CCD, s.80D, computation of tax, deductions
  allowable, HRA FAQ, brief on Budget 2025) and incometax.gov.in (regime FAQ, official calculator). URLs, fetch
  dates and hashes: `project_a/rule_texts/_snapshots/manifest.json`, `project_a/tax_sources.py`.
* NYC Open Data, 311 Service Requests from 2020 to Present (`erm2-nwe9`), created in 2023.
* Gemini pricing: ai.google.dev/gemini-api/docs/pricing (checked 2026-11-08) and aistudio.google.com/docs/pricing
  (3.x models, checked 2026-11-09).

## Design decisions
* **Numbers come from rules.** LLMs draft or check rules offline and handle language at the edges. See
  FINAL_DESIGN_A.md and DECISION_MATRIX.md.
* **Fail loudly on the law's version.** One file per financial year, explicit year on every request, no fallback.
* **Tests before the change.** FY 2025-26 expectations existed before any engine was migrated. Mutation tests show
  the suite catches realistic migration slips.
* **Data quality: the cheapest check that measurably works, per problem.** The hybrid picks winners from the
  measured table, not from intuition. Measured LLM review did not earn its cost, so it is off in the shipped pipeline.
