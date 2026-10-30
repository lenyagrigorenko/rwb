"""Semantic checks of the IR. Trivial issues are auto-fixed; the rest go back to the LLM."""

from __future__ import annotations

import re

from .gen import naming as n
from .ir import AppSpec

RESERVED = {"User", "AppUser", "OutboxEvent", "ConsumedEvent", "AppNotification", "IdempotencyRecord",
            "EntityBase", "Program", "Task", "Event", "Notification", "Permissions"}


def _pascal_enum(v: str) -> str:
    parts = re.split(r"[_\-\s]+", v)
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or v


def normalize_enums(spec: AppSpec) -> list[str]:
    """Enum values become PascalCase identifiers (safe for C#/Kotlin/TS, e.g. `new` -> `New`)."""
    fixes = []
    for e in spec.entities:
        for f in e.fields:
            if f.type != "enum" or not f.enum_values:
                continue
            mapping = {v: _pascal_enum(v) for v in f.enum_values}
            if all(k == v for k, v in mapping.items()):
                continue
            f.enum_values = [mapping[v] for v in f.enum_values]
            if f.enum_titles:
                f.enum_titles = {mapping.get(k, k): t for k, t in f.enum_titles.items()}
            if isinstance(f.default, str):
                f.default = mapping.get(f.default, f.default)
            if e.lifecycle and e.lifecycle.field == f.name:
                e.lifecycle.initial = mapping.get(e.lifecycle.initial, e.lifecycle.initial)
                for t in e.lifecycle.transitions:
                    t.from_states = [mapping.get(x, x) for x in t.from_states]
                    t.to = mapping.get(t.to, t.to)
            for sc in spec.screens:
                for w in sc.widgets:
                    flt = w.props.get("filter") if isinstance(w.props, dict) else None
                    if isinstance(flt, dict) and w.entity == e.name and f.name in flt:
                        val = flt[f.name]
                        if isinstance(val, list):
                            flt[f.name] = [mapping.get(x, x) if isinstance(x, str) else x for x in val]
                        elif isinstance(val, str):
                            flt[f.name] = mapping.get(val, val)
            fixes.append(f"{e.name}.{f.name}: значения перечисления приведены к PascalCase")
    return fixes


