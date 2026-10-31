"""LLM agents: analyst, architect (domain + behaviour), clarifier. Each call returns validated IR parts."""

from __future__ import annotations

import asyncio
import json

from typing import Literal

from pydantic import BaseModel, Field as PField

from .ir import (AcceptanceCriterion, Ambiguity, Analysis, Dataset, Demo, Entity, Event, Integration, Job, Operation,
                 Permission, Requirement, Role, Scenario, Screen)
from .examples import BEHAVIOUR_EXAMPLE, DOMAIN_EXAMPLE
from .llm import LLM

ANALYST_SYSTEM = """Ты — ведущий системный аналитик корпоративной разработки Russtech.
По формализованным бизнес-требованиям ты готовишь структурированный анализ — основу документа системного анализа.

Правила:
- Функциональные требования (FR-001, FR-002, …): одно атомарное, проверяемое требование на пункт. Требования заказчика
  помечай source=customer. Выведенные тобой (необходимые для работы, но явно не написанные) — source=assumption.
  trace — трассировка к бизнес-требованиям: номер раздела/пункта или короткая цитата.
- Не выдумывай функции, которых нет в требованиях. Нефункциональные требования (NFR-001…) — конкретные и измеримые.
- Роли: name — PascalCase на английском (Dispatcher, Driver, Manager), title — по-русски, demo_login — латиница в нижнем
  регистре (dispatcher). Обычно 2–3 роли: администрирующая (видит всё) и полевая/пользовательская (видит своё).
- Критерии приёмки: у каждого FR 1–2 критерия в поле acceptance, в формате Given/When/Then с конкретными значениями
  (числа, статусы, тексты уведомлений). test_level: api для проверяемых через API, ui для интерфейсных.
- Сценарии использования (UC-001…) — в формате UseCase: actor, preconditions, steps (основной поток), alternatives,
  exceptions (ошибки и реакция системы), postconditions, expected_result. Покрой ключевые потоки каждой роли.
- Неоднозначности: не больше 5, только существенно влияющие на архитектуру, безопасность или пользовательский сценарий.
  Для каждой: impact, затрагиваемые FR, варианты, default_answer (разумное решение по умолчанию) и обоснование.
- Допущения (assumptions) явно отделены от требований заказчика.
- Если приложены данные, используй факты из их профиля: форматы, единицы измерения, аномалии, ограничения — и вынеси
  важные наблюдения в риски и неоднозначности.
- components: web/mobile/kafka/geo — true, если компонент нужен по разделу «Ожидаемые компоненты» или по смыслу
  (геоданные → geo, модель событий/обмен событиями → kafka, мобильное приложение → mobile).
- Пиши по-русски, кратко и по делу."""

ARCHITECT_RULES = """Ты — архитектор корпоративной платформы. Ты проектируешь техническую модель (IR) приложения,
по которой детерминированные генераторы создадут backend ASP.NET Core, Angular-админку, Android-приложение, БД PostgreSQL/PostGIS,
Kafka-события, тесты и документацию. CRUD, авторизация, пагинация, поиск, фильтры, журнал событий, уведомления, импорт
приложенных данных и демо-режим генерируются автоматически — проектируй только предметную часть.

Соглашения IR:
- Сущности: name — PascalCase, английский, единственное число; title/title_plural — по-русски. Поля: name — camelCase,
  title — по-русски. НЕ добавляй id, createdAt, updatedAt — они есть всегда. Не создавай сущность User: учётные записи
  уже есть, ссылайся на них полем type=ref, ref="User".
- Типы полей: string (обязательно max_length), text, int, decimal, money, bool, date, datetime, enum (enum_values — латинские
  идентификаторы, enum_titles — русские подписи), ref (ref — имя сущности или "User"), email, phone, url, json,
  geo_point, geo_line, geo_polygon, geometry.
- Для строк с форматом задай regex (госномер, телефон, код). Для чисел — min/max. examples — 2–3 реалистичных значения
  на русском для тестовых данных. readonly=true для полей, которые заполняет система, а не пользователь.
- owner_field — поле ref на User, по которому работают права read_own/update_own (запись «принадлежит» пользователю).
- display_field — поле-подпись записи в списках и ссылках. lifecycle — если у сущности есть статусы и переходы
  (field — enum-поле статуса, transitions с ролями и событиями).
- permissions: для каждой роли и сущности список действий из read, read_own, create, update, update_own, delete.
  Административная роль обычно read/create/update/delete, полевая — read_own (свои записи) и read справочников.
- datasets: если приложены файлы данных — сопоставь их сущности: records_path (путь к массиву записей), mapping
  (поле сущности → ключ записи, вложенность через точку), geometry_format (wkt/geojson/none). Тип поля подбирай по профилю.
- integrations: внешние системы из требований заменяются адаптером + mock-сервисом. Для каждого метода задай
  mock_response (JSON ответа; {поле} подставляется из запроса).
- map_center — [широта, долгота] города из требований.
- Не создавай сущности-журналы событий/уведомлений: доменные события публикуются в Kafka, журнал событий и уведомления
  генерируются автоматически. Текущее положение движущегося объекта — поле geo_point у самой сущности (readonly),
  история координат — отдельная сущность с полем geo_point.
- Если у роли «свои» записи (водитель — свой автомобиль и свои сессии, техник — свои заявки), добавь поле ref на User,
  укажи его в owner_field и дай роли read_own вместо read. Если разные роли владеют записью по разным полям (автор
  заявки и назначенный исполнитель), укажи owner_field в праве каждой такой роли.
- Каждый приложенный файл данных ОБЯЗАТЕЛЬНО опиши в datasets, имена полей сущности бери понятные (не копируй ключи файла)."""

