# Final design B — data-quality layer at 50M rows/day, 120 customers (max 2 pages)

Labels: **[M]** measured in this repo (`benchmarks/project_b/*.json`, laptop: AMD Ryzen 5 7640HS, single thread);
**[P]** projection computed from [M] + [A] by `project_b/scale_estimates.py`; **[A]** assumption.

**Decision in one line:** rules and statistics check every row and every batch. ML handles only the four problems where
a model measurably beat the rule. The LLM is not in the online path: measured with real calls, it never beat the
selected detector by more than sampling error, cost 10^3.8-10^7.5 times more per row, and as a veto it destroyed more
true detections than false alarms. Its job is offline: mapping a new customer's schema.

## Architecture (what each stage does, and why it is there)

| Stage | Label | Runs on | Detectors (chosen from the 36-way benchmark, not hand-picked) | Evidence |
|---|---|---|---|---|
| 0 Schema gate | Rule | each batch | Pandera column contract; a batch with a missing or renamed column is quarantined | schema_drift F1 1.00, 0 false alarms [M] |
| 1 Row rules | Rule | every row | ISO-8601 regex, NYC bounding box, closed ≥ created, frozen ZIP→borough table | F1 1.00 each, 0.000% clean-row FA [M] |
| 2 Batch statistics | Statistical | each batch | type-share test, duplicate-rate z-test, per-agency null-rate binomial test | F1 1.00; the fixed-threshold null rule scored F1 0.77 with 2.04% FA [M] |
| 3 ML | ML | every row | rapidfuzz near-dup, TF-IDF+LogReg descriptor and status, phonenumbers+OOV-name PII | F1 0.92 / 0.99 / 0.89 / 0.98, against 0.71 / 0.96 / 0.52 / 0.25 for the best rule [M] |
| 4 LLM review | LLM | **off** | confirm/veto stage-3 semantic flags | Real calls, 360 flagged rows: removed 5 false alarms but **85 true detections**; resolution/status recall 0.82 → 0.60 [M] |

Winners are selected automatically (`select_winners`): best F1 with clean-row FA ≤ 0.1%; ties go to the simpler family. Fit on
Jan–Sep 2023 (203,918 clean rows); scored on held-out Oct–Dec batches (8 corrupted per problem + 8 clean, 5,000 rows) [M].

## End-to-end evidence (hybrid on 6 mixed batches with all 10 row-level problems injected at once) [M]

* Any-problem row level: precision **0.996**, recall **0.988**, F1 **0.992** (TP 7,770 · FP 31 · FN 95 · TN 22,704).
* Weakest: near-duplicates (recall 0.79), resolution/status contradiction (0.82). The LLM reviewer was tested exactly there and made both worse.
* Against single-family pipelines, row-level clean false-alarm rate: rules-only **7.9%** (F1 0.80), stat/ML-only 0.125%
  (F1 0.990), hybrid **0.12%** (F1 0.990, macro-F1 0.971 against 0.936).
* Throughput **≈16-17k rows/s** on one thread (15,884 and 17,025 in two runs); ≈0.3 s per 5,000-row batch. Time
  share: ML 68%, batch statistics 22-29%, rules 3-10%. Compute ≈ **$0.0008-0.0009 per million rows** [A price].
* LLM detectors (gemini-3.5-flash-lite, real calls, 60-row samples): **$2.8-70 per million rows per check** [M]; F1
  below the winner on 5 problems (e.g. resolution/status 0.40 vs 0.89, exact duplicates 0.00), tied on 5, higher on
  near-duplicates and PII within sampling error.

## 50M rows/day across 120 customers

| Quantity | Value | Label |
|---|---|---|
| Average / peak ingest | 579 / 2,315 rows/s | [P]; peak-to-average 4× [A] |
| CPU for detection | ≈8,800-9,400 core-s/day; **≈0.4 cores at peak** | [P]; 3× overhead for parsing, IO and serialisation [A] |
| Detection compute cost | **≈ $0.12-0.13/day** (≈ $4/month for all tenants) | [P] |
| Stage-4 LLM if re-enabled | 5,000 rows/day → ≈ $0.45/day; ≈ $0.001/day with a text cache | [P]; 10% of semantic flags reviewed [A]; only 657 unique (status, text) pairs in 328,529 rows [M] |
| LLM on every row, every check | ≈ **$10,940/day** | [P] |
| Raw ingest / Parquet storage | 25.3 GB/day CSV / 3.2 GB/day | [P] from bytes per row [M] |
| Latency added per batch | ~0.3 s per 5k rows per core; scales out by partition | [M] per batch, [P] at scale |

Compute is not the cost driver; data movement, alert volume and per-tenant setup are.

## What changes at this scale, and why

1. **Alert volume becomes the bottleneck, not CPU.** The hybrid raised at least one alert in **100% of clean
   batches** [M]: a 0.083% clean-row false-alarm rate × 5,000 rows is about 4 rows per batch. At 50M rows/day that is
   about **41,700 false row alerts per day** [P]. Change: page on issues (a problem type in a tenant crossing a rate
   threshold), never on rows. Control the false-discovery rate across 120 tenants × columns × batches. Keep row
   flags as an audit table that people query.
2. **Schemas differ per customer.** Each tenant gets a versioned *contract*: its column names mapped onto canonical
   semantic types (timestamp, geo point, postal code, category, free text, status). Detectors bind to semantic types,
   not to NYC column names. The LLM's best use is **offline**: it proposes the mapping once per schema
   version; a human approves; stage 0 enforces it.
3. **References and models become per tenant and versioned.** Frozen lookup tables (ZIP→borough, allowed
   type/descriptor pairs) and the fitted statistics are NYC-specific; nothing transfers to other tenants for free. New
   tenants run stages 0–2 only until they have reference history; references refit on a rolling window with drift
   checks so slow corruption cannot become "normal".
4. **Near-duplicates need state across batches:** a per-tenant blocking-key store holding a 60-minute window.
5. **Execution model.** Stateless workers partitioned by tenant behind a queue, ~5,000-row micro-batches; the ML
   stage (68% of CPU) gets its own autoscaled pool so one noisy tenant cannot starve others.
6. **LLM placement.** Schema mapping at onboarding and plain-English summaries of aggregated issues, both reviewed by a
   person. Stage 4 stays off: re-enable it per tenant only if a re-measurement on that tenant's labelled flags shows it
   removes more false alarms than true detections (`measure_stage4`).

## Multi-tenancy

* Per-tenant contract, reference store, thresholds and LLM budget; every flag records the detector, reference and contract versions.
* Shared engine code, isolated data; PII masking runs before any text reaches an LLM, which receives only needed columns.
* Onboarding: contract → 2-week shadow mode (learn reference, tune thresholds to the FA budget) → enforcement.

## Limitations (stated, not hidden)

* LLM results use one small model (gemini-3.5-flash-lite; 2.5-flash was unavailable to new keys) on small samples; a stronger model may do better on the semantic checks.
* Corruption is synthetic, injected into *filtered* clean pools; real-data alert volumes are the profile counts in RESULTS_B §2.
* Throughput is single-thread laptop Python; the 3× overhead and 4× peak factors are assumptions.
* Only one customer (NYC 311) was measured; cross-schema claims are reasoning, not evidence.
* Full-year counts come from the NYC API where it answered; `missing_latitude` is exact only if all 12 monthly queries succeed, otherwise unavailable (never estimated).
