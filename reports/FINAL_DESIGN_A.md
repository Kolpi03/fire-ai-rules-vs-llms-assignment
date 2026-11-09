# Final design A — income-tax service for 100,000 users (max 2 pages)

**Decision in one line:** rules compute every rupee. An LLM is used *offline* to draft and cross-check rules when the
law changes, and optionally *at the edges* (understanding messy text, rephrasing). It is never on the path that
produces a tax number.

## Architecture

| # | Component | Label | What it does | Evidence (RESULTS_A) |
|---|---|---|---|---|
| 1 | Text understanding | **Rule** (LLM optional, shadow only) | Regex/lexicon extractor -> fields with evidence and status (stated / zero / unknown / conflict). | 0 unsafe outputs on 70 descriptions after fixes; the held-out *first* run had 2 unsafe of 20, so this stays guarded by step 2. A raw Gemini extractor got 24/29 complete cases right and guessed instead of asking on 9/21: not shipped. |
| 2 | Clarification + validation | **Rule** | Pydantic schema plus ask-don't-guess rules (period, CTC vs gross, EMI vs interest, parents' age, conflicting values, implausible amounts). A profile exists only when nothing is pending. | The assignment example asks 4 correct questions; no profile is produced for any clarify case. |
| 3 | Tax engine | **Rule** | Engine A: Python over one versioned YAML per financial year; Decimal arithmetic; s.288A/288B rounding. | 100% rupee-exact on 293 + 242 cases; 10/10 deterministic; median 0.036 ms; about $5e-7 per 1,000 requests; 100% of lines cite clause + rule id + version. |
| 4 | Shadow verifier | **Hybrid** (LLM at build time, rule at run time) | Engine C, the LLM-compiled artifact, runs on every request; any disagreement withholds the answer and pages the owner. | 0 mismatches vs A on every case it answers and on random profiles; ~0.8 ms median. Inputs it does not read make it refuse, not guess. |
| 5 | Explanation | **Rule** (LLM rephrase optional) | Deterministic template over the computation lines; an optional LLM rewrite must reproduce every number exactly (regex check) or is discarded. | Cannot invent numbers; every line traceable. |
| 6 | Law-change pipeline | **Hybrid** | Official-source snapshots with hashes -> tests for the new year first -> human edits YAML (A) + LLM drafts artifact (C) -> gates -> human sign-off -> activate by effective date. | FY 2025-26 migration: A = 3 rule values, 0 code files; C = 2 reviewed diff items; 8/9 seeded YAML mistakes caught (the 9th is behaviourally equivalent) and 4/4 compiled mistakes caught. |
| 7 | Year routing | **Rule** | Financial year is explicit; the registry fails loudly for an unknown year. | Year-blind answering would be silently wrong on 84% (203/242) of FY 2025-26 cases; all engines raised errors instead. |

**Not shipped: the query-time LLM as the calculator (Engine B).** Measured with real calls (gemini-3.5-flash-lite,
40 sampled cases per year, 140 calls):
1. Rupee-exact on only **25% (FY 2024-25) and 30% (FY 2025-26)**. Its validation layer (schema, arithmetic,
   citations, shadow check against Engine A) rejected the other 30/40 and 28/40, so no wrong number reached a user,
   but the user got no answer either.
2. Non-deterministic: identical output across 10 repeats for only 1 of 3 and 0 of 3 cases.
3. **$3.50-3.66 per 1,000 requests** at list price (measured tokens), against about $0.0000006 for Engine A.
4. Median latency **3.9 s**, against 0.04 ms.

Every answer it gets right still needs Engine A to prove it is right, so the LLM adds cost and latency but no
correctness. The decision would change only if it beat the rule engine on something rules cannot do, and computing
tax is not such a thing.

## Capacity and cost at 100,000 users
Assume a filing-season peak: 100,000 users x 5 computations in 30 days. That is 500,000 requests, peaking at maybe
50 requests/s.
- Engines A + C together are under 1 ms of CPU per request. One vCPU sustains over 1,000 requests/s. Two small
  instances behind a load balancer (for redundancy, not load) cover the peak.
- Compute for the season, measured cost per request x 500k: well under $1. The bill is the always-on instances, not
  tax computation.
- An LLM computing every request at the measured rate (about $3.6 per 1,000) would cost about $1,800 for the
  season, answer only 25-30% of requests, and still need the rule engine as the shadow check.
- Optional LLM extraction only for texts the rule parser cannot complete: if 20% of sessions (20,000 calls) at about
  $0.0011 per call (a 477-token prompt measured by chars/4, plus an assumed 400 output tokens, at the Gemini 3.5 Flash-Lite
  list price), about $23 per season, capped per user.

## Keeping it correct through the next Budget
1. **Watch the sources.** Re-snapshot the official pages weekly. A SHA-256 change on any cited page opens a ticket.
   This build already found two contradictions on official pages (the new-regime standard deduction, and the
   87A threshold on a summary page). The statute text decides.
2. **Tests first.** From the Finance Bill and Act, add next year's constants to the independent oracle, write
   hand-worked cases with their arithmetic, and generate boundary cases for every changed threshold. In particular,
   cover both ends of any marginal-relief band: the "threshold updated in 1 of 3 places" mistake was caught by only
   7 of 242 cases, all in that band.
3. **Two independent encodings.** An engineer edits a new YAML file (never overwrites a year). The LLM compiler
   produces a new artifact from the same official passages. The reviewer diff shows only what changed.
4. **Gates before activation:**
   - static validation (DSL whitelist, citations exist)
   - 100% of the new year's suite on both engines
   - random-profile differential A vs C with zero mismatches
   - mutation tests showing the suite detects plausible slips
   - the 30-case worksheet re-run on the official calculator (done for FY 2024-25: 30/30 match)
   - two human approvals
5. **Activate by date.** A new rule set carries `effective_from`. Old years remain callable for revised returns.
   Requests never fall back to "latest".
6. **Monitor in production.** Track the A vs C disagreement rate (target 0), the clarification rate, and user
   corrections ("this number is wrong" reports), all tied to the rule-set version.

## Risks I accept, and how they are bounded
- **Interpretation risk.** The 80D allocation, the 24(b) Rs 30,000 case being out of scope and the residency
  defaults are documented choices. Mitigation: tax-expert review, plus the official-calculator worksheet.
  **None of those 30 checks has been performed yet.**
- **Text-understanding risk.** A rule parser will meet phrasings it has not seen. Mitigation: it asks rather than
  guesses, every assumed value is shown to the user, and the user confirms the structured profile before seeing the tax.
- **Scope.** Salary only. Adding a new head of income means new rules, tests and gates, the same process as a Budget.
