"""Coder agent: writes bodies of business operations and jobs, then fixes them from compiler diagnostics."""

from __future__ import annotations

import asyncio
import json
import re
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
- Не называй локальные переменные `request` и `ct` — это параметры метода (ошибка CS0136). Запись называй по сущности:
  `var vacation = await vacationRequestService.GetAccessibleAsync(id, ct)`; поля входа — `request.Comment`.
- Доступ к записи по id с проверкой прав роли: `await <entity>Service.GetAccessibleAsync(id, ct)` (бросит 404/403).
  Сервисы возвращают отслеживаемые сущности — их можно менять и сохранять.
- Текущий пользователь: `currentUser.UserId` (Guid?), `currentUser.Role` (string?), `currentUser.RequireUserId()`.
  Это id учётной записи, а НЕ id записи сущности: «свою» запись (профиль сотрудника, свой автомобиль) ищи по полю
  владельца — `await db.Employees.FirstOrDefaultAsync(x => x.UserId == currentUser.RequireUserId(), ct)`;
  в GetAccessibleAsync передавай только id записи этой сущности. Если пользователь — это запись сущности его роли
  (врач, инженер, курьер), найди её по полю UserId: `var me = await db.Doctors.FirstOrDefaultAsync(d => d.UserId ==
  currentUser.RequireUserId(), ct)` и сравнивай ссылки с me.Id, а не с id пользователя.
- Время: только `clock.GetUtcNow()` (UTC, DateTimeOffset). Местное время для текстов: `AppTime.Hm(t)` → "15:20".
- Поля date — тип DateOnly: сегодня `AppTime.Today(now)`, дата момента `AppTime.Date(t)`, число дней
  `AppTime.Days(from, to)` (оператора «−» у DateOnly НЕТ), начало дня в UTC `AppTime.StartOf(date)`.
  DateOnly с DateTimeOffset напрямую не сравнивай — приводи через эти функции. Поля datetime — DateTimeOffset.
- События: `await events.Publish(new XPayload(...), entityId)` ДО `SaveChangesAsync` — событие и уведомление сохранятся в той же
  транзакции. Аргументы payload — в порядке объявления.
- Напоминание «за N до момента T» срабатывает, пока момент ВПЕРЕДИ: `T > now && T <= now.Add(N)`;
  просрочка «через N после T»: `T <= now.Add(-N)`.
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


def system_contract(spec: AppSpec) -> str:
    """What every operation and job does: shared assumptions keep independently written methods consistent."""
    lines = ["# Контракт системы (все операции и задачи — их пишут другие разработчики по этим описаниям)"]
    for e in spec.entities:
        if e.lifecycle:
            lc = e.lifecycle
            lines.append(f"- Статусы {e.name}.{lc.field}: начальный {lc.initial}; переходы: " +
                         "; ".join(f"{t.name}: {','.join(t.from_states)}→{t.to}" for t in lc.transitions))
    for op in spec.operations:
        lines.append(f"- Операция {op.name} ({op.method} /api/v1{op.path}): {op.description} События: {', '.join(op.emits) or '—'}")
    for job in spec.jobs:
        lines.append(f"- Задача {job.name}: {job.description} События: {', '.join(job.emits) or '—'}")
    lines.append("Не дублируй чужую ответственность и не требуй от данных того, что другие операции не создают.")
    return "\n".join(lines)


def _acceptance_for(spec: AppSpec, covers: list[str]) -> str:
    acs = [a for a in spec.analysis.acceptance if a.fr in covers]
    return "\n".join(f"- {a.id} ({a.fr}): Дано {a.given}; Когда {a.when}; Тогда {a.then}" for a in acs) or "—"


def _owner_hint(spec: AppSpec, op: Operation) -> str:
    """Owner fields the handler must fill when it creates a record (otherwise the role never sees its own records)."""
    e = spec.entity(op.output_entity or "")
    if not e:
        return ""
    fields = list(bgen.owner_fields(spec, e))
    if not fields:
        return ""
    return (f"\nПри создании записи {e.name} заполни поле владельца "
            + ", ".join(f"{n.pascal(f)}" for f in fields)
            + " = currentUser.RequireUserId(), если по логике владелец не другой пользователь.")


def person_refs(spec: AppSpec) -> dict[str, tuple[str, str]]:
    """Ref fields that point to records of people (Employee, Doctor — entities with userId): Pascal name → (owner, target)."""
    people = {e.name for e in spec.entities if any(f.type == "ref" and f.ref == "User" and f.name == "userId" for f in e.fields)}
    return {n.pascal(f.name): (e.name, f.ref) for e in spec.entities for f in e.fields
            if f.type == "ref" and f.ref in people}


