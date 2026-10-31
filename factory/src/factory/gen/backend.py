"""Backend generator: IR -> ASP.NET Core (.NET 10) solution with clean layering."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

from ..ir import AppSpec, Entity, Field, Operation
from . import naming as n
from .render import Renderer

STRINGY = {"string", "text", "email", "phone", "url"}
VALUE_TYPES = {"int", "decimal", "money", "bool", "date", "datetime", "enum", "ref"}
ADMIN_FALLBACK = 0


def is_geo(f: Field) -> bool:
    return f.type.startswith("geo") or f.type == "geometry"


def admin_roles(spec: AppSpec) -> list[str]:
    counts = {r.name: sum(1 for p in spec.permissions if p.role == r.name and "read" in p.actions)
              for r in spec.roles}
    top = max(counts.values(), default=0)
    return [r for r, c in counts.items() if c == top] or [spec.roles[0].name]


def own_roles(spec: AppSpec, entity: str) -> list[str]:
    return [p.role for p in spec.permissions if p.entity == entity
            and ({"read_own", "update_own"} & set(p.actions)) and "read" not in p.actions]


def owner_fields(spec: AppSpec, e: Entity) -> dict[str, list[str]]:
    """Ownership fields of an entity (entity-level and per-role) → roles that see «their» records through each."""
    out: dict[str, list[str]] = {}
    for p in spec.permissions:
        if p.entity == e.name and {"read_own", "update_own"} & set(p.actions):
            f = p.owner_field or e.owner_field
            if f and e.field(f):
                out.setdefault(f, []).append(p.role)
    if e.owner_field and e.owner_field not in out and e.field(e.owner_field):
        out[e.owner_field] = own_roles(spec, e.name)
    return out


def login_of(spec: AppSpec, role: str) -> str:
    return next(r.demo_login for r in spec.roles if r.name == role)


_NOT_UNITS = r"(?!\s*(шт|дн|сут|час|мин|руб|₽|%|мест|км|м\b|кг|раз))"
ORDINALS = ((re.compile(r"\b(трет\w*|3-?[йяеm]\w*)\b|\b[а-яёa-z]+\s+№?\s*3\b" + _NOT_UNITS, re.I), 3),
            (re.compile(r"\b(втор\w*|2-?[йяеm]\w*|ещё одн\w*|еще одн\w*|друг(ой|ая|ого) (пользов|сотрудн|пациент|клиент|участ))"
                        r"|\b[а-яёa-z]+\s+№?\s*2\b" + _NOT_UNITS, re.I), 2))


def pick_role(spec: AppSpec, roles: list[str], title: str) -> str:
    """Of the roles allowed to do a step, the one the step title names («HR согласует заявку» → HrManager, not Manager)."""
    low = title.lower()
    best, score = roles[0], 0
    for name in roles:
        r = next((x for x in spec.roles if x.name == name), None)
        if not r:
            continue
        words = {r.name.lower(), r.demo_login.lower(), *(w for w in re.findall(r"\w{2,}", r.title.lower()))}
        words |= {w[:-1] for w in words if len(w) > 4}  # «руководитель» ~ «руководителя»
        hit = max((len(w) for w in words if re.search(rf"(?<!\w){re.escape(w)}", low)), default=0)
        if hit > score:
            best, score = name, hit
    return best


def actor_login(spec: AppSpec, role: str, title: str) -> str:
    """Demo step actor: «второй пациент», «третий сотрудник» act as login2 / login3 of a non-admin role."""
    login = login_of(spec, role)
    if role in admin_roles(spec):
        return login
    r = next((x for x in spec.roles if x.name == role), None)
    stems = {w[:5] for w in ({role.lower(), login.lower()} | set(re.findall(r"\w{4,}", r.title.lower() if r else ""))) if len(w) >= 4}
    for rx, n_ in ORDINALS:
        for m in rx.finditer(title):
            # «Второй пациент», «пациент 2» — другой участник; «вторая зона», «второй час» — нет.
            nxt = re.match(r"\s*(\w+)", title[m.end():])
            words = re.findall(r"\w+", m.group(0).lower()) + ([nxt.group(1).lower()] if nxt else [])
            if any(w[:5] in stems or PEOPLE_RE.match(w) for w in words):
                return f"{login}{n_}"
    return login


PEOPLE_RE = re.compile(r"(пользоват|участ|пациент|клиент|сотрудн|человек|гост|покупат|заказчик|водител|курьер|user|member|customer)", re.I)


def payload_type(f: Field) -> str:
    opt = "" if f.required else "?"
    if is_geo(f) or f.type == "json":
        return "JsonElement?"
    if f.type == "enum" or f.type in STRINGY:
        return "string" + opt
    return n.cs_type(AppSpec.model_construct(), None, f)  # value type mapping only


def _range_attr(f: Field) -> str | None:
    if f.min is None and f.max is None:
        return None
    integral = f.type == "int"
    lo = f.min if f.min is not None else (-2147483648 if integral else -1e15)
    hi = f.max if f.max is not None else (2147483647 if integral else 1e15)
    fmt = (lambda v: str(int(v))) if integral else (lambda v: repr(float(v)))
    return (f'[Range({fmt(lo)}, {fmt(hi)}, ErrorMessage = "Поле «{f.title}» должно быть '
            f'в диапазоне {fmt(lo)}..{fmt(hi)}")]')


def _string_attrs(f: Field) -> list[str]:
    a = []
    if f.max_length and f.type != "text":
        a.append(f'[MaxLength({f.max_length}, ErrorMessage = "Поле «{f.title}»: не более '
                 f'{f.max_length} символов")]')
    if f.regex:
        a.append(f'[RegularExpression({n.cs_verbatim(f.regex)}, ErrorMessage = "Поле «{f.title}» '
                 'имеет неверный формат")]')
    elif f.type == "email":
        a.append(f'[EmailAddress(ErrorMessage = "Поле «{f.title}»: неверный адрес почты")]')
    elif f.type == "url":
        a.append(f'[Url(ErrorMessage = "Поле «{f.title}»: неверная ссылка")]')
    elif f.type == "phone":
        a.append(f'[RegularExpression(@"^\\+?[0-9 ()\\-]{{7,20}}$", ErrorMessage = "Поле «{f.title}»: '
                 'неверный номер телефона")]')
    return a


def value_base(spec: AppSpec, e: Entity | None, f: Field) -> str:
    return n.cs_type(spec, e, f, force_nullable=True).rstrip("?")


def field_vm(spec: AppSpec, e: Entity, f: Field) -> dict:
    name = n.pascal(f.name)
    vm: dict = {"name": f.name, "Name": name, "title": f.title, "unique": f.unique,
                "required": f.required, "type": f.type, "attrs": [], "ref_set": None,
                "geo_required": False, "label_expr": None, "filter_type": None}
    if f.required:
        vm["attrs"].append(f'[Required(ErrorMessage = "Поле «{f.title}» обязательно")]')
    geo = is_geo(f)
    if geo:
        vm["dto_type"], vm["to_dto"] = "JsonElement?", f"GeoJson.ToJson(e.{name})"
        vm["req_type"] = "JsonElement?"
        cast = " as Point" if f.type == "geo_point" else ""
        if f.required:
            vm["geo_required"] = True
            vm["to_entity"] = f"({f.name}Geometry{cast})!" if cast else f"{f.name}Geometry"
        else:
            vm["to_entity"] = f"GeoJson.FromJson(r.{name}){cast}"
    elif f.type == "json":
        vm["dto_type"], vm["to_dto"] = "JsonElement?", f"GeoJson.Raw(e.{name})"
        vm["req_type"] = "JsonElement?"
        vm["to_entity"] = f"r.{name}?.GetRawText()" + (' ?? "{}"' if f.required else "")
    else:
        vm["dto_type"], vm["to_dto"] = n.cs_type(spec, e, f), f"e.{name}"
        if f.type in STRINGY:
            vm["req_type"] = "string?"
            vm["attrs"] += _string_attrs(f)
            vm["to_entity"] = f'r.{name} ?? ""' if f.required else f"r.{name}"
        else:
            base = value_base(spec, e, f)
            vm["req_type"] = base + "?"
            if f.type in ("int", "decimal", "money") and (ra := _range_attr(f)):
                vm["attrs"].append(ra)
            if f.required:
                vm["to_entity"] = f"r.{name}.GetValueOrDefault()" + (
                    ".ToUniversalTime()" if f.type == "datetime" else "")
            else:
                vm["to_entity"] = f"r.{name}" + ("?.ToUniversalTime()" if f.type == "datetime" else "")
    if f.type in ("enum", "ref", "bool"):
        vm["filter_type"] = value_base(spec, e, f) + "?"
    if f.type == "ref":
        vm["ref_set"] = "Users" if f.ref == "User" else n.plural(f.ref)
        vm["label_expr"] = ref_label_expr(spec, f.ref)
    return vm


def ref_label_expr(spec: AppSpec, ref: str) -> str:
    if ref == "User":
        return "x.DisplayName"
    target = spec.entity(ref)
    if target is None:
        return "x.Id.ToString()"
    return entity_label_expr(target)


def entity_label_expr(e: Entity) -> str:
    lf = e.label_field
    f = e.field(lf)
    if f is None or lf == "id":
        return "x.Id.ToString()"
    if f.type in STRINGY:
        return f"x.{n.pascal(lf)}"
    return f"x.{n.pascal(lf)}.ToString()"


def geo_prop_expr(spec: AppSpec, e: Entity, f: Field) -> str | None:
    if is_geo(f) or f.type in ("json", "text"):
        return None
    name = f"x.{n.pascal(f.name)}"
    if f.type in ("enum", "ref", "date"):
        return f"{name}.ToString()" if f.required else f"{name}?.ToString()"
    return name


def transition_payload(spec: AppSpec, e: Entity, event_name: str) -> str | None:
    ev = next((x for x in spec.events if x.name == event_name), None)
    if ev is None:
        return None
    args = []
    for p in ev.payload:
        ptype = payload_type(p)
        if p.name in ("id", n.camel(e.name) + "Id"):
            args.append("entity.Id.ToString()" if ptype.startswith("string") else "entity.Id")
            continue
        src = e.field(p.name)
        if src is None:
            args.append("default")
        elif ptype.startswith("string") and src.type not in STRINGY:
            args.append(f"entity.{n.pascal(src.name)}" + (".ToString()" if src.required else "?.ToString()"))
        else:
            args.append(f"entity.{n.pascal(src.name)}")
    return ", ".join(args)


def entity_vm(spec: AppSpec, e: Entity) -> dict:
    fields = [field_vm(spec, e, f) for f in e.fields]
    lifecycle_field = e.lifecycle.field if e.lifecycle else None
    search = [n.pascal(f.name) for f in e.fields if f.searchable and f.type in STRINGY]
    if not search:
        lf = e.field(e.label_field)
        if lf and lf.type in STRINGY:
            search = [n.pascal(lf.name)]
    geo = next((fv for fv, f in zip(fields, e.fields) if is_geo(f)), None)
    vm = {
        "set": n.plural(e.name),
        "fields": fields,
        "upsert_fields": [fv for fv, f in zip(fields, e.fields)
                          if fv["name"] != lifecycle_field and not f.readonly],
        "filter_fields": [fv for fv in fields if fv["filter_type"]],
        "sort_fields": [fv for fv, f in zip(fields, e.fields)
                        if not is_geo(f) and f.type not in ("json", "text")],
        "search_fields": search,
        "ref_fields": [fv for fv in fields if fv["type"] == "ref"],
        "Owner": n.pascal(e.owner_field) if e.owner_field else None,
        "own_filters": [{"role": p.role, "Field": n.pascal(p.owner_field)} for p in spec.permissions
                        if p.entity == e.name and p.owner_field and p.owner_field != e.owner_field],
        "geo": geo,
        "label_expr": entity_label_expr(e),
        "geo_props": [{"name": f.name, "expr": x} for f in e.fields if f.list_visible
                      and (x := geo_prop_expr(spec, e, f))],
        "transition_payloads": {},
        "autonumber": [{"Name": n.pascal(f.name), "prefix": _number_prefix(e, f), "max": f.max_length or 32}
                       for f in e.fields if f.readonly and f.required and f.type == "string"],
    }
    if e.lifecycle:
        lf = e.field(e.lifecycle.field)
        vm["Lifecycle"] = n.pascal(e.lifecycle.field)
        vm["lifecycle_enum"] = n.enum_name_for(spec, e, lf)
        for t in e.lifecycle.transitions:
            if t.emits:
                vm["transition_payloads"][t.name] = transition_payload(spec, e, t.emits)
    return vm


def _number_prefix(e: Entity, f: Field) -> str:
    if f.regex:
        m = re.match(r"^\^?([A-ZА-Я]{1,4})", f.regex)
        if m:
            return m.group(1)
    return "".join(ch for ch in e.name if ch.isupper())[:3] or "N"


def op_input_vm(f: Field, method: str) -> dict:
    name = n.pascal(f.name)
    vm = {"name": f.name, "Name": name, "attrs": []}
    body = method != "GET"
    if f.required and not body:
        vm["attrs"].append(f'[Required(ErrorMessage = "Поле «{f.title}» обязательно")]')
    if f.type in STRINGY:
        vm["attrs"] += _string_attrs(f)
        vm["req_type"] = ("required string" if (f.required and body) else "string?")
    elif is_geo(f) or f.type == "json":
        vm["req_type"] = "JsonElement?"
    else:
        base = value_base(AppSpec.model_construct(), None, f) if f.type != "enum" else "string"
        if f.type in ("int", "decimal", "money") and (ra := _range_attr(f)):
            vm["attrs"].append(ra)
        vm["req_type"] = f"required {base}" if (f.required and body) else base + "?"
    return vm


def op_vm(spec: AppSpec, op: Operation) -> dict:
    params = [(p, "Guid" if p.endswith("Id") or p == "id" else "string") for p in op.path_params]
    if op.output_entity:
        rt = f"{op.output_entity}Dto"
        rt = f"IReadOnlyList<{rt}>" if op.output_list else rt
    else:
        rt = f"{op.name}Response"
    sig = [f"{t} {p}" for p, t in params]
    if op.input:
        sig.append(f"{op.name}Request request")
    sig.append("CancellationToken ct")
    route = "api/v1/" + op.path.lstrip("/")
    route_attr = op.path.lstrip("/")
    for p, t in params:
        if t == "Guid":
            route_attr = route_attr.replace("{" + p + "}", "{" + p + ":guid}")
    action_params = "".join(f", {t} {p}" for p, t in params)
    call_args = "".join(f"{p}, " for p, _ in params)
    if op.input:
        action_params += f", [From{'Query' if op.method == 'GET' else 'Body'}] {op.name}Request request"
        call_args += "request, "
    return {
        "op": op, "inputs": [op_input_vm(f, op.method) for f in op.input],
        "response_record": not op.output_entity, "return_type": rt,
        "signature": ", ".join(sig), "route": route_attr, "full_route": route,
        "action_params": action_params, "call_args": call_args,
    }


# --------------------------------------------------------------------------- seed / demo / mocks

def _uid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join(parts)))


def seed_users(spec: AppSpec) -> list[dict]:
    users = []
    own = {p.role for p in spec.permissions if {"read_own", "update_own"} & set(p.actions)}
    admins = set(admin_roles(spec)) if spec.permissions else set()
    for r in spec.roles:
        # Полевые роли — три пользователя: «свои» записи и сценарии со вторым и третьим участником.
        logins = [r.demo_login] + ([r.demo_login + "2", r.demo_login + "3"] if r.name in own or r.name not in admins else [])
        for i, login in enumerate(logins):
            users.append({"id": _uid(spec.slug, "user", login), "login": login, "role": r.name,
                          "displayName": f"{r.title} {i + 1}" if len(logins) > 1 else r.title})
    return users


def _topo(spec: AppSpec) -> list[Entity]:
    done: list[Entity] = []
    pending = list(spec.entities)
    while pending:
        progressed = False
        for e in list(pending):
            deps = {f.ref for f in e.fields if f.type == "ref" and f.ref not in ("User", e.name)}
            if deps <= {d.name for d in done} | {x.name for x in spec.entities if x not in pending}:
                done.append(e)
                pending.remove(e)
                progressed = True
        if not progressed:
            done += pending
            break
    return done


CAPACITY_RE = re.compile(r"capacit|limit|max|total|balance|вмест|лимит", re.I)
UPCOMING_RE = re.compile(r"start|begin|slot|due|deadline|scheduled|planned|from|until|ends?At|expires", re.I)


def upcoming_field(e: Entity):
    """Datetime field that says when a record happens (slot start, deadline): the demo prefers records still ahead."""
    return next((f for f in e.fields if f.type == "datetime" and UPCOMING_RE.search(f.name)), None)


def _sample(spec: AppSpec, e: Entity, f: Field, i: int):
    center = getattr(spec, "map_center", None) or [55.7558, 37.6173]
    if f.type in ("int", "decimal", "money") and CAPACITY_RE.search(f.name) and not re.search(r"min|threshold|порог", f.name, re.I):
        # Ёмкости и лимиты щедрые: демо-сценарий не должен упираться в тестовые данные.
        hi = f.max if f.max is not None else 10 ** 7
        value = min(1000 + i * 100, hi)
        return int(value) if f.type == "int" else float(value)
    if f.examples and f.type not in ("enum", "json", "geo_point", "geo_line", "geo_polygon", "geometry", "ref"):
        v = f.examples[i % len(f.examples)]
        try:
            if f.type == "int":
                return int(float(str(v).replace(",", ".")))
            if f.type in ("decimal", "money"):
                return float(str(v).replace(",", "."))
            if f.type == "bool":
                return str(v).strip().lower() in ("true", "1", "да", "yes")
            if f.type == "datetime":
                return v if _iso(v) else None
            if f.type == "date":
                return v if re.match(r"^\d{4}-\d{2}-\d{2}$", str(v)) else None
        except ValueError:
            return None
        return str(v)[: f.max_length or 10000]
    t = f.type
    if t == "enum":
        vals = f.enum_values or []
        if vals and re.search(r"status|state|condition|availab|состоян", f.name, re.I):
            # Статус объекта (инструмент, место): большинство записей в рабочем состоянии, иначе демо упрётся
            # в «недоступно»; каждая третья показывает остальные значения.
            normal = f.default if f.default in vals else vals[0]
            rest = [v for v in vals if v != normal] or [normal]
            return normal if i % 3 != 2 else rest[(i // 3) % len(rest)]
        return (f.default if i == 0 and f.default in vals else vals[i % len(vals)]) if vals else None
    if t in ("string",):
        if f.regex:
            return _regex_sample(f, i)
        return f"{f.title} {i + 1}"[: f.max_length or 255]
    if t == "text":
        return f"{f.title}: тестовая запись {i + 1}"
    if t == "int":
        name = f.name.lower()
        lo = int(f.min) if f.min is not None else 1
        hi = int(f.max) if f.max is not None else lo + 100000
        if re.search(r"capacit|limit|max|total|balance|stock|остат|вмест|лимит", name) and not re.search(r"min|threshold|порог", name):
            return min(max(lo, 1000) + i * 100, hi)  # ёмкости и остатки: сценарий не должен упираться в тестовые данные
        if re.search(r"min|threshold|порог", name):
            return min(max(lo, 10), hi)
        return min(lo + i * 5, hi)
    if t in ("decimal", "money"):
        lo = float(f.min) if f.min is not None else 10.0
        hi = float(f.max) if f.max is not None else lo + 10000
        return round(min(lo + 12.5 * (i + 1), hi), 2)
    if t == "bool":
        # «Да» — только для признаков доступности (активен, доступен, включён); занятость, оплата, отметки — «нет»,
        # иначе тестовые данные заранее «заняты» и сценарию нечего бронировать.
        return bool(re.search(r"activ|enabl|availab|visib|open|valid|allow|public|free", f.name.lower()))
    if t == "date":
        return f"2026-10-{(i % 27) + 1:02d}"
    if t == "datetime":
        if UPCOMING_RE.search(f.name) and i % 2 == 1:
            # Начала и сроки: половина записей — предстоящие (слоты, брони), половина — история.
            from datetime import datetime, timedelta, timezone
            day = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0) + timedelta(days=1 + i // 2)
            return day.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Прошедшие даты: тестовые записи не пересекаются с демо-сценарием, который идёт «сейчас».
        return f"2026-09-{(i % 27) + 1:02d}T{(8 + i) % 24:02d}:00:00Z"
    if t == "email":
        return f"user{i + 1}@example.com"
    if t == "phone":
        return f"+7900000{i + 1:04d}"
    if t == "url":
        return f"https://example.com/{n.kebab(e.name)}/{i + 1}"
    if t == "json":
        return "{}"
    if t == "geo_point":
        return {"type": "Point", "coordinates": [center[1] + 0.01 * i, center[0] + 0.005 * i]}
    if f.required and t in ("geo_line", "geometry"):
        return {"type": "LineString", "coordinates": [[center[1], center[0]],
                                                       [center[1] + 0.002, center[0] + 0.001]]}
    if f.required and t == "geo_polygon":
        lo, la = center[1] + 0.01 * i, center[0]
        return {"type": "Polygon", "coordinates": [[[lo, la], [lo + .004, la], [lo + .004, la + .002],
                                                    [lo, la + .002], [lo, la]]]}
    return None


def _regex_sample(f: Field, i: int) -> str | None:
    """Deterministic value matching the field regex (for seed data)."""
    import random

    import rstr
    rnd = random.Random(f"{f.name}:{i}")
    gen = rstr.Rstr(rnd)
    for _ in range(30):
        try:
            v = gen.xeger(f.regex)
        except Exception:
            return None
        if re.fullmatch(f.regex, v) and len(v) <= (f.max_length or 255):
            return v
    return None


def seed_data(spec: AppSpec) -> dict:
    users = seed_users(spec)
    dataset_entities = {d.entity for d in spec.datasets}
    ids: dict[str, list[str]] = {}
    rows_of: dict[str, list[dict]] = {}
    blocks = []
    # Записи демо-пользователей (основные логины) и всё, что на них ссылается, получают завершающий статус:
    # история не мешает сценарию («уже есть активная сессия»), а записи вторых пользователей остаются в работе.
    base_users = {u["id"] for u in users if not u["login"][-1:].isdigit()}
    demo_ids: set[str] = set()
    for e in _topo(spec):
        if e.name in dataset_entities or e.seed_count <= 0:
            continue
        own = own_roles(spec, e.name)
        owners = [u["id"] for u in users if u["role"] in own] or [u["id"] for u in users]
        # Каждое поле владения заполняется пользователями «своих» ролей: у демо-пользователя всегда есть свои записи.
        by_field = {name: [u["id"] for u in users if u["role"] in roles] or owners
                    for name, roles in owner_fields(spec, e).items()}
        rows = []
        count = e.seed_count
        # «Человеческая» сущность (Employee) со ссылкой на себя по роли (managerId): руководители — тоже её записи,
        # иначе ссылке «руководитель» не на кого указывать.
        self_roles = [r.name for f in e.fields if f.type == "ref" and f.ref == e.name
                      for r in spec.roles if r.name.lower() in f.name.lower()]
        person_pool = []
        if any(f.name == "userId" and f.type == "ref" and f.ref == "User" for f in e.fields):
            role = next((r.name for r in spec.roles if r.name.lower() == e.name.lower()
                         or e.name.lower().startswith(r.name.lower()) and len(r.name) > 3), None)
            roles = ([role] if role else []) + self_roles
            person_pool = [u["id"] for u in users if u["role"] in roles] if roles else []
            uid = next(f for f in e.fields if f.name == "userId")
            if person_pool:
                count = len(person_pool) if uid.unique else max(count, len(person_pool))
        user_role = {u["id"]: u["role"] for u in users}
        for f in e.fields:
            if f.unique and f.examples and not f.regex:
                count = min(count, len(set(f.examples)))
            # Уникальная ссылка (один профиль на пользователя): записей не больше, чем есть на что ссылаться.
            if f.unique and f.type == "ref" and f.ref != e.name:
                count = min(count, len(person_pool) if f.name == "userId" and person_pool
                            else len(users) if f.ref == "User" else len(ids.get(f.ref, [])) or count)
        used: dict[str, set] = {}
        for i in range(count):
            row: dict = {"id": _uid(spec.slug, e.name, str(i))}
            for f in e.fields:
                if e.lifecycle and f.name == e.lifecycle.field:
                    row[f.name] = e.lifecycle.initial
                elif f.name == "userId" and person_pool:
                    row[f.name] = person_pool[i % len(person_pool)]
                elif f.name in by_field:
                    row[f.name] = by_field[f.name][i % len(by_field[f.name])]
                elif f.name == e.owner_field:
                    row[f.name] = owners[i % len(owners)]
                elif f.type == "ref":
                    if f.ref == e.name:
                        row[f.name] = None  # ссылка на запись этой же сущности — заполняется после всех строк
                    elif f.ref == "User":
                        # Поле со ссылкой на пользователя, названное по роли (managerUserId, dispatcherId), заполняется
                        # пользователями этой роли: демо-руководитель — руководитель записей из тестовых данных.
                        role = next((r.name for r in spec.roles if f.name == "userId" and (r.name.lower() == e.name.lower()
                                     or e.name.lower().startswith(r.name.lower()) and len(r.name) > 3)), None) or next(
                            (r.name for r in spec.roles if r.name.lower() in f.name.lower()), None)
                        pool = [u["id"] for u in users if u["role"] == role] if role else []
                        row[f.name] = (pool or [u["id"] for u in users])[i % len(pool or users)] if (f.required or pool) else None
                    elif ids.get(f.ref):
                        # Ссылка, названная по роли (employeeId, dispatcherId), указывает на записи людей этой роли.
                        role = next((r.name for r in spec.roles if r.name.lower() in f.name.lower()), None)
                        pool = [r["id"] for r in rows_of.get(f.ref, []) if role and user_role.get(r.get("userId")) == role]
                        pool = pool or ids[f.ref]
                        row[f.name] = pool[i % len(pool)]
                    else:
                        row[f.name] = None
                else:
                    v = _sample(spec, e, f, i)
                    if f.unique and v is not None and v in used.setdefault(f.name, set()):
                        v = _regex_sample(f, i) if f.regex else f"{v}-{i + 1}"
                    if v is not None:
                        if f.unique and isinstance(v, (str, int, float)):
                            used.setdefault(f.name, set()).add(v)
                        row[f.name] = v
            owners_here = [row.get(name) for name in owner_fields(spec, e)] + ([row.get(e.owner_field)] if e.owner_field else [])
            refs_here = [row.get(f.name) for f in e.fields if f.type == "ref" and f.ref != "User"]
            if any(o in base_users for o in owners_here if o) or any(r in demo_ids for r in refs_here if r):
                demo_ids.add(row["id"])
                if e.lifecycle:
                    lf = e.field(e.lifecycle.field)
                    sources = {s for t in e.lifecycle.transitions for s in t.from_states}
                    terminal = [v for v in (lf.enum_values if lf else []) if v not in sources and v != e.lifecycle.initial]
                    if terminal:
                        row[e.lifecycle.field] = terminal[0]
            rows.append(row)
        for f in e.fields:
            if f.type == "ref" and f.ref == e.name and rows:
                role = next((r.name for r in spec.roles if r.name.lower() in f.name.lower()), None)
                # Основной демо-пользователь роли — первый кандидат: у демо-сотрудника руководитель — демо-руководитель.
                cands = [r for r in rows if role and user_role.get(r.get("userId")) == role] or rows
                for r in rows:
                    other = next((c for c in cands if c is not r), None)
                    r[f.name] = other["id"] if other else None
                # Вставка по порядку: сначала записи, на которые ссылаются (внешний ключ проверяется построчно).
                done, ordered = set(), []
                while len(ordered) < len(rows):
                    ready = [r for r in rows if r["id"] not in done and (r[f.name] is None or r[f.name] in done)]
                    for r in ready or [r for r in rows if r["id"] not in done]:
                        r[f.name] = r[f.name] if r[f.name] is None or r[f.name] in done else None
                        done.add(r["id"])
                        ordered.append(r)
                rows[:] = ordered
        if not rows:
            continue
        rows_of[e.name] = rows
        ids[e.name] = [r["id"] for r in rows]
        blocks.append({"entity": e.name, "rows": rows})
    return {"users": users, "entities": blocks}


def demo_script(spec: AppSpec) -> dict:
    if not spec.demo:
        return {"title": "Демонстрационный сценарий не задан", "steps": []}
    ops = {o.name: o for o in spec.operations}
    datasets_geo = [d.entity for d in spec.datasets if (x := spec.entity(d.entity)) and x.geo_fields]
    geo_entities = [e.name for e in spec.entities if e.geo_fields]
    steps = []
    base = _demo_time_base(spec)
    produced: list[str] = []  # сущности, созданные предыдущими шагами (по порядку)
    created_by: list[tuple[str, str]] = []  # (сущность, логин создателя) — для «записи второго участника»
    for s in spec.demo.steps:
        if s.action == "create" and s.entity:
            produced.append(s.entity)
        elif s.action == "operation" and (o := ops.get(s.operation or "")) and o.output_entity:
            produced.append(o.output_entity)
        st: dict = {"id": s.id, "title": s.title, "action": s.action, "expectError": s.expect_error}
        op = ops.get(s.operation or "")
        if op:
            st["method"] = op.method
            st["path"] = "api/v1/" + op.path.lstrip("/")
            st["login"] = actor_login(spec, pick_role(spec, op.roles, s.title), s.title)
        if s.action == "route" and op:
            path_entities = {p[:-2].lower() for p in op.path_params}
            # Движение к объекту, созданному ранее в сценарии (заказ, задача с адресом), — маршрут к его точке.
            target = next((g for g in reversed(produced) if g in geo_entities and g.lower() not in path_entities), None)
            layer = s.args.get("layer") or target or next(
                (g for g in datasets_geo + geo_entities if g.lower() not in path_entities), None)
            st["layer"] = layer
            if target and layer == target:
                st["targetVar"] = n.camel(target) + "Id"
            st["zones"] = int(s.args.get("zones", 2))
            st["leg"] = int(s.args.get("leg", 0))
            names = {f.name.lower(): f.name for f in op.input}
            st["latField"] = next((names[k] for k in ("lat", "latitude") if k in names), "lat")
            st["lonField"] = next((names[k] for k in ("lon", "lng", "longitude") if k in names), "lon")
            point = next((f.name for f in op.input if f.type == "geo_point"), None)
            if point and not ({"lat", "latitude"} & set(names)):
                st["pointField"] = point  # операция принимает точку одним полем (GeoJSON)
            st["extra"] = {f.name: _default_input(f) for f in op.input
                           if f.name not in (st["latField"], st["lonField"], point)
                           and (f.required or "speed" in f.name.lower())}
        elif s.action == "operation" and op:
            st["args"] = _fit_args(op.input, {**{f.name: _default_input(f) for f in op.input if f.required and f.name not in s.args
                                                 and f.name not in op.path_params}, **s.args}, s.expect_error)
            st["resolve"] = {**_missing_refs(spec, op.input, s.args, st["login"], op.path_params),
                             **demo_resolvers(spec, op, s.args), **_time_resolvers(s.args, base),
                             **_zone_point_resolvers(spec, op.input, s.args, s.expect_error),
                             **_generated_field_resolvers(spec, st["args"], produced[:-1] if op.output_entity and
                                                          produced and produced[-1] == op.output_entity else produced,
                                                          created_by, s.title)}
            if op.output_entity:
                st["outputVar"] = n.camel(op.output_entity) + "Id"
                out_entity = spec.entity(op.output_entity)
                st["updatesExisting"] = bool(out_entity) and any(p in id_aliases(spec, out_entity) for p in op.path_params)
                if not st["updatesExisting"]:
                    created_by.append((op.output_entity, st["login"]))
        elif s.action == "create" and s.entity and spec.entity(s.entity):
            e = spec.entity(s.entity)
            creators = [p.role for p in spec.permissions if p.entity == e.name and "create" in p.actions]
            st["method"] = "POST"
            st["path"] = f"api/v1/{n.route(e)}"
            st["login"] = actor_login(spec, pick_role(spec, creators, s.title), s.title) if creators else spec.roles[0].demo_login
            st["args"] = {**{f.name: _default_input(f) for f in e.fields if f.required and not f.readonly and f.name not in s.args
                             and f.default is None and not (e.lifecycle and f.name == e.lifecycle.field)}, **s.args}
            st["args"] = _fit_args(e.fields, st["args"], s.expect_error)
            fake = Operation(id="X", name="Create", title="", description="", path="/x", roles=[creators[0] if creators else spec.roles[0].name],
                             input=[f for f in e.fields])
            st["resolve"] = {**_missing_refs(spec, e.fields, s.args, st["login"], []),
                             **demo_resolvers(spec, fake, s.args), **_time_resolvers(s.args, base),
                             **_zone_point_resolvers(spec, e.fields, s.args, s.expect_error)}
            st["outputVar"] = n.camel(e.name) + "Id"
            st["action"] = "operation"
            created_by.append((e.name, st["login"]))
            # CRUD создаёт запись в начальном статусе; если сценарий просит другой — проводим её по переходам.
            wanted = s.args.get(e.lifecycle.field) if e.lifecycle else None
            if e.lifecycle and isinstance(wanted, str) and wanted != e.lifecycle.initial:
                chain = _transition_chain(e, e.lifecycle.initial, wanted)
                st["thenTransitions"] = [{"path": f"api/v1/{n.route(e)}/{{id}}/{n.kebab(t.name)}",
                                          "login": login_of(spec, t.roles[0]) if t.roles else st["login"]} for t in chain]
        elif s.action == "transition" and s.entity:
            e = spec.entity(s.entity)
            t = next((t for t in (e.lifecycle.transitions if e and e.lifecycle else [])
                      if t.name == s.transition), None)
            st["path"] = f"api/v1/{n.route(s.entity)}/{{{n.camel(s.entity)}Id}}/{n.kebab(s.transition or '')}"
            st["login"] = actor_login(spec, pick_role(spec, t.roles, s.title), s.title) if t else spec.roles[0].demo_login
        elif s.action == "view" and s.entity and spec.entity(s.entity):
            e = spec.entity(s.entity)
            readers = [p.role for p in spec.permissions if p.entity == e.name and "read" in p.actions] or admin_roles(spec)
            st["method"], st["path"], st["login"] = "GET", f"api/v1/{n.route(e)}", login_of(spec, readers[0])
        elif s.action == "advance_time":
            st["minutes"] = s.minutes or int(s.args.get("minutes", 10))
            if jump := _reminder_jump(spec, s, produced):
                st["jumpTo"] = jump
        elif s.action == "expect":
            st["event"] = s.args.get("event", "")
        steps.append(st)
    return {"title": spec.demo.title, "timeBase": base is not None, "timeBaseOffset": _time_base_offset(spec),
            "adminLogin": login_of(spec, admin_roles(spec)[0]) if spec.permissions else spec.roles[0].demo_login, "steps": steps}


def _fit_args(fields: list[Field], args: dict, negative: bool = False) -> dict:
    """Demo values that break the field's own rules (ISBN «978-5-17-…» into 13 characters, a code against its pattern)
    are brought to the form the API accepts: separators removed, then a value generated from the pattern."""
    out = dict(args)
    if negative:  # шаг проверяет отказ на неверных данных — значения не трогаем
        return out
    for f in fields:
        v = out.get(f.name)
        if not isinstance(v, str) or v.startswith("$") or f.type not in ("string", "phone", "email", "text"):
            continue
        def ok(x: str) -> bool:
            try:
                return (not f.max_length or len(x) <= f.max_length) and (not f.regex or re.fullmatch(f.regex, x) is not None)
            except re.error:
                return not f.max_length or len(x) <= f.max_length
        if ok(v):
            continue
        compact = re.sub(r"[\s\-–()]", "", v)
        out[f.name] = compact if ok(compact) else (_regex_sample(f, 0) if f.regex else v[: f.max_length])
    return out


def _missing_refs(spec: AppSpec, fields: list[Field], args: dict, login: str, path_params: list[str]) -> dict:
    """Required references the scenario did not give (organizerId of a new event): the acting user for a User ref,
    the record made earlier in the scenario (or the first available) for an entity ref."""
    out = {}
    for f in fields:
        if f.type != "ref" or not f.required or f.readonly or f.name in args or f.name in path_params:
            continue
        if f.ref == "User":
            # assigneeEngineerId → пользователь роли «инженер»; organizerId → сам действующий пользователь.
            role = next((r.name for r in spec.roles if r.name.lower() in f.name.lower()), None)
            out[f.name] = {"kind": "user", "login": login_of(spec, role) if role else login}
        elif spec.entity(f.ref):
            out[f.name] = {"kind": "entity", "var": n.camel(f.ref) + "Id"}
    return out


def _transition_chain(e: Entity, start: str, target: str) -> list:
    """Shortest sequence of lifecycle transitions from start to target (BFS); empty if unreachable."""
    from collections import deque
    queue, seen = deque([(start, [])]), {start}
    while queue:
        state, path = queue.popleft()
        if state == target:
            return path
        for t in e.lifecycle.transitions:
            if state in t.from_states and t.to not in seen:
                seen.add(t.to)
                queue.append((t.to, path + [t]))
    return []


def _zone_point_resolvers(spec: AppSpec, inputs: list[Field], args: dict, outside: bool) -> dict:
    """A point argument (geo_point input) is placed inside a real area of the geo layer (zone of service, parking zone);
    for a step that checks a refusal — far outside of all areas. Hard-coded demo coordinates rarely hit generated zones."""
    areas = [e for e in spec.entities if any(f.type in ("geo_polygon", "geometry", "geo_line") for f in e.fields)]
    if not areas:
        return {}
    area = areas[0]
    geo = next(f for f in area.fields if f.type in ("geo_polygon", "geometry", "geo_line"))
    out = {}
    for f in inputs:
        if f.type == "geo_point" and f.name in args:
            v = args[f.name]
            shape = "latlon" if isinstance(v, dict) and ("lat" in v or "latitude" in v) else "geojson"
            out[f.name] = {"kind": "zonepoint", "outside": bool(outside), "shape": shape,
                           "sql": f"SELECT ST_Y(ST_PointOnSurface({n.column(geo)}::geometry)), ST_X(ST_PointOnSurface({n.column(geo)}::geometry)) "
                                  f"FROM {n.table(area)} WHERE {n.column(geo)} IS NOT NULL ORDER BY created_at LIMIT 1"}
    return out


def _ordinal(title: str) -> int | None:
    for rx, n_ in ((re.compile(r"\b(перв\w*|1-?[йяеm]\w*)\b", re.I), 1), *[(rx, k) for rx, k in ORDINALS]):
        if rx.search(title):
            return n_
    return None


def _generated_field_resolvers(spec: AppSpec, args: dict, produced: list[str],
                               created_by: list[tuple[str, str]] | None = None, title: str = "") -> dict:
    """Inputs that repeat a system-generated field of a record created earlier (access code, number) are taken from that
    record: the demo cannot know the value the system generated."""
    out = {}
    for key in args:
        for name in reversed(produced):
            e = spec.entity(name)
            f = e.field(key) if e else None
            if f and f.readonly and f.type not in ("ref", "geo_point", "geo_line", "geo_polygon", "geometry", "datetime", "date"):
                var = n.camel(e.name) + "Id"
                lookup = var
                k = _ordinal(title)
                logins = [lg for ent, lg in (created_by or []) if ent == e.name]
                if k and logins:
                    match = next((lg for lg in logins if (lg[-1:] == str(k)) if k > 1), None) if k > 1 else \
                        next((lg for lg in logins if not lg[-1:].isdigit()), None)
                    if match:
                        lookup = f"{var}@{match}"
                out[key] = {"kind": "field", "var": lookup, "path": f"api/v1/{n.route(e)}/{{{lookup}}}", "field": key}
                break
    return out


LEAD_RE = re.compile(r"(\d+)\s*(минут|мин|minute|min|час|ч\b|hour|сут|дн|день|day)", re.I)


def _lead_minutes(text: str, default: int) -> int:
    m = LEAD_RE.search(text or "")
    if m:
        k = m.group(2).lower()
        return int(m.group(1)) * (1 if k.startswith(("мин", "min")) else 1440 if k.startswith(("сут", "дн", "ден", "day")) else 60)
    if re.search(r"за\s+сутки|за\s+день|day before", text or "", re.I):
        return 1440
    if re.search(r"за\s+час", text or "", re.I):
        return 60
    return default


def _reminder_jump(spec: AppSpec, step, produced: list[str]) -> dict | None:
    """«Сдвиг времени, чтобы пришло напоминание о приёме»: a fixed shift rarely lands in the reminder window of a real
    record (its slot is tomorrow). The runner moves the clock to the middle of the window before the record's time —
    the time field is taken from the record itself or one reference away (appointment → slot.startTime)."""
    steps = spec.demo.steps
    i = steps.index(step)
    nxt = next((s for s in steps[i + 1:] if s.action != "advance_time"), None)
    ev = spec.event(nxt.args.get("event", "")) if nxt and nxt.action == "expect" and hasattr(spec, "event") else None
    if ev is None and nxt and nxt.action == "expect":
        ev = next((x for x in spec.events if x.name == nxt.args.get("event")), None)
    if not ev or not (REMINDER_RE.search(ev.name) or REMINDER_RE.search(ev.title)):
        return None
    keyed = [e for e in spec.entities if n.camel(e.name) + "Id" == ev.key_field]
    for e in keyed + [spec.entity(x) for x in reversed(produced) if spec.entity(x) not in keyed]:
        if not e:
            continue
        sql = None
        if f := upcoming_field(e):
            sql = f"SELECT {n.column(f)} FROM {n.table(e)} WHERE id = @id"
        else:
            for r in e.fields:
                t = spec.entity(r.ref) if r.type == "ref" and r.ref not in ("User", e.name) else None
                if t and (tf := upcoming_field(t)):
                    sql = (f"SELECT t.{n.column(tf)} FROM {n.table(e)} s JOIN {n.table(t)} t ON t.id = s.{n.column(r)} "
                           f"WHERE s.id = @id")
                    break
        if sql:
            job = next((j for j in spec.jobs if ev.name in j.emits), None)
            window = _lead_minutes(f"{job.description} {job.title}" if job else "", step.minutes or 120)
            return {"var": n.camel(e.name) + "Id", "sql": sql, "leadMinutes": max(1, window // 2)}
    return None


def _time_base_offset(spec: AppSpec) -> int:
    """Where the earliest demo time lands relative to now. Reminder-type checks («за 2 часа до приёма») need the moment
    still ahead after the clock is advanced; deadline-type checks need it behind — the default hour does that."""
    advanced = 0
    events = {e.name: e for e in spec.events}
    for st in spec.demo.steps:
        if st.action == "advance_time":
            advanced += st.minutes or int(st.args.get("minutes", 10) or 10)
        elif st.action == "expect" and advanced:
            ev = events.get(st.args.get("event", ""))
            if ev and (REMINDER_RE.search(ev.name) or REMINDER_RE.search(ev.title)):
                return advanced + 1
    return 60


def _iso(v):
    from datetime import datetime
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", v):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _default_input(f: Field):
    """Value for a required input the demo step did not specify."""
    if f.type == "datetime":
        return "$now"
    if f.type in ("int", "decimal", "money"):
        if "speed" in f.name.lower():
            return 25
        return f.min if f.min is not None and f.min > 0 else (1 if f.type == "int" else 1.0)
    if f.type == "bool":
        return False
    if f.type == "enum" and f.enum_values:
        return f.enum_values[0]
    if f.type == "date":
        return "$today"
    return f.examples[0] if f.examples else "Демо"


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REMINDER_RE = re.compile(r"remind|upcoming|soon|before|approach|напомин|предупреж|скоро|приближ", re.I)


def _parse_date(v: str):
    """ISO date that tolerates impossible days from the model (2026-02-30 → 2026-02-28)."""
    from calendar import monthrange
    from datetime import datetime
    y, m, d = (int(x) for x in v.split("-"))
    m = min(max(m, 1), 12)
    return datetime(y, m, min(max(d, 1), monthrange(y, m)[1]))
CLOCK_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _demo_time_base(spec: AppSpec):
    """Earliest moment mentioned in demo args: full ISO, or earliest date + earliest HH:MM."""
    from datetime import datetime, timedelta
    values = [v for st in spec.demo.steps for v in st.args.values() if isinstance(v, str)]
    full = sorted(t for v in values if (t := _iso(v)))
    if full:
        return full[0]
    dates = sorted(v for v in values if DATE_RE.match(v))
    clocks = sorted(v for v in values if CLOCK_RE.match(v))
    if not dates and not clocks:
        return None
    day = _parse_date(dates[0]) if dates else datetime(2026, 1, 1)
    # «Расписание на завтра»: первая дата сценария — завтрашний день (операции с датой «в будущем» иначе откажут).
    if dates and any(dates[0] in st.args.values() and TOMORROW_RE.search(st.title) for st in spec.demo.steps):
        day -= timedelta(days=1)
    h, m = (clocks[0].split(":") if clocks else ("0", "0"))
    return day + timedelta(hours=int(h), minutes=int(m))


TOMORROW_RE = re.compile(r"завтра|tomorrow|следующ\w* (день|дня)", re.I)


def _time_resolvers(args: dict, base) -> dict:
    """Absolute demo times become offsets from a base aligned to the app clock (start ≈ now + 60 min)."""
    from datetime import datetime
    out = {}
    if base is None:
        return out
    naive = base.replace(tzinfo=None)
    day = next((_parse_date(v) for v in args.values() if isinstance(v, str) and DATE_RE.match(v)), None)
    for k, v in args.items():
        if not isinstance(v, str):
            continue
        if (t := _iso(v)) is not None:
            out[k] = {"kind": "time", "offsetMinutes": int((t.replace(tzinfo=None) - naive).total_seconds() // 60)}
        elif DATE_RE.match(v):
            out[k] = {"kind": "date", "offsetMinutes": int((_parse_date(v) - naive.replace(hour=0, minute=0)).total_seconds() // 60)}
        elif CLOCK_RE.match(v):
            h, m = map(int, v.split(":"))
            moment = (day or naive).replace(hour=h, minute=m)
            out[k] = {"kind": "clock", "offsetMinutes": int((moment - naive).total_seconds() // 60)}
    return out


LAT_NAMES = ("lat", "latitude")
LON_NAMES = ("lon", "lng", "longitude")


def _geo_point_sql(spec: AppSpec, entity: str) -> tuple[str, str] | None:
    e = spec.entity(entity)
    if not e or not e.geo_fields:
        return None
    return n.table(e), n.column(e.geo_fields[0])


def demo_resolvers(spec: AppSpec, op: Operation, args: dict) -> dict:
    """How the demo runner turns placeholder arguments into real values (ids, users, coordinates)."""
    fields = {f.name: f for f in op.input}
    acting = op.roles[0]
    path_entity = next((e for p in op.path_params for e in spec.entities if p in id_aliases(spec, e)), None)
    resolve: dict = {}
    for key in args:
        f = fields.get(key)
        lname = key.lower()
        if lname in LAT_NAMES or lname in LON_NAMES:
            anchor = None
            if path_entity:
                for rf in path_entity.fields:
                    if rf.type == "ref" and rf.ref and (geo := _geo_point_sql(spec, rf.ref)):
                        anchor = {"sql": f"SELECT ST_Y(ST_PointOnSurface(g.{geo[1]})), ST_X(ST_PointOnSurface(g.{geo[1]})) "
                                          f"FROM {n.table(path_entity)} p JOIN {geo[0]} g ON g.id = p.{n.column(rf)} WHERE p.id = @id",
                                  "var": n.camel(path_entity.name) + "Id"}
                        break
                if anchor is None and (geo := _geo_point_sql(spec, path_entity.name)):
                    anchor = {"sql": f"SELECT ST_Y(ST_PointOnSurface({geo[1]})), ST_X(ST_PointOnSurface({geo[1]})) "
                                      f"FROM {geo[0]} WHERE id = @id", "var": n.camel(path_entity.name) + "Id"}
            if anchor:
                resolve[key] = {"kind": "geo", "coord": "lat" if lname in LAT_NAMES else "lon", **anchor}
            continue
        is_ref = (f is not None and f.type == "ref") or key.endswith("Id")
        if not is_ref:
            continue
        target = f.ref if f is not None and f.type == "ref" else None
        if target is None:
            target = next((e.name for e in spec.entities if key in id_aliases(spec, e)), "User")
        if target == "User" or spec.entity(target) is None:
            if any(w in lname for w in ("created", "author", "reporter", "requester", "submitted", "owner")):
                resolve[key] = {"kind": "user", "login": login_of(spec, acting)}
                continue
            # Роль — по значению из сценария («engineer-1»), затем по имени поля без суффикса Id/UserId
            # (assigneeUserId не должен совпасть с ролью «User»); более конкретные роли проверяются первыми.
            value = str(args.get(key, "")).lower()
            stem = re.sub(r"(user)?id$", "", lname)
            by_len = sorted(spec.roles, key=lambda r: -len(r.name))
            named = next((r.name for r in by_len if r.demo_login in value or r.name.lower() in value), None) or next(
                (r.name for r in by_len if r.name.lower() in stem), None)
            assignees = [p.role for p in spec.permissions if path_entity and p.entity == path_entity.name
                         and {"read_own", "update_own"} & set(p.actions)]
            role = named or next((r for r in assignees if r != acting), None) or next(
                (r.name for r in spec.roles if r.name != acting and r.name not in admin_roles(spec)), acting)
            login = login_of(spec, role)
            digit = re.search(r"(\d+)\s*$", value)
            if digit and digit.group(1) in ("2", "3") and role not in admin_roles(spec):
                login += digit.group(1)  # «engineer-2» — второй пользователь роли
            resolve[key] = {"kind": "user", "login": login}
        else:
            var = n.camel(target) + "Id"
            nth = sum(1 for r in resolve.values() if r.get("kind") == "entity" and r.get("var") == var)
            resolve[key] = {"kind": "entity", "var": var}
            if key != var:
                resolve[key]["alias"] = key
            if nth:  # вторая ссылка на ту же сущность в шаге (хозяева и гости) — другая запись
                resolve[key]["nth"] = nth
    return resolve


def mocks(spec: AppSpec) -> list[dict]:
    out = []
    for i in spec.integrations:
        for m in i.methods:
            resp = m.mock_response or {f.name: (f"https://example.org/{n.kebab(i.name)}" if f.type == "url"
                                                else f"mock-{f.name}") for f in m.output}
            out.append({"path": f"/api/{n.kebab(i.name)}/{n.kebab(m.name)}", "response": resp})
    return out


DEFAULT_LOGIC = {"body": 'throw new NotImplementedException("Логика не сгенерирована");',
                 "helpers": "", "usings": []}


def generate(spec: AppSpec, out: Path, logic: dict[str, dict] | None = None,
             data_dir: Path | None = None) -> list[Path]:
    logic = logic or {}
    N = spec.namespace
    r = Renderer("backend", spec, extra={
        "payload_type": payload_type,
        "ef_extra": ef_extra,
        "sql_check": sql_check,
        "sql_string": lambda s: "'" + s.replace("'", "''") + "'",
        "admin_roles": admin_roles(spec),
        "id_aliases": lambda e: id_aliases(spec, e),
        "owner_fields": lambda e: list(owner_fields(spec, e)),
        "upcoming_field": upcoming_field,
    })
    b = out / "backend"
    src = b / "src"
    clean_sources(b)
    r.render("Directory.Build.props.j2", b / "Directory.Build.props")
    r.render("Directory.Packages.props.j2", b / "Directory.Packages.props")
    r.render("solution.slnx.j2", b / f"{N}.slnx")
    r.render("Dockerfile.j2", b / "Dockerfile")

    d = src / f"{N}.Domain"
    r.render("Domain/Domain.csproj.j2", d / f"{N}.Domain.csproj")
    r.render("Domain/Entities.cs.j2", d / "Entities" / "Entities.cs", enums=n.all_enums(spec))

    a = src / f"{N}.Application"
    r.render("Application/Application.csproj.j2", a / f"{N}.Application.csproj")
    r.render("Application/Abstractions.cs.j2", a / "Abstractions" / "Abstractions.cs")
    r.render("Application/Common.cs.j2", a / "Common" / "Common.cs")
    r.render("Application/Events.cs.j2", a / "Events" / "Events.cs")
    r.render("Application/Integrations.cs.j2", a / "Integrations" / "Integrations.cs")
    r.render("Application/DependencyInjection.cs.j2", a / "DependencyInjection.cs")
    for e in spec.entities:
        vm = entity_vm(spec, e)
        r.render("Application/EntityDtos.cs.j2", a / "Dtos" / f"{e.name}Dtos.cs", e=e, vm=vm)
        r.render("Application/EntityService.cs.j2", a / "Services" / f"{e.name}Service.cs", e=e, vm=vm)
    for op in spec.operations:
        r.render("Application/Operation.cs.j2", a / "Operations" / f"{op.name}.cs", op=op,
                 vm=op_vm(spec, op), logic={**DEFAULT_LOGIC, **logic.get(op.name, {})})
    for job in spec.jobs:
        r.render("Application/Job.cs.j2", a / "Jobs" / f"{job.name}Job.cs", job=job,
                 logic={**DEFAULT_LOGIC, **logic.get(job.name, {})})

    i = src / f"{N}.Infrastructure"
    r.render("Infrastructure/Infrastructure.csproj.j2", i / f"{N}.Infrastructure.csproj")
    r.render("Infrastructure/AppDbContext.cs.j2", i / "Persistence" / "AppDbContext.cs")
    r.render("Infrastructure/init.sql.j2", i / "Persistence" / "Migrations" / "V001__init.sql")
    r.render("Infrastructure/Persistence.cs.j2", i / "Persistence" / "Persistence.cs",
             dataset_vms=dataset_vms(spec))
    r.render("Infrastructure/Services.cs.j2", i / "Services.cs")
    write_json(i / "Persistence" / "seed.json", seed_data(spec))

    api = src / f"{N}.Api"
    r.render("Api/Api.csproj.j2", api / f"{N}.Api.csproj")
    r.render("Api/Program.cs.j2", api / "Program.cs")
    r.render("Api/appsettings.json.j2", api / "appsettings.json", db_name=n.snake(N))
    r.render("Api/ApiInfrastructure.cs.j2", api / "Infrastructure" / "ApiInfrastructure.cs")
    r.render("Api/Observability.cs.j2", api / "Infrastructure" / "Observability.cs")
    r.render("Api/PlatformControllers.cs.j2", api / "Controllers" / "PlatformControllers.cs")
    r.render("Api/DemoScenarioRunner.cs.j2", api / "Demo" / "DemoScenarioRunner.cs")
    try:
        script = demo_script(spec)
    except Exception as e:  # сценарий не должен ронять генерацию проекта: стенд работает и без демо-режима
        script = {"title": "Демонстрационный сценарий не сформирован", "steps": [], "error": str(e)[:300]}
    write_json(api / "Demo" / "demo.json", script)
    for e in spec.entities:
        r.render("Api/EntityController.cs.j2", api / "Controllers" / f"{n.plural(e.name)}Controller.cs", e=e)
    if spec.operations:
        r.render("Api/OperationsController.cs.j2", api / "Controllers" / "OperationsController.cs",
                 ops=[op_vm(spec, op) for op in spec.operations])

    t = b / "tests" / f"{N}.Tests"
    owner_entities = []
    for e in spec.entities:
        roles = own_roles(spec, e.name)
        if e.owner_field and roles:
            e_copy = e.model_copy()
            object.__setattr__(e_copy, "_own_login", login_of(spec, roles[0]))
            owner_entities.append(e_copy)
    r.render("Tests/Tests.csproj.j2", t / f"{N}.Tests.csproj")
    r.render("Tests/TestInfrastructure.cs.j2", t / "TestInfrastructure.cs")
    r.render("Tests/GeneratedTests.cs.j2", t / "GeneratedTests.cs", owner_entities=owner_entities)

    seed_dir = b / "seed-data"
    seed_dir.mkdir(parents=True, exist_ok=True)
    (seed_dir / ".gitkeep").touch()
    if data_dir:
        for ds in spec.datasets:
            srcf = data_dir / ds.file
            if srcf.exists():
                shutil.copy(srcf, seed_dir / ds.file)

    if spec.integrations:
        m = Renderer("mocks", spec)
        md = out / "mocks"
        m.render("Program.cs.j2", md / "Program.cs")
        m.render("Mocks.csproj.j2", md / "Mocks.csproj")
        m.render("Dockerfile.j2", md / "Dockerfile")
        write_json(md / "mocks.json", mocks(spec))
    return r.written


def id_aliases(spec: AppSpec, e: Entity) -> list[str]:
    """Path-parameter names that refer to an entity id: maintenanceRequestId, requestId (if unambiguous)."""
    words = re.findall(r"[A-Z][a-z0-9]*", e.name)
    aliases = []
    for i in range(len(words)):
        alias = n.camel("".join(words[i:])) + "Id"
        owners = [x for x in spec.entities if re.sub(r"^.*?(?=" + "".join(words[i:]) + "$)", "", x.name) == "".join(words[i:])]
        if i == 0 or len(owners) == 1:
            aliases.append(alias)
    return list(dict.fromkeys(aliases))


def clean_sources(backend: Path) -> None:
    """Remove previously generated sources but keep bin/obj for fast incremental builds."""
    for root in (backend / "src", backend / "tests"):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), reverse=True):
            parts = set(path.relative_to(root).parts)
            if parts & {"bin", "obj"}:
                continue
            if path.is_file():
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()


def dataset_vm(spec: AppSpec, ds) -> dict:
    e = spec.entity(ds.entity)
    assignments = []
    for field_name, key in ds.mapping.items():
        f = e.field(field_name) if e else None
        if f is None:
            continue
        Name = n.pascal(f.name)
        k = n.cs_string(key)
        if is_geo(f):
            parts = [x.strip() for x in re.split(r"[,;| ]+", key) if x.strip()]
            if f.type == "geo_point" and len(parts) == 2:
                # Координаты в двух полях записи: «lat,lon» (порядок — по именам ключей).
                lat, lon = (parts[1], parts[0]) if re.search(r"^(lon|lng|long|x)", parts[0], re.I) else (parts[0], parts[1])
                expr = f"P(r, {n.cs_string(lat)}, {n.cs_string(lon)})"
            else:
                expr = f'G(r, {k}, "{ds.geometry_format}")'
                if f.type == "geo_point":
                    expr = f'({expr} as Point ?? P(r, "lat", "lon") ?? P(r, "latitude", "longitude"))'
            if f.required:
                expr = f"({expr} ?? Center())"
        elif f.type == "json":
            expr = f"J(r, {k})" + (' ?? "{}"' if f.required else "")
        elif f.type in STRINGY:
            expr = f"S(r, {k})"
            if f.max_length and f.type != "text":
                expr = f"Trim({expr}, {f.max_length})"
            expr += ' ?? ""' if f.required else ""
        elif f.type == "int":
            expr = f"I(r, {k})" + (" ?? 0" if f.required else "")
        elif f.type in ("decimal", "money"):
            expr = f"D(r, {k})" + (" ?? 0m" if f.required else "")
        elif f.type == "bool":
            expr = f"B(r, {k})" + (" ?? false" if f.required else "")
        elif f.type == "datetime":
            expr = f"T(r, {k})" + (" ?? DateTimeOffset.UnixEpoch" if f.required else "")
        elif f.type == "date":
            expr = f"Dt(r, {k})" + (" ?? DateOnly.MinValue" if f.required else "")
        elif f.type == "enum":
            en = n.enum_name_for(spec, e, f)
            expr = (f"(Enum.TryParse<{en}>(S(r, {k}), true, out var {f.name}Value) ? {f.name}Value : "
                    f"{'default' if f.required else 'null'})")
            if not f.required:
                expr = f"(Enum.TryParse<{en}>(S(r, {k}), true, out var {f.name}Value) ? {f.name}Value : ({en}?)null)"
        else:
            continue
        assignments.append({"Name": Name, "expr": expr})
    for f in (e.fields if e else []):
        if f.type == "geo_point" and f.name not in ds.mapping:
            # Поле точки не сопоставлено, но в записях есть lat/lon — берём их.
            expr = 'P(r, "lat", "lon") ?? P(r, "latitude", "longitude")'
            assignments.append({"Name": n.pascal(f.name), "expr": f"({expr} ?? Center())" if f.required else expr})
        if f.type == "bool" and f.name not in ds.mapping:
            default = f.default if isinstance(f.default, bool) else bool(
                re.search(r"activ|enabl|availab|visib|open|valid", f.name.lower()))
            assignments.append({"Name": n.pascal(f.name), "expr": "true" if default else "false"})
        elif f.type == "bool" and f.required:
            for a in assignments:
                if a["Name"] == n.pascal(f.name) and a["expr"].endswith("?? false"):
                    fallback = "true" if re.search(r"activ|enabl|availab|visib|open|valid", f.name.lower()) else "false"
                    a["expr"] = a["expr"][: -len("false")] + fallback
    return {"name": ds.name, "Name": n.pascal(ds.name.replace("-", "_").title().replace("_", "")),
            "file": ds.file, "entity": ds.entity, "set": n.plural(ds.entity),
            "records_path": ds.records_path, "assignments": assignments, "children": []}


def dataset_vms(spec: AppSpec) -> list[dict]:
    """Datasets for import; a dataset nested in another one («objects.meters» inside «objects») is imported inside the
    parent's loop and linked to the parent record through its ref field."""
    vms = {ds.name: dataset_vm(spec, ds) for ds in spec.datasets}
    roots = []
    for ds in spec.datasets:
        vm = vms[ds.name]
        parent = next((p for p in spec.datasets if p is not ds and p.file == ds.file
                       and ds.records_path.startswith(p.records_path + ".")), None)
        if parent is None:
            roots.append(vm)
            continue
        child = spec.entity(ds.entity)
        ref = next((f for f in (child.fields if child else []) if f.type == "ref" and f.ref == parent.entity), None)
        vm["rel_path"] = ds.records_path[len(parent.records_path) + 1:]
        vm["parent_ref"] = n.pascal(ref.name) if ref else None
        vms[parent.name]["children"].append(vm)
    return roots


