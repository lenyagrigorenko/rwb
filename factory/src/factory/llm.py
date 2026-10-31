"""OpenAI-compatible LLM client with structured output and per-call telemetry.

Every call is recorded (tokens, latency, attempts) so the pipeline can report its own
efficiency exactly the way the organizers measure it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass
class CallRecord:
    stage: str
    attempt: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    seconds: float
    ok: bool
    error: str = ""


@dataclass
class Telemetry:
    calls: list[CallRecord] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    log_path: Path | None = None

    def add(self, rec: CallRecord) -> None:
        self.calls.append(rec)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")

    def summary(self) -> dict:
        by_stage: dict[str, dict] = {}
        for c in self.calls:
            if not c.ok:
                continue
            s = by_stage.setdefault(
                c.stage, {"calls": 0, "retries": 0, "prompt_tokens": 0,
                          "completion_tokens": 0, "seconds": 0.0}
            )
            s["calls"] += 1
            s["retries"] += 1 if c.attempt > 1 else 0
            s["prompt_tokens"] += c.prompt_tokens
            s["completion_tokens"] += c.completion_tokens
            s["seconds"] += c.seconds
        ok = [c for c in self.calls if c.ok]
        return {
            "calls": len(ok),
            "retries": sum(1 for c in ok if c.attempt > 1),
            "transport_errors": sum(1 for c in self.calls if not c.ok),
            "prompt_tokens": sum(c.prompt_tokens for c in ok),
            "completion_tokens": sum(c.completion_tokens for c in ok),
            "llm_seconds": round(sum(c.seconds for c in ok), 1),
            "wall_seconds": round(time.time() - self.started, 1),
            "by_stage": by_stage,
        }


def estimate_tokens(text: str) -> int:
    """Rough token count for budgeting (Qwen: ~3 characters per token for Russian text and JSON)."""
    return len(text) // 3 + 1


class RateLimiter:
    """Client-side copy of the provider limits: parallel requests and tokens per minute.

    The hackathon LiteLLM proxy rejects requests over its limits (429) and every rejection is a wasted, counted attempt,
    so requests wait here instead. Limits come from LLM_CONCURRENCY / LLM_TPM and are learned from 429 messages and
    x-ratelimit-* headers. A request reserves prompt + max_tokens; after the answer the reservation shrinks to real usage.
    """

    def __init__(self, parallel: int, tpm: int | None):
        self.parallel = max(1, parallel)
        self.tpm = tpm or None
        self.active = 0
        self.window: deque[list] = deque()  # [timestamp, tokens]
        self.blocked_until = 0.0
        self.cond = asyncio.Condition()

    def _used(self, now: float) -> int:
        while self.window and now - self.window[0][0] > 60:
            self.window.popleft()
        return sum(t for _, t in self.window)

    async def acquire(self, tokens: int) -> list:
        async with self.cond:
            while True:
                now = time.monotonic()
                used = self._used(now)
                wait: float | None = None
                if self.active >= self.parallel:
                    wait = None  # until a running request finishes
                elif now < self.blocked_until:
                    wait = self.blocked_until - now
                elif self.tpm and self.window and used + min(tokens, self.tpm) > self.tpm:
                    wait = self.window[0][0] + 60.5 - now
                else:
                    self.active += 1
                    entry = [now, tokens]
                    self.window.append(entry)
                    return entry
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass

    async def release(self, entry: list, actual_tokens: int | None) -> None:
        async with self.cond:
            self.active -= 1
            if actual_tokens is not None:
                entry[1] = actual_tokens
            self.cond.notify_all()

    async def learn(self, message: str = "", headers=None) -> float:
        """Adopt limits from a 429 message or response headers; returns seconds to wait before retrying."""
        wait = 0.0
        async with self.cond:
            if headers is not None:
                limit = headers.get("x-ratelimit-limit-tokens")
                if limit and limit.isdigit() and int(limit) > 0:
                    self.tpm = int(limit)
                limit = headers.get("x-ratelimit-limit-requests") or headers.get("x-ratelimit-limit-parallel-requests")
                if limit and limit.isdigit() and 0 < int(limit) < self.parallel:
                    self.parallel = int(limit)
            m = re.search(r"Limit type: (\w+)\. Current limit: (\d+)", message)
            if m:
                kind, value = m.group(1), int(m.group(2))
                if kind == "tokens":
                    self.tpm = value
                elif "parallel" in kind:
                    self.parallel = min(self.parallel, max(1, value))
            m = re.search(r"Limit resets at: (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", message)
            if m:
                reset = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                wait = min(max(reset - time.time() + 1, 1.0), 65.0)
            elif message:
                wait = 3.0 if "parallel" in message else 10.0
            self.blocked_until = max(self.blocked_until, time.monotonic() + wait)
            self.cond.notify_all()
        return wait


class Truncated(Exception):
    """The answer hit max_tokens: the JSON is cut off."""

    def __init__(self, raw: str):
        super().__init__("ответ обрезан по max_tokens")
        self.raw = raw


class LLM:
    def __init__(self, telemetry: Telemetry | None = None):
        self.base_url = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
        self.model = os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b")
        self.provider = os.environ.get("LLM_PROVIDER", "").strip()
        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=os.environ.get("LLM_API_KEY", "missing"),
            timeout=float(os.environ.get("LLM_TIMEOUT", "900")),
            max_retries=0,
        )
        self.limiter = RateLimiter(int(os.environ.get("LLM_CONCURRENCY", "3")), int(os.environ.get("LLM_TPM", "0") or 0))
        # Total time a call may wait for rate limits before giving up.
        self.rate_wait = float(os.environ.get("LLM_RATE_WAIT", "900"))
        self.telemetry = telemetry or Telemetry()
        self.structured_supported: bool | None = None
        # LLM_THINKING: auto | template (chat_template_kwargs) | soft (/no_think в промпте) | none
        self.thinking_mode = os.environ.get("LLM_THINKING", "auto").strip().lower()
        if os.environ.get("LLM_STRUCTURED", "auto").strip().lower() == "off":
            self.structured_supported = False

    def _extra(self, think: bool) -> dict:
        extra: dict = {}
        if self.thinking_mode in ("none", "soft"):
            return extra
        if "openrouter.ai" in self.base_url and self.thinking_mode == "auto":
            extra["reasoning"] = {"enabled": think}
            if self.provider:
                extra["provider"] = {"only": [self.provider], "allow_fallbacks": False}
        else:
            # vLLM / LiteLLM in front of Qwen3: toggle the thinking chat template
            extra["chat_template_kwargs"] = {"enable_thinking": think}
        return extra

    async def _call(self, stage: str, attempt: int, messages: list[dict], *,
                    think: bool, max_tokens: int, response_format: dict | None) -> str:
        """One logical request: waits for rate limits, retries transport errors, raises Truncated at max_tokens."""
        t0 = time.perf_counter()
        if self.thinking_mode == "soft" and not think:
            messages = messages[:-1] + [{**messages[-1], "content": messages[-1]["content"] + "\n/no_think"}]
        kwargs: dict = dict(model=self.model, messages=messages, max_tokens=max_tokens,
                            temperature=0.2 if not think else 0.6,
                            extra_body=self._extra(think))
        if response_format:
            kwargs["response_format"] = response_format
        reserve = sum(estimate_tokens(m["content"]) for m in messages) + max_tokens
        if response_format:
            reserve += estimate_tokens(json.dumps(response_format, ensure_ascii=False)) // 2
        resp = None
        transport_try = 0
        deadline = time.monotonic() + self.rate_wait
        while resp is None:
            entry = await self.limiter.acquire(reserve)
            actual: int | None = None
            try:
                raw = await self.client.chat.completions.with_raw_response.create(**kwargs)
                resp = raw.parse()
                await self.limiter.learn(headers=raw.headers)
                actual = (getattr(resp.usage, "total_tokens", 0) or 0) if resp.usage else None
            except APIStatusError as e:
                self.telemetry.add(CallRecord(stage, attempt, 0, 0, 0, round(time.perf_counter() - t0, 2),
                                              False, f"{e.status_code}: {str(e)[:300]}"))
                if e.status_code == 429 and time.monotonic() < deadline:
                    # Лимит провайдера: ждём, сколько он сказал, — это не ошибка запроса, попытки не тратим.
                    actual = 0
                    await self.limiter.learn(str(e))
                    continue
                if e.status_code in (400, 422) and kwargs["extra_body"] and "response_format" not in str(e).lower():
                    # Сервер не принимает дополнительные параметры (например, chat_template_kwargs):
                    # дальше выключаем «размышления» мягким переключателем Qwen3 в тексте запроса.
                    self.thinking_mode = "soft"
                    kwargs["extra_body"] = {}
                    if not think:
                        msgs = kwargs["messages"]
                        kwargs["messages"] = msgs[:-1] + [{**msgs[-1], "content": msgs[-1]["content"] + "\n/no_think"}]
                    continue
                transport_try += 1
                if e.status_code not in (429, 500, 502, 503, 504) or transport_try >= 6:
                    raise
                if transport_try >= 2 and "provider" in kwargs["extra_body"]:
                    kwargs["extra_body"]["provider"] = {"order": [self.provider], "allow_fallbacks": True}
                await asyncio.sleep(min(2 ** transport_try, 20))
            except (APIConnectionError, APITimeoutError) as e:  # network blips: retry the same request
                self.telemetry.add(CallRecord(stage, attempt, 0, 0, 0, round(time.perf_counter() - t0, 2),
                                              False, f"connection: {str(e)[:200]}"))
                transport_try += 1
                if transport_try >= 6:
                    raise
                await asyncio.sleep(min(2 ** transport_try, 20))
            except Exception as e:  # recorded, then re-raised for the caller to decide
                self.telemetry.add(CallRecord(stage, attempt, 0, 0, 0,
                                              round(time.perf_counter() - t0, 2), False, str(e)[:300]))
                raise
            finally:
                await self.limiter.release(entry, actual)
        u = resp.usage
        details = getattr(u, "completion_tokens_details", None)
        self.telemetry.add(CallRecord(
            stage, attempt,
            getattr(u, "prompt_tokens", 0) or 0,
            getattr(u, "completion_tokens", 0) or 0,
            (getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
            round(time.perf_counter() - t0, 2), True,
        ))
        content = resp.choices[0].message.content or ""
        if resp.choices[0].finish_reason == "length":
            raise Truncated(content)
        return content

    async def text(self, stage: str, system: str, user: str, *, think: bool = False,
                   max_tokens: int = 4000) -> str:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            raw = await self._call(stage, 1, msgs, think=think, max_tokens=max_tokens, response_format=None)
        except Truncated as e:
            raw = e.raw
        return strip_think(raw)

    async def structured(self, stage: str, system: str, user: str, model: type[T], *,
                         think: bool = False, max_tokens: int = 8000, max_tokens_cap: int = 16000,
                         max_attempts: int = 3) -> T:
        """Ask for JSON matching `model`. Uses provider-side JSON schema when available, otherwise schema-in-prompt.
        Cut-off answers are retried with a larger budget and a request to be concise; schema violations are sent back
        to the model with the exact errors."""
        schema = compact_schema(model.model_json_schema())
        rf = {"type": "json_schema",
              "json_schema": {"name": model.__name__, "schema": schema, "strict": False}}
        sys = (system + "\n\nОтвечай ТОЛЬКО валидным JSON без пояснений и без markdown. "
               "JSON должен соответствовать схеме:\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
        base = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
        msgs = base
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            use_rf = rf if self.structured_supported is not False else None
            try:
                try:
                    raw = await self._call(stage, attempt, msgs, think=think,
                                           max_tokens=max_tokens, response_format=use_rf)
                except APIStatusError as e:
                    if use_rf and e.status_code in (400, 404, 422):
                        self.structured_supported = False
                        raw = await self._call(stage, attempt, msgs, think=think,
                                               max_tokens=max_tokens, response_format=None)
                    else:
                        raise
            except Truncated:
                # Ответ не поместился: больше бюджета и просьба писать компактнее; исправлять обрывок бессмысленно.
                last_err = f"ответ обрезан на {max_tokens} токенах"
                max_tokens = min(int(max_tokens * 1.6), max_tokens_cap)
                msgs = [base[0], {"role": "user", "content": base[1]["content"] +
                                  "\n\nВАЖНО: предыдущий ответ не поместился в лимит. Пиши компактнее: короткие "
                                  "описания, без повторов, только необходимые элементы."}]
                continue
            if use_rf and self.structured_supported is None:
                self.structured_supported = True
            try:
                return model.model_validate(extract_json(raw))
            except (ValidationError, ValueError) as e:
                last_err = str(e)[:4000]
                msgs = base + [
                    {"role": "assistant", "content": raw[:20000]},
                    {"role": "user", "content": "JSON не прошёл проверку схемы. Исправь только "
                     "указанные ошибки и верни полный JSON целиком.\nОшибки:\n" + last_err},
                ]
        raise RuntimeError(f"{stage}: structured output failed: {last_err}")


def compact_schema(node, props_map: bool = False):
    """JSON schema without auto-generated titles: a third fewer prompt tokens, same constraints and descriptions."""
    if isinstance(node, dict):
        if props_map:
            return {k: compact_schema(v) for k, v in node.items()}
        return {k: compact_schema(v, k in ("properties", "$defs")) for k, v in node.items()
                if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [compact_schema(v) for v in node]
    return node


def strip_think(s: str) -> str:
    return re.sub(r"<think>.*?</think>", "", s, flags=re.S).strip()


def extract_json(s: str):
    s = strip_think(s)
    m = re.search(r"```(?:json)?\s*(.*?)```", s, flags=re.S)
    if m:
        s = m.group(1)
    start = min([i for i in (s.find("{"), s.find("[")) if i != -1], default=-1)
    if start == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(s[start:s.rfind("}" if s[start] == "{" else "]") + 1])
