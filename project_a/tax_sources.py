"""Registry of the official sources behind every tax rule, plus a reproducible snapshotter.

Only Income Tax Department (incometaxindia.gov.in / incometax.gov.in) pages are used, as the
assignment requires. ``snapshot_sources()`` downloads each page, strips HTML to text and stores
it with a SHA-256 hash and fetch date under ``project_a/rule_texts/_snapshots`` so a reviewer
can see exactly what text the rules were read from, and detect when a page later changes.

Known inconsistencies on the official site (found while building this, resolved by the statute
text, recorded in ``reports/RESULTS_A.md``):

* The incometax.gov.in "New vs Old regime FAQ" still says the standard deduction is Rs 50,000
  for both regimes; the "Deductions allowable" page and Finance (No.2) Act 2024 give Rs 75,000
  for the new regime from AY 2025-26. We use Rs 75,000 (statute).
* The "Computation of tax for individual" page says the Rs 60,000 rebate (AY 2026-27) applies
  up to Rs 7,00,000; the Finance Act 2025 / "Brief on budget" page say Rs 12,00,000. We use
  Rs 12,00,000 (statute).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass

import requests

from common.paths import PA_RULE_TEXTS

SNAPSHOT_DIR = PA_RULE_TEXTS / "_snapshots"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


@dataclass(frozen=True)
class Source:
    source_id: str
    url: str
    title: str
    provisions: tuple[str, ...]
    used_for: str


SOURCES: tuple[Source, ...] = (
    Source("ITD-COMPUTATION-INDIVIDUAL",
           "https://wm.incometaxindia.gov.in/w/computation-of-tax-for-individual-1",
           "Computation of Tax for Individual (Income Tax Department)",
           ("First Schedule rates", "115BAC(1A)", "87A", "Surcharge", "Health and Education Cess"),
           "Old/new regime slabs (AY 2025-26 and AY 2026-27), surcharge table, cess rate, 87A summary"),
    Source("ITD-87A-FA2024-NO2",
           "https://wm.incometaxindia.gov.in/w/section-87a-25",
           "Section 87A, Income-tax Act 1961, as amended up to Finance (No.2) Act 2024",
           ("87A", "87A first proviso (a),(b)"),
           "87A rebate Rs 12,500 / Rs 5 lakh; new-regime Rs 25,000 / Rs 7 lakh with marginal relief"),
    Source("ITD-87A-FA2025",
           "https://wm.incometaxindia.gov.in/w/section-87a-26",
           "Section 87A, Income-tax Act 1961, as amended by Finance Act 2025",
           ("87A",),
           "FY 2025-26 rebate Rs 60,000 / Rs 12 lakh with marginal relief"),
    Source("ITD-DEDUCTIONS-ALLOWABLE",
           "https://wm.incometaxindia.gov.in/w/deductions-allowable-to-tax-payer",
           "Deductions allowable to tax payer (Income Tax Department)",
           ("16(ia)", "24(b)", "80C", "80CCD(1B)", "80D"),
           "Standard deduction (50,000 old / 75,000 new), 24(b) cap, Chapter VI-A caps"),
    Source("ITD-BRIEF-ON-BUDGET-2025",
           "https://wm.incometaxindia.gov.in/w/brief-on-budget",
           "Brief on Budget 2025-26 (Finance Act 2025 changes)",
           ("115BAC(1A)", "87A"),
           "FY 2025-26 new-regime slabs and 87A change (Task 5)"),
    Source("ITD-80D",
           "https://wm.incometaxindia.gov.in/web/guest/w/section-80d-63",
           "Section 80D, Income-tax Act 1961",
           ("80D(2)", "80D(3)", "80D(4)", "80D(5)"),
           "Health insurance premium / preventive check-up / senior-citizen limits"),
    Source("ITD-80CCD",
           "https://wm.incometaxindia.gov.in/w/section-80ccd-20",
           "Section 80CCD, Income-tax Act 1961",
           ("80CCD(1B)",),
           "80CCD(1B) additional NPS deduction up to Rs 50,000"),
    Source("ITD-HRA-FAQ",
           "https://wm.incometaxindia.gov.in/w/what-is-the-taxability-of-house-rent-allowance-hra-",
           "What is the taxability of House Rent Allowance (HRA)? (Income Tax Department)",
           ("10(13A)", "Rule 2A"),
           "HRA exemption = least of three amounts; 50% metro / 40% other"),
    Source("ITD-REGIME-FAQ",
           "https://www.incometax.gov.in/iec/foportal/help/new-tax-vs-old-tax-regime-faqs",
           "FAQs on New Tax vs Old Tax Regime (e-filing portal)",
           ("115BAC", "10(13A)", "24(b)", "Chapter VI-A"),
           "Which exemptions/deductions are unavailable under the new regime"),
    Source("ITD-CALCULATOR",
           "https://eportal.incometax.gov.in/iec/foservices/#/TaxCalc/calculator",
           "Official Income Tax Calculator (e-filing portal)",
           (),
           "Reference calculator for the manual 30-case verification worksheet"),
)

SOURCE_BY_ID = {s.source_id: s for s in SOURCES}


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?s)<script.*?</script>|<style.*?</style>", " ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def snapshot_sources(timeout: int = 40) -> list[dict]:
    """Fetch each source page; store text + sha256 + fetch date. Failures are recorded, not hidden."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []
    for src in SOURCES:
        entry = {**asdict(src), "fetched_at": dt.date.today().isoformat()}
        if "#" in src.url:  # single-page app: no static text to snapshot
            entry.update(status="not_snapshottable_spa")
            manifest.append(entry)
            continue
        try:
            resp = requests.get(src.url, headers={"User-Agent": _UA}, timeout=timeout)
            resp.raise_for_status()
            text = _html_to_text(resp.text)
            (SNAPSHOT_DIR / f"{src.source_id}.txt").write_text(text, encoding="utf-8")
            entry.update(status="ok", http_status=resp.status_code, chars=len(text),
                         sha256=hashlib.sha256(text.encode()).hexdigest())
        except Exception as exc:  # noqa: BLE001
            entry.update(status="unavailable", error=str(exc)[:200])
        manifest.append(entry)
    (SNAPSHOT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    for m in snapshot_sources():
        print(f"{m['status']:<24} {m['source_id']}")