def ef_extra(f: Field) -> str:
    parts = []
    if f.type == "enum":
        parts.append(".HasConversion<string>().HasMaxLength(64)")
    elif f.type == "json":
        parts.append('.HasColumnType("jsonb")')
    elif is_geo(f):
        parts.append(f'.HasColumnType("{n.sql_type(f)}")')
    elif f.type in ("string", "email", "phone", "url") and f.max_length:
        parts.append(f".HasMaxLength({f.max_length})")
    elif f.type == "money":
        parts.append(".HasPrecision(18, 2)")
    elif f.type == "decimal":
        parts.append(".HasPrecision(18, 6)")
    if f.required and f.type not in ("int", "decimal", "money", "bool", "date", "datetime", "enum", "ref"):
        parts.append(".IsRequired()")
    return "".join(parts)


def sql_check(f: Field) -> str:
    col = n.column(f)
    if f.type == "enum" and f.enum_values:
        vals = ", ".join("'" + v + "'" for v in f.enum_values)
        return f" CHECK ({col} IN ({vals}))"
    if f.type in ("int", "decimal", "money") and (f.min is not None or f.max is not None):
        conds = []
        if f.min is not None:
            conds.append(f"{col} >= {f.min:g}")
        if f.max is not None:
            conds.append(f"{col} <= {f.max:g}")
        return f" CHECK ({' AND '.join(conds)})"
    return ""


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
