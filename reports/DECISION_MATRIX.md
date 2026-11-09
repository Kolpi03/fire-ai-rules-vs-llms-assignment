# DECISION MATRIX

_Generated from the benchmark JSON. LLM figures are real Gemini calls where a key was available (model and sample sizes in RESULTS_A/RESULTS_B); 'not measured' = no key in the run that produced them._

## Project A

| Component | Problem | Selected approach | Class | Measured justification | Cost | Latency | Accuracy | Auditability |
|---|---|---|---|---|---|---|---|---|
| Tax computation (both regimes) | rupee-exact tax + recommendation | Engine A: hand-coded rules over versioned YAML | Rule | 100% rupee-exact on 293 + 242 cases, 10/10 deterministic; Engine B: real calls (gemini-3.5-flash-lite, n=40/year): 25.0% / 30.0% rupee-exact, deterministic on 1/3 and 0/3 cases, median 3,903.7 ms | $5.79e-07/1k | median 0.040 ms | 100% | every line: clause + rule id + version |
| Independent verifier | catch a wrong ground truth | LLM-compiled artifact (Engine C) run as a shadow of A | Hybrid (LLM at build time, rule at run time) | 0 mismatches vs A on every suite case it answers and on 5,000 random FY25-26 profiles (0 mismatches); a second, independently derived encoding | $1.36e-05/1k | median 0.881 ms | 100% | step id + clause + passage + artifact hash |
| Query-time LLM (Engine B) | compute per request | Not used for numbers | LLM | real calls (gemini-3.5-flash-lite, n=40/year): 25.0% / 30.0% rupee-exact, deterministic on 1/3 and 0/3 cases, median 3,903.7 ms; must be shadow-checked by A anyway | $3.50/1k (measured tokens) | median 3,903.7 ms | 25.0% | citations only, cannot prove arithmetic |
| Rule storage | law changes yearly | one YAML per FY + fail-loud registry | Rule | year-blind fallback would be silently wrong on 84% of FY25-26 cases; registry raised errors instead | — | — | 242/242 after migration | git diff of YAML |
| Plain-language extraction | text -> profile | deterministic extractor + validation (LLM extractor optional) | Rule (Hybrid-ready) | dev 50/50 in-sample; held-out first run 17/20 with 2 unsafe -> fixed to 0 unsafe; raw Gemini extractor (gemini-3.5-flash-lite): 24/29 complete cases right, flagged only 12/21 cases that need a question (guessed on 9) | ~0 | <10 ms | see RESULTS_A s.4 | evidence span + status per field |
| Clarification policy | missing/ambiguous info | rule-based ask-don't-guess (period, CTC, EMI, parents' age, conflicts) | Rule | 0 unsafe outputs on 70 descriptions after fixes | ~0 | ~0 | — | question + reason logged |
| Explanation text | why this tax | deterministic template over computation lines | Rule | cannot introduce numbers; 100% traceable | ~0 | ~0 | exact | clause per line |
| Law-change ingestion | new Finance Act | LLM drafts compiled diff from official text; human review + test gate | Hybrid | compiled migration: 2 reviewed diff items, all FY25-26 cases at the time; 4/4 seeded compile mistakes caught (3 by tests, 1 by validation) | one LLM call per year | — | gate blocks mismatches | prompt + artifact hash + reviewer |
| Source verification | official text changes / conflicts | snapshot + SHA-256 per source page | Rule | found 2 contradictions on official pages | ~0 | — | — | hash manifest |

## Project B