def _person_hint(spec: AppSpec, op: Operation) -> str:
    """«Руководитель согласует заявку своего сотрудника»: Employee.ManagerId — id записи Employee, а не пользователя."""
    hits = [(name, owner, target) for name, (owner, target) in person_refs(spec).items()
            if any(r.lower() in name.lower() for r in op.roles)]
    if not hits:
        return ""
    name, owner, target = hits[0]
    plural = n.plural(target)
    return (f"\n{owner}.{name} — id записи {target}, а не пользователя. Текущего {target} находи так: "
            f"`var me = await db.{plural}.FirstOrDefaultAsync(x => x.UserId == currentUser.RequireUserId(), ct)` "
            f"и сравнивай {name} с me.Id.")


def op_task(spec: AppSpec, op: Operation) -> str:
    vm = bgen.op_vm(spec, op)
    req = ""
    if op.input:
        req = f"class {op.name}Request {{ " + "; ".join(f"{i['req_type']} {i['Name']}" for i in vm["inputs"]) + "; }"
    resp = ""
    if vm["response_record"]:
        resp = f"record {op.name}Response(" + ", ".join(f"{bgen.payload_type(f)} {n.pascal(f.name)}" for f in op.output) + ")"
    return (f"## Операция {op.id} {op.name}: {op.title}\nРоли: {', '.join(op.roles)}. HTTP {op.method} /api/v1{op.path}\n"
            f"Спецификация: {op.description}{_owner_hint(spec, op)}{_person_hint(spec, op)}\nПубликует события: {', '.join(op.emits) or 'нет'}\n"
            f"Критерии приёмки:\n{_acceptance_for(spec, op.covers)}\n\n"
            f"{req}\n{resp}\n\nМетод (напиши тело):\npublic async Task<{vm['return_type']}> HandleAsync({vm['signature']})")


def reminder_hint(spec: AppSpec, job: Job) -> str:
    """Reminder jobs: the model often inverts the window and selects moments already in the past."""
    evs = [e for e in spec.events if e.name in job.emits]
    if not any(bgen.REMINDER_RE.search(e.name) or bgen.REMINDER_RE.search(e.title) for e in evs):
        return ""
    return ("\nЭто напоминание о БУДУЩЕМ моменте T (начало приёма, слота, брони): выбирай записи, у которых T ещё впереди "
            "и попадает в окно: `.Where(x => x.T > now && x.T <= now.AddHours(N))`. Условие `T < now` или `T >= now.AddHours(-N)` "
            "ошибочно — оно ищет прошедшие моменты.")


def job_task(spec: AppSpec, job: Job) -> str:
    return (f"## Фоновая задача {job.name}: {job.title} (каждые {job.interval_seconds} с)\n"
            f"Спецификация: {job.description}{reminder_hint(spec, job)}\nПубликует события: {', '.join(job.emits) or 'нет'}\n"
            f"Критерии приёмки:\n{_acceptance_for(spec, job.covers)}\n\n"
            "В задаче нет текущего пользователя и сервисов сущностей — работай через db, clock, events, geo.\n"
            "Метод (напиши тело):\npublic async Task RunAsync(CancellationToken ct)")


async def write_logic(llm: LLM, spec: AppSpec, extra_context: str = "") -> dict[str, dict]:
    ref = api_reference(spec) + "\n```\n\n" + system_contract(spec) + "\n```csharp"
    tasks: list[tuple[str, str]] = [(op.name, op_task(spec, op)) for op in spec.operations]
    tasks += [(job.name, job_task(spec, job)) for job in spec.jobs]

    async def one(name: str, task: str) -> tuple[str, dict]:
        try:
            logic = await llm.structured(f"coder.{name}", CODER_SYSTEM,
                                         f"# Доступный API\n```csharp\n{ref}\n```\n{extra_context}\n\n{task}",
                                         CodeLogic, max_tokens=2500, max_tokens_cap=5000)
            return name, logic.model_dump()
        except Exception as e:  # один метод не должен ронять весь прогон: заглушка + запись в дефекты
            return name, {"body": FALLBACK, "helpers": "", "usings": [], "failed": f"не сгенерирована: {str(e)[:200]}"}

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


# Подсказки к частым ошибкам компиляции: модель повторяет их из попытки в попытку, если не дать готового решения.
HINTS = [
    (re.compile(r"Operator '-' cannot be applied to operands of type 'DateOnly'"),
     "Подсказка: у DateOnly нет оператора «−»; число дней — AppTime.Days(from, to)."),
    (re.compile(r"'(DateTime|DateTimeOffset)' and 'DateOnly'|'DateOnly' and '(DateTime|DateTimeOffset)'|to 'System.DateOnly"),
     "Подсказка: дата момента — AppTime.Date(t) (DateOnly), начало дня — AppTime.StartOf(date) (DateTimeOffset)."),
    (re.compile(r"from 'System.DateTimeOffset' to 'System.DateTime'"),
     "Подсказка: время везде DateTimeOffset (clock.GetUtcNow()); для DateTime используй t.UtcDateTime, для даты — AppTime.Date(t)."),
    (re.compile(r"CS0136|CS0128|already defined|cannot be declared in this scope"),
     "Подсказка: переменная с этим именем уже объявлена в методе (в том числе во вложенном блоке или ниже) — "
     "переименуй одну из них (например, добавь суффикс 2) во всех её использованиях."),
    (re.compile(r"Cannot implicitly convert type 'System\.Collections\.Generic\.List<"),
     "Подсказка: метод возвращает ОДИН объект, а не список — сигнатуру менять нельзя; собери один ответ "
     "(агрегат или выбранная запись), а не List."),
    (re.compile(r"CS0411"), "Подсказка: тип в Select не выводится — создавай объект явно: `.Select(x => new ИмяТипа(...))`, "
     "или вынеси вычисление в отдельную переменную с явным типом."),
    (re.compile(r"CS0266|explicit conversion exists"), "Подсказка: добавь явное приведение типа или проверку на null (?? / .Value)."),
    (re.compile(r"CS8602|CS8604|possible null"), "Подсказка: проверь значение на null перед использованием."),
]


