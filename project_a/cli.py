"""Project A command line.

Examples
  python -m project_a.cli ask "I earn 15 lakh, 30 years old, no investments, no rent"
  python -m project_a.cli calc --json '{"age": 30, "gross_salary": 1500000}' --engine all
  python -m project_a.cli generate-tests
  python -m project_a.cli benchmark [--fy 2024-25]
  python -m project_a.cli compile run --fy 2025-26          # needs GEMINI_API_KEY
  python -m project_a.cli compile verify --candidate PATH --fy 2025-26
  python -m project_a.cli compile approve --candidate PATH --fy 2025-26 --reviewer "Your Name"
  python -m project_a.cli migration-report
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_a import llm_compiled_engine, llm_query_engine, rules_engine  # noqa: E402
from project_a.models import TaxpayerProfile  # noqa: E402
from project_a.natural_language_parser import parse  # noqa: E402
from project_a.tax_explainer import explain  # noqa: E402

ENGINES = {"rules": rules_engine.compute, "compiled": llm_compiled_engine.compute, "llm": llm_query_engine.compute}


def cmd_ask(a: argparse.Namespace) -> int:
    res = parse(a.text, financial_year=a.fy)
    if not res.complete:
        print("I need a bit more information before I can compute your tax:")
        for c in res.clarifications:
            print(f"  - {c.question}")
        if a.verbose:
            print(json.dumps(res.to_dict(), indent=1, default=str))
        return 2
    comp = rules_engine.compute(res.profile)
    print(explain(comp, res))
    if a.verbose:
        print("\nAudit trail:\n  " + "\n  ".join(res.audit))
    return 0


def cmd_calc(a: argparse.Namespace) -> int:
    p = TaxpayerProfile(**json.loads(a.json))
    for name in (ENGINES if a.engine == "all" else [a.engine]):
        try:
            print(explain(ENGINES[name](p)))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            print(f"[{name}] error: {exc}")
        print()
    return 0


def cmd_generate(a: argparse.Namespace) -> int:
    from project_a.test_case_generator import generate
    print(json.dumps(generate(), indent=1))
    return 0


def cmd_benchmark(a: argparse.Namespace) -> int:
    from project_a.benchmark import run
    print(json.dumps(run(fys=a.fy or None), indent=1, default=str)[:4000])
    return 0


def cmd_compile(a: argparse.Namespace) -> int:
    from project_a import rule_compiler as rc
    if a.action == "run":
        out = rc.compile_with_llm(a.fy)
        print(f"candidate written to {out}; now run: compile verify --candidate {out} --fy {a.fy}")
    elif a.action == "verify":
        art = json.loads(Path(a.candidate).read_text())
        print("validation problems:", rc.validate_artifact(art, a.fy))
        print(json.dumps(rc.verify_against_suite(art, a.fy), indent=1))
    elif a.action == "approve":
        print(rc.approve(Path(a.candidate), a.fy, a.reviewer, a.note or []))
    elif a.action == "diff":
        old = json.loads(rc.artifact_path(a.base_fy).read_text())
        new = json.loads(Path(a.candidate).read_text())
        print("\n".join(rc.diff_artifacts(old, new)))
    return 0


def cmd_migration(a: argparse.Namespace) -> int:
    from project_a.law_change_migration import build_report
    print(json.dumps(build_report(), indent=1, default=str)[:6000])
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="project_a", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ask", help="plain-language question -> tax or clarifying questions")
    s.add_argument("text")
    s.add_argument("--fy", default="2024-25")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("calc", help="structured profile JSON -> tax")
    s.add_argument("--json", required=True)
    s.add_argument("--engine", choices=["rules", "compiled", "llm", "all"], default="rules")
    s.set_defaults(fn=cmd_calc)
    s = sub.add_parser("generate-tests")
    s.set_defaults(fn=cmd_generate)
    s = sub.add_parser("benchmark")
    s.add_argument("--fy", action="append")
    s.set_defaults(fn=cmd_benchmark)
    s = sub.add_parser("compile")
    s.add_argument("action", choices=["run", "verify", "approve", "diff"])
    s.add_argument("--fy", default="2024-25")
    s.add_argument("--base-fy", default="2024-25")
    s.add_argument("--candidate")
    s.add_argument("--reviewer", default="")
    s.add_argument("--note", action="append")
    s.set_defaults(fn=cmd_compile)
    s = sub.add_parser("migration-report")
    s.set_defaults(fn=cmd_migration)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