def autofix(spec: AppSpec) -> list[str]:
    """Deterministic repairs that do not need a model call. Returns a log of fixes."""
    fixes: list[str] = normalize_enums(spec)
    if spec.demo:
        for st in spec.demo.steps:
            if st.action in ("operation", "create") and not st.expect_error and re.search(
                    r"пыта|отказ|запрещ|ошибк|невозможн|не может|недопустим|конфликт|отклон", st.title.lower()):
                st.expect_error = True
                fixes.append(f"демо {st.id}: шаг проверяет отказ")
    roles = {r.name for r in spec.roles}
    entities = {e.name for e in spec.entities}
    events = {e.name for e in spec.events}
    ops = {o.name for o in spec.operations}

    before = len(spec.permissions)
    spec.permissions = [p for p in spec.permissions if p.role in roles and p.entity in entities]
    if len(spec.permissions) != before:
        fixes.append("удалены права для неизвестных ролей/сущностей")
    for e in spec.entities:
        if e.owner_field and not (f := e.field(e.owner_field)) or (e.owner_field and f and f.ref != "User"):
            fixes.append(f"{e.name}: owner_field {e.owner_field} не ссылается на User — сброшен")
            e.owner_field = None
        if e.display_field and not e.field(e.display_field):
            e.display_field = None
        for f in e.fields:
            if f.type == "ref" and f.ref not in entities and f.ref != "User":
                fixes.append(f"{e.name}.{f.name}: ссылка на неизвестную сущность {f.ref} → string")
                f.type, f.ref = "string", None
        if e.lifecycle:
            lf = e.field(e.lifecycle.field)
            if not lf or lf.type != "enum":
                fixes.append(f"{e.name}: жизненный цикл без enum-поля — удалён")
                e.lifecycle = None
            else:
                states = set(lf.enum_values or [])
                if e.lifecycle.initial not in states:
                    e.lifecycle.initial = lf.enum_values[0]
                for t in e.lifecycle.transitions:
                    t.roles = [r for r in t.roles if r in roles] or [spec.roles[0].name]
                    t.from_states = [s for s in t.from_states if s in states] or list(states)
                    if t.emits and t.emits not in events:
                        t.emits = None
                e.lifecycle.transitions = [t for t in e.lifecycle.transitions if t.to in states]
    for e in spec.entities:
        if not e.lifecycle:
            continue
        route = n.route(e)
        kept = []
        for t in e.lifecycle.transitions:
            clash = next((o for o in spec.operations if o.method == "POST" and
                          re.fullmatch(rf"/{route}/\{{\w+\}}/{n.kebab(t.name)}", re.sub(r"^/api/v1", "", o.path))), None)
            if clash:
                fixes.append(f"{e.name}: переход {t.name} совпадает с операцией {clash.name} — переход реализует операция")
            else:
                kept.append(t)
        e.lifecycle.transitions = kept
    for o in spec.operations:
        o.roles = [r for r in o.roles if r in roles] or [r.name for r in spec.roles]
        o.emits = [x for x in o.emits if x in events]
        if o.output_entity and o.output_entity not in entities:
            o.output_entity = None
        if not o.path.startswith("/"):
            o.path = "/" + o.path
        o.path = re.sub(r"^/api/v1", "", o.path)
    for j in spec.jobs:
        j.emits = [x for x in j.emits if x in events]
    for ev in spec.events:
        names = {p.name for p in ev.payload}
        if ev.key_field not in names:
            ev.key_field = ev.payload[0].name if ev.payload else "id"
        if ev.recipient_field and ev.recipient_field not in names:
            ev.recipient_field = None
    for s in spec.screens:
        s.roles = [r for r in s.roles if r in roles]
        s.widgets = [w for w in s.widgets if (not w.entity or w.entity in entities)
                     and (not w.operation or w.operation in ops)]
    spec.datasets = [d for d in spec.datasets if d.entity in entities]
    return fixes


