PY ?= python

.PHONY: all install test test-a test-b project-a project-b benchmarks reports validate clean-generated

all:            ## reproduce every number (downloads NYC data once, then cached)
	$(PY) run_all.py

install:
	$(PY) -m pip install -r requirements.txt

test: test-a test-b
test-a:
	$(PY) -m pytest project_a/tests
test-b:
	$(PY) -m pytest project_b/tests

project-a:      ## Project A only: tests, benchmark, migration report
	$(PY) scripts/generate_tax_tests.py
	$(PY) -m pytest project_a/tests
	$(PY) scripts/run_benchmarks.py --a-only

project-b:      ## Project B only: data, profile, benchmark, hybrid
	$(PY) -m project_b.cli download
	$(PY) -m project_b.cli profile
	$(PY) -m pytest project_b/tests
	$(PY) scripts/run_benchmarks.py --b-only

reports:
	$(PY) scripts/generate_reports.py

validate:
	$(PY) scripts/validate_submission.py