BEHAVIOUR_RULES = """Правила поведения:
- operations — только бизнес-логика сверх CRUD (расчёты, геопоиск, запуск процессов, сводки). Если при создании записи
  нужна логика (расчёт срока, номера, статуса, проверки по правилам) — сделай операцию Create<Сущность> (путь
  /<сущности>/create) и используй её в демо вместо create; поля, которые вычисляет операция, пометь readonly. Путь — относительно
  /api/v1, вида /vehicles/{vehicleId}/track (параметры camelCase, id сущности — {entityNameId}); путь из одного сегмента
  запрещён (конфликт с CRUD). input/output — поля запроса и ответа; output_entity — если операция возвращает запись
  сущности. description — точная спецификация для программиста: шаги, проверки, ошибки, какие события публиковать.
  covers — FR, которые операция реализует. idempotent=true для повторяемых команд.
- Геопоиск выполняется сервисом: «ближайший объект слоя в радиусе N метров». Если зоны заданы линиями — используй радиус.
- events — PascalCase в прошедшем времени (ZoneEntered). topic — kebab-case. payload — минимальный набор полей; если по
  событию нужно уведомить пользователя — добавь в payload поле с id пользователя (ref User как string) и укажи его в
  recipient_field, текст уведомления — notify с {полями payload}. key_field — поле для партиционирования.
- jobs — периодические проверки (истечение сроков, напоминания) с interval_seconds.
- screens — web: обзорный экран для административной роли (виджеты kpi, map, event_feed, table); mobile — экраны для
  полевой роли, если нужно мобильное приложение (map, status_card с operation, countdown, action_button с operation,
  notifications, list с entity). Каждый экран — covers.
- demo — шаги по разделу «Демонстрационный сценарий»: action = route (движение по маршруту через объекты геослоя:
  operation — операция приёма координат, args {leg, zones: 2}, где leg: 0 — движение вне объектов, 1 — въезд в первый
  объект, 2 — переезд в соседний объект, 3 — выезд из объектов на свободное место; координаты в демо не задавай —
  маршрут строится по реальным объектам слоя), create (создание записи через
  стандартный CRUD: entity и args — значения полей; используй, если для создания нет отдельной операции),
  operation (вызов операции, args — тело запроса),
  advance_time (minutes — сдвиг часов, чтобы наступили события по времени), transition (entity, transition),
  view (entity — роль просматривает список/историю записей, например «события отображаются в интерфейсе»),
  expect (args {event: ИмяСобытия} — проверка, что событие произошло). Для шагов, где по сценарию система должна
  отказать (конфликт, нарушение правила), ставь expect_error=true.
- Моменты времени во входах операций — одно поле type=datetime (ISO 8601), не разделяй дату и время на два поля.
- Используй только сущности, роли и события, которые объявлены в модели. Названия операций — PascalCase глагол+объект."""


