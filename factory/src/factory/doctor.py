"""`factory doctor`: readiness check of the host and the LLM endpoint before a live run."""

from __future__ import annotations

import asyncio
import shutil
import socket
import time

from pydantic import BaseModel

from .builder import ANDROID_BUILDER, SDK, run
from .llm import LLM
from .recorder import IMAGE as PLAYWRIGHT_IMAGE

IMAGES = [SDK, "mcr.microsoft.com/dotnet/aspnet:10.0", "postgis/postgis:17-3.5", "node:22-alpine",
          "nginx:1.27-alpine", "apache/kafka:3.9.1", ANDROID_BUILDER, PLAYWRIGHT_IMAGE]


class Probe(BaseModel):
    product: str
    roles: list[str]
    count: int


def line(ok: bool | None, text: str) -> None:
    mark = "✅" if ok else ("⚠️ " if ok is None else "❌")
    print(f"{mark} {text}", flush=True)


async def check_host() -> None:
    print("== Хост")
    code, out = await run(["docker", "version", "--format", "{{.Server.Version}}"])
    line(code == 0, f"Docker: {out.strip() or 'недоступен'}")
    code, out = await run(["docker", "compose", "version", "--short"])
    line(code == 0, f"Docker Compose: {out.strip()}")
    code, out = await run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
    have = set(out.split())
    for img in IMAGES:
        line(img in have or None, f"образ {img}" + ("" if img in have else " — не скачан (скачается при первом прогоне)"))
    from .runtime import low_memory, total_memory_gb
    ram = total_memory_gb()
    swap = 0.0
    try:
        for row in open("/proc/meminfo"):
            if row.startswith("SwapTotal"):
                swap = int(row.split()[1]) / 2**20
    except OSError:
        pass
    line(ram >= 6 or None, f"ОЗУ: {ram:.1f} ГБ, swap: {swap:.1f} ГБ" +
         (" — режим малой памяти: APK собирается после проверок, Kafka 256 МБ, одновременно один стенд" if low_memory() else ""))
    if ram < 6 and swap < 2:
        line(None, "рекомендуется swap 4–8 ГБ (DEPLOY.md, раздел «Мало памяти»)")
    free = shutil.disk_usage("/").free // 2**30
    line(free > 20, f"свободно на диске: {free} ГБ")
    for port in (8000,):
        with socket.socket() as s:
            busy = s.connect_ex(("127.0.0.1", port)) == 0
        line(None if busy else True, f"порт {port} " + ("занят (интерфейс уже запущен?)" if busy else "свободен"))


async def check_llm() -> None:
    print("== Модель")
    llm = LLM()
    print(f"   адрес: {llm.base_url}  модель: {llm.model}  режим размышлений: {llm.thinking_mode}")
    try:
        t0 = time.perf_counter()
        answer = await llm.text("doctor.text", "Отвечай одним словом.", "Столица России?", max_tokens=50)
        line(bool(answer), f"ответ модели: {answer[:40]!r} за {time.perf_counter() - t0:.1f} с")
    except Exception as e:
        line(False, f"модель недоступна: {e}")
        return
    rec = llm.telemetry.calls[-1]
    line(rec.reasoning_tokens == 0 or None, f"режим размышлений выключен (reasoning tokens: {rec.reasoning_tokens}, "
                                            f"фактический режим: {llm.thinking_mode})")
    try:
        probe = await llm.structured("doctor.json", "Извлеки данные.", "Продукт «Автопарк», роли: диспетчер и водитель, 12 машин.",
                                     Probe, max_tokens=300)
        line(True, f"JSON по схеме: {probe.model_dump()} (на стороне сервера: {'да' if llm.structured_supported else 'нет, схема в промпте'})")
    except Exception as e:
        line(False, f"JSON по схеме не получен: {e}")
    t0 = time.perf_counter()
    await llm.text("doctor.speed", "Пиши подробно.", "Опиши архитектуру веб-приложения учёта заявок, 300 слов.", max_tokens=500)
    dt = time.perf_counter() - t0
    tokens = llm.telemetry.calls[-1].completion_tokens
    line(tokens / dt > 20 or None, f"скорость генерации: {tokens / dt:.0f} ток/с ({tokens} токенов за {dt:.1f} с)")
    t0 = time.perf_counter()
    await asyncio.gather(*(llm.text(f"doctor.parallel{i}", "Отвечай кратко.", f"Назови число {i}", max_tokens=20) for i in range(4)))
    line(True, f"4 параллельных запроса за {time.perf_counter() - t0:.1f} с")
    print(f"   итого токенов на проверку: {sum(c.prompt_tokens + c.completion_tokens for c in llm.telemetry.calls)}")


def main() -> None:
    async def go():
        await check_host()
        await check_llm()
    asyncio.run(go())
