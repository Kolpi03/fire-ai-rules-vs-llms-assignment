"""Generate project_a/data/tax_test_cases.json and the official-calculator worksheet."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_a.test_case_generator import generate  # noqa: E402

if __name__ == "__main__":
    print(json.dumps(generate(), indent=1))