class DomainModel(BaseModel):
    slug: str = PField(description="kebab-case идентификатор проекта, латиница")
    namespace: str = PField(description="Корневой namespace C#, PascalCase, латиница")
    product_name: str
    map_center: list[float] = PField(default=[55.7558, 37.6173])
    entities: list[Entity]
    permissions: list[Permission]
    datasets: list[Dataset] = []
    integrations: list[Integration] = []


class BehaviourModel(BaseModel):
    operations: list[Operation]
    events: list[Event]
    jobs: list[Job]
    screens: list[Screen]
    demo: Demo


def _req_block(requirements: str, profile: str, answers: str = "") -> str:
    parts = [f"## Бизнес-требования\n\n{requirements.strip()}"]
    if profile:
        parts.append(f"## Профиль приложенных данных\n\n{profile}")
    if answers:
        parts.append(f"## Ответы на уточняющие вопросы и принятые допущения\n\n{answers}")
    return "\n\n".join(parts)


class AnalystCriterion(BaseModel):
    given: str
    when: str
    then: str
    test_level: Literal["unit", "api", "ui"] = "api"


class AnalystRequirement(BaseModel):
    id: str = PField(description="FR-001")
    text: str
    priority: Literal["must", "should", "could"] = "must"
    source: Literal["customer", "assumption"] = "customer"
    trace: str = PField(default="", description="Откуда в БТ: номер раздела/пункта или цитата до 8 слов; для допущений — почему нужно")
    acceptance: list[AnalystCriterion] = PField(min_length=1, description="1–2 критерия приёмки этого FR")


class AnalystOutput(BaseModel):
    """Ответ аналитика: критерии приёмки вложены в FR, чтобы ни одно требование не осталось без проверки."""

    product_name: str
    summary: str
    goals: list[str]
    scope_in: list[str]
    scope_out: list[str]
    roles: list[Role]
    functional: list[AnalystRequirement]
    non_functional: list[Requirement]
    constraints: list[str]
    scenarios: list[Scenario]
    integrations: list[str]
    security: list[str]
    risks: list[str]
    ambiguities: list[Ambiguity]
    assumptions: list[str]
    components: dict[str, bool] = PField(default_factory=lambda: {"web": True, "mobile": False, "kafka": False,
                                                                  "geo": False})

    def to_analysis(self) -> Analysis:
        functional, acceptance = [], []
        for fr in self.functional:
            functional.append(Requirement(id=fr.id, text=fr.text, priority=fr.priority, source=fr.source, trace=fr.trace))
            for ac in fr.acceptance:
                acceptance.append(AcceptanceCriterion(id=f"AC-{len(acceptance) + 1:03d}", fr=fr.id, given=ac.given,
                                                      when=ac.when, then=ac.then, test_level=ac.test_level))
        return Analysis(**self.model_dump(exclude={"functional"}), functional=functional, acceptance=acceptance)


async def analyze(llm: LLM, requirements: str, profile: str, *, think: bool = False) -> Analysis:
    out = await llm.structured("analyst", ANALYST_SYSTEM, _req_block(requirements, profile), AnalystOutput,
                               think=think, max_tokens=9000, max_tokens_cap=14000)
    return out.to_analysis()


class AcceptanceList(BaseModel):
    acceptance: list[AcceptanceCriterion]


async def complete_acceptance(llm: LLM, requirements: str, analysis: Analysis, missing: list[str]) -> Analysis:
    frs = "\n".join(f"- {fr.id}: {fr.text}" for fr in analysis.functional if fr.id in missing)
    start = len(analysis.acceptance) + 1
    extra = await llm.structured(
        "analyst.acceptance", ANALYST_SYSTEM,
        f"## Бизнес-требования\n\n{requirements}\n\n## ФТ без критериев приёмки\n\n{frs}\n\n"
        f"Напиши по 1–2 критерия приёмки на каждое из этих ФТ (Given/When/Then с конкретными значениями). "
        f"Нумерация id с AC-{start:03d}.", AcceptanceList, max_tokens=4000)
    analysis.acceptance += extra.acceptance
    return analysis


def answers_text(analysis: Analysis) -> str:
    lines = []
    for q in analysis.ambiguities:
        lines.append(f"- {q.id} {q.question} → {q.answer or q.default_answer}")
    lines += [f"- Допущение: {a}" for a in analysis.assumptions]
    return "\n".join(lines)


