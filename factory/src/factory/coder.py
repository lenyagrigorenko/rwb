"""Coder agent: writes bodies of business operations and jobs, then fixes them from compiler diagnostics."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, Field as PField

from .gen import backend as bgen
from .gen import naming as n
from .ir import AppSpec, Job, Operation
from .llm import LLM

CODER_SYSTEM = """Ты — senior C# разработчик (.NET 10, C# 14, ASP.NET Core, EF Core 10, Npgsql + NetTopologySuite).
Ты пишешь ТОЛЬКО тело метода HandleAsync/RunAsync уже существующего класса. Класс, using, конструктор, DTO и запросы
сгенерированы — не объявляй их заново. Можно добавить приватные вспомогательные методы (helpers) и using (usings).

Правила:
- Данные: `db` (IAppDbContext), только async-методы EF Core (FirstOrDefaultAsync, ToListAsync, AnyAsync…). Изменения
  сохраняй `await db.SaveChangesAsync(ct)`. Новые записи: `db.X.Add(new X { ... CreatedAt = now, UpdatedAt = now })`.
- Доступ к записи по id с проверкой прав роли: `await <entity>Service.GetAccessibleAsync(id, ct)` (бросит 404/403).
  Сервисы возвращают отслеживаемые сущности — их можно менять и сохранять.
- Текущий пользователь: `currentUser.UserId` (Guid?), `currentUser.Role` (string?), `currentUser.RequireUserId()`.
- Время: только `clock.GetUtcNow()` (UTC). Местное время для текстов: `AppTime.Hm(t)` → "15:20".
- События: `await events.Publish(new XPayload(...), entityId)` ДО `SaveChangesAsync` — событие и уведомление сохранятся в той же
  транзакции. Аргументы payload — в порядке объявления.
- Повторная публикация (напоминания, просрочки в фоновых задачах): проверяй
  `await db.OutboxEvents.AnyAsync(e => e.Type == "ИмяСобытия" && e.EntityId == entity.Id, ct)`; entityId передавай в Publish.
- Геопоиск: `await geo.FindNearestAsync(nameof(Entity), lon, lat, radiusMeters, ct)` → GeoMatch? (Id, DistanceMeters).
  Точка: `GeoJson.Point(lon, lat)` (SRID 4326, порядок lon, lat). Не пиши SQL.
- Ошибки: `throw ValidationException.For("field", "Сообщение")` (400), `throw new BusinessRuleException("…")` (422),
  `throw new NotFoundException("…")`, `throw new ForbiddenException()`. Тексты — по-русски.
- Возврат: DTO сущности — `XDto.From(entity)`; ответ операции — `new XResponse(...)` с аргументами по порядку.
- Строковые входы со значениями перечислений разбирай `Enum.TryParse<T>(value, ignoreCase: true, out var v)`.
- decimal/double: приводи явно ((double)request.Lat). Nullable value types: `.Value` только после проверки.
- Не используй DateTime.Now, Thread.Sleep, Console, reflection, сырые SQL, сторонние библиотеки.
- Код должен компилироваться с <Nullable>enable</Nullable>. Пиши компактно и понятно, с короткими комментариями к шагам."""


class CodeLogic(BaseModel):
    body: str = PField(description="Тело метода без фигурных скобок метода")
    helpers: str = PField(default="", description="Приватные вспомогательные методы класса (или пусто)")
    usings: list[str] = PField(default=[], description="Дополнительные namespace для using, без слова using")


def api_reference(spec: AppSpec) -> str:
    """Compact C#-style reference of everything the handler can use."""
    N = spec.namespace
    lines = [f"// Сущности ({N}.Domain.Entities). Все наследуют EntityBase {{ Guid Id; DateTimeOffset CreatedAt; DateTimeOffset UpdatedAt; }}"]
    for e in spec.entities:
        props = "; ".join([f"{n.cs_type(spec, e, f)} {n.pascal(f.name)}" for f in e.fields] +
                          [f"{t}? {nav} /*навигация, нужен Include*/" for nav, t, _ in n.navigations(spec, e)])
        lines.append(f"class {e.name} {{ {props}; }}  // {e.title}; DbSet: db.{n.plural(e.name)}")
    for name, _, f in n.all_enums(spec):
        lines.append(f"enum {name} {{ {', '.join(f.enum_values or [])} }}")
    lines.append("class AppUser { Guid Id; string Login; string DisplayName; string Role; }  // db.Users")
    lines.append("class OutboxEvent { Guid Id; string Type; Guid? EntityId; string Payload; DateTimeOffset OccurredAt; }  "
                 "// db.OutboxEvents — журнал опубликованных событий (только чтение)")
    lines.append("")
    lines.append("// Сервисы сущностей (внедрены как <camelName>Service):")
    for e in spec.entities:
        lines.append(f"{e.name}Service {n.camel(e.name)}Service: Task<{e.name}> GetAccessibleAsync(Guid id, CancellationToken ct); "
                     f"IQueryable<{e.name}> Visible();  // {e.name}Dto.From({e.name})")
    if spec.events:
        lines.append("")
        lines.append("// События (events.Publish(new XPayload(...), entityId)):")
        for ev in spec.events:
            args = ", ".join(f"{bgen.payload_type(p)} {n.pascal(p.name)}" for p in ev.payload)
            note = f" → уведомление «{ev.notify}» пользователю из {ev.recipient_field}" if ev.notify and ev.recipient_field else ""
            lines.append(f"record {ev.name}Payload({args});  // {ev.title}{note}")
    for i in spec.integrations:
        lines.append("")
        lines.append(f"// Интеграция {i.title}: I{i.name} {n.camel(i.name)}")
        for m in i.methods:
            ins = ", ".join(f"{bgen.payload_type(f)} {n.pascal(f.name)}" for f in m.input)
            outs = ", ".join(f"{bgen.payload_type(f)} {n.pascal(f.name)}" for f in m.output)
            lines.append(f"Task<{i.name}{m.name}Response> {m.name}Async(new {i.name}{m.name}Request({ins}), ct);  "
                         f"// ответ: ({outs})")
    geo = [e.name for e in spec.entities if e.geo_fields]
    if geo:
        lines.append("")
        lines.append(f"// Геослои для geo.FindNearestAsync/FindWithinAsync: {', '.join(geo)}")
    return "\n".join(lines)


