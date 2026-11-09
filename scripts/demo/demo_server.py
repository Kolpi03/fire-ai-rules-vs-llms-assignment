"""Local browser front-end for recording the demo: runs the project's REAL command-line tools and streams their
real output into a browser terminal. The project has no web UI of its own; nothing here computes or invents a
result — every line shown comes from the subprocess.

Safety: only the fixed, allow-listed STEPS below can be run (no arbitrary commands); the server binds to
127.0.0.1; GEMINI_API_KEY and other tokens are removed from the subprocess environment, so no LLM call is possible.

Usage: python scripts/demo/demo_server.py [--port 8765]   (then open http://127.0.0.1:8765)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable

P24 = {"financial_year": "2024-25", "age": 30, "gross_salary": 1500000, "section_80c": 150000}
P25 = {**P24, "financial_year": "2025-26"}
CHILD_INSURED = {"financial_year": "2025-26", "age": 45, "gross_salary": 1800000, "spouse_is_senior": True,
                 "self_insured": False, "spouse_insured": False, "health_insurance_self_family": 40000}
SENIOR_INSURED = {"financial_year": "2025-26", "age": 45, "gross_salary": 1800000, "spouse_is_senior": True,
                  "self_insured": False, "spouse_insured": True, "health_insurance_self_family": 50000}
SPOUSE_MEDICAL = {"financial_year": "2025-26", "age": 45, "gross_salary": 1800000, "spouse_is_senior": True,
                  "self_insured": False, "spouse_insured": False, "health_insurance_self_family": 20000,
                  "medical_expenditure_spouse": 40000}
AMBIGUOUS = {"financial_year": "2025-26", "age": 70, "gross_salary": 1500000, "health_insurance_self_family": 10000,
             "medical_expenditure_self": 30000}


def calc(profile: dict, engine: str) -> list[str]:
    return [PY, "-m", "project_a.cli", "calc", "--engine", engine, "--json", json.dumps(profile)]


STEPS = [
    ("1. FY 2024-25: old vs new regime (rules engine)",
     "Salaried, age 30, gross Rs 15 lakh, 80C Rs 1.5 lakh. Every line cites its clause and rule id.",
     calc(P24, "rules")),
    ("2. FY 2025-26: same person after the Finance Act 2025",
     "New-regime slabs and the Rs 12 lakh 87A rebate apply; the old regime is unchanged.",
     calc(P25, "rules")),
    ("3. Section 80D: insured NON-senior child, UNINSURED senior spouse",
     "Approved compiled rules (Engine C). s.80D(4): the Rs 50,000 limit needs an insured senior -> premium 40,000 "
     "is limited to 25,000.",
     calc(CHILD_INSURED, "compiled")),
    ("4. Section 80D: the senior spouse IS insured",
     "Premium for an insured senior citizen: the limit is Rs 50,000.",
     calc(SENIOR_INSURED, "compiled")),
    ("5. Section 80D: uninsured senior spouse's medical bills",
     "First proviso, per person: child premium 20,000 + spouse's medical 40,000, capped at 50,000 (second proviso).",
     calc(SPOUSE_MEDICAL, "rules")),
    ("6. Plain language: missing information -> clarifying questions",
     "The parser asks instead of guessing.",
     [PY, "-m", "project_a.cli", "ask", "I earn 15 lakh annually, pay 25k rent in Bangalore, invest 1.5 lakh in 80C "
                                        "and have medical insurance."]),
    ("7. Ambiguous structured input is rejected",
     "Medical bills for a senior while a premium is paid: is that person insured? The engine refuses to guess.",
     calc(AMBIGUOUS, "rules")),
    ("8. Approved compiled rules re-verified against the FY 2025-26 suite",
     "Static validation + every FY 2025-26 test case, both regimes.",
     [PY, "-m", "project_a.cli", "compile", "verify", "--candidate", "project_a/compiled_rules/fy2025_26.compiled.json",
      "--fy", "2025-26"]),
    ("9. Official Income Tax Department calculator comparison (30 cases)",
     "Recomputed offline from the captured portal figures (screenshots in evidence/official_calculator/).",
     [PY, "scripts/official_calculator_compare.py"]),
    ("10. Section 80D test suite",
     "Hand-derived expectations, every Rs 25,000 / 50,000 boundary, mutation tests.",
     [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "project_a/tests/test_80d_per_person.py"]),
    ("11. Final results",
     "Read from the committed result files and a live validator run.",
     [PY, "scripts/demo/final_summary.py"]),
]


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "GEMINI"))}
    env.update(GEMINI_API_KEY="", PYTHONUNBUFFERED="1", COLUMNS="160", NO_COLOR="1")
    return env


def display(argv: list[str]) -> str:
    shown = ["python" if a == PY else a for a in argv]
    return " ".join(f"'{a}'" if (" " in a or "{" in a) else a for a in shown)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, (Path(__file__).with_name("index.html")).read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/steps":
            data = [{"title": t, "caption": c, "command": display(a)} for t, c, a in STEPS]
            self._send(200, json.dumps(data).encode(), "application/json")
        elif self.path.startswith("/run/"):
            try:
                i = int(self.path.rsplit("/", 1)[1])
                argv = STEPS[i][2]
            except (ValueError, IndexError):
                self._send(404, b"unknown step", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            proc = subprocess.Popen(argv, cwd=ROOT, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
            for line in proc.stdout:  # type: ignore[union-attr]
                self.wfile.write(f"data: {json.dumps(line.rstrip(chr(10)))}\n\n".encode())
                self.wfile.flush()
            code = proc.wait()
            self.wfile.write(f"event: done\ndata: {code}\n\n".encode())
            self.wfile.flush()
        else:
            self._send(404, b"not found", "text/plain")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    port = ap.parse_args().port
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
