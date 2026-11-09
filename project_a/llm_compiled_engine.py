"""Engine C — LLM-compiled rules, executed WITHOUT an LLM.

An LLM translated the official passages into a JSON artifact of steps in a restricted expression
language (see :mod:`project_a.rule_dsl` and :mod:`project_a.rule_compiler`). The artifact was
validated, verified against the test suite and approved before use; this module only interprets it.

Runtime guarantees
------------------
* Refuses to run an artifact whose ``review.status`` is not ``approved`` (unless explicitly allowed
  for verification of candidates).
* Refuses a financial year with no artifact (no fallback to another year — fail loud).
* Refuses an input that states (or sets to a non-default value) a field the artifact never reads (``UnsupportedInput``): an artifact
  compiled before a schema extension must not silently ignore the new information.
* Each explanation line carries the step's clause, step id (as rule_id) and the artifact hash
  prefix as the rule version, so every rupee traces to an approved, hash-identified rule.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from functools import lru_cache
from typing import Any

from project_a import rule_dsl
from project_a.models import (ComponentLine, EngineStatus, RegimeResult, TaxComputation, TaxpayerProfile,
                             insured_senior_flags)
from project_a.rule_compiler import artifact_path
from project_a.rule_registry import RuleSetNotFound

ENGINE_NAME = "llm_compiled_engine"
D = Decimal


class UnapprovedArtifact(RuntimeError):
    pass


class UnsupportedInput(ValueError):
    """The profile sets an input field that the artifact never reads; answering would silently ignore it."""


@lru_cache(maxsize=64)
def _names_in(exprs: tuple[str, ...]) -> frozenset[str]:
    return frozenset(n for e in exprs for n in rule_dsl.names_used(e))


def referenced_names(art: dict[str, Any]) -> frozenset[str]:
    return _names_in(tuple(st["expr"] for reg in art["regimes"].values() for st in reg["steps"]))


def check_supported(art: dict[str, Any], p: TaxpayerProfile) -> None:
    used = referenced_names(art)
    # a field the user STATED carries information even when its value equals the default (e.g. spouse_insured=False
    # switches 80D to per-person insurance), so refuse on any stated or non-default field the artifact never reads
    ignored = sorted(f for f, info in TaxpayerProfile.model_fields.items()
                     if f != "financial_year" and f not in used
                     and (f in p.model_fields_set or getattr(p, f) != info.default))
    if ignored:
        raise UnsupportedInput(f"compiled artifact for FY {art.get('financial_year')} does not read {ignored}; "
                               "it needs recompiling and re-approval before it can answer this profile")


@lru_cache(maxsize=None)
def load_artifact(fy: str) -> dict[str, Any]:
    path = artifact_path(fy)
    if not path.exists():
        raise RuleSetNotFound(f"No compiled rule artifact for FY {fy} ({path.name})")
    art = json.loads(path.read_text(encoding="utf-8"))
    if art.get("review", {}).get("status") != "approved":
        raise UnapprovedArtifact(f"{path.name} has not been approved")
    return art


def _run_regime(art: dict[str, Any], regime: str, env0: dict[str, Any], version: str) -> RegimeResult:
    env = dict(env0)
    lines: list[ComponentLine] = []
    tables = art["tables"]
    for st in art["regimes"][regime]["steps"]:
        val = rule_dsl.evaluate(st["expr"], env, tables)
        env[st["output"]] = val
        if st.get("line"):
            lines.append(ComponentLine(name=st["line"], amount=D(val) if not isinstance(val, bool) else D(int(val)),
                                       clause=st["clause"], rule_id=st["id"], rule_version=version,
                                       explanation=f"{st['output']} = {st['expr']}"))
    return RegimeResult(regime=regime, gross_salary=D(env0["gross_salary"]),
                        total_income=env["total_income"], tax_on_income=env["tax_on_income"],
                        rebate_87a=env["rebate_87a"], surcharge=env["surcharge"], cess=env["cess"],
                        total_tax=env["total_tax"], tax_payable=env["tax_payable"], lines=lines)


def run_artifact(art: dict[str, Any], p: TaxpayerProfile) -> TaxComputation:
    t0 = time.perf_counter()
    if art.get("financial_year") != p.financial_year:
        raise RuleSetNotFound(f"artifact is for {art.get('financial_year')}, request is {p.financial_year}")
    check_supported(art, p)
    # profile fields + derived 80D(4) inputs (whether a premium in a group is paid for an insured senior)
    env0 = {**p.model_dump(exclude={"financial_year"}), **insured_senior_flags(p)}
    version = (art.get("review", {}).get("artifact_sha256") or "unreviewed")[:12]
    old = _run_regime(art, "old", env0, version)
    new = _run_regime(art, "new", env0, version)
    tie = art.get("regime_choice", {}).get("tie_breaker", "new")
    if old.tax_payable < new.tax_payable:
        rec, sav = "old", new.tax_payable - old.tax_payable
    elif new.tax_payable < old.tax_payable:
        rec, sav = "new", old.tax_payable - new.tax_payable
    else:
        rec, sav = tie, D(0)
    return TaxComputation(engine=ENGINE_NAME, status=EngineStatus.OK, financial_year=p.financial_year,
                          rule_set_id=f"COMPILED-FY{p.financial_year}", rule_set_version=version, old=old, new=new,
                          recommended_regime=rec, savings=sav,
                          llm_meta={"mode": "compiled_no_llm_at_runtime",
                                    "compiler_model": art.get("compiler", {}).get("model"),
                                    "latency_ms": (time.perf_counter() - t0) * 1000})


def compute(p: TaxpayerProfile) -> TaxComputation:
    return run_artifact(load_artifact(p.financial_year), p)
