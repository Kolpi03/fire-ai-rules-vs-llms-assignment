"""Versioned rule-set registry for the hand-coded engine.

Each financial year lives in its own YAML file (``config/tax_rules_fy<YYYY>_<YY>.yaml``). Nothing is
overwritten when the law changes: a new year is a new file with a new ``rule_set_id``.

Fail-loud policy: asking for a year that has no rule set raises :class:`RuleSetNotFound`.
We deliberately do NOT fall back to "the latest rule set" — that fallback is exactly the kind of
silent failure Task A5 is about (an FY 2025-26 request answered with FY 2024-25 law).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any

from common.config import load_yaml
from common.paths import CONFIG_DIR


class RuleSetNotFound(KeyError):
    """No rule set has been registered for the requested financial year."""


@dataclass(frozen=True)
class RuleSet:
    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return self.raw["rule_set_id"]

    @property
    def version(self) -> str:
        return self.raw["rule_set_version"]

    @property
    def fy(self) -> str:
        return self.raw["financial_year"]

    def regime(self, name: str) -> dict[str, Any]:
        return self.raw["regimes"][name]

    @property
    def cess_rate(self) -> Decimal:
        return Decimal(self.raw["cess"]["rate"])

    def rule_ids(self) -> set[str]:
        """All rule ids defined in this rule set (used by the traceability check)."""
        found: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if "rule_id" in node:
                    found.add(node["rule_id"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(self.raw)
        return found


def _file_for(fy: str) -> str:
    start, end = fy.split("-")
    return f"tax_rules_fy{start}_{end}.yaml"


def available_years() -> list[str]:
    years = []
    for p in sorted(CONFIG_DIR.glob("tax_rules_fy*.yaml")):
        stem = p.stem.replace("tax_rules_fy", "")
        a, b = stem.split("_")
        years.append(f"{a}-{b}")
    return years


@lru_cache(maxsize=None)
def get_rule_set(fy: str) -> RuleSet:
    path = CONFIG_DIR / _file_for(fy)
    if not path.exists():
        raise RuleSetNotFound(f"No rule set for FY {fy}. Available: {available_years()}")
    rs = RuleSet(load_yaml(path.name))
    if rs.fy != fy:
        raise RuleSetNotFound(f"{path.name} declares FY {rs.fy}, expected {fy}")
    return rs