def with_hints(errors: list[str]) -> list[str]:
    hints = {h for rx, h in HINTS for e in errors if rx.search(e)}
    return errors + sorted(hints)


async def fix_logic(llm: LLM, spec: AppSpec, name: str, current: dict, file_text: str, errors: list[str],
                    runtime: bool = False) -> dict:
    errors = with_hints(errors)
    job = next((j for j in spec.jobs if j.name == name), None)
    if runtime and job and (hint := reminder_hint(spec, job)):
        errors.append("Подсказка:" + hint.strip())
    for name_, (owner, target) in person_refs(spec).items():
        if re.search(rf"\.{name_}\s*[!=]=\s*(currentUser\.\w+|currentUserId|userId)\b", file_text):
            errors.append(f"Подсказка: {owner}.{name_} — id записи {target}, а сравнивается с id пользователя. Найди свою запись "
                          f"{target} по UserId == currentUser.RequireUserId() и сравнивай {name_} с её Id.")
    if re.search(r"GetAccessibleAsync\(\s*currentUser\.", file_text):
        errors.append("Подсказка: в GetAccessibleAsync передан id пользователя вместо id записи — ищи запись по полю "
                      "владельца: db.X.FirstOrDefaultAsync(x => x.<ПолеВладельца> == currentUser.RequireUserId(), ct).")
    ref = api_reference(spec) + ("\n```\n\n" + system_contract(spec) + "\n```csharp" if runtime else "")
    numbered = "\n".join(f"{i + 1:4}: {line}" for i, line in enumerate(file_text.splitlines()))
    kind = "Ошибки при выполнении демо-сценария" if runtime else "Ошибки компилятора"
    entity_limits = "\n".join(f"- {e.name}.{f.name}: строка до {f.max_length} символов" for e in spec.entities for f in e.fields
                               if f.type == "string" and f.max_length) if runtime else ""
    prompt = (f"# Доступный API\n```csharp\n{ref}\n```\n\n# Текущий файл\n```csharp\n{numbered}\n```\n\n"
              + (f"# Ограничения длины полей БД\n{entity_limits}\n\n" if entity_limits else "")
              + f"# {kind}\n" + "\n".join(f"- {e}" for e in errors) +
              f"\n\n# Текущая логика (JSON)\n{json.dumps(current, ensure_ascii=False)}\n\n"
              "Исправь ошибки. Верни полный исправленный body, helpers и usings.")
    try:
        logic = await llm.structured(f"coder.fix.{name}", CODER_SYSTEM, prompt, CodeLogic, max_tokens=2500,
                                     max_tokens_cap=5000)
    except Exception:
        return current  # исправление не получилось — остаётся текущая версия, решает цикл сборки
    return logic.model_dump()


class FilePatch(BaseModel):
    content: str = PField(description="Полный исправленный текст файла")


async def fix_file(llm: LLM, path: str, text: str, errors: list[str]) -> str | None:
    """Last resort for compile errors outside model-written methods: the model patches the generated file itself."""
    numbered = "\n".join(f"{i + 1:4}: {line}" for i, line in enumerate(text.splitlines()))
    prompt = (f"# Файл {path}\n```csharp\n{numbered}\n```\n\n# Ошибки компилятора\n" + "\n".join(f"- {e}" for e in errors)
              + "\n\nИсправь только эти ошибки минимальными изменениями (например, укажи полное имя типа при "
                "неоднозначности). Не меняй публичные сигнатуры. Верни полный текст файла без номеров строк.")
    try:
        patch = await llm.structured(f"coder.patch.{Path(path).stem}", CODER_SYSTEM, prompt, FilePatch,
                                     max_tokens=min(16000, len(text) // 2 + 1500), max_tokens_cap=16000)
    except Exception:
        return None
    content = re.sub(r"^\s*\d+: ", "", patch.content, flags=re.M) if re.match(r"^\s*1: ", patch.content) else patch.content
    return content if content.strip() else None


FALLBACK = ('// Логику не удалось сгенерировать автоматически: см. docs/12-limitations.md\n'
            'throw new BusinessRuleException("Операция пока не реализована");')
