# AI usage note

## Tools used and for what

| Tool | Used for | Not used for |
|---|---|---|
| **Claude Code (Claude Opus 5.5, Anthropic)** — coding agent in the terminal | Reading the assignment PDF; finding and snapshotting the official Income Tax Department pages; writing all Python code, YAML rule sets, tests, the 50 + 20 plain-language cases, the hand-worked tax cases, report generator and design documents; running every test and benchmark; acting as the **one-off rule compiler** for Engine C (see below). | It did not produce any number in the reports by hand: every metric is generated from JSON written by the benchmarks. |
| **Gemini (Google), `gemini-3.5-flash-lite`, free tier** | The system under test: Engine B (query-time LLM, 140 calls), the raw field extractor (50 calls), the 12 LLM detectors and the hybrid's stage 4. Integrated via `common/llm_client.py` (timeouts, retry/backoff, pacing, JSON-shape validation, token/cost logging, response cache). | Engine C recompilation was not re-run with Gemini (see below). `gemini-2.5-flash` returned 404 for new keys and `gemini-3.8-flash` timed out, hence the model choice. |
| **Playwright** (driven by Claude Code) | Recording the silent demo video of the real command-line application in a browser terminal (`scripts/demo/`). | No UI or result was invented: the page streams the real program output. |
| **Claude in Chrome** (browser extension, driven by Claude Code) | Entering the 30 worksheet cases on the official Income Tax Department calculator in the user's own Chrome (public page, no login, no CAPTCHA), reading the result lines and saving one screenshot per case and regime. | It did not compute anything: every official figure is what the portal displayed (`evidence/official_calculator/`). |
| Web search / fetch | Locating official incometaxindia.gov.in / incometax.gov.in pages and the Gemini pricing page. | No third-party tax calculator or blog was used as a legal source. |
| Python libraries | pandas, scikit-learn, pandera, rapidfuzz, phonenumbers, pydantic | — |

### Engine C was compiled by an LLM, but not by Gemini
The assignment asks for "an LLM converts the rule text into executable rules once; you review them". Because no
Gemini key was available, the compile prompts (`project_a/compiled_rules/compile_prompt_fy2024_25.txt`,
`..._fy2025_26.txt`) were answered by Claude in the development session. The artifacts record this
(`compiler.mode = "llm_authored_without_gemini"`). They then went through the same automated gate a Gemini output
would: whitelist-parsed DSL, static validation, the full test suite of that year and a 20,000-profile differential run
against Engine A. **Human review is still pending**: the `review.reviewer` field says so, and the submitter
should read both artifacts (about 40 steps each) before signing off.

**Gemini compile, run once a key was available (2026-11-09):** `compile run` with `gemini-3.5-flash-lite` produced
candidates for both years (`project_a/compiled_rules/candidates/fy20*_20261109_*.json`). They pass only **116/179**
(FY 2024-25) and **90/128** (FY 2025-26) test cases in both regimes; most failures are surcharge / marginal-relief
cases. The approval gate refuses them, so the approved artifacts remain the Claude-compiled ones. Evidence:
`benchmarks/project_a/gemini_compile_verification.json`. This is the design working as intended: the LLM output is
never trusted without the suite.

## Which parts were AI-assisted
Effectively all code and text was AI-generated and then executed and checked against independent evidence:
an independent oracle, hand-worked arithmetic, official text snapshots, mutation tests, held-out NL cases
and real-data inspection. The submitter is responsible for understanding and defending every line.

## Where the AI was wrong, and how it was caught (all of these happened during this build)

1. **Wrong expected tax in a regression test.** While writing `test_regression.py`, the AI asserted that a
   Rs 12.75 lakh salary owes Rs 71,500 under the FY 2024-25 new regime. The correct figure is Rs 83,200
   (TI 12,00,000 -> 20,000 + 30,000 + 30,000 = 80,000 + 4% cess). It had reused the number from a different
   hand-worked case (HW-09, TI 11.25 lakh). **Caught by** the test failing against two independent implementations
   (Engine A and the Fraction-based oracle), which both gave 83,200. **Fixed** the test, not the engine.
2. **Claimed "verbatim" legal text that was not on the cited page.** The first rule-text corpus labelled the
   80C/80CCD(1B) passages *verbatim* from the "Deductions allowable" page. A grep of the saved snapshot showed the
   Rs 50,000 80CCD(1B) limit is not on that page. **Fixed** by fetching the s.80CCD section text (new source
   `ITD-80CCD`) and quoting it, and by marking paraphrased passages as such.
3. **A docstring claimed an experiment that had not been run.** `ml_detector.py` first said Isolation Forest "was
   evaluated during development and rejected". It had not been. **Caught** during self-review before any report was
   generated. **Fixed** by implementing and benchmarking it (it misses swapped coordinates; see RESULTS_B).
