"""Gemini client for Backlog Buster: disk cache, throttling, retries and a daily request cap.

Why this exists: the free tier allows only ~10 requests/minute and ~1,000/day, and an evaluation
re-runs the same tickets many times while the prompt is being tuned. So every request goes through
one place that:

* CACHES to disk. The cache key is a hash of the whole request (model + system prompt + prompt +
  schema), so an edited prompt can never return a stale answer, and an unchanged one is free.
* THROTTLES to LLM_REQUESTS_PER_MINUTE by sleeping between real API calls.
* RETRIES with exponential backoff on 429 / 5xx / timeouts / unparseable JSON.
* STOPS at LLM_DAILY_REQUEST_CAP (counted from data/llm_calls.jsonl, so it survives restarts)
  and raises DailyCapReached instead of failing halfway through a run.

Only SYNTHETIC ticket text may be sent (free-tier data can be used by the provider).
The LLM understands and writes; it never decides what is allowed (that is code, elsewhere).

Self-test (no network, no quota):  python src/agent/llm.py
One real call to check your key:   python src/agent/llm.py --live
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # so `import config` works from src/agent/

import config as C  # noqa: E402

RETRYABLE_CODES = {429, 500, 502, 503, 504}
BACKOFF_BASE_S = 5.0
BACKOFF_MAX_S = 60.0


class LLMError(RuntimeError):
    """A request failed for good (after retries, or with a non-retryable error)."""


class DailyCapReached(LLMError):
    """The daily request cap was hit; no call was made."""


class LLMClient:
    """Sends one request at a time to Gemini and returns the parsed JSON answer."""

    def __init__(self, *, model: str | None = None, cache_path: Path | None = None,
                 call_log_path: Path | None = None, requests_per_minute: float | None = None,
                 daily_cap: int | None = None, max_attempts: int | None = None, client: Any = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self.model = model or C.LLM_MODEL
        self.cache_path = Path(cache_path or C.LLM_CACHE_PATH)
        self.call_log_path = Path(call_log_path or C.LLM_CALL_LOG_PATH)
        self.min_gap_s = 60.0 / (requests_per_minute or C.LLM_REQUESTS_PER_MINUTE)
        self.daily_cap = daily_cap or C.LLM_DAILY_REQUEST_CAP
        self.max_attempts = max_attempts or C.LLM_MAX_ATTEMPTS
        self._client = client
        self._sleep, self._clock, self._now = sleep, clock, now
        self._last_call_at: float | None = None
        self.stats = {"cache_hits": 0, "api_calls": 0, "retries": 0}
        self._cache = self._load_cache()
        self._day, self._calls_today = self._now().date().isoformat(), self._count_calls(self._now().date().isoformat())

    # ------------------------------------------------------------ public
    def cache_key(self, prompt: str, system: str | None, schema: dict | None) -> str:
        payload = json.dumps({"model": self.model, "system": system, "prompt": prompt, "schema": schema,
                              "temperature": 0}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def generate_json(self, prompt: str, *, system: str | None = None, schema: dict | None = None,
                      label: str = "") -> dict[str, Any]:
        """Return the model's JSON answer for `prompt`, from the cache if this exact request was seen.

        `label` (e.g. a ticket id or prompt version) is stored next to the cached answer for humans;
        it is not part of the key.
        """
        key = self.cache_key(prompt, system, schema)
        hit = self._cache.get(key)
        if hit is not None:
            self.stats["cache_hits"] += 1
            return hit["response"]
        response, usage = self._call_with_retries(prompt, system, schema)
        record = {"key": key, "model": self.model, "label": label, "created_at": self._now().isoformat(timespec="seconds"),
                  "usage": usage, "response": response}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._cache[key] = record
        return response

    @property
    def calls_today(self) -> int:
        return self._calls_today

    # ------------------------------------------------------------ internals
    def _load_cache(self) -> dict[str, dict]:
        cache: dict[str, dict] = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                    cache[record["key"]] = record
                except (json.JSONDecodeError, KeyError):
                    continue   # a half-written last line must not break the run
        return cache

    def _count_calls(self, day: str) -> int:
        if not self.call_log_path.exists():
            return 0
        return sum(1 for line in self.call_log_path.read_text(encoding="utf-8").splitlines()
                   if f'"day": "{day}"' in line)

    def _reserve_call(self) -> None:
        """Enforce the daily cap and the per-minute rate, then record the attempt."""
        today = self._now().date().isoformat()
        if today != self._day:
            self._day, self._calls_today = today, self._count_calls(today)
        if self._calls_today >= self.daily_cap:
            raise DailyCapReached(f"daily cap of {self.daily_cap} requests reached; try again tomorrow "
                                  "or raise LLM_DAILY_REQUEST_CAP if your quota allows")
        if self._last_call_at is not None:
            wait = self._last_call_at + self.min_gap_s - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_call_at = self._clock()
        self._calls_today += 1
        self.call_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.call_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"day": today, "ts": self._now().isoformat(timespec="seconds"), "model": self.model}) + "\n")

    def _get_client(self) -> Any:
        if self._client is None:
            if not C.GEMINI_API_KEY or C.GEMINI_API_KEY.startswith("your-"):
                raise LLMError("GEMINI_API_KEY is not set. Put your key in .env (see .env.example).")
            from google import genai
            self._client = genai.Client(api_key=C.GEMINI_API_KEY)
        return self._client

    def _call_with_retries(self, prompt: str, system: str | None, schema: dict | None) -> tuple[dict, dict]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._reserve_call()
            self.stats["api_calls"] += 1
            try:
                response = self._get_client().models.generate_content(
                    model=self.model, contents=prompt, config=self._config(system, schema))
                parsed = json.loads(response.text)
                if not isinstance(parsed, dict):
                    raise ValueError("model did not return a JSON object")
                return parsed, self._usage(response)
            except DailyCapReached:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                last_error = exc
                retryable = (getattr(exc, "code", None) in RETRYABLE_CODES
                             or isinstance(exc, (TimeoutError, ConnectionError, ValueError)))   # ValueError covers bad JSON
                if not retryable or attempt == self.max_attempts:
                    raise LLMError(f"request failed after {attempt} attempt(s): {type(exc).__name__}: {exc}") from exc
                self.stats["retries"] += 1
                self._sleep(min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (attempt - 1)))
        raise LLMError(f"request failed: {last_error}")   # unreachable, keeps type checkers happy

    @staticmethod
    def _config(system: str | None, schema: dict | None) -> Any:
        from google.genai import types
        kwargs: dict[str, Any] = {"temperature": 0, "response_mime_type": "application/json",
                                  "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True)}
        if system:
            kwargs["system_instruction"] = system
        if schema:
            kwargs["response_json_schema"] = schema
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _usage(response: Any) -> dict[str, int]:
        meta = getattr(response, "usage_metadata", None)
        return {"prompt_tokens": getattr(meta, "prompt_token_count", 0) or 0,
                "output_tokens": getattr(meta, "candidates_token_count", 0) or 0}


# ---------------------------------------------------------------- tests
class _FakeAPIError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"fake API error {code}")
        self.code = code


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text, self.usage_metadata = text, None


class _FakeGemini:
    """Stands in for genai.Client: plays back a script of answers/errors and counts calls."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script, self.calls = list(script or []), 0
        self.models = self

    def generate_content(self, *, model: str, contents: str, config: Any) -> _FakeResponse:
        self.calls += 1
        step = self.script.pop(0) if self.script else json.dumps({"echo": contents})
        if isinstance(step, Exception):
            raise step
        return _FakeResponse(step)


