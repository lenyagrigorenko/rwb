"""LLM agents: analyst, architect (domain + behaviour), clarifier. Each call returns validated IR parts."""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, Field as PField

from .ir import (AcceptanceCriterion, Analysis, Dataset, Demo, Entity, Event, Integration, Job, Operation, Permission,
                 Screen)
from .examples import BEHAVIOUR_EXAMPLE, DOMAIN_EXAMPLE
from .llm import LLM

ANALYST_SYSTEM = """Ты — ведущий системный аналитик корпоративной разработки Russtech.
По формализованным бизнес-требованиям ты готовишь структурированный анализ — основу документа системного анализа.

Правила:
- Функциональные требования (FR-001, FR-002, …): одно атомарное, проверяемое требование на пункт. Требования заказчика
  помечай source=customer. Выведенные тобой (необходимые для работы, но явно не написанные) — source=assumption.
- Не выдумывай функции, которых нет в требованиях. Нефункциональные требования (NFR-001…) — конкретные и измеримые.
- Роли: name — PascalCase на английском (Dispatcher, Driver, Manager), title — по-русски, demo_login — латиница в нижнем
  регистре (dispatcher). Обычно 2–3 роли: администрирующая (видит всё) и полевая/пользовательская (видит своё).
- Критерии приёмки (AC-001…): минимум один на каждое FR, в формате Given/When/Then с конкретными значениями (числа,
  статусы, тексты уведомлений). test_level: api для проверяемых через API, ui для интерфейсных.
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


async def analyze(llm: LLM, requirements: str, profile: str, *, think: bool = False) -> Analysis:
    return await llm.structured("analyst", ANALYST_SYSTEM, _req_block(requirements, profile), Analysis,
                                think=think, max_tokens=14000)


class AcceptanceList(BaseModel):
    acceptance: list[AcceptanceCriterion]


async def complete_acceptance(llm: LLM, requirements: str, analysis: Analysis, missing: list[str]) -> Analysis:
    frs = "\n".join(f"- {fr.id}: {fr.text}" for fr in analysis.functional if fr.id in missing)
    start = len(analysis.acceptance) + 1
    extra = await llm.structured(
        "analyst.acceptance", ANALYST_SYSTEM,
        f"## Бизнес-требования\n\n{requirements}\n\n## ФТ без критериев приёмки\n\n{frs}\n\n"
        f"Напиши по 1–2 критерия приёмки на каждое из этих ФТ (Given/When/Then с конкретными значениями). "
        f"Нумерация id с AC-{start:03d}.", AcceptanceList, max_tokens=6000)
    analysis.acceptance += extra.acceptance
    return analysis


def answers_text(analysis: Analysis) -> str:
    lines = []
    for q in analysis.ambiguities:
        lines.append(f"- {q.id} {q.question} → {q.answer or q.default_answer}")
    lines += [f"- Допущение: {a}" for a in analysis.assumptions]
    return "\n".join(lines)


def _analysis_brief(a: Analysis) -> str:
    return json.dumps(a.model_dump(include={"product_name", "summary", "roles", "functional", "non_functional",
                                            "integrations", "security", "acceptance", "components"}),
                      ensure_ascii=False)


async def design(llm: LLM, requirements: str, profile: str, analysis: Analysis) -> tuple[DomainModel, BehaviourModel]:
    ctx = _req_block(requirements, profile, answers_text(analysis))
    brief = _analysis_brief(analysis)
    domain = await llm.structured(
        "architect.domain", ARCHITECT_RULES,
        f"## Пример формата (другая предметная область, НЕ копируй содержание)\n\n{json.dumps(DOMAIN_EXAMPLE, ensure_ascii=False)}\n\n"
        f"{ctx}\n\n## Результат анализа (JSON)\n\n{brief}\n\nСпроектируй модель данных: сущности, права, датасеты, интеграции.",
        DomainModel, max_tokens=14000)
    behaviour = await llm.structured(
        "architect.behaviour", ARCHITECT_RULES + "\n\n" + BEHAVIOUR_RULES,
        f"## Пример формата (другая предметная область, НЕ копируй содержание)\n\n{json.dumps(BEHAVIOUR_EXAMPLE, ensure_ascii=False)}\n\n"
        f"{ctx}\n\n## Результат анализа (JSON)\n\n{brief}\n\n## Модель данных (JSON)\n\n"
        f"{domain.model_dump_json(exclude={'datasets'})}\n\n"
        "Спроектируй поведение: операции, события (с уведомлениями из требований), фоновые задачи (контроль сроков), "
        "экраны и демо-сценарий строго по шагам раздела «Демонстрационный сценарий».",
        BehaviourModel, max_tokens=14000)
    return domain, behaviour


async def repair(llm: LLM, stage: str, model: type[BaseModel], current: BaseModel, errors: list[str],
                 context: str) -> BaseModel:
    return await llm.structured(
        f"{stage}.repair", ARCHITECT_RULES + "\n\n" + BEHAVIOUR_RULES,
        f"{context}\n\n## Текущая версия (JSON)\n\n{current.model_dump_json()}\n\n## Ошибки проверки\n\n"
        + "\n".join(f"- {e}" for e in errors) + "\n\nИсправь только эти ошибки и верни полный JSON.",
        model, max_tokens=14000)


async def gather(*aws):
    return await asyncio.gather(*aws)