4. **The first parser was unsafe.** On its first run over the 50 hand-written cases the plain-language parser
   produced 7 wrong profiles (e.g. "80C" was hidden by the code that masks section numbers, and "60" in "parents above
   60" was added to a premium). On the 20 held-out cases written afterwards it still produced 2 wrong 80D
   self/parent attributions. **Caught by** the expected outputs, which were written before running the parser.
   **Fixed** by asking instead of guessing; the held-out first-run figure (17/20, 2 unsafe) is what RESULTS_A reports.
5. **Benchmark ground truth was corrupted by the injector.** The first mixed-batch evaluation lost the
   exact-duplicate labels: labels were re-keyed by `unique_key`, which duplicates share. It also injected
   closed-before-created *after* re-formatting dates, which silently produced NaT. **Caught by** implausible metrics
   (precision = recall = 0.498 for a trivial duplicate check). **Fixed** by carrying labels as hidden columns and
   ordering the injections.
6. **Retrieval bug in Engine B.** Regime tags (`new_regime`) acted as retrieval triggers, so the surcharge passage was
   sent for every request. **Caught by** a unit test (`test_retrieval_is_profile_specific`).
7. **False alarms mistaken for detector quality.** The PII regex flagged about 4% of clean rows. Inspection showed
   these are agency phone numbers and e-mails inside official templates (for example one DOHMH number in 121 of
   5,000 rows). The statistical detector now allow-lists contacts that recur across many unrelated requests. The
   regex rule was deliberately left naive and is reported as the weak baseline it is.
8. **A report labelled a split that had not been used** (found in the recovery audit, 2026-11-10). The earlier
   RESULTS_B said "fit on Jan-Sep, evaluated on Oct-Dec", but the benchmark JSON it was generated from recorded
   reference months 1-5 and evaluation months 6-7 (an older config). The label was a hard-coded string in
   `generate_reports.py`. **Caught by** comparing the report against the `config` block of `detector_results.json`.
   **Fixed** by rendering the months from the recorded config, adding a validator check that fails unless the results
   use Jan-Sep / Oct-Dec, and re-running the benchmark on the 37-day sample.
9. **An interrupted pipeline left stale and fresh artifacts side by side.** A laptop shutdown stopped `run_all.py` at
   the detector benchmark, after the pools and corrupted batches had been rewritten but before scoring. Nothing
   recorded which outputs were current. **Fixed** by adding a per-step checkpoint (`logs/run_all.checkpoint.json`),
   `--resume` / `--from-step`, and an exit-code file. The resume run started at step 10 and did not re-download data.

10. **Code that only ever ran against a mock broke on the real model** (2026-11-09). With a real key, two LLM
    detectors crashed: the model sometimes returned a bare JSON list instead of `{"flagged": [...]}`, and the
    null-spike reviewer answers per batch while the evaluator scored it per row. **Caught by** the first real run
    failing. **Fixed** by validating the response shape (retry on anything unexpected; nothing guessed) and scoring
    batch-style reviews per batch on all 8 + 8 batches instead of 1 + 1.
11. **A portal reader that skipped decimals.** The script reading the official calculator matched only whole-rupee
    values, so for one case it skipped `Rs 19096995.25` and picked up a later "0". **Caught by** checking the
    suspicious 0 against a screenshot. **Fixed** the pattern, re-read all 60 results, and compared on the portal's
    s.288B-rounded *Balance Tax Payable* line.
12. **A hand-typed example in a report was wrong.** A draft of the calculator report said our pre-rounding tax for
    HW-03 was 64,422.40 and that all differences were "< Rs 1". The data say 64,423.84 and a maximum of Rs 1.00.
    **Fixed** by generating the example and the maxima from the comparison JSON.
13. **Detector selection depended on timing noise.** Ties in F1 were broken by measured cost, which for
    sub-microsecond detectors is noise, so the selected `closed_before_created` detector changed between runs.
    **Fixed** by breaking ties on family (rule, then statistical, then ML) before cost.
14. **The stage-4 LLM veto looked sensible and was harmful.** Measured on real flags, it removed 5 false alarms and 85
    true detections. It is now off by default; the design documents say so.
15. **A "reproducibility" run quietly spent the API quota.** With a key present, the Task A5 migration probe called
    Engine B once per test case (307 calls) inside `run_all.py`, and exhausted the free-tier daily quota (HTTP 429)
    half-way. **Caught by** the step taking minutes instead of seconds. The run was stopped before it could overwrite
    the stored report. **Fixed**: no pipeline step makes new LLM calls unless `LLM_REMEASURE=1` (or
    `MIGRATION_LLM_PROBE=1`); stored real measurements are reused and cited with their date.

16. **Section 80D medical expenditure was judged per group, not per person** (found in the human review of the
    compiled rules, 2026-11-09). All three implementations and the oracle allowed a senior's medical bills only if
    no premium was paid for *anyone* in the group, although the first proviso says "such person". Because the oracle
    shared the same reading, every test agreed with the bug. **Caught by** a person reading the compiled rules against
    the statute. **Fixed** with per-person inputs (validated: ambiguous or double-counted inputs are rejected), new
    hand-derived tests, 6 mutation tests and 80,000 random profiles; the corrected compiled rules were approved by
    the submitter (Kolpi03) after review.
    The same review then flagged that the Rs 50,000 limit of s.80D(4) was also raised by *any* senior in the group,
    including an uninsured one; it now requires an insured senior (5 more mutation tests, 48 more cases).

## What I would not trust the AI with without more evidence
* FY 2025-26 against the official calculator (only FY 2024-25 was compared), and HRA / 80D sub-limit logic, which the portal takes as entered amounts.
* Engine B and the LLM detectors beyond the small samples measured here (40 tax cases/year, 60 rows per detector).
* Legal interpretation choices (80D allocation, 24(b) Rs 30,000 case excluded, residency defaults): documented in
  RESULTS_A section 7, but they should be reviewed by someone qualified in Indian tax.
