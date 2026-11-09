"""Gemini client with timeouts, retry/backoff, pacing, JSON validation, cost logging and caching.

Modes (always recorded on every response, so reports can never mix them up):

* ``real``        — an actual Gemini API call was made in this run.
* ``cached``      — a real Gemini response replayed from ``logs/llm_cache/`` (same model + prompt).
* ``mock``        — a scripted response from :class:`MockLLMClient` (unit tests only).
* ``unavailable`` — no API key / offline. **No content is fabricated**: callers receive
  :class:`LLMUnavailable` and must report the step as not measured.

The API key is read from the ``GEMINI_API_KEY`` environment variable (or ``.env``); it is never
logged or written anywhere.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from common.config import env, settings
from common.logging_utils import get_logger, log_event
from common.paths import LOG_DIR

log = get_logger("llm.client")
CACHE_DIR = LOG_DIR / "llm_cache"


class LLMUnavailable(RuntimeError):
    """Raised when no real LLM can be called (missing key, SDK missing, or offline)."""


class LLMResponseError(RuntimeError):
    """The model answered but the answer was not valid JSON / failed validation."""


@dataclass
class LLMResponse:
    text: str
    parsed: Any
    model: str
    mode: str                       # real | cached | mock
    input_tokens: int = 0
    output_tokens: int = 0          # includes thinking tokens (billed as output)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    attempts: int = 1
    meta: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("text"), d.pop("parsed")
        return d


def _llm_cfg() -> dict[str, Any]:
    return settings()["llm"]


def model_name() -> str:
    return env("GEMINI_MODEL") or _llm_cfg()["default_model"]


def price_for(model: str) -> dict[str, float]:
    table = _llm_cfg()["pricing_usd_per_million_tokens"]
    if model in table:
        return table[model]
    for k, v in table.items():  # e.g. "gemini-2.5-flash-001" -> "gemini-2.5-flash"
        if model.startswith(k):
            return v
    return table[_llm_cfg()["default_model"]]


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p = price_for(model)
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1e6


def estimate_tokens(text: str) -> int:
    """chars/4 approximation, used only for cost *projections* when no real usage data exists."""
    return int(len(text) / _llm_cfg()["chars_per_token_estimate"]) + 1


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> Any:
    """Parse a JSON object from model text (tolerates ```json fences, nothing else)."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        raise LLMResponseError(f"invalid JSON from model: {exc}; head={cleaned[:200]!r}") from exc


class GeminiClient:
    """Thin wrapper over ``google-genai`` adding the reliability features the assignment requires."""

    _lock = threading.Lock()
    _last_call = 0.0

    def __init__(self, model: str | None = None, use_cache: bool = True) -> None:
        key = env("GEMINI_API_KEY")
        if not key:
            raise LLMUnavailable("GEMINI_API_KEY is not set; LLM features run in 'unavailable' mode")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailable("google-genai is not installed") from exc
        cfg = _llm_cfg()
        self._types = types
        self._client = genai.Client(api_key=key,
                                    http_options=types.HttpOptions(timeout=int(cfg["timeout_seconds"] * 1000)))
        self.model = model or model_name()
        self.use_cache = use_cache
        self.cfg = cfg

    # -- pacing for the free tier ----------------------------------------------------------
    def _pace(self) -> None:
        with GeminiClient._lock:
            wait = self.cfg["min_interval_seconds"] - (time.monotonic() - GeminiClient._last_call)
            if wait > 0:
                time.sleep(wait)
            GeminiClient._last_call = time.monotonic()

    def _cache_path(self, system: str, prompt: str):
        h = hashlib.sha256(f"{self.model}\n{system}\n{prompt}".encode()).hexdigest()[:32]
        return CACHE_DIR / f"{h}.json"

    def generate_json(self, prompt: str, system: str = "", validator: Callable[[Any], Any] | None = None,
                      use_cache: bool | None = None) -> LLMResponse:
        """Call Gemini expecting a JSON object. Retries on 429/5xx/timeouts and on invalid JSON once."""
        use_cache = self.use_cache if use_cache is None else use_cache
        cpath = self._cache_path(system, prompt)
        if use_cache and cpath.exists():
            data = json.loads(cpath.read_text())
            resp = LLMResponse(**{**data, "mode": "cached"})
            if validator:
                resp.parsed = validator(resp.parsed)
            return resp

        types = self._types
        config = types.GenerateContentConfig(system_instruction=system or None,
                                             temperature=self.cfg["temperature"],
                                             response_mime_type="application/json")
        last_err: Exception | None = None
        for attempt in range(1, self.cfg["max_retries"] + 1):
            self._pace()
            t0 = time.perf_counter()
            try:
                raw = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
                latency = (time.perf_counter() - t0) * 1000
                text = raw.text or ""
                um = raw.usage_metadata
                in_tok = int(getattr(um, "prompt_token_count", 0) or 0)
                out_tok = int(getattr(um, "candidates_token_count", 0) or 0) + int(
                    getattr(um, "thoughts_token_count", 0) or 0)
                parsed = parse_json(text)
                if validator:
                    parsed = validator(parsed)
                resp = LLMResponse(text=text, parsed=parsed, model=self.model, mode="real", input_tokens=in_tok,
                                   output_tokens=out_tok, latency_ms=latency,
                                   cost_usd=cost_usd(self.model, in_tok, out_tok), attempts=attempt)
                log_event(log, "llm call", **resp.summary())
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cpath.write_text(json.dumps({**asdict(resp)}, default=str))
                return resp
            except LLMResponseError as exc:
                last_err = exc
                log_event(log, "invalid llm response", attempt=attempt, error=str(exc)[:300])
            except Exception as exc:  # noqa: BLE001 - classify below
                last_err = exc
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                retryable = code in (429, 500, 502, 503, 504) or "timeout" in str(exc).lower() or code is None
                log_event(log, "llm call failed", attempt=attempt, code=code, error=str(exc)[:300])
                if not retryable:
                    break
            time.sleep(self.cfg["backoff_base_seconds"] * 2 ** (attempt - 1))
        raise LLMResponseError(f"Gemini call failed after retries: {last_err}")


class MockLLMClient:
    """Scripted client for unit tests. Its responses are labelled ``mode='mock'`` everywhere."""

    def __init__(self, responses: list[Any] | Callable[[str], Any], model: str = "mock-model") -> None:
        self._responses = responses
        self.model = model
        self.calls: list[str] = []

    def generate_json(self, prompt: str, system: str = "", validator: Callable[[Any], Any] | None = None,
                      use_cache: bool | None = None) -> LLMResponse:
        self.calls.append(prompt)
        if callable(self._responses):
            item = self._responses(prompt)
        else:
            item = self._responses[min(len(self.calls), len(self._responses)) - 1]
        text = item if isinstance(item, str) else json.dumps(item)
        parsed = parse_json(text)
        if validator:
            parsed = validator(parsed)
        return LLMResponse(text=text, parsed=parsed, model=self.model, mode="mock",
                           input_tokens=estimate_tokens(system + prompt), output_tokens=estimate_tokens(text))


def get_client(use_cache: bool = True) -> GeminiClient:
    """Return a real Gemini client or raise :class:`LLMUnavailable` (never silently mock)."""
    return GeminiClient(use_cache=use_cache)


def llm_status() -> dict[str, Any]:
    """Describe LLM availability without revealing the key."""
    has_key = bool(env("GEMINI_API_KEY"))
    try:
        from google import genai  # noqa: F401  (import check only)
        sdk = True
    except ImportError:  # pragma: no cover
        sdk = False
    return {"gemini_api_key_present": has_key, "sdk_installed": sdk, "model": model_name(),
            "mode": "real" if (has_key and sdk) else "unavailable"}
