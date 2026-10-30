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
from dataclasses import asdict, dataclass, field
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


class LLM:
    def __init__(self, telemetry: Telemetry | None = None):
        self.base_url = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
        self.model = os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b")
        self.provider = os.environ.get("LLM_PROVIDER", "").strip()
        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=os.environ.get("LLM_API_KEY", "missing"),
            timeout=600,
            max_retries=0,
        )
        self.sem = asyncio.Semaphore(int(os.environ.get("LLM_CONCURRENCY", "6")))
        self.telemetry = telemetry or Telemetry()
        self.structured_supported: bool | None = None

    def _extra(self, think: bool) -> dict:
        extra: dict = {}
        if "openrouter.ai" in self.base_url:
            extra["reasoning"] = {"enabled": think}
            if self.provider:
                extra["provider"] = {"only": [self.provider], "allow_fallbacks": False}
        else:
            # vLLM / LiteLLM in front of Qwen3: toggle the thinking chat template
            extra["chat_template_kwargs"] = {"enable_thinking": think}
        return extra

    async def _call(self, stage: str, attempt: int, messages: list[dict], *,
                    think: bool, max_tokens: int, response_format: dict | None) -> str:
        t0 = time.perf_counter()
        kwargs: dict = dict(model=self.model, messages=messages, max_tokens=max_tokens,
                            temperature=0.2 if not think else 0.6,
                            extra_body=self._extra(think))
        if response_format:
            kwargs["response_format"] = response_format
        resp = None
        for transport_try in range(6):
            try:
                async with self.sem:
                    resp = await self.client.chat.completions.create(**kwargs)
                break
            except APIStatusError as e:  # rate limits / provider hiccups: back off, same request
                self.telemetry.add(CallRecord(stage, attempt, 0, 0, 0, round(time.perf_counter() - t0, 2),
                                              False, f"{e.status_code}: {str(e)[:200]}"))
                if e.status_code not in (429, 500, 502, 503, 504) or transport_try == 5:
                    raise
                if transport_try >= 1 and "provider" in kwargs["extra_body"]:
                    kwargs["extra_body"]["provider"] = {"order": [self.provider], "allow_fallbacks": True}
                await asyncio.sleep(min(2 ** transport_try, 20))
            except (APIConnectionError, APITimeoutError) as e:  # network blips: retry the same request
                self.telemetry.add(CallRecord(stage, attempt, 0, 0, 0, round(time.perf_counter() - t0, 2),
                                              False, f"connection: {str(e)[:200]}"))
                if transport_try == 5:
                    raise
                await asyncio.sleep(min(2 ** transport_try, 20))
            except Exception as e:  # recorded, then re-raised for the caller to decide
                self.telemetry.add(CallRecord(stage, attempt, 0, 0, 0,
                                              time.perf_counter() - t0, False, str(e)[:300]))
                raise
        assert resp is not None
        u = resp.usage
        details = getattr(u, "completion_tokens_details", None)
        self.telemetry.add(CallRecord(
            stage, attempt,
            getattr(u, "prompt_tokens", 0) or 0,
            getattr(u, "completion_tokens", 0) or 0,
            (getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
            round(time.perf_counter() - t0, 2), True,
        ))
        return resp.choices[0].message.content or ""

    async def text(self, stage: str, system: str, user: str, *, think: bool = False,
                   max_tokens: int = 4000) -> str:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return strip_think(await self._call(stage, 1, msgs, think=think,
                                            max_tokens=max_tokens, response_format=None))

    async def structured(self, stage: str, system: str, user: str, model: type[T], *,
                         think: bool = False, max_tokens: int = 16000,
                         max_attempts: int = 2) -> T:
        """Ask for JSON matching `model`. Uses provider-side JSON schema when available,
        otherwise schema-in-prompt. One targeted repair attempt on validation errors."""
        schema = model.model_json_schema()
        rf = {"type": "json_schema",
              "json_schema": {"name": model.__name__, "schema": schema, "strict": False}}
        sys = (system + "\n\nОтвечай ТОЛЬКО валидным JSON без пояснений и без markdown. "
               "JSON должен соответствовать схеме:\n" + json.dumps(schema, ensure_ascii=False))
        msgs = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            use_rf = rf if self.structured_supported is not False else None
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
            if use_rf and self.structured_supported is None:
                self.structured_supported = True
            try:
                return model.model_validate(extract_json(raw))
            except (ValidationError, ValueError) as e:
                last_err = str(e)[:4000]
                msgs = msgs + [
                    {"role": "assistant", "content": raw[:20000]},
                    {"role": "user", "content": "JSON не прошёл проверку схемы. Исправь только "
                     "указанные ошибки и верни полный JSON целиком.\nОшибки:\n" + last_err},
                ]
        raise RuntimeError(f"{stage}: structured output failed: {last_err}")


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