| Check | Problem | Selected approach | Class | Measured justification (F1 / clean FA) | Cost $/M rows | Latency ms/5k | Accuracy | Auditability |
|---|---|---|---|---|---|---|---|---|
| schema_drift | schema drift | pandera_column_contract (Pandera required-column contract) | Rule | F1 1.000, FA 0.000%; vs column_profile_matcher F1 1.000/FA 0.000%; LLM F1 1.000 on a sample at $0.10/M | $4.24e-08 | 0.0 | P 1.000 R 1.000 | structured alert, rubric 5.00/5 |
| type_change | type change | pandera_dtype_contract (Pandera column dtype check) | Rule | F1 1.000, FA 0.000%; vs type_share_test F1 1.000/FA 0.000%; LLM F1 0.857 on a sample at $0.10/M | $1.78e-05 | 6.4 | P 1.000 R 1.000 | structured alert, rubric 5.00/5 |
| date_format_switch | date format switch | iso8601_regex (regex: ISO-8601 with millis) | Rule | F1 1.000, FA 0.000%; vs date_shape_changepoint F1 1.000/FA 0.000%; LLM F1 1.000 on a sample at $70.00/M | $6.40e-06 | 2.3 | P 1.000 R 1.000 | structured alert, rubric 5.00/5 |
| lat_lon_swap | lat lon swap | nyc_bbox_rule (range check: NYC bounding box) | Rule | F1 1.000, FA 0.000%; vs xy_latlon_regression F1 1.000/FA 0.000%; LLM F1 1.000 on a sample at $62.88/M | $1.97e-06 | 0.7 | P 1.000 R 1.000 | structured alert, rubric 5.00/5 |
| exact_duplicates | exact duplicates | exact_duplicate_rule (pandas duplicated(all columns)) | Rule | F1 1.000, FA 0.000%; vs duplicate_rate_ztest F1 1.000/FA 0.000%; LLM F1 0 on a sample at $30.82/M | $6.04e-05 | 22.1 | P 1.000 R 1.000 | structured alert, rubric 5.00/5 |
| near_duplicates | near duplicates | rapidfuzz_blocking (blocking + rapidfuzz similarity) | ML | F1 0.923, FA 0.005%; vs blocking_key_rule F1 0.706/FA 0.000%; LLM F1 0.947 on a sample at $2.79/M | $4.81e-04 | 175.6 | P 0.994 R 0.862 | structured alert, rubric 5.00/5 |
| null_spike | null spike | null_rate_binomial (binomial z-test per agency/column) | Statistical | F1 1.000, FA 0.000%; vs null_contract_rule F1 0.773/FA 2.040%; LLM F1 0.667 on a sample at $0.13/M | $1.46e-05 | 5.3 | P 1.000 R 1.000 | structured alert, rubric 5.00/5 |
| closed_before_created | closed before created | date_order_rule (closed_date >= created_date) | Rule | F1 1.000, FA 0.000%; vs duration_tail_test F1 1.000/FA 0.000%; LLM F1 1.000 on a sample at $52.08/M | $6.00e-06 | 2.2 | P 1.000 R 1.000 | structured alert, rubric 5.00/5 |
| zip_borough_mismatch | zip borough mismatch | zip_borough_lookup (frozen ZIP->borough table) | Rule | F1 1.000, FA 0.000%; vs zip_centroid_distance F1 0.993/FA 0.010%; LLM F1 1.000 on a sample at $46.58/M | $5.32e-06 | 1.9 | P 1.000 R 1.000 | structured alert, rubric 5.00/5 |
| complaint_descriptor_mismatch | complaint descriptor mismatch | tfidf_logreg_descriptor (TF-IDF char n-grams + LogisticRegression) | ML | F1 0.987, FA 0.013%; vs allowed_pairs_rule F1 0.958/FA 0.128%; LLM F1 0.667 on a sample at $30.23/M | $3.61e-05 | 13.0 | P 0.989 R 0.986 | structured alert, rubric 5.00/5 |
| resolution_status_contradiction | resolution status contradiction | tfidf_logreg_status (TF-IDF word n-grams + LogisticRegression) | ML | F1 0.890, FA 0.015%; vs status_lexicon_rule F1 0.516/FA 1.738%; LLM F1 0.400 on a sample at $26.83/M | $2.88e-05 | 10.4 | P 0.991 R 0.808 | structured alert, rubric 5.00/5 |
| pii_in_text | pii in text | phonenumbers_oov_names (libphonenumber + OOV proper-noun bigrams) | Statistical | F1 0.982, FA 0.057%; vs pii_regex_rule F1 0.254/FA 3.837%; LLM F1 1.000 on a sample at $46.13/M | $1.64e-04 | 59.2 | P 0.965 R 1.000 | structured alert, rubric 5.00/5 |
| Hybrid pipeline | all 12 | schema gate -> rules -> stats -> ML -> (LLM on borderline) | Hybrid | end-to-end any-problem F1 0.992, clean FA 0.083% | $8.32e-04 | 300 | P 0.996 R 0.988 | per-stage alerts |
| LLM review (stage 4) | confirm/veto semantic flags | **Off by default**; LLM kept for offline schema mapping only | LLM (not shipped online) | real calls on 360 flagged rows: removed 5 false alarms but 85 true detections | $0.030 for the run | 2,400 ms/call | made recall worse | prompt + response logged |