def _self_test() -> None:
    class FakeTime:
        def __init__(self) -> None:
            self.t, self.sleeps = 1000.0, []

        def sleep(self, s: float) -> None:
            self.sleeps.append(s)
            self.t += s

        def clock(self) -> float:
            return self.t

    with tempfile.TemporaryDirectory() as tmp:
        cache, log = Path(tmp) / "cache.jsonl", Path(tmp) / "calls.jsonl"

        def make(fake: _FakeGemini, ft: FakeTime | None = None, **kw: Any) -> LLMClient:
            ft = ft or FakeTime()
            return LLMClient(cache_path=cache, call_log_path=log, client=fake, sleep=ft.sleep, clock=ft.clock, **kw)

        fake = _FakeGemini(['{"a": 1}'])
        c = make(fake)
        assert c.generate_json("hello") == {"a": 1} and fake.calls == 1
        assert c.generate_json("hello") == {"a": 1} and fake.calls == 1 and c.stats["cache_hits"] == 1
        print("ok  identical request is served from the cache (1 API call for 2 requests)")

        fake2 = _FakeGemini()
        c2 = make(fake2)
        assert c2.generate_json("hello") == {"a": 1} and fake2.calls == 0
        print("ok  cache survives a restart (new client, same file, 0 API calls)")

        c2.generate_json("hello, edited prompt")
        c2.generate_json("hello", system="different system prompt")
        assert fake2.calls == 2
        print("ok  editing the prompt or system prompt invalidates the cache automatically")

        ft = FakeTime()
        c3 = make(_FakeGemini(), ft, requests_per_minute=10)
        start = ft.t
        for i in range(4):
            c3.generate_json(f"throttle {i}")
        assert ft.t - start >= 3 * 6.0 - 1e-9, ft.t - start
        print(f"ok  throttle: 4 calls at 10/min took {ft.t - start:.0f}s of (fake) time, >= 18s")

        ft = FakeTime()
        c4 = make(_FakeGemini([_FakeAPIError(429), '{"ok": true}']), ft)
        assert c4.generate_json("retry me") == {"ok": True}
        assert BACKOFF_BASE_S in ft.sleeps and c4.stats["retries"] == 1
        print("ok  429 -> waits and retries -> succeeds")

        c5 = make(_FakeGemini(["not json at all", '{"fixed": true}']), FakeTime())
        assert c5.generate_json("bad json first") == {"fixed": True}
        print("ok  unparseable answer -> retried, and the bad answer is not cached")

        fake6 = _FakeGemini([_FakeAPIError(400)])
        try:
            make(fake6, FakeTime()).generate_json("bad request")
            raise AssertionError("expected LLMError")
        except LLMError:
            assert fake6.calls == 1
        print("ok  non-retryable error (400) fails immediately, no retry")

        fake7 = _FakeGemini([_FakeAPIError(503)] * 10)
        try:
            make(fake7, FakeTime(), max_attempts=3).generate_json("always down")
            raise AssertionError("expected LLMError")
        except LLMError:
            assert fake7.calls == 3
        print("ok  gives up after max_attempts (3) and raises LLMError")

        log.unlink()
        capped = make(_FakeGemini(), FakeTime(), daily_cap=2)
        capped.generate_json("cap 1")
        capped.generate_json("cap 2")
        fake8 = _FakeGemini()
        try:
            make(fake8, FakeTime(), daily_cap=2).generate_json("cap 3")
            raise AssertionError("expected DailyCapReached")
        except DailyCapReached:
            assert fake8.calls == 0
        assert capped.generate_json("cap 1") == {"echo": "cap 1"}   # cached answers stay free at the cap
        print("ok  daily cap: stops before the 3rd call, persists across restarts, cached answers still work")

    print("llm.py self-test passed (no real API calls were made).")


def _live_test() -> None:
    schema = {"type": "object", "properties": {"language": {"type": "string"}}, "required": ["language"]}
    client = LLMClient()
    prompt = 'What language is this sentence? Answer with JSON {"language": "..."}: "parcel tak sampai lagi dah 5 hari boss"'
    answer = client.generate_json(prompt, schema=schema, label="live-test")
    print(f"model {client.model} answered: {answer}")
    print(f"real API calls this run: {client.stats['api_calls']}, calls today: {client.calls_today}")
    client.generate_json(prompt, schema=schema, label="live-test")
    print(f"asked again -> cache hits: {client.stats['cache_hits']} (no second API call)")


if __name__ == "__main__":
    _live_test() if "--live" in sys.argv else _self_test()
