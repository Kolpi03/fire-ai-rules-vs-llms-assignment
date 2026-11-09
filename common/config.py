"""YAML configuration loading (cached) and .env handling."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from common.paths import CONFIG_DIR, ROOT

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


@lru_cache(maxsize=None)
def load_yaml(name: str) -> dict[str, Any]:
    path = Path(name) if Path(name).is_absolute() else CONFIG_DIR / name
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def settings() -> dict[str, Any]:
    return load_yaml("settings.yaml")


def env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default
