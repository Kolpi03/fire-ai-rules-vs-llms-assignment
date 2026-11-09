"""Plain-language (English / Hinglish) description -> validated TaxpayerProfile, or clarifying questions.

Approach: deterministic extraction (regex + lexicon), chosen over an LLM as the primary path because
the output feeds a rupee-exact calculation and every extracted value must be auditable. An optional
LLM extractor (:func:`llm_extract`) produces the same intermediate structure; the SAME validation and
clarification rules then apply to it (hybrid), so the LLM can never bypass the "ask, don't guess" policy.

Algorithm
---------
1. Normalise text; mask spans that look numeric but are not amounts (section names like 80C/80D/24(b),
   financial years like 2024-25).
2. Extract *mentions*: (field, value, period, evidence span) using concept keywords and amounts with
   Indian units (k, thousand/hazaar, lakh/lac/L/LPA, crore/cr). Each amount is attached to the nearest
   concept keyword inside the same clause.
3. Resolve each field to a :class:`FieldValue` with a status:
   STATED, STATED_ZERO ("no rent", "koi investment nahi"), DERIVED (e.g. "maxed out 80C" -> cap,
   "senior citizen" -> age band), ASSUMED (only residency and self-occupied status, always disclosed),
   NOT_MENTIONED (treated as 0 for optional deductions and disclosed), AMBIGUOUS, CONFLICT.
4. Ask a clarifying question for: missing salary or age; salary given as CTC / in-hand; any amount
   whose period (monthly vs annual) is unclear; rent without HRA/basic split or city; a deduction
   mentioned without an amount; parents' insurance without knowing whether they are senior; home-loan
   EMI without the interest part; conflicting values; implausible numbers.
5. Only when there are no open questions is a :class:`TaxpayerProfile` built (Pydantic re-validates).

Every decision is appended to ``audit`` so the extraction is reviewable after the fact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import ValidationError

from project_a.models import TaxpayerProfile


class FieldStatus(str, Enum):
    STATED = "stated"
    STATED_ZERO = "stated_zero"
    DERIVED = "derived"
    ASSUMED = "assumed"
    NOT_MENTIONED = "not_mentioned"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"


@dataclass
class FieldValue:
    value: Any
    status: FieldStatus
    evidence: str = ""
    note: str = ""


@dataclass
class Clarification:
    field: str
    question: str
    reason: str


@dataclass
class ParseResult:
    text: str
    fields: dict[str, FieldValue]
    clarifications: list[Clarification]
    audit: list[str]
    profile: TaxpayerProfile | None = None
    extractor: str = "rules"

    @property
    def complete(self) -> bool:
        return self.profile is not None and not self.clarifications

    def to_dict(self) -> dict[str, Any]:
        return {
            "extractor": self.extractor, "complete": self.complete,
            "fields": {k: {"value": v.value, "status": v.status.value, "evidence": v.evidence, "note": v.note}
                       for k, v in self.fields.items()},
            "clarifications": [c.__dict__ for c in self.clarifications],
            "profile": self.profile.model_dump() if self.profile else None,
            "audit": self.audit,
        }


# ------------------------------------------------------------------------------------------------
# Lexicon
# ------------------------------------------------------------------------------------------------
METRO = {"delhi", "new delhi", "mumbai", "bombay", "kolkata", "calcutta", "chennai", "madras"}
NON_METRO = {"bangalore", "bengaluru", "hyderabad", "pune", "gurgaon", "gurugram", "noida", "ahmedabad", "jaipur",
             "lucknow", "kochi", "cochin", "indore", "chandigarh", "bhopal", "nagpur", "coimbatore", "surat",
             "thane", "navi mumbai", "ghaziabad", "faridabad", "mysore", "mysuru", "patna", "bhubaneswar",
             "vizag", "visakhapatnam", "trivandrum", "thiruvananthapuram", "mohali", "dehradun", "goa", "vadodara"}

MONTHLY = r"(?:per\s*month|/\s*month|/\s*mo\b|p\.?\s*m\.?(?!\w)|monthly|a\s+month|every\s+month|each\s+month|month(?:ly)?\s+rent|mahin[ae]|mahine|har\s+mahine|pm\b)"
ANNUAL = r"(?:per\s*annum|p\.?\s*a\.?(?!\w)|annual(?:ly)?|a\s+year|per\s+year|/\s*year|/\s*yr|yearly|lpa|every\s+year|saal(?:ana)?|salana|sal\s+ka|in\s+a\s+year|for\s+the\s+year|this\s+year)"

# Section names are masked to "§" digits before matching, so patterns accept both forms ((?:80|§§)c ...).
CONCEPTS: dict[str, list[str]] = {
    "salary": [r"salary", r"\bsalry\b", r"\bearn(?:s|ing)?\b", r"\bincome\b", r"\bctc\b", r"\bpackage\b", r"\bgross\b",
               r"\blpa\b", r"in[- ]hand", r"take[- ]home", r"kamat[ai]", r"kamai", r"tankhwah",
               r"\bmake\b", r"\bdrawing\b"],
    "rent": [r"\brent\b", r"kiraya", r"kiraaya", r"\brented\b", r"\brenting\b"],
    "hra": [r"\bhra\b", r"house\s+rent\s+allowance"],
    "basic": [r"\bbasic\b", r"\bda\b"],
    "section_80c": [r"(?:80|§§)c\b", r"\bppf\b", r"\belss\b", r"\bepf\b", r"\bpf\b", r"\blic\b", r"life\s+insurance",
                    r"tax[- ]saving", r"sukanya", r"\bnsc\b", r"principal\s+repay", r"\bulip\b", r"tuition"],
    "section_80ccd_1b": [r"\bnps\b", r"(?:80|§§)ccd", r"national\s+pension"],
    "health": [r"health\s+insurance", r"medical\s+insurance", r"mediclaim", r"health\s+(?:policy|cover|plan)",
               r"(?:80|§§)d\b", r"health\s+premium", r"family\s+floater"],
    "home_loan": [r"home\s*loan", r"housing\s+loan", r"\bemi\b", r"loan\s+interest",
                  r"interest\s+on\s+(?:my\s+)?(?:home|house)"],
    "preventive": [r"health\s*check[- ]?up", r"preventive", r"full\s+body\s+check"],
    "medical_exp": [r"medical\s+(?:bills|expenses|expenditure|treatment)", r"hospital", r"doctor"],
}
PARENT_WORDS = r"(?:parents?|mother|father|mom|mum|dad|maa|papa|mummy|in-?laws)"
SELF_WORDS = r"(?:myself|self|\bme\b|wife|husband|spouse|kids|children|family|own\s+family|family\s+floater|both\s+of\s+us)"

AMOUNT_RE = re.compile(
    r"(?:₹|rs\.?|inr|rupees?)?\s*(\d+(?:,\d+)*(?:\.\d+)?)\s*"
    r"(crores?|cr\b|lakhs?|lacs?|lac\b|lakh|l\b|lpa\b|k\b|thousand|hazaar|hazar|hajar)?", re.IGNORECASE)
UNIT = {"crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000, "lakh": 100_000, "lakhs": 100_000,
        "lac": 100_000, "lacs": 100_000, "l": 100_000, "lpa": 100_000, "k": 1_000, "thousand": 1_000,
        "hazaar": 1_000, "hazar": 1_000, "hajar": 1_000}

ZERO_PATTERNS: dict[str, list[str]] = {
    "rent": [r"(?:no|zero)\s+rent", r"(?:don'?t|do\s+not)\s+pay\s+(?:any\s+)?rent", r"own\s+(?:a\s+)?(?:house|home|flat|apartment)",
             r"live\s+with\s+(?:my\s+)?parents", r"rent\s+(?:bhi\s+)?(?:nahi|nhi)", r"kiraya\s+(?:bhi\s+)?(?:nahi|nhi)",
             r"apne\s+ghar", r"company\s+(?:accommodation|quarters)", r"rent[- ]free", r"(?:not|never)\s+(?:paying\s+)?rent"],
    "section_80c": [r"no\s+(?:(?:80|§§)c|investments?|tax[- ]saving)", r"(?:koi|kuch)\s+investment\s+(?:nahi|nhi)",
                    r"(?:haven'?t|have\s+not|didn'?t|did\s+not)\s+invest", r"investment\s+(?:nahi|nhi)",
                    r"nothing\s+in\s+(?:80|§§)c", r"no\s+deductions?", r"zero\s+investments?"],
    "section_80ccd_1b": [r"no\s+nps", r"(?:not|don'?t)\s+(?:have|in)\s+nps", r"nps\s+(?:nahi|nhi)", r"no\s+deductions?",
                         r"no\s+investments?", r"(?:koi|kuch)\s+investment\s+(?:nahi|nhi)"],
    "health": [r"no\s+(?:health|medical)\s+insurance", r"(?:don'?t|do\s+not)\s+have\s+(?:any\s+)?(?:health|medical)\s+insurance",
               r"insurance\s+(?:nahi|nhi)", r"no\s+mediclaim", r"no\s+deductions?"],
    "home_loan": [r"no\s+(?:home|housing)\s+loan", r"(?:don'?t|do\s+not)\s+have\s+(?:a\s+|any\s+)?(?:home|housing)?\s*loan",
                  r"loan\s+(?:nahi|nhi)", r"no\s+loans?", r"no\s+deductions?"],
    "hra": [r"no\s+hra", r"(?:don'?t|do\s+not)\s+get\s+(?:any\s+)?hra", r"hra\s+(?:nahi|nhi)", r"without\s+hra",
            r"no\s+house\s+rent\s+allowance"],
}

REQUIRED = ("gross_salary", "age")


@dataclass
class _Mention:
    concept: str
    value: int | None
    period: str | None  # monthly | annual | None
    span: str
    pos: int
    qualifier: str | None = None  # "parents" | "self" | None (health concepts only)


def _normalise(text: str) -> str:
    t = text.replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", t).strip()


def _mask(t: str) -> str:
    """Replace digits that are not amounts (section names, financial years) with '§' (length-preserving)."""
    t = re.sub(r"\b(80ccd\s*\(?1b\)?|80ccd|80c|80d|87a|24\s*\(?b\)?|10\s*\(13a\)|115bac)",
               lambda m: re.sub(r"\d", "§", m.group(0)), t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:fy|ay)?\s*20\d\d\s*[-/]\s*\d{2,4}\b", lambda m: "§" * len(m.group(0)), t, flags=re.IGNORECASE)
    return t


# Sentence ends: . ! ? ; followed by space/end — but not the dot of "Rs." / "INR." / "No." abbreviations.
_SENT_END = re.compile(r"(?<!\brs)(?<!\binr)(?<!\bno)[.!?;](?=\s|$)", re.IGNORECASE)
_CLAUSE_SEP = re.compile(r",\s+|\s+(?:and|but|aur|lekin|plus|also|which|so|while)\s+", re.IGNORECASE)


def _segments(t: str, sep: re.Pattern) -> list[tuple[int, str]]:
    out, start = [], 0
    for m in sep.finditer(t):
        out.append((start, t[start:m.start()]))
        start = m.end()
    out.append((start, t[start:]))
    return [(s, x) for s, x in out if x.strip()]


def _parse_amount(num: str, unit: str | None) -> int:
    return int(round(float(num.replace(",", "")) * UNIT.get((unit or "").lower(), 1)))


def _period_near(text: str, start: int, end: int) -> str | None:
    after = text[end:end + 30].lower()
    before = text[max(0, start - 25):start].lower()
    if re.match(r"\s*(?:hai\s+)?(?:of\s+|ka\s+|ki\s+)?" + MONTHLY, after) or re.search(MONTHLY + r"\s*(?:of|ka|ke|ki)?\s*$", before):
        return "monthly"
    if re.match(r"\s*(?:hai\s+)?(?:" + ANNUAL + ")", after) or re.search(ANNUAL + r"\s*(?:of|ka|ke|ki)?\s*$", before):
        return "annual"
    return None


def _sentence_period(sentence: str) -> str | None:
    """Period that applies to the whole sentence, e.g. "Annual gross salary X including HRA Y and basic Z".

    A period word that sits right next to one amount ("15 lakh annually") belongs to THAT amount only and
    is not inherited by other amounts in the sentence (otherwise "15 lakh annually, pay 25k rent" would
    silently read the rent as annual).
    """
    free = {"monthly": False, "annual": False}
    amounts = [(m.start(), m.end()) for m in AMOUNT_RE.finditer(sentence) if m.group(1)]
    for tag, pat in (("monthly", MONTHLY), ("annual", ANNUAL)):
        for pm in re.finditer(pat, sentence):
            consumed = any(0 <= pm.start() - a_end <= 14 and not re.search(r"[a-z]{4,}", sentence[a_end:pm.start()]
                                                                           .replace("hai", "").replace("of", ""))
                           or 0 <= a_start - pm.end() <= 4 for a_start, a_end in amounts)
            if not consumed:
                free[tag] = True
    if free["monthly"] and not free["annual"]:
        return "monthly"
    if free["annual"] and not free["monthly"]:
        return "annual"
    return None


def _qualifier(context: str, amount_pos: int) -> str | None:
    """Nearest family qualifier (parents vs self) to the amount within a context window (fallback rule)."""
    best: tuple[int, str] | None = None
    for pat, tag in ((PARENT_WORDS, "parents"), (SELF_WORDS, "self")):
        for m in re.finditer(pat, context):
            d = abs(m.start() - amount_pos)
            if best is None or d < best[0]:
                best = (d, tag)
    return best[1] if best else None


def _clause_qualifier(clause: str, a_start: int, a_end: int, prev_amount_end: int) -> str | None:
    """Who a health amount is for, decided inside its own clause (preferred over the wider context):
    1. a qualifier word between the previous amount and this one ("parents health insurance 50000");
    2. else a qualifier introduced by for/of/towards right after the amount ("30000 for parents").
    """
    before = clause[prev_amount_end:a_start]
    hits = [(m.start(), tag) for pat, tag in ((PARENT_WORDS, "parents"), (SELF_WORDS, "self"))
            for m in re.finditer(pat, before)]
    if hits:
        return max(hits)[1]
    m = re.match(r"\s*(?:/-)?\s*(?:for|of|towards|on)\s+(?:[a-z']+\s+){0,3}?(?:(?:of|for)\s+)?(?:my\s+|our\s+|the\s+)?"
                 r"(?:own\s+)?(" + PARENT_WORDS + "|" + SELF_WORDS + ")", clause[a_end:a_end + 50])
    if m:
        return "parents" if re.match(PARENT_WORDS, m.group(1)) else "self"
    if re.search(PARENT_WORDS, clause) and re.search(SELF_WORDS + "|health|insurance|mediclaim", clause[:a_start]):
        # the clause names parents but nothing ties THIS amount to them or to self -> do not guess
        return "ambiguous"
    return None


def _is_non_amount(clause: str, m: re.Match) -> bool:
    nxt = clause[m.end():m.end() + 14]
    if "§" in clause[max(0, m.start() - 1):m.end()]:
        return True
    if re.match(r"\s*[%+]", nxt):                       # percentages, "65+"
        return True
    if m.group(2) is None and re.match(r"\s*(?:years?|yrs?|y/o|yo\b|saal|-?year)", nxt):  # ages
        return True
    if m.group(2) is None and float(m.group(1).replace(",", "")) < 100:  # bare small numbers are not rupees
        return True
    return False


def _extract_mentions(text: str) -> tuple[list[_Mention], list[str], list[tuple[int, str]]]:
    """Return (mentions, audit, unattached_amounts)."""
    audit: list[str] = []
    unattached: list[tuple[int, str]] = []
    masked = _mask(text.lower())
    mentions: list[_Mention] = []
    sentences = _segments(masked, _SENT_END)
    prev_sentence = ""
    for s_start, sentence in sentences:
        s_period = _sentence_period(sentence)
        carry: str | None = None
        for c_off, clause in _segments(sentence, _CLAUSE_SEP):
            c_start = s_start + c_off
            kw: list[tuple[int, str]] = []
            for concept, pats in CONCEPTS.items():
                for pat in pats:
                    kw.extend((m.start(), concept) for m in re.finditer(pat, clause))
            amounts = [(m.start(), m.end(), _parse_amount(m.group(1), m.group(2)), m.group(2))
                       for m in AMOUNT_RE.finditer(clause) if m.group(1) and not _is_non_amount(clause, m)]
            prev_end = 0
            for a_start, a_end, val, unit in amounts:
                clause_qual = _clause_qualifier(clause, a_start, a_end, prev_end)
                prev_end = a_end
                if kw:
                    concept = min(kw, key=lambda k: abs(k[0] - a_start) + (0 if k[0] <= a_start else 3))[1]
                elif carry:
                    concept = carry
                    audit.append(f"amount {val:,} in '{clause.strip()}' attached to previous concept '{carry}'")
                else:
                    unattached.append((val, clause.strip()))
                    audit.append(f"amount {val:,} in '{clause.strip()}' has no concept -> will ask")
                    continue
                if unit and unit.lower() == "lpa":
                    period = "annual"
                else:
                    period = _period_near(clause, a_start, a_end) or s_period
                qual = None
                if concept in ("health", "preventive", "medical_exp"):
                    ctx = prev_sentence + " " + sentence[:c_off + a_end + 40]
                    qual = clause_qual or _qualifier(ctx, len(prev_sentence) + 1 + c_off + a_start)
                span = text[c_start + max(0, a_start - 25): c_start + a_end + 20].strip()
                mentions.append(_Mention(concept, val, period, span, c_start + a_start, qual))
            for pos, concept in kw:
                if not any(abs(a[0] - pos) < 60 for a in amounts):
                    qual = None
                    if concept in ("health", "preventive", "medical_exp"):
                        qual = _qualifier(sentence, c_off + pos)
                    mentions.append(_Mention(concept, None, None, clause.strip()[:80], c_start + pos, qual))
            if kw:
                carry = max(kw)[1]  # concept of the right-most keyword in this clause
        prev_sentence = sentence
    return mentions, audit, unattached


def _extract_age(low: str) -> tuple[list[tuple[int, str]], str | None]:
    ages = []
    for pat in (r"\b(\d{2,3})\s*(?:years?|yrs?|y/o|yo)\b(?:\s*old)?", r"\bage[d]?\s*[:\-]?\s*(?:is\s*)?(?:of\s*)?(\d{2,3})\b",
                r"\bi'?m\s+(?:a\s+)?(\d{2,3})\b(?!\s*(?:k\b|lakh|lac|l\b|%|,\d|\d))",
                r"\bi\s+am\s+(\d{2,3})\b(?!\s*(?:k\b|lakh|lac|l\b|%|,\d|\d))",
                r"\b(\d{2,3})\s*saal\b", r"\bumar\s*(?:hai\s*)?(\d{2,3})\b", r"\bumr\s*(\d{2,3})\b",
                r"\b(\d{2,3})\s*-?\s*year[- ]old\b"):
        for m in re.finditer(pat, low):
            ages.append((int(m.group(1)), m.group(0)))
    band = None
    for m in re.finditer(r"super\s*senior|very\s+senior|senior\s+citizens?", low):
        before = low[max(0, m.start() - 60):m.start()]
        if re.search(PARENT_WORDS, before) or re.search(r"\s*parents?", low[m.end():m.end() + 10]):
            continue  # describes the parents, not the taxpayer
        band = "super_senior" if re.match(r"super|very", m.group(0)) else band or "senior"
    return ages, band


def _dedupe(vals: list[_Mention]) -> list[_Mention]:
    seen, out = set(), []
    for v in vals:
        key = (v.value, v.period, v.pos)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def parse(text: str, financial_year: str = "2024-25") -> ParseResult:
    """Parse a free-text description. Returns a profile only when nothing needs clarifying."""
    norm = _normalise(text)
    low = norm.lower()
    masked_low = _mask(low)
    mentions, audit, unattached = _extract_mentions(norm)
    fields: dict[str, FieldValue] = {}
    clar: list[Clarification] = []

    def ask(fld: str, q: str, why: str) -> None:
        if not any(c.field == fld for c in clar):
            clar.append(Clarification(fld, q, why))
            audit.append(f"clarify {fld}: {why}")

    def zero_stated(concept: str) -> str | None:
        for pat in ZERO_PATTERNS.get(concept, []):
            m = re.search(pat, masked_low)
            if m:
                return m.group(0)
        return None

    by: dict[str, list[_Mention]] = {}
    for m in mentions:
        by.setdefault(m.concept, []).append(m)

    # ---------------- salary ----------------
    sal = [m for m in by.get("salary", []) if m.value is not None]
    if re.search(r"\bctc\b|\bpackage\b", low) and sal:
        ask("gross_salary", "Is that amount your CTC/package? Please give your annual GROSS salary (excluding employer PF/"
                            "gratuity contributions).", "CTC is not the same as gross salary")
    if re.search(r"in[- ]hand|take[- ]home|net\s+salary|after\s+tax", low):
        ask("gross_salary", "That sounds like in-hand/take-home pay. What is your annual GROSS salary before TDS and PF?",
            "net pay cannot be converted to gross salary without guessing")
    sal_annual: list[tuple[int, _Mention]] = []
    for m in _dedupe(sal):
        if m.period == "monthly":
            sal_annual.append((m.value * 12, m))
            audit.append(f"salary {m.value:,}/month -> {m.value * 12:,}/year (period stated)")
        elif m.period == "annual":
            sal_annual.append((m.value, m))
        elif m.value >= 500_000:
            sal_annual.append((m.value, m))
            audit.append(f"salary {m.value:,} with no period read as annual (a monthly salary >= 5 lakh would be "
                         "stated explicitly; documented heuristic)")
        else:
            ask("gross_salary", f"Is your salary of Rs {m.value:,} per month or per year?", "salary period not stated")
    distinct = sorted({v for v, _ in sal_annual})
    if len(distinct) > 1:
        fields["gross_salary"] = FieldValue(distinct, FieldStatus.CONFLICT, "; ".join(m.span for _, m in sal_annual))
        ask("gross_salary", f"You mentioned different salary figures ({', '.join(f'Rs {v:,}' for v in distinct)} per "
                            f"year). Which is your annual gross salary?", "conflicting salary values")
    elif distinct:
        fields["gross_salary"] = FieldValue(distinct[0], FieldStatus.STATED, sal_annual[0][1].span)
    elif not any(c.field == "gross_salary" for c in clar):
        ask("gross_salary", "What is your annual gross salary?", "salary not mentioned")
    for val, clause in unattached:
        ask("gross_salary" if "gross_salary" not in fields else f"unattached_{val}",
            f"What does the amount Rs {val:,} ('{clause}') refer to - salary, rent, an investment?",
            "amount without a recognisable label")

    # ---------------- age ----------------
    ages, band = _extract_age(low)
    valid_ages = sorted({a for a, _ in ages if 18 <= a <= 120})
    invalid_ages = sorted({a for a, _ in ages if not 18 <= a <= 120})
    if invalid_ages and not valid_ages:
        ask("age", f"An age of {invalid_ages[0]} is outside the supported range (18-120). How old are you?", "invalid age")
    elif len(valid_ages) > 1:
        fields["age"] = FieldValue(valid_ages, FieldStatus.CONFLICT)
        ask("age", f"You mentioned ages {valid_ages}. What is your age?", "conflicting ages")
    elif valid_ages:
        a = valid_ages[0]
        if (band == "senior" and not 60 <= a < 80) or (band == "super_senior" and a < 80):
            fields["age"] = FieldValue(a, FieldStatus.CONFLICT, note=f"also described as {band}")
            ask("age", f"You said you are {a} but also a {band.replace('_', ' ')} citizen. What is your age?",
                "age contradicts senior-citizen statement")
        else:
            fields["age"] = FieldValue(a, FieldStatus.STATED, next(s_ for v, s_ in ages if v == a))
    elif band == "senior":
        fields["age"] = FieldValue(60, FieldStatus.DERIVED, "senior citizen",
                                   "only the age band (60-79) matters; represented as 60")
    elif band == "super_senior":
        fields["age"] = FieldValue(80, FieldStatus.DERIVED, "super senior", "only the age band (80+) matters")
    else:
        ask("age", "How old are you (or will you be by 31 March)? Senior citizens have different old-regime slabs.",
            "age not mentioned")

    # ---------------- residency ----------------
    if re.search(r"\bnri\b|non[- ]resident", low):
        fields["is_resident"] = FieldValue(False, FieldStatus.STATED, "NRI/non-resident")
    else:
        fields["is_resident"] = FieldValue(True, FieldStatus.ASSUMED, note="resident unless stated (disclosed to user)")

    # ---------------- money fields ----------------
    def money(concept: str, fld: str, label: str, period_matters: bool = True,
              filt=lambda m: True, zero_concept: str | None = None) -> None:
        ms = [m for m in by.get(concept, []) if filt(m)]
        valued = _dedupe([m for m in ms if m.value is not None])
        zero = zero_stated(zero_concept or concept)
        if not valued:
            if zero:
                fields[fld] = FieldValue(0, FieldStatus.STATED_ZERO, zero)
            elif ms:
                fields[fld] = FieldValue(None, FieldStatus.AMBIGUOUS, ms[0].span)
                ask(fld, f"How much do you pay/claim for {label} in the year?", f"{label} mentioned without an amount")
            else:
                fields[fld] = FieldValue(0, FieldStatus.NOT_MENTIONED, note="not mentioned; treated as nil (disclosed)")
            return
        if zero:
            fields[fld] = FieldValue(None, FieldStatus.CONFLICT, f"{zero} / {valued[0].span}")
            ask(fld, f"You said '{zero}' but also mentioned an amount for {label}. Which is correct?",
                f"contradictory {label}")
            return
        total, unclear = 0, []
        for m in valued:
            if m.period == "monthly":
                total += m.value * 12
                audit.append(f"{label}: {m.value:,}/month -> {m.value * 12:,}/year")
            elif m.period == "annual" or not period_matters:
                total += m.value
            else:
                unclear.append(m)
        if unclear:
            fields[fld] = FieldValue(None, FieldStatus.AMBIGUOUS, unclear[0].span)
            ask(fld, f"Is the {label} amount of Rs {unclear[0].value:,} per month or per year?",
                f"{label} period not stated")
            return
        fields[fld] = FieldValue(total, FieldStatus.STATED, "; ".join(m.span for m in valued))

    is_par = lambda m: m.qualifier == "parents"  # noqa: E731
    not_par = lambda m: m.qualifier != "parents"  # noqa: E731
    money("rent", "annual_rent_paid", "rent")
    money("hra", "hra_received", "HRA")
    money("basic", "basic_plus_da", "basic salary + DA")
    # Investments/premiums are annual by nature: no period means annual; a monthly figure must say so.
    money("section_80c", "section_80c", "80C investments", period_matters=False)
    if re.search(r"max(?:ed)?\s*(?:out)?\s*(?:my\s+)?(?:80|§§)c|full\s+(?:80|§§)c|(?:80|§§)c\s+(?:is\s+)?(?:fully\s+)?"
                 r"(?:maxed|full|exhausted)", masked_low):
        if fields["section_80c"].status in (FieldStatus.AMBIGUOUS, FieldStatus.NOT_MENTIONED):
            fields["section_80c"] = FieldValue(150000, FieldStatus.DERIVED, "maxed out 80C",
                                               "'maxed out' = at least the Rs 1.5 lakh cap; the deduction is capped there")
            clar[:] = [c for c in clar if c.field != "section_80c"]
    money("section_80ccd_1b", "section_80ccd_1b", "NPS (80CCD(1B))", period_matters=False)
    money("health", "health_insurance_self_family", "health insurance for yourself/family", False, not_par)
    money("health", "health_insurance_parents", "parents' health insurance", False, is_par, "__none__")
    money("preventive", "preventive_checkup_self_family", "preventive health check-up", False, not_par, "__none__")
    money("preventive", "preventive_checkup_parents", "parents' preventive check-up", False, is_par, "__none__")
    money("medical_exp", "medical_expenditure_parents", "parents' medical expenses", False, is_par, "__none__")
    money("medical_exp", "medical_expenditure_self_family", "your own medical expenses", False, not_par, "__none__")
    money("home_loan", "home_loan_interest_self_occupied", "home-loan interest", period_matters=False)

    if any(m.qualifier == "ambiguous" and m.value is not None
           for c in ("health", "preventive", "medical_exp") for m in by.get(c, [])):
        ask("health_insurance_parents", "Which health-insurance amount is for your parents and which for yourself/family?",
            "health amounts cannot be attributed to self vs parents without guessing")

    # home loan: EMI = principal + interest; "loan of X" = principal outstanding
    if re.search(r"\bemi\b", low) and not re.search(r"interest\s+(?:paid|of|is|component|was)|interest\s+\d", low):
        ask("home_loan_interest_self_occupied", "Your EMI includes principal and interest. How much INTEREST did you pay "
                                                "on the home loan this year?", "EMI given without interest component")
    if re.search(r"loan\s+(?:of|amount|outstanding)|outstanding\s+loan|borrowed", low) and \
            not re.search(r"interest\s+(?:paid|of|is|component|was)", low):
        fields["home_loan_interest_self_occupied"] = FieldValue(None, FieldStatus.AMBIGUOUS, "loan principal given")
        ask("home_loan_interest_self_occupied", "That looks like the loan amount, not the interest. How much interest did "
                                                "you pay on the home loan during the year?", "loan principal, not interest")
    if re.search(r"let\s+out|rented\s+out|tenant\s+in\s+my", low) and fields[
            "home_loan_interest_self_occupied"].value:
        ask("home_loan_interest_self_occupied", "Is the house with the home loan let out? Only self-occupied property is "
                                                "supported.", "let-out property is out of scope")

    # self medical expenditure only counts for a senior; a non-senior's is not deductible -> drop with a note
    if fields["medical_expenditure_self_family"].value and fields.get("age") and isinstance(fields["age"].value, int) \
            and fields["age"].value < 60:
        audit.append("own medical expenditure ignored: deductible only for an uninsured senior citizen")

    # parents' senior status (needed for the 80D parents' cap)
    par_senior = None
    for sent in re.split(r"[.!?;]\s", low):
        for pm in re.finditer(PARENT_WORDS, sent):
            window = sent[max(0, pm.start() - 20):pm.end() + 45]  # words describing the parents themselves
            if re.search(r"(?:not|no)\s+senior|below\s+60|under\s+60|less\s+than\s+60", window):
                par_senior = False if par_senior is None else par_senior
            elif re.search(r"senior|above\s+60|over\s+60|60\s*se\s+upar|60\s*\+|\b(?:6\d|7\d|8\d|9\d)\b(?!\s*(?:k\b|lakh|,\d))",
                           sent[pm.start():pm.end() + 45]):
                par_senior = True
    needs_parent_band = any(fields[f].value for f in
                            ("health_insurance_parents", "medical_expenditure_parents", "preventive_checkup_parents"))
    if par_senior is None:
        fields["parents_are_senior"] = FieldValue(False, FieldStatus.NOT_MENTIONED)
        if needs_parent_band:
            ask("parents_are_senior", "Is either of your parents 60 or older? The 80D limit for parents is Rs 50,000 "
                                      "instead of Rs 25,000 if so.", "parents' age needed for 80D cap")
    else:
        fields["parents_are_senior"] = FieldValue(par_senior, FieldStatus.STATED)
    sp = re.search(r"(?:wife|husband|spouse)[^.]{0,30}?(?:senior|\b6\d\b|\b7\d\b|\b8\d\b)", low)
    fields["spouse_is_senior"] = (FieldValue(True, FieldStatus.STATED, sp.group(0)) if sp
                                  else FieldValue(False, FieldStatus.NOT_MENTIONED))

    # ---------------- city / HRA consistency ----------------
    city = next((c for c in sorted(METRO | NON_METRO, key=len, reverse=True) if re.search(rf"\b{c}\b", low)), None)
    if city:
        fields["metro_city"] = FieldValue(city in METRO, FieldStatus.STATED, city,
                                          "Rule 2A metro = Delhi, Mumbai, Kolkata, Chennai only")
    else:
        fields["metro_city"] = FieldValue(False, FieldStatus.NOT_MENTIONED)
    rent, hra = fields["annual_rent_paid"], fields["hra_received"]
    rent_present = bool(rent.value) or rent.status in (FieldStatus.AMBIGUOUS, FieldStatus.CONFLICT)
    if rent_present:
        if hra.status == FieldStatus.NOT_MENTIONED:
            ask("hra_received", "Does your salary include HRA? If yes, what are your annual HRA and basic salary (+DA)?",
                "rent paid but HRA/basic split unknown (needed for 10(13A))")
        if hra.value and fields["basic_plus_da"].status == FieldStatus.NOT_MENTIONED:
            ask("basic_plus_da", "What is your annual basic salary + DA? It is needed for the HRA exemption.",
                "HRA given without basic salary")
        if not city and hra.status != FieldStatus.STATED_ZERO:
            ask("metro_city", "Which city is your rented home in? HRA exemption is 50% of salary in Delhi/Mumbai/"
                              "Kolkata/Chennai and 40% elsewhere.", "city needed for HRA")
    elif hra.value and rent.status == FieldStatus.NOT_MENTIONED:
        ask("annual_rent_paid", "You receive HRA. How much rent do you pay per month?", "HRA without rent")

    # ---------------- plausibility ----------------
    g = fields.get("gross_salary")
    if g and g.status == FieldStatus.STATED and isinstance(g.value, int):
        if g.value <= 0 or g.value > 1_000_000_000:
            ask("gross_salary", f"Rs {g.value:,} looks implausible as an annual salary. Please confirm.", "implausible salary")
        for fld in ("annual_rent_paid", "section_80c", "hra_received", "basic_plus_da", "home_loan_interest_self_occupied"):
            fv = fields.get(fld)
            if fv and isinstance(fv.value, int) and fv.value > g.value:
                ask(fld, f"The {fld.replace('_', ' ')} (Rs {fv.value:,}) is more than your salary (Rs {g.value:,}). "
                         "Please confirm the amounts and whether they are monthly or yearly.", "amount exceeds salary")

    fy_m = re.search(r"(?:fy|financial\s+year)\s*(20\d\d)\s*[-/]\s*(\d{2})", low)
    if fy_m:
        financial_year = f"{fy_m.group(1)}-{fy_m.group(2)}"
        audit.append(f"financial year taken from text: {financial_year}")

    result = ParseResult(text=text, fields=fields, clarifications=clar, audit=audit)
    if not clar:
        kwargs = {k: v.value for k, v in fields.items() if v.value is not None and k in TaxpayerProfile.model_fields}
        try:
            result.profile = TaxpayerProfile(financial_year=financial_year, **kwargs)
        except ValidationError as exc:
            msg = "; ".join(e["msg"] for e in exc.errors())
            clar.append(Clarification("profile", f"Some amounts are inconsistent ({msg}). Please re-check them.", msg))
            audit.append(f"profile validation failed: {msg}")
    return result


# ------------------------------------------------------------------------------------------------
# Optional LLM extractor (hybrid): LLM proposes fields; the same validation/clarification rules apply.
# ------------------------------------------------------------------------------------------------
LLM_SYSTEM = ("Extract Indian salaried-taxpayer facts from the user's message. Never invent numbers. Use null for "
              "anything not stated. Output JSON only.")


def llm_extract(text: str, client: Any) -> dict[str, Any]:
    """Return the LLM's raw field proposals (value + period + evidence). Raises LLMUnavailable without a key."""
    schema = {f: {"value": "number|bool|null", "period": "monthly|annual|null", "evidence": "quoted text|null"}
              for f in ("gross_salary", "age", "annual_rent_paid", "hra_received", "basic_plus_da", "metro_city",
                        "section_80c", "section_80ccd_1b", "health_insurance_self_family", "health_insurance_parents",
                        "parents_are_senior", "home_loan_interest_self_occupied", "is_resident")}
    prompt = (f"Message: {text!r}\nReturn JSON with this shape (all money in rupees as stated, do not annualise):\n"
              f"{schema}\nAlso return 'ambiguities': a list of fields whose meaning is unclear.")
    return client.generate_json(prompt, system=LLM_SYSTEM).parsed
