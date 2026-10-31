"""Documentation generator: facts from the IR and run results, prose from the analysis."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from ..ir import AppSpec, Entity, Field, Operation
from .. import validate
from . import naming as n
from .backend import _sample, _uid, admin_roles, id_aliases, seed_users
from .render import Renderer


def constraints(f: Field) -> str:
    parts = []
    if f.max_length and f.type in ("string", "email", "phone", "url"):
        parts.append(f"до {f.max_length} симв.")
    if f.regex:
        parts.append(f"формат `{f.regex}`")
    if f.min is not None:
        parts.append(f"≥ {f.min:g}")
    if f.max is not None:
        parts.append(f"≤ {f.max:g}")
    if f.unique:
        parts.append("уникальное")
    if f.enum_values:
        parts.append("одно из: " + ", ".join(f"{v} ({(f.enum_titles or {}).get(v, v)})" for v in f.enum_values))
    if f.type == "email":
        parts.append("адрес e-mail")
    if f.type in ("geo_point", "geo_line", "geo_polygon", "geometry"):
        parts.append("GeoJSON, WGS84")
    return "; ".join(parts) or "—"


def json_type(f: Field) -> str:
    return {"int": "integer", "decimal": "number", "money": "number", "bool": "boolean",
            "json": "object"}.get(f.type, "string")


def _example(spec: AppSpec, f: Field, i: int = 0):
    if f.type == "ref":
        return _uid(spec.slug, f.ref or "User", str(i))
    if f.type == "string" and f.name.endswith("Id") and not f.regex:
        return _uid(spec.slug, f.name, str(i))  # идентификатор, переданный строкой
    owner = spec.entities[0] if spec.entities else Entity(name="X", title="", title_plural="", fields=[])
    v = _sample(spec, owner, f, i)
    if v is None and f.type in ("geo_line", "geometry", "geo_polygon"):
        v = _sample(spec, owner, f.model_copy(update={"required": True}), i)
    return v


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _roles_text(spec: AppSpec, roles: list[str]) -> str:
    titles = [f"{r.title} (`{r.name}`)" for r in spec.roles if r.name in roles]
    return "Роли: " + (", ".join(titles) or "все авторизованные пользователи") + "."


def _entity_response(spec: AppSpec, e: Entity) -> list[dict]:
    rows = [{"name": "id", "type": "uuid", "required": True, "title": "Идентификатор", "example": f"`{_uid(spec.slug, e.name, '0')}`"}]
    for f in e.fields:
        ex = _example(spec, f)
        rows.append({"name": f.name, "type": json_type(f) + (f" ({f.type})" if f.type not in ("string", "int", "bool") else ""),
                     "required": f.required, "title": f.title,
                     "example": f"`{json.dumps(ex, ensure_ascii=False)[:60]}`" if ex is not None else "—"})
    rows += [{"name": "createdAt", "type": "string (datetime)", "required": True, "title": "Создано", "example": "`2026-10-31T09:00:00Z`"},
             {"name": "updatedAt", "type": "string (datetime)", "required": True, "title": "Изменено", "example": "`2026-10-31T09:00:00Z`"}]
    return rows


def _entity_example(spec: AppSpec, e: Entity) -> dict:
    obj = {"id": _uid(spec.slug, e.name, "0")}
    obj.update({f.name: _example(spec, f) for f in e.fields})
    obj.update({"createdAt": "2026-10-31T09:00:00Z", "updatedAt": "2026-10-31T09:00:00Z"})
    return obj


def _validations(fields: list[Field]) -> str:
    items = [f"`{f.name}`: {'обязательное; ' if f.required else ''}{constraints(f)}" for f in fields
             if f.required or constraints(f) != "—"]
    return "; ".join(items) + "." if items else "Специальных проверок входных данных нет (кроме прав доступа)."


def api_methods(spec: AppSpec) -> list[tuple[str, list[dict]]]:
    """Context for docs/api/methods/*.md: one page per business operation, one page per entity CRUD, login."""
    pages: list[tuple[str, list[dict]]] = []
    entities = {e.name: e for e in spec.entities}
    integrations = ", ".join(f"{i.title} (`I{i.name}`, mock-сервис `mocks`)" for i in spec.integrations)
    for op in spec.operations:
        path_entity = next((e for p in op.path_params for e in spec.entities if p in id_aliases(spec, e)), None)
        body = [f for f in op.input if f.name not in op.path_params] if op.method in ("POST", "PUT") else []
        query = [f for f in op.input if f.name not in op.path_params] if op.method in ("GET", "DELETE") else []
        out_entity = entities.get(op.output_entity or "")
        response = _entity_response(spec, out_entity) if out_entity else [
            {"name": f.name, "type": json_type(f), "required": f.required, "title": f.title,
             "example": f"`{json.dumps(_example(spec, f), ensure_ascii=False)[:60]}`"} for f in op.output]
        example_resp = _entity_example(spec, out_entity) if out_entity else {f.name: _example(spec, f) for f in op.output}
        if op.output_list:
            example_resp = [example_resp]
        url = "/api/v1" + op.path
        example_url = url
        for p in op.path_params:
            ent = next((e for e in spec.entities if p in id_aliases(spec, e)), None)
            example_url = example_url.replace("{" + p + "}", _uid(spec.slug, ent.name if ent else p, "0"))
        if query:
            example_url += "?" + "&".join(f"{f.name}={_example(spec, f)}" for f in query[:3])
        tables = []
        for name in dict.fromkeys([x for x in [path_entity.name if path_entity else None, op.output_entity,
                                               *[f.ref for f in op.input if f.type == "ref" and f.ref != "User"]] if x]):
            if name in entities:
                tables.append({"name": n.table(entities[name]), "title": entities[name].title_plural})
        if op.emits:
            tables.append({"name": "outbox_events", "title": "Исходящие доменные события (transactional outbox)"})
        pages.append((f"{n.kebab(op.name)}.md", [{
            "method": op.method, "url": url, "name": op.name, "title": op.title,
            "purpose": op.title + (f". Покрывает {', '.join(op.covers)}" if op.covers else ""),
            "tag": "Operations", "events": op.emits, "public": False, "idempotent": op.idempotent or op.method == "POST",
            "path_params": [{"name": p, "title": f"Идентификатор записи «{(path_entity.title if path_entity else p)}»",
                             "example": _uid(spec.slug, path_entity.name if path_entity else p, "0")} for p in op.path_params],
            "query": [{"name": f.name, "title": f.title, "type": json_type(f), "example": _example(spec, f)} for f in query],
            "body": body, "example_url": example_url,
            "example_request": _json({f.name: _example(spec, f) for f in body}),
            "roles_text": _roles_text(spec, op.roles), "success_code": 200,
            "success_text": (f"Список записей «{out_entity.title}»" if op.output_list else f"Запись «{out_entity.title}»")
            if out_entity else "Результат операции",
            "response": response, "example_response": _json(example_resp), "business": True,
            "integrations": bool(spec.integrations),
            "error_field": next((f.name for f in body if f.required), body[0].name if body else "id"),
            "tables": tables, "algorithm": op.description,
            "validations": _validations(body or query),
            "external": (f"Внешние системы: {integrations}. " if spec.integrations else "")
            + (f"Публикуемые события: {', '.join(op.emits)} (Kafka, через outbox)." if op.emits else
               "Публикации событий нет." if not spec.integrations else ""),
        }]))
    for e in spec.entities:
        route = "/api/v1/" + n.route(e)
        roles_for = lambda action: [p.role for p in spec.permissions if p.entity == e.name and (
            action in p.actions or f"{action}_own" in p.actions)]
        editable = [f for f in e.fields if not f.readonly]
        filters = [f for f in e.fields if f.type in ("enum", "ref", "bool")]
        query = [{"name": "page", "title": "Номер страницы (с 1)", "type": "integer", "example": 1},
                 {"name": "pageSize", "title": "Размер страницы (до 500, по умолчанию 20)", "type": "integer", "example": 20},
                 {"name": "sort", "title": "Поле сортировки", "type": "string", "example": "createdAt"},
                 {"name": "dir", "title": "Направление: asc / desc", "type": "string", "example": "desc"},
                 {"name": "q", "title": "Полнотекстовый поиск по текстовым полям", "type": "string", "example": "…"}]
        query += [{"name": f.name, "title": f"Фильтр: {f.title}", "type": json_type(f), "example": _example(spec, f)}
                  for f in filters]
        idp = [{"name": "id", "title": f"Идентификатор записи «{e.title}»", "example": _uid(spec.slug, e.name, "0")}]
        own = [r for r in spec.roles if any(p.role == r.name and p.entity == e.name and "read_own" in p.actions
                                            for p in spec.permissions)]
        own_note = (" Роли " + ", ".join(r.title for r in own) + " видят только свои записи.") if own else ""
        common = {"tag": n.plural(e.name), "events": [], "public": False, "idempotent": False, "business": False,
                  "integrations": False, "tables": [{"name": n.table(e), "title": e.title_plural}],
                  "external": "Нет.", "query": [], "path_params": [], "body": [], "example_request": "{}"}
        example = _entity_example(spec, e)
        listed = {"items": [example], "total": 1, "page": 1, "pageSize": 20}
        methods = [
            {**common, "method": "GET", "url": route, "name": f"List{n.plural(e.name)}", "title": f"Список: {e.title_plural}",
             "purpose": f"Постраничный список записей «{e.title_plural}» с поиском, сортировкой и фильтрами.{own_note}",
             "query": query, "example_url": route + "?page=1&pageSize=20", "roles_text": _roles_text(spec, roles_for("read")),
             "success_code": 200, "success_text": "Страница записей: items, total, page, pageSize",
             "response": [{"name": "items", "type": "array", "required": True, "title": f"Записи «{e.title}»", "example": "—"},
                          {"name": "total", "type": "integer", "required": True, "title": "Всего записей", "example": "`1`"},
                          {"name": "page", "type": "integer", "required": True, "title": "Страница", "example": "`1`"},
                          {"name": "pageSize", "type": "integer", "required": True, "title": "Размер страницы", "example": "`20`"}],
             "example_response": _json(listed), "error_field": "pageSize",
             "algorithm": "Применяет права роли (все записи или только свои), фильтры и поиск, сортирует и возвращает страницу.",
             "validations": "`page` ≥ 1; `pageSize` от 1 до 500; `dir` — asc или desc."},
            {**common, "method": "GET", "url": route + "/{id}", "name": f"Get{e.name}", "title": f"Карточка: {e.title}",
             "purpose": f"Получение записи «{e.title}» по идентификатору.{own_note}", "path_params": idp,
             "example_url": route + "/" + idp[0]["example"], "roles_text": _roles_text(spec, roles_for("read")),
             "success_code": 200, "success_text": f"Запись «{e.title}»", "response": _entity_response(spec, e),
             "example_response": _json(example), "error_field": "id",
             "algorithm": "Находит запись по идентификатору с учётом прав роли и возвращает её.", "validations": "`id` — UUID."},
            {**common, "method": "POST", "url": route, "name": f"Create{e.name}", "title": f"Создание: {e.title}",
             "purpose": f"Создание записи «{e.title}».", "body": editable, "idempotent": True,
             "example_url": route, "example_request": _json({f.name: _example(spec, f) for f in editable}),
             "roles_text": _roles_text(spec, roles_for("create")), "success_code": 200, "success_text": f"Созданная запись «{e.title}»",
             "response": _entity_response(spec, e), "example_response": _json(example), "business": True,
             "error_field": next((f.name for f in editable if f.required), "id"),
             "algorithm": "Проверяет права и входные данные, сохраняет запись, возвращает её с присвоенным идентификатором.",
             "validations": _validations(editable)},
            {**common, "method": "PUT", "url": route + "/{id}", "name": f"Update{e.name}", "title": f"Изменение: {e.title}",
             "purpose": f"Изменение записи «{e.title}».", "path_params": idp, "body": editable,
             "example_url": route + "/" + idp[0]["example"], "example_request": _json({f.name: _example(spec, f) for f in editable}),
             "roles_text": _roles_text(spec, roles_for("update")), "success_code": 200, "success_text": f"Изменённая запись «{e.title}»",
             "response": _entity_response(spec, e), "example_response": _json(example), "business": True,
             "error_field": next((f.name for f in editable if f.required), "id"),
             "algorithm": "Находит запись с учётом прав, проверяет входные данные, сохраняет изменения.",
             "validations": _validations(editable)},
            {**common, "method": "DELETE", "url": route + "/{id}", "name": f"Delete{e.name}", "title": f"Удаление: {e.title}",
             "purpose": f"Удаление записи «{e.title}».", "path_params": idp, "example_url": route + "/" + idp[0]["example"],
             "roles_text": _roles_text(spec, roles_for("delete")), "success_code": 204, "success_text": "Запись удалена",
             "response": [], "example_response": "", "business": True, "error_field": "id",
             "algorithm": "Находит запись с учётом прав и удаляет; при наличии связанных записей — 409.",
             "validations": "`id` — UUID."},
        ]
        pages.append((f"{n.kebab(e.name)}-crud.md", methods))
    pages.append(("auth-login.md", [{
        "method": "POST", "url": "/api/v1/auth/login", "name": "Login", "title": "Вход в систему",
        "purpose": "Проверка логина и пароля, выдача JWT-токена.", "tag": "Auth", "events": [], "public": True,
        "idempotent": False, "path_params": [], "query": [],
        "body": [Field(name="login", title="Логин", type="string", required=True, max_length=64),
                 Field(name="password", title="Пароль", type="string", required=True, max_length=128)],
        "example_url": "/api/v1/auth/login",
        "example_request": _json({"login": spec.roles[0].demo_login if spec.roles else "user", "password": "<пароль>"}),
        "roles_text": "Без авторизации; не более 30 попыток в минуту с одного адреса.", "success_code": 200,
        "success_text": "Токен, срок действия, пользователь",
        "response": [{"name": "token", "type": "string", "required": True, "title": "JWT-токен", "example": "`eyJhbGciOi…`"},
                     {"name": "expiresAt", "type": "string (datetime)", "required": True, "title": "Срок действия", "example": "`2026-10-31T21:00:00Z`"},
                     {"name": "user", "type": "object", "required": True, "title": "Пользователь: id, login, displayName, role", "example": "—"}],
        "example_response": _json({"token": "eyJhbGciOi…", "expiresAt": "2026-10-31T21:00:00Z",
                                   "user": {"id": _uid(spec.slug, "user", "demo"), "login": spec.roles[0].demo_login if spec.roles else "user",
                                            "displayName": spec.roles[0].title if spec.roles else "", "role": spec.roles[0].name if spec.roles else ""}}),
        "business": False, "integrations": False, "error_field": "login",
        "tables": [{"name": "users", "title": "Учётные записи пользователей"}],
        "algorithm": "Находит пользователя по логину, проверяет хеш пароля (PBKDF2), выпускает JWT с ролью пользователя.",
        "validations": "`login` и `password` обязательны; при неверной паре — 401 `invalid_credentials`.",
        "external": "Нет.",
    }]))
    return pages


def adrs(spec: AppSpec) -> list[dict]:
    """Architecture decisions of the generated solution in the ADR template."""
    today = date.today().isoformat()
    base = {"status": "ПРИНЯТО", "date": today}
    items = [{**base, "id": "ADR-001", "slug": "architecture-style", "title": "Архитектурный стиль: модульный монолит",
              "context": f"Прототип «{spec.product_name}» должна быстро развернуть и продолжить развивать обычная продуктовая "
                         f"команда. Предметная область: {len(spec.entities)} сущностей, {len(spec.operations)} бизнес-операций, "
                         f"роли: {', '.join(r.title for r in spec.roles)}.",
              "alternatives": [
                  {"title": "Модульный монолит с Clean Architecture (Domain / Application / Infrastructure / Api)",
                   "pros": ["Одна единица развёртывания и одна БД — простой запуск из чистого окружения",
                            "Чёткие слои и зависимости внутрь — код легко сопровождать и тестировать",
                            "Транзакционная согласованность без распределённых транзакций"],
                   "cons": ["Масштабирование целиком, а не по отдельным функциям",
                            "Нужна дисциплина, чтобы модули не срастались"]},
                  {"title": "Микросервисы по сущностям",
                   "pros": ["Независимое масштабирование и развёртывание"],
                   "cons": ["Сетевые вызовы и согласованность данных между сервисами",
                            "Несоразмерная сложность инфраструктуры для прототипа"]}],
              "decision": "Модульный монолит ASP.NET Core (.NET 10) с Clean Architecture; границы модулей совпадают с сущностями "
                          "IR, бизнес-операции — отдельные обработчики (Application/Operations).",
              "consequences": ["Запуск одной командой `docker compose up`", "Выделение модуля в отдельный сервис возможно "
                               "без переписывания логики: обработчики зависят только от абстракций",
                               "Горизонтальное масштабирование — несколькими экземплярами за балансировщиком"]}]
    if spec.events:
        items.append({**base, "id": "ADR-002", "slug": "events-outbox-kafka", "title": "Доменные события: transactional outbox и Kafka",
                      "context": "Требования предусматривают модель событий и уведомления: " + ", ".join(ev.title for ev in spec.events[:6])
                                 + ". Событие не должно теряться при сбое после сохранения данных и не должно публиковаться при откате.",
                      "alternatives": [
                          {"title": "Transactional outbox + фоновая отправка в Kafka, идемпотентный консьюмер",
                           "pros": ["Атомарность: событие пишется в той же транзакции, что и данные",
                                    "Гарантия доставки at-least-once, повтор после сбоя Kafka",
                                    "Идемпотентная обработка по id события (таблица consumed_events)"],
                           "cons": ["Задержка публикации до интервала отправки (≈0,5 с)", "Дополнительная таблица и фоновая задача"]},
                          {"title": "Прямая публикация в Kafka из обработчика",
                           "pros": ["Меньше кода, минимальная задержка"],
                           "cons": ["Потеря или «фантомные» события при сбое между БД и брокером"]}],
                      "decision": "Transactional outbox (`outbox_events`) с фоновой отправкой в Kafka; конверт события "
                                  "`{id, type, version, occurredAt, payload}`, ключ партиционирования — поле предмета события; "
                                  "схема — `docs/asyncapi.yaml`.",
                      "consequences": ["События переживают недоступность Kafka (статус Degraded в /health)",
                                       "Потребители обязаны быть идемпотентными", "Версия события увеличивается при "
                                       "несовместимом изменении схемы"]})
    if spec.uses_geo:
        items.append({**base, "id": f"ADR-00{len(items) + 1}", "slug": "geo-postgis", "title": "Геоданные и геопоиск в PostGIS",
                      "context": "Сценарии требуют определять положение объектов относительно геообъектов (зон, адресов) и "
                                 "показывать их на карте.",
                      "alternatives": [
                          {"title": "PostGIS: тип geography (WGS84), GIST-индексы, ST_DWithin / ST_Distance",
                           "pros": ["Точный расчёт расстояний в метрах на сфере", "Индексы — быстрый поиск ближайшего объекта",
                                    "Стандартный формат GeoJSON на API"],
                           "cons": ["Требуется расширение PostGIS в БД"]},
                          {"title": "Расчёт расстояний в приложении",
                           "pros": ["Нет зависимости от расширения БД"],
                           "cons": ["Полный перебор объектов, неточность планарных формул, дублирование логики"]}],
                      "decision": "Геометрия хранится в PostGIS (geography, SRID 4326), геопоиск — сервис `IGeoService` "
                                  "(ближайший объект слоя в радиусе N метров).",
                      "consequences": ["Поиск по зонам выполняется за миллисекунды на тысячах объектов",
                                       "Карта во всех интерфейсах получает GeoJSON"]})
    if spec.integrations:
        items.append({**base, "id": f"ADR-00{len(items) + 1}", "slug": "integrations-adapter-mock",
                      "title": "Внешние системы через адаптер и mock-сервис",
                      "context": "Промышленный доступ к внешним системам (" + ", ".join(i.real_system for i in spec.integrations)
                                 + ") в рамках прототипа недоступен, но интеграция должна быть заменяемой без изменения бизнес-логики.",
                      "alternatives": [
                          {"title": "Интерфейс в Application + HTTP-адаптер + mock-сервис с документированными ответами",
                           "pros": ["Бизнес-логика не зависит от поставщика", "Сценарии демонстрируются без внешних систем",
                                    "Замена — новая реализация интерфейса и адрес в конфигурации"],
                           "cons": ["Ответы mock не покрывают все ошибки реальной системы"]},
                          {"title": "Заглушки внутри бизнес-логики",
                           "pros": ["Быстрее написать"],
                           "cons": ["Смешение логики и интеграции, переписывание при подключении реальной системы"]}],
                      "decision": "Для каждой внешней системы — интерфейс `I<Система>`, HTTP-адаптер и mock-сервис `mocks` "
                                  "(ответы — `mocks/mocks.json`); недоступность системы → 502 `integration_unavailable`.",
                      "consequences": ["Подключение реальной системы — смена `Integrations__BaseUrl` и адаптера",
                                       "Контракт интеграции описан в документации API"]})
    items.append({**base, "id": f"ADR-00{len(items) + 1}", "slug": "auth-jwt", "title": "Аутентификация и авторизация: JWT и матрица прав",
                  "context": "Разные роли видят разные данные: " + "; ".join(f"{r.title} — {r.description or 'по матрице прав'}" for r in spec.roles)
                             + ". Доступ нужен из web-интерфейса" + (" и мобильного приложения." if spec.has_mobile else "."),
                  "alternatives": [
                      {"title": "JWT Bearer (HS256) + роли + права на записи по полю-владельцу",
                       "pros": ["Без состояния на сервере, подходит для web и мобильного клиента",
                                "Права «свои записи» проверяются на уровне запроса к БД"],
                       "cons": ["Отзыв токена до истечения срока требует доработки (список отзыва)"]},
                      {"title": "Серверные сессии (cookie)",
                       "pros": ["Простой отзыв сессии"],
                       "cons": ["Неудобно для мобильного клиента, состояние на сервере"]}],
                  "decision": "JWT Bearer со сроком 12 часов; матрица прав из IR (`Permissions`), фильтрация «своих» записей "
                              "по полю-владельцу; ограничение частоты запросов и отдельный лимит на вход.",
                  "consequences": ["Интеграция с корпоративным IdP (OIDC) — замена выдачи токена без изменения проверок",
                                   "Права покрыты автотестами (роль × сущность)"]})
    return items


UI_KIND = {"string": "Текстовое поле (mat-input)", "text": "Многострочное поле (textarea)", "int": "Числовое поле",
           "decimal": "Числовое поле", "money": "Числовое поле (₽)", "bool": "Переключатель (mat-slide-toggle)",
           "date": "Выбор даты (mat-datepicker)", "datetime": "Дата и время (mat-datepicker + время)",
           "enum": "Выпадающий список (mat-select)", "ref": "Выпадающий список со ссылкой (mat-select)",
           "email": "Поле e-mail", "phone": "Поле телефона", "url": "Поле ссылки", "json": "Поле JSON",
           "geo_point": "Точка на карте (Leaflet)", "geo_line": "Линия на карте", "geo_polygon": "Полигон на карте",
           "geometry": "Геометрия на карте"}


def glossary(spec: AppSpec) -> list[dict]:
    terms = [{"term": r.title, "text": r.description or f"Роль пользователя `{r.name}`"} for r in spec.roles]
    terms += [{"term": e.title, "text": (e.description or f"Сущность предметной области, таблица `{n.table(e)}`")}
              for e in spec.entities]
    terms += [{"term": ev.title, "text": f"Доменное событие `{ev.name}` (топик `{ev.topic}`)"} for ev in spec.events[:8]]
    terms += [{"term": "БТ", "text": "Бизнес-требования — исходный документ заказчика"},
              {"term": "ФТ / FR", "text": "Функциональное требование"}, {"term": "НФТ / NFR", "text": "Нефункциональное требование"},
              {"term": "ЭФ", "text": "Экранная форма"},
              {"term": "JWT", "text": "JSON Web Token — токен доступа, передаётся в заголовке Authorization: Bearer"},
              {"term": "ProblemDetails", "text": "Единый формат ошибок API (RFC 9457, application/problem+json)"}]
    if spec.events:
        terms += [{"term": "Outbox", "text": "Таблица исходящих событий, запись в одной транзакции с данными (transactional outbox)"},
                  {"term": "Kafka", "text": "Брокер сообщений для обмена доменными событиями"}]
    if spec.uses_geo:
        terms.append({"term": "PostGIS", "text": "Расширение PostgreSQL для геоданных; координаты — WGS84 (SRID 4326)"})
    if spec.integrations:
        terms.append({"term": "Mock-сервис", "text": "Заменитель внешней системы с документированными ответами"})
    return terms


def ui_screens(spec: AppSpec) -> list[dict]:
    """Screen-by-screen UI specification (element, type, visibility by role, availability, BE method)."""
    titles = {r.name: r.title for r in spec.roles}
    who = lambda roles: ", ".join(titles.get(r, r) for r in roles) or "—"
    screens = [{"title": "Вход в систему", "description": "Первый экран web-интерфейса" + (" и мобильного приложения" if spec.has_mobile else "") + ".",
                "elements": [
                    {"name": "Логин", "kind": "Текстовое поле", "text": "Логин пользователя", "visible": "все", "enabled": "всегда", "action": "—"},
                    {"name": "Пароль", "kind": "Поле пароля", "text": "Символы скрыты", "visible": "все", "enabled": "всегда", "action": "—"},
                    {"name": "Войти", "kind": "Кнопка (mat-flat-button)", "text": "Отправка формы", "visible": "все",
                     "enabled": "недоступна во время отправки", "action": "`POST /api/v1/auth/login`; при ошибке — текст ошибки с backend"}],
                "fields": []}]
    for sc in spec.screens:
        if sc.platform != "web":
            continue
        els = [{"name": w.title, "kind": {"kpi": "Карточка показателя", "map": "Карта (Leaflet)", "event_feed": "Лента событий",
                                           "table": "Таблица (mat-table)"}.get(w.type, w.type),
                "text": f"Данные: {w.entity or w.operation or 'события'}", "visible": who(sc.roles), "enabled": "всегда",
                "action": f"`GET /api/v1/{n.route(w.entity)}`" if w.entity else ("`GET /api/v1/events`" if w.type == "event_feed" else "`GET /api/v1/dashboard`")}
               for w in sc.widgets]
        screens.append({"title": sc.title + " (web)", "description": sc.description or "Обзорный экран.", "elements": els, "fields": []})
    for e in spec.entities:
        readers = [p.role for p in spec.permissions if p.entity == e.name and ({"read", "read_own"} & set(p.actions))]
        creators = [p.role for p in spec.permissions if p.entity == e.name and "create" in p.actions]
        editors = [p.role for p in spec.permissions if p.entity == e.name and ({"update", "update_own"} & set(p.actions))]
        deleters = [p.role for p in spec.permissions if p.entity == e.name and "delete" in p.actions]
        route = "/api/v1/" + n.route(e)
        els = [{"name": "Пункт меню «" + e.title_plural + "»", "kind": "Ссылка навигации", "text": "Открывает список",
                "visible": who(readers), "enabled": "всегда", "action": f"`GET {route}`"},
               {"name": "Поиск", "kind": "Текстовое поле", "text": "Поиск по текстовым полям", "visible": who(readers),
                "enabled": "всегда", "action": f"`GET {route}?q=…`"},
               {"name": "Таблица", "kind": "mat-table + mat-paginator + сортировка", "text": "Колонки: " +
                ", ".join(f.title for f in e.fields if f.list_visible)[:200], "visible": who(readers), "enabled": "всегда",
                "action": f"`GET {route}?page=&pageSize=&sort=&dir=`"}]
        for f in e.fields:
            if f.type in ("enum", "ref", "bool"):
                els.append({"name": f"Фильтр «{f.title}»", "kind": "Выпадающий список", "text": "Фильтрация списка",
                            "visible": who(readers), "enabled": "всегда", "action": f"`GET {route}?{f.name}=…`"})
        if creators:
            els.append({"name": "Создать", "kind": "Кнопка", "text": "Открывает форму новой записи", "visible": who(creators),
                        "enabled": "всегда", "action": f"`POST {route}`"})
        if editors:
            els.append({"name": "Изменить", "kind": "Кнопка в строке", "text": "Открывает форму записи", "visible": who(editors),
                        "enabled": "для своих записей у ролей с update_own", "action": f"`PUT {route}/{{id}}`"})
        if deleters:
            els.append({"name": "Удалить", "kind": "Кнопка в строке + подтверждение «Удалить запись?»", "text": "Удаление записи",
                        "visible": who(deleters), "enabled": "всегда; при связанных записях — ошибка 409", "action": f"`DELETE {route}/{{id}}`"})
        for t in (e.lifecycle.transitions if e.lifecycle else []):
            els.append({"name": t.title, "kind": "Кнопка перехода статуса", "text": f"{', '.join(t.from_states)} → {t.to}",
                        "visible": who(t.roles), "enabled": f"статус записи: {', '.join(t.from_states)}",
                        "action": f"`POST {route}/{{id}}/{n.kebab(t.name)}`"})
        for op in spec.operations:
            if any(p in id_aliases(spec, e) for p in op.path_params):
                els.append({"name": op.title, "kind": "Кнопка операции + диалог (mat-dialog)", "text": op.description[:150],
                            "visible": who(op.roles), "enabled": "по правилам операции (ошибка 422 с текстом)",
                            "action": f"`{op.method} /api/v1{op.path}`"})
        fields = [{"title": f.title, "kind": UI_KIND.get(f.type, f.type), "required": f.required,
                   "format": constraints(f), "api": f.name,
                   "example": (json.dumps(_example(spec, f), ensure_ascii=False)[:40] if _example(spec, f) is not None else "—")}
                  for f in e.fields if not f.readonly]
        screens.append({"title": f"{e.title_plural}: список и карточка (web)",
                        "description": f"Список с поиском, сортировкой и фильтрами; форма записи «{e.title}»."
                        + (" Записи на карте." if e.geo_fields else ""), "elements": els, "fields": fields})
    for sc in spec.screens:
        if sc.platform != "mobile":
            continue
        els = []
        for w in sc.widgets:
            op = next((o for o in spec.operations if o.name == w.operation), None)
            els.append({"name": w.title, "kind": {"map": "Карта (osmdroid)", "status_card": "Карточка статуса", "countdown": "Обратный отсчёт",
                                                  "action_button": "Кнопка действия", "notifications": "Список уведомлений",
                                                  "list": "Список записей"}.get(w.type, w.type),
                        "text": (op.title if op else w.entity or "") or w.type, "visible": who(sc.roles),
                        "enabled": "при наличии данных; ошибка — текст с backend",
                        "action": f"`{op.method} /api/v1{op.path}`" if op else (f"`GET /api/v1/{n.route(w.entity)}`" if w.entity else
                                  "`GET /api/v1/notifications`" if w.type == "notifications" else "—")})
        screens.append({"title": sc.title + " (мобильное приложение)", "description": sc.description or "Экран мобильного приложения.",
                        "elements": els, "fields": []})
    return screens


def generate(spec: AppSpec, out: Path, *, run_dir: Path | None = None, verification: dict | None = None,
             telemetry: dict | None = None, defects: list[str] | None = None, questions: list | None = None,
             result: dict | None = None, spec_warnings: list[str] | None = None) -> list[Path]:
    users = seed_users(spec)
    sources: dict[str, list[str]] = {}
    for op in spec.operations:
        for ev in op.emits:
            sources.setdefault(ev, []).append(f"операция {op.name}")
    for j in spec.jobs:
        for ev in j.emits:
            sources.setdefault(ev, []).append(f"задача {j.name}")
    for e in spec.entities:
        for t in (e.lifecycle.transitions if e.lifecycle else []):
            if t.emits:
                sources.setdefault(t.emits, []).append(f"переход {e.name}.{t.name}")
    tel = telemetry or {"calls": 0, "retries": 0, "prompt_tokens": 0, "completion_tokens": 0, "llm_seconds": 0,
                        "wall_seconds": 0, "by_stage": {}}
    r = Renderer("docs", spec, extra={
        "a": spec.analysis, "coverage": validate.coverage(spec), "admin_roles": admin_roles(spec),
        "constraints": constraints, "json_type": json_type, "users": users,
        "role_title": lambda name: next((x.title for x in spec.roles if x.name == name), name),
        "verification": verification or {}, "telemetry": tel, "defects": defects or [],
        "questions": questions or [], "result": result or {"build_ok": True}, "sources": sources,
        "spec_warnings": spec_warnings or [], "security_findings": (result or {}).get("security_findings", ""),
        "topics": sorted({ev.topic for ev in spec.events}),
        "model": os.environ.get("LLM_MODEL", "qwen3.8-27b").split("/")[-1], "today": date.today().isoformat(),
        "glossary": glossary(spec), "ui_screens": ui_screens(spec),
        "mobile_roles": {r for s in spec.screens if s.platform == "mobile" for r in s.roles},
        "demo_actor": lambda st: next((i + 1 for i, r in enumerate(spec.roles) if r.name in (
            next((o.roles for o in spec.operations if o.name == st.operation), None) or
            [p.role for p in spec.permissions if p.entity == st.entity and "create" in p.actions] or [spec.roles[0].name])), 1),
    })
    docs = out / "docs"
    r.render("README.md.j2", out / "README.md")
    r.render("AGENTS.md.j2", out / "AGENTS.md")
    r.render("tz.md.j2", docs / "01-system-analysis.md")
    r.render("service.md.j2", docs / "service.md")
    for adr in adrs(spec):
        r.render("adr.md.j2", docs / "adr" / f"{adr['id']}-{adr['slug']}.md", adr=adr)
    method_tpl = r.env.get_template("api-method.md.j2")
    for fname, methods in api_methods(spec):
        dest = docs / "api" / "methods" / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n\n---\n\n".join(method_tpl.render(m=m) for m in methods), encoding="utf-8")
        r.written.append(dest)
    if spec.events:
        r.render("asyncapi.yaml.j2", docs / "asyncapi.yaml")
    pages = {
        "docs.md.j2": [("product", "02-product.md"), ("architecture", "03-architecture.md"), ("runbook", "07-runbook.md"),
                       ("user_guide", "08-user-guide.md"), ("dev_guide", "09-dev-guide.md"),
                       ("limitations", "12-limitations.md")],
        "reports.md.j2": [("api", "04-api.md"), ("data_model", "05-data-model.md"), ("events", "06-events.md"),
                          ("test_report", "10-test-report.md"), ("security", "11-security-report.md"),
                          ("traceability", "traceability.md"), ("scorecard", "jury-scorecard.md"),
                          ("pipeline_report", "pipeline-report.md")],
    }
    for template, items in pages.items():
        module = r.env.get_template(template).make_module()
        for macro, fname in items:
            dest = docs / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(str(getattr(module, macro)()).lstrip(), encoding="utf-8")
            r.written.append(dest)
    return r.written