def _acceptance_for(spec: AppSpec, covers: list[str]) -> str:
    acs = [a for a in spec.analysis.acceptance if a.fr in covers]
    return "\n".join(f"- {a.id} ({a.fr}): Дано {a.given}; Когда {a.when}; Тогда {a.then}" for a in acs) or "—"


def op_task(spec: AppSpec, op: Operation) -> str:
    vm = bgen.op_vm(spec, op)
    req = ""
    if op.input:
        req = f"class {op.name}Request {{ " + "; ".join(f"{i['req_type']} {i['Name']}" for i in vm["inputs"]) + "; }"
    resp = ""
    if vm["response_record"]:
        resp = f"record {op.name}Response(" + ", ".join(f"{bgen.payload_type(f)} {n.pascal(f.name)}" for f in op.output) + ")"
    return (f"## Операция {op.id} {op.name}: {op.title}\nРоли: {', '.join(op.roles)}. HTTP {op.method} /api/v1{op.path}\n"
            f"Спецификация: {op.description}\nПубликует события: {', '.join(op.emits) or 'нет'}\n"
            f"Критерии приёмки:\n{_acceptance_for(spec, op.covers)}\n\n"
            f"{req}\n{resp}\n\nМетод (напиши тело):\npublic async Task<{vm['return_type']}> HandleAsync({vm['signature']})")


def job_task(spec: AppSpec, job: Job) -> str:
    return (f"## Фоновая задача {job.name}: {job.title} (каждые {job.interval_seconds} с)\n"
            f"Спецификация: {job.description}\nПубликует события: {', '.join(job.emits) or 'нет'}\n"
            f"Критерии приёмки:\n{_acceptance_for(spec, job.covers)}\n\n"
            "В задаче нет текущего пользователя и сервисов сущностей — работай через db, clock, events, geo.\n"
            "Метод (напиши тело):\npublic async Task RunAsync(CancellationToken ct)")


async def write_logic(llm: LLM, spec: AppSpec, extra_context: str = "") -> dict[str, dict]:
    ref = api_reference(spec)
    tasks: list[tuple[str, str]] = [(op.name, op_task(spec, op)) for op in spec.operations]
    tasks += [(job.name, job_task(spec, job)) for job in spec.jobs]

    async def one(name: str, task: str) -> tuple[str, dict]:
        logic = await llm.structured(f"coder.{name}", CODER_SYSTEM,
                                     f"# Доступный API\n```csharp\n{ref}\n```\n{extra_context}\n\n{task}",
                                     CodeLogic, max_tokens=6000)
        return name, logic.model_dump()

    return dict(await asyncio.gather(*(one(nm, t) for nm, t in tasks)))


def file_owner(spec: AppSpec, path: str) -> str | None:
    p = Path(path).name
    for op in spec.operations:
        if p == f"{op.name}.cs":
            return op.name
    for job in spec.jobs:
        if p == f"{job.name}Job.cs":
            return job.name
    return None


async def fix_logic(llm: LLM, spec: AppSpec, name: str, current: dict, file_text: str, errors: list[str],
                    runtime: bool = False) -> dict:
    ref = api_reference(spec)
    numbered = "\n".join(f"{i + 1:4}: {line}" for i, line in enumerate(file_text.splitlines()))
    kind = "Ошибки при выполнении демо-сценария" if runtime else "Ошибки компилятора"
    entity_limits = "\n".join(f"- {e.name}.{f.name}: строка до {f.max_length} символов" for e in spec.entities for f in e.fields
                               if f.type == "string" and f.max_length) if runtime else ""
    prompt = (f"# Доступный API\n```csharp\n{ref}\n```\n\n# Текущий файл\n```csharp\n{numbered}\n```\n\n"
              + (f"# Ограничения длины полей БД\n{entity_limits}\n\n" if entity_limits else "")
              + f"# {kind}\n" + "\n".join(f"- {e}" for e in errors) +
              f"\n\n# Текущая логика (JSON)\n{json.dumps(current, ensure_ascii=False)}\n\n"
              "Исправь ошибки. Верни полный исправленный body, helpers и usings.")
    logic = await llm.structured(f"coder.fix.{name}", CODER_SYSTEM, prompt, CodeLogic, max_tokens=6000)
    return logic.model_dump()


FALLBACK = ('// Логику не удалось сгенерировать автоматически: см. docs/12-limitations.md\n'
            'throw new BusinessRuleException("Операция пока не реализована");')
