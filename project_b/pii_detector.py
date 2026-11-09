"""PII detection helpers shared by the rule, statistical and hybrid detectors.

* ``regex_pii`` (rule family): US/Indian phone formats, e-mail addresses, and name patterns introduced by
  cue words ("caller", "Mr.", "Ms.", "name is", "contact"). Cannot tell an agency's published phone/e-mail
  from a citizen's, and misses names without a cue word (measured precision/recall in RESULTS_B).
* ``phonenumbers_pii`` (statistical family): Google's libphonenumber matcher in lenient mode (validates
  number structure/region metadata) + out-of-vocabulary capitalised token pairs, i.e. proper-noun-like
  tokens that never appear in the reference resolution vocabulary. NYC 311 resolution texts are agency
  templates, so a novel capitalised bigram is strong statistical evidence of a name. Phones/e-mails that
  recur across >= 5 unrelated reference requests are agency contact points (e.g. a DOHMH line that appears
  in ~2% of rows), not personal data, and are allow-listed by frequency.

Presidio was considered; it needs a spaCy model download (~hundreds of MB) and was not required to meet
the measured detection rates, so it is not a dependency.
"""
from __future__ import annotations

import re

import phonenumbers

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b|\+91[\s-]?\d{5}[\s-]?\d{5}")
NAME_CUE = re.compile(r"\b(?:caller|complainant|resident|tenant|contact|name is|spoke (?:to|with)|mr\.?|mrs\.?|ms\.?|dr\.?)"
                      r"\s*:?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")


def regex_pii(text: str | None) -> list[tuple[str, str]]:
    if not text:
        return []
    out = [("email", m.group(0)) for m in EMAIL.finditer(text)]
    out += [("phone", m.group(0)) for m in PHONE.finditer(text)]
    out += [("name", m.group(1)) for m in NAME_CUE.finditer(text)]
    return out


_CAP_PAIR = re.compile(r"\b([A-Z][a-z]{1,15})\s+([A-Z][a-z]{1,15})\b")


def _norm(contact: str) -> str:
    return contact.lower() if "@" in contact else re.sub(r"\D", "", contact)[-10:]


def phonenumbers_pii(text: str | None, vocab: set[str], allow: set[str] = frozenset()) -> list[tuple[str, str]]:
    if not text:
        return []
    out = []
    for m in phonenumbers.PhoneNumberMatcher(text, "US", leniency=phonenumbers.Leniency.POSSIBLE):
        if _norm(m.raw_string) not in allow:
            out.append(("phone", m.raw_string))
    for m in EMAIL.finditer(text):
        if _norm(m.group(0)) not in allow:
            out.append(("email", m.group(0)))
    for m in _CAP_PAIR.finditer(text):
        a, b = m.group(1).lower(), m.group(2).lower()
        if a not in vocab and b not in vocab:
            out.append(("name", m.group(0)))
    return out
