"""Build and verify the Section 80D per-person CANDIDATE artifacts (never approves them).

The previously approved artifacts judged s.80D(2)(c)/(d) medical expenditure and the s.80D(4) Rs 50,000 limit at
group level. Human reviews (2026-11-09) asked for (1) the first proviso per person and (2) the 50,000 premium limit
only where the premium is for an insured senior. This script replaces ONLY the Section 80D block of the old regime
of each year's approved artifact, then runs static validation and the full test suite, and writes a manifest with
fingerprints. Deterministic: rerunning it
reproduces byte-identical candidates. Approval stays a human step:
    python -m project_a.cli compile approve --candidate <file> --fy <year> --reviewer "<name>"
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from project_a.rule_compiler import (artifact_path, build_prompt, diff_artifacts, sha256_of,  # noqa: E402
                                     validate_artifact, verify_against_suite)

CAND = ROOT / "project_a/compiled_rules/candidates"
MED_A = ("(medical_expenditure_self if (age >= 60 and not self_insured) else 0)"
         " + (medical_expenditure_spouse if (spouse_is_senior and not spouse_insured) else 0)"
         " + (medical_expenditure_self_family if (self_family_senior and health_insurance_self_family == 0) else 0)")
MED_B = ("(medical_expenditure_parent_a if (parent_a_is_senior and not parent_a_insured) else 0)"
         " + (medical_expenditure_parent_b if (parent_b_is_senior and not parent_b_insured) else 0)"
         " + (medical_expenditure_parents if (parents_are_senior and health_insurance_parents == 0) else 0)")


def _steps_80d(pid: str) -> list[dict]:
    """The Section 80D block (old regime). s.80D(4): the 50,000 premium limit needs an INSURED senior; first
    proviso per person; second proviso (a)+(c), (b)+(d) <= 50,000; (2A) preventive 5,000 in aggregate."""
    def st(sid, out, expr, clause, line=None):
        return {"id": sid, "output": out, "expr": expr, "clause": clause, "passage_id": pid, "line": line}
    return [
        st("OLD.80D.SELF_SENIOR", "self_family_senior", "age >= 60 or spouse_is_senior", "Section 80D(4) (senior in group)"),
        st("OLD.80D.CAP_A", "cap_80d_self", "50000 if insured_senior_self_family else 25000",
           "Section 80D(2)(a), (4): 50,000 only for insurance on the health of a senior citizen"),
        st("OLD.80D.CAP_B", "cap_80d_parents", "50000 if insured_senior_parents else 25000",
           "Section 80D(2)(b), (4): 50,000 only for insurance on the health of a senior citizen"),
        st("OLD.80D.MED_A", "med_self_allowed", MED_A,
           "Section 80D(2)(c), first proviso (per person: senior citizen, no insurance on such person)"),
        st("OLD.80D.MED_B", "med_parents_allowed", MED_B,
           "Section 80D(2)(d), first proviso (per person: senior parent, no insurance on such person)"),
        st("OLD.80D.PREM_A", "prem_80d_self", "min(health_insurance_self_family, cap_80d_self)", "Section 80D(2)(a), (4)"),
        st("OLD.80D.PREM_B", "prem_80d_parents", "min(health_insurance_parents, cap_80d_parents)", "Section 80D(2)(b), (4)"),
        st("OLD.80D.MEDDED_A", "med_80d_self", "min(med_self_allowed, 50000 - prem_80d_self)",
           "Section 80D(2)(c), second proviso ((a)+(c) <= 50,000)"),
        st("OLD.80D.MEDDED_B", "med_80d_parents", "min(med_parents_allowed, 50000 - prem_80d_parents)",
           "Section 80D(2)(d), second proviso ((b)+(d) <= 50,000)"),
        st("OLD.80D.ROOM_A", "room_80d_self",
           "max(0, min(cap_80d_self - prem_80d_self, 50000 - prem_80d_self - med_80d_self))",
           "Section 80D(2)(a) limit and second proviso (room left for preventive check-up)"),
        st("OLD.80D.ROOM_B", "room_80d_parents",
           "max(0, min(cap_80d_parents - prem_80d_parents, 50000 - prem_80d_parents - med_80d_parents))",
           "Section 80D(2)(b) limit and second proviso (room left for preventive check-up)"),
        st("OLD.80D.PREV_A", "prev_self", "min(preventive_checkup_self_family, room_80d_self, 5000)",
           "Section 80D(2)(a), (2A)"),
        st("OLD.80D.PREV_B", "prev_parents", "min(preventive_checkup_parents, room_80d_parents, 5000 - prev_self)",
           "Section 80D(2)(b), (2A)"),
        st("OLD.80D", "ded_80d", "prem_80d_self + med_80d_self + prev_self + prem_80d_parents + med_80d_parents + "
           "prev_parents", "Section 80D", "Section 80D"),
    ]


def candidate_path(fy: str) -> Path:
    return CAND / f"fy{fy.replace('-', '_')}_80d_per_person.candidate.json"


def build(fy: str, base: dict) -> dict:
    cand = {k: copy.deepcopy(v) for k, v in base.items() if k != "review"}
    steps = cand["regimes"]["old"]["steps"]
    idx = [i for i, st in enumerate(steps) if st["id"] == "OLD.80D" or st["id"].startswith("OLD.80D.")]
    pid = steps[idx[0]]["passage_id"]
    steps[idx[0]:idx[-1] + 1] = _steps_80d(pid)  # the 80D block is contiguous in the approved artifact
    cand["compiler"] = {
        "model": "claude-opus-5-5 (Claude Code)", "mode": "llm_authored_reviewed_diff",
        "note": ("Corrections requested in the human reviews of 2026-11-09: (1) medical expenditure under "
                 "s.80D(2)(c)/(d) is judged per person (first proviso, 'such person'); (2) the 50,000 premium limit of "
                 "s.80D(4) applies only where the premium is paid for an insured senior citizen. Only the Section 80D "
                 "block of the old regime differs from the previously approved artifact. Not compiled by Gemini: the "
                 "free-tier daily quota was exhausted on 2026-11-09 and earlier Gemini compiles failed the test gate."),
        "prompt_file": f"compile_prompt_fy{fy.replace('-', '_')}.txt",
        "based_on_sha256": base["review"]["artifact_sha256"]}
    return cand


def main() -> int:
    man_path = CAND / "80d_per_person_manifest.json"
    if man_path.exists():
        prev = json.loads(man_path.read_text())
        if all(json.loads(artifact_path(fy).read_text())["review"]["artifact_sha256"] == c["fingerprint_sha256"]
               for fy, c in prev["candidates"].items()):
            print("the 80D candidates are already approved (approved artifacts == candidate fingerprints); nothing to "
                  "build. Approval record: 80d_per_person_manifest.json")
            return 0
    manifest = {"purpose": "Section 80D: first proviso per person and s.80D(4) limit per insured senior "
                           "(human reviews 2026-11-09)",
                "status": "CANDIDATES - awaiting human review; NOT approved, NOT signed", "candidates": {}}
    ok = True
    for fy in ("2024-25", "2025-26"):
        # the compile prompt lists the input schema, now including the per-person 80D fields
        (ROOT / f"project_a/compiled_rules/compile_prompt_fy{fy.replace('-', '_')}.txt").write_text(
            build_prompt(fy), encoding="utf-8")
        path = candidate_path(fy)
        if path.exists():  # rebuild from the approved base recorded in the existing candidate (idempotent)
            base_sha = json.loads(path.read_text())["compiler"]["based_on_sha256"]
        base = json.loads(artifact_path(fy).read_text())
        if path.exists() and base["review"]["artifact_sha256"] != base_sha:
            print(f"{fy}: approved artifact changed since the candidate was built; rebuilding from the new one")
        cand = build(fy, base)
        path.write_text(json.dumps(cand, indent=1), encoding="utf-8")
        problems = validate_artifact(cand, fy)
        ver = verify_against_suite(cand, fy)
        fp = sha256_of({k: v for k, v in cand.items() if k != "review"})
        ok &= not problems and not ver["failed"]
        manifest["candidates"][fy] = {
            "path": str(path.relative_to(ROOT)), "fingerprint_sha256": fp, "static_validation_problems": problems,
            "suite": {k: ver[k] for k in ("cases", "passed", "failed")}, "first_failures": ver["failures"][:5],
            "diff_vs_approved": diff_artifacts(base, cand)}
        print(f"{fy}: {path.name} sha256={fp} validation={len(problems)} problems, "
              f"suite {ver['passed']}/{ver['cases']} cases pass ({ver['failed']} regime failures)")
    (CAND / "80d_per_person_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