def check(spec: AppSpec, data_files: set[str] | None = None, requirements: str = "") -> list[str]:
    """Problems the model must fix (empty list = valid)."""
    errors: list[str] = []
    req = requirements.lower()
    for fname in sorted(data_files or []):
        if not any(d.file == fname for d in spec.datasets):
            errors.append(f"Приложенный файл {fname} не описан в datasets (сопоставь его сущности)")
    if spec.analysis.components.get("kafka") and not spec.events:
        errors.append("Требуется модель событий, но events пуст")
    if ("уведомл" in req or "предупрежд" in req) and not any(e.notify for e in spec.events):
        errors.append("В требованиях есть уведомления, но ни у одного события нет notify/recipient_field")
    if "демонстрационный сценарий" in req and (not spec.demo or len(spec.demo.steps) < 3):
        errors.append("Нет демо-сценария по разделу «Демонстрационный сценарий»")
    if spec.analysis.components.get("mobile") and not any(s.platform == "mobile" for s in spec.screens):
        errors.append("Требуется мобильное приложение, но нет экранов platform=mobile")
    roles = {r.name for r in spec.roles}
    entities = [e.name for e in spec.entities]
    if not spec.entities:
        errors.append("Нет ни одной сущности")
    for name in entities:
        if name in RESERVED:
            errors.append(f"Имя сущности {name} зарезервировано, переименуй")
    if len(set(entities)) != len(entities):
        errors.append("Имена сущностей должны быть уникальны")
    if len({n.lower() for n in [r.demo_login for r in spec.roles]}) != len(spec.roles):
        errors.append("demo_login ролей должны быть уникальны")
    for e in spec.entities:
        if not any("read" in p.actions or "read_own" in p.actions for p in spec.permissions if p.entity == e.name):
            errors.append(f"Сущность {e.name} не доступна ни одной роли на чтение")
        names = [f.name for f in e.fields]
        if len(set(names)) != len(names):
            errors.append(f"{e.name}: повторяющиеся имена полей")
        if any(n in ("id", "createdAt", "updatedAt") for n in names):
            errors.append(f"{e.name}: поля id/createdAt/updatedAt создаются автоматически, убери их")
        for f in e.fields:
            if f.regex:
                try:
                    re.compile(f.regex)
                except re.error:
                    errors.append(f"{e.name}.{f.name}: некорректное регулярное выражение")
        own = [p for p in spec.permissions if p.entity == e.name and ({"read_own", "update_own"} & set(p.actions))]
        if own and not e.owner_field:
            errors.append(f"{e.name}: есть права *_own, но не задан owner_field (поле ref на User)")
    seen_routes: set[tuple[str, str]] = set()
    for o in spec.operations:
        key = (o.method, re.sub(r"\{\w+\}", "{}", o.path))
        if key in seen_routes:
            errors.append(f"Операция {o.name}: дублирует маршрут {o.method} {o.path}")
        seen_routes.add(key)
        first = o.path.strip("/").split("/")
        if len(first) == 1 and o.method in ("GET", "POST"):
            errors.append(f"Операция {o.name}: путь {o.path} конфликтует с CRUD, добавь сегмент действия")
        for p in o.path_params:
            if not re.match(r"^[a-z][A-Za-z0-9]*$", p):
                errors.append(f"Операция {o.name}: параметр пути {p} должен быть camelCase")
    for ev in spec.events:
        names = {p.name for p in ev.payload}
        for ph in re.findall(r"\{(\w+)\}", ev.notify or ""):
            if ph not in names:
                errors.append(f"Событие {ev.name}: в шаблоне уведомления {{{ph}}} нет в payload")
    for ds in spec.datasets:
        if data_files is not None and ds.file not in data_files:
            errors.append(f"Датасет {ds.name}: файл {ds.file} не приложен (есть: {sorted(data_files)})")
    if spec.demo:
        events = {e.name for e in spec.events}
        op_names = {o.name for o in spec.operations}
        entity_names = {e.name for e in spec.entities}
        for st in spec.demo.steps:
            if st.action in ("operation", "route") and st.operation not in op_names:
                errors.append(f"Демо {st.id}: операции {st.operation} нет — используй существующую операцию или action=create с entity")
            if st.action == "create" and st.entity not in entity_names:
                errors.append(f"Демо {st.id}: create для неизвестной сущности {st.entity}")
            if st.action == "expect" and st.args.get("event") not in events:
                errors.append(f"Демо {st.id}: ожидается неизвестное событие {st.args.get('event')}")
    for r in spec.roles:
        if not re.match(r"^[a-z][a-z0-9_]*$", r.demo_login):
            errors.append(f"Роль {r.name}: demo_login должен быть латиницей в нижнем регистре")
    return errors


def coverage(spec: AppSpec) -> dict[str, list[str]]:
    """FR id -> artifacts that cover it."""
    cov: dict[str, list[str]] = {fr.id: [] for fr in spec.analysis.functional}
    def add(ids, what):
        for i in ids:
            cov.setdefault(i, []).append(what)
    for e in spec.entities:
        add(e.covers, f"сущность {e.name}")
    for o in spec.operations:
        add(o.covers, f"операция {o.id} {o.name}")
    for j in spec.jobs:
        add(j.covers, f"задача {j.name}")
    for s in spec.screens:
        add(s.covers, f"экран {s.id} {s.title}")
    for ac in spec.analysis.acceptance:
        add([ac.fr], f"критерий {ac.id}")
    return cov