def _analysis_brief(a: Analysis) -> str:
    """Выжимка анализа для архитектора: только то, что влияет на модель (без критериев приёмки и НФТ)."""
    lines = [f"Продукт: {a.product_name}. {a.summary}",
             "Компоненты: " + ", ".join(k for k, v in a.components.items() if v),
             "Роли: " + "; ".join(f"{r.name} ({r.title}, вход {r.demo_login})" for r in a.roles),
             "Функциональные требования:"]
    lines += [f"- {fr.id}: {fr.text}" for fr in a.functional]
    if a.integrations:
        lines.append("Интеграции: " + "; ".join(a.integrations))
    if a.security:
        lines.append("Безопасность: " + "; ".join(a.security))
    return "\n".join(lines)


def _field_brief(f) -> str:
    kind = f.type
    if f.type == "ref":
        kind = f"ref {f.ref}"
    elif f.type == "enum":
        kind = "enum(" + "|".join(f.enum_values or []) + ")"
    flags = [x for x, on in (("обяз.", f.required), ("readonly", f.readonly), ("unique", f.unique)) if on]
    return f"{f.name}:{kind}" + (f"[{','.join(flags)}]" if flags else "")


def domain_brief(d: DomainModel) -> str:
    """Модель данных компактным текстом (вместо JSON): имена, типы, связи, статусы, права."""
    lines = []
    for e in d.entities:
        lines.append(f"- {e.name} ({e.title}): " + ", ".join(_field_brief(f) for f in e.fields))
        if e.owner_field:
            lines.append(f"  владелец записи: {e.owner_field}")
        if e.lifecycle:
            lc = e.lifecycle
            lines.append(f"  статусы {lc.field}, начальный {lc.initial}; переходы: " + "; ".join(
                f"{t.name}: {'|'.join(t.from_states)}→{t.to} ({', '.join(t.roles)})" for t in lc.transitions))
    lines.append("Права: " + "; ".join(
        f"{p.role}/{p.entity}: {','.join(p.actions)}" + (f" по {p.owner_field}" if p.owner_field else "")
        for p in d.permissions))
    for i in d.integrations:
        lines.append(f"Интеграция {i.name} ({i.real_system}): " + ", ".join(m.name for m in i.methods))
    return "\n".join(lines)


async def design(llm: LLM, requirements: str, profile: str, analysis: Analysis) -> tuple[DomainModel, BehaviourModel]:
    ctx = _req_block(requirements, profile, answers_text(analysis))
    brief = _analysis_brief(analysis)
    domain = await llm.structured(
        "architect.domain", ARCHITECT_RULES,
        f"## Пример формата (другая предметная область, НЕ копируй содержание)\n\n{json.dumps(DOMAIN_EXAMPLE, ensure_ascii=False)}\n\n"
        f"{ctx}\n\n## Результат анализа\n\n{brief}\n\nСпроектируй модель данных: сущности, права, датасеты, интеграции.",
        DomainModel, max_tokens=7000, max_tokens_cap=12000)
    behaviour = await llm.structured(
        "architect.behaviour", ARCHITECT_RULES + "\n\n" + BEHAVIOUR_RULES,
        f"## Пример формата (другая предметная область, НЕ копируй содержание)\n\n{json.dumps(BEHAVIOUR_EXAMPLE, ensure_ascii=False)}\n\n"
        f"{ctx}\n\n## Результат анализа\n\n{brief}\n\n## Модель данных\n\n{domain_brief(domain)}\n\n"
        "Спроектируй поведение: операции, события (с уведомлениями из требований), фоновые задачи (контроль сроков), "
        "экраны и демо-сценарий строго по шагам раздела «Демонстрационный сценарий».",
        BehaviourModel, max_tokens=8000, max_tokens_cap=14000)
    return domain, behaviour


async def repair(llm: LLM, stage: str, model: type[BaseModel], current: BaseModel, errors: list[str],
                 context: str) -> BaseModel:
    return await llm.structured(
        f"{stage}.repair", ARCHITECT_RULES + "\n\n" + BEHAVIOUR_RULES,
        f"{context}\n\n## Текущая версия (JSON)\n\n{current.model_dump_json()}\n\n## Ошибки проверки\n\n"
        + "\n".join(f"- {e}" for e in errors) + "\n\nИсправь только эти ошибки и верни полный JSON.",
        model, max_tokens=8000, max_tokens_cap=14000)


async def gather(*aws):
    return await asyncio.gather(*aws)
