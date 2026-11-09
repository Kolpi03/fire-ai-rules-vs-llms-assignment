"""Random-profile comparison for the Section 80D candidates: Engine A vs candidate artifact vs reference oracle.

20,000 profiles per financial year in each input mode (group-only 80D inputs, per-person 80D inputs), fixed seeds.
The oracle receives only the fields the profile states (model_dump(exclude_unset=True)), as it does for test cases.
Writes benchmarks/project_a/differential_80d_per_person.json. Usage: python scripts/differential_80d.py [--n 20000]
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from project_a import llm_compiled_engine, rules_engine  # noqa: E402
from project_a.differential import random_profile  # noqa: E402
from project_a.reference_oracle import expected  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    n = ap.parse_args().n
    out: dict = {"profiles_per_run": n, "runs": {}, "branch_coverage": {}}
    bad = 0
    for fy in ("2024-25", "2025-26"):
        art = json.loads((ROOT / f"project_a/compiled_rules/candidates/fy{fy.replace('-', '_')}"
                                 "_80d_per_person.candidate.json").read_text())
        for per_person, seed in ((False, 7), (True, 11)):
            rng = random.Random(seed)
            mis_c = mis_o = 0
            cov: collections.Counter = collections.Counter()
            t0 = time.time()
            for _ in range(n):
                p = random_profile(rng, fy, per_person_80d=per_person)
                a, c = rules_engine.compute(p), llm_compiled_engine.run_artifact(art, p)
                o = expected(p.model_dump(exclude_unset=True))
                if a.old.key_figures() != c.old.key_figures() or a.new.key_figures() != c.new.key_figures():
                    mis_c += 1
                if any(int(getattr(a, r).tax_payable) != o[r]["tax_payable"] or
                       int(getattr(a, r).total_income) != o[r]["total_income"] for r in ("old", "new")):
                    mis_o += 1
                if per_person:
                    ins_sen = (p.age >= 60 and p.self_insured) or (p.spouse_is_senior and p.spouse_insured)
                    if p.health_insurance_self_family > 25000 and (p.age >= 60 or p.spouse_is_senior) and not ins_sen:
                        cov["premium > 25,000, senior present but no insured senior (limit 25,000)"] += 1
                    if p.medical_expenditure_spouse and p.spouse_is_senior and not p.spouse_insured \
                            and p.health_insurance_self_family > 0:
                        cov["uninsured senior spouse's medical while a premium is paid in the group"] += 1
                    if (p.parent_a_is_senior != p.parent_b_is_senior) and p.health_insurance_parents > 0:
                        cov["mixed senior/non-senior parents with a premium"] += 1
                    if p.medical_expenditure_spouse and not p.spouse_is_senior:
                        cov["non-senior spouse's medical (must not count)"] += 1
            key = f"{fy} {'per-person' if per_person else 'group-only'}"
            out["runs"][key] = {"seed": seed, "engineA_vs_candidate_mismatches": mis_c,
                                "engineA_vs_oracle_mismatches": mis_o, "seconds": round(time.time() - t0, 1)}
            if cov:
                out["branch_coverage"][key] = dict(cov)
            bad += mis_c + mis_o
            print(key, out["runs"][key], dict(cov) if cov else "")
    (ROOT / "benchmarks/project_a/differential_80d_per_person.json").write_text(json.dumps(out, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
