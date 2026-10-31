"""Semantic checks of the IR. Trivial issues are auto-fixed; the rest go back to the LLM."""

from __future__ import annotations

import re

from .gen import naming as n
from .gen.backend import admin_roles, id_aliases
from .ir import AppSpec, Field, Permission, Screen, Widget

TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                     "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def _unique(name: str, taken: set[str], suffix: str) -> str:
    candidate, i = name + suffix, 2
    while candidate in taken:
        candidate, i = f"{name}{suffix}{i}", i + 1
    return candidate


def _all_fields(spec: AppSpec):
    for e in spec.entities:
        yield from e.fields
    for o in spec.operations:
        yield from o.input
        yield from o.output
    for ev in spec.events:
        yield from ev.payload
    for i in spec.integrations:
        for m in i.methods:
            yield from m.input
            yield from m.output


def rename_entity(spec: AppSpec, old: str, new: str) -> None:
    """Rename an entity everywhere it is referenced, including routes and id parameters of operation paths."""
    entity = next(e for e in spec.entities if e.name == old)
    old_route, old_aliases = n.route(old), set(id_aliases(spec, entity))
    entity.name = new
    new_alias = n.camel(new) + "Id"
    for f in _all_fields(spec):
        if f.type == "ref" and f.ref == old:
            f.ref = new
    for p in spec.permissions:
        p.entity = new if p.entity == old else p.entity
    for d in spec.datasets:
        d.entity = new if d.entity == old else d.entity
    renamed_params: dict[str, str] = {}
    for o in spec.operations:
        if o.output_entity == old:
            o.output_entity = new
        parts = []
        for seg in o.path.split("/"):
            if seg == old_route:
                seg = n.route(new)
            elif seg.startswith("{") and seg.strip("{}") in old_aliases:
                renamed_params[seg.strip("{}")] = new_alias
                seg = "{" + new_alias + "}"
            parts.append(seg)
        o.path = "/".join(parts)
    for sc in spec.screens:
        for w in sc.widgets:
            w.entity = new if w.entity == old else w.entity
    if spec.demo:
        for st in spec.demo.steps:
            st.entity = new if st.entity == old else st.entity
            st.args = {renamed_params.get(k, k): v for k, v in st.args.items()}


def rename_member(spec: AppSpec, kind: str, old: str, new: str) -> None:
    """Rename an operation, job or event and every reference to it."""
    if kind == "operation":
        for o in spec.operations:
            o.name = new if o.name == old else o.name
        for sc in spec.screens:
            for w in sc.widgets:
                w.operation = new if w.operation == old else w.operation
        for st in (spec.demo.steps if spec.demo else []):
            st.operation = new if st.operation == old else st.operation
    elif kind == "job":
        for j in spec.jobs:
            j.name = new if j.name == old else j.name
    else:
        for ev in spec.events:
            ev.name = new if ev.name == old else ev.name
        for x in [*spec.operations, *spec.jobs]:
            x.emits = [new if v == old else v for v in x.emits]
        for e in spec.entities:
            for t in (e.lifecycle.transitions if e.lifecycle else []):
                t.emits = new if t.emits == old else t.emits
        for st in (spec.demo.steps if spec.demo else []):
            if st.action == "expect" and st.args.get("event") == old:
                st.args = {**st.args, "event": new}


def _fix_names(spec: AppSpec) -> list[str]:
    """Names that would clash with platform classes in the generated C# are renamed (GeoPoint → GeoPointItem)."""
    fixes = []
    taken = {e.name for e in spec.entities}
    for e in list(spec.entities):
        if e.name in n.reserved_entity_names():
            new = _unique(e.name, taken | n.reserved_entity_names(), "Item")
            taken.add(new)
            fixes.append(f"сущность {e.name} переименована в {new} (имя занято платформой)")
            rename_entity(spec, e.name, new)
    members = {o.name for o in spec.operations} | {j.name for j in spec.jobs} | {ev.name for ev in spec.events} | taken
    for kind, items, suffix in (("operation", spec.operations, "Action"), ("job", spec.jobs, "Check"),
                                ("event", spec.events, "Occurred")):
        for x in list(items):
            if x.name in n.reserved_member_names() or (kind != "event" and x.name in taken):
                new = _unique(x.name, members | n.reserved_member_names(), suffix)
                members.add(new)
                fixes.append(f"{x.name} переименовано в {new} (имя занято платформой)")
                rename_member(spec, kind, x.name, new)
    return fixes


def _fix_entities(spec: AppSpec) -> list[str]:
    fixes = []
    admins = admin_roles(spec) if spec.permissions else [spec.roles[0].name]
    for e in spec.entities:
        seen: set[str] = set()
        kept = []
        for f in e.fields:
            if f.name in ("id", "createdAt", "updatedAt") or f.name in seen:
                fixes.append(f"{e.name}.{f.name}: лишнее поле удалено")
                continue
            seen.add(f.name)
            if f.regex:
                try:
                    re.compile(f.regex)
                except re.error:
                    fixes.append(f"{e.name}.{f.name}: некорректное регулярное выражение убрано")
                    f.regex = None
            if f.type == "string" and not f.max_length:
                f.max_length = 200
            if f.type == "string" and f.readonly and (f.max_length or 0) < 40 and re.search(r"number|code|num$|номер|код", f.name, re.I):
                # Номера и коды формирует система (INC-20261030-0001): короткое поле переполняется на первой же записи.
                f.max_length = 40
            kept.append(f)
        e.fields = kept
        if not any("read" in p.actions or "read_own" in p.actions for p in spec.permissions if p.entity == e.name):
            for role in admins:
                p = next((p for p in spec.permissions if p.role == role and p.entity == e.name), None)
                if p:
                    p.actions = list(dict.fromkeys([*p.actions, "read"]))
                else:
                    spec.permissions.append(Permission(role=role, entity=e.name, actions=["read"]))
            fixes.append(f"{e.name}: доступ на чтение для {', '.join(admins)}")
        # Сущность-человек с ролью (Doctor ↔ роль Doctor, Courier ↔ Courier) связана со своей учётной записью.
        person_role = next((r.name for r in spec.roles if r.name.lower() == e.name.lower()
                            or e.name.lower().startswith(r.name.lower()) and len(r.name) > 3), None)
        if person_role and not any(f.type == "ref" and f.ref == "User" for f in e.fields):
            e.fields.append(Field(name="userId", title="Учётная запись", type="ref", ref="User", readonly=True))
            fixes.append(f"{e.name}: связь с учётной записью роли {person_role} (поле userId)")
        if person_role and e.seed_count < 3 and not any(d.entity == e.name for d in spec.datasets):
            e.seed_count = 3  # у каждого демо-пользователя роли есть своя карточка (пациент, курьер)
        own = [p for p in spec.permissions if p.entity == e.name and {"read_own", "update_own"} & set(p.actions)]
        if own and not e.owner_field and not all(p.owner_field for p in own):
            # Владелец — поле, названное по самой роли или нейтрально (userId, authorId); поле другой роли
            # (managerId у сотрудника) владельцем не становится.
            own_roles = {p.role.lower() for p in own}
            others = [r.name.lower() for r in spec.roles if r.name.lower() not in own_roles]
            user_refs = [f for f in e.fields if f.type == "ref" and f.ref == "User"
                         and not any(o in f.name.lower() for o in others)]
            user_refs.sort(key=lambda f: (not any(r in f.name.lower() for r in own_roles),
                                          not re.search(r"user|account|owner|author|creat", f.name.lower())))
            if user_refs:
                e.owner_field = user_refs[0].name
            else:
                e.fields.append(Field(name="userId", title="Учётная запись", type="ref", ref="User", readonly=True))
                e.owner_field = "userId"
            fixes.append(f"{e.name}: владелец записи — поле {e.owner_field}")
    for r in spec.roles:
        if not re.match(r"^[a-z][a-z0-9_]*$", r.demo_login):
            login = "".join(TRANSLIT.get(ch, ch) for ch in r.demo_login.lower())
            login = re.sub(r"[^a-z0-9_]", "", login) or n.snake(r.name)
            r.demo_login = login if login[:1].isalpha() else "u" + login
            fixes.append(f"роль {r.name}: логин {r.demo_login}")
    return fixes


def _fix_operations(spec: AppSpec) -> list[str]:
    fixes = []
    entities = {e.name: e for e in spec.entities}
    seen: set[tuple[str, str]] = set()
    for o in spec.operations:
        segs = [x for x in o.path.strip("/").split("/") if x]
        if len(segs) == 1 and o.method in ("GET", "POST"):
            action = "create" if o.name.startswith("Create") else n.kebab(o.name)
            o.path = f"/{segs[0]}/{action}"
            fixes.append(f"операция {o.name}: путь {o.path}")
        for p in o.path_params:
            fixed = p
            if not re.match(r"^[a-z][A-Za-z0-9]*$", p):
                fixed = n.camel(re.sub(r"[^A-Za-z0-9]", "", p.title() if "_" in p or "-" in p else p))
            if fixed.endswith("Id") and not any(fixed in id_aliases(spec, e) for e in spec.entities):
                stem = fixed[:-2].lower()
                match = [e for e in spec.entities if e.name.lower().endswith(stem) or stem.endswith(e.name.lower())]
                if len(match) == 1:
                    fixed = n.camel(match[0].name) + "Id"
            if fixed != p:
                o.path = o.path.replace("{" + p + "}", "{" + fixed + "}")
                for st in (spec.demo.steps if spec.demo else []):
                    if st.operation == o.name and p in st.args:
                        st.args = {fixed if k == p else k: v for k, v in st.args.items()}
                fixes.append(f"операция {o.name}: параметр {{{p}}} → {{{fixed}}}")
        key = (o.method, re.sub(r"\{\w+\}", "{}", o.path))
        if key in seen:
            o.path = o.path.rstrip("/") + "/" + n.kebab(o.name)
            fixes.append(f"операция {o.name}: уникальный путь {o.path}")
        seen.add((o.method, re.sub(r"\{\w+\}", "{}", o.path)))
        if (o.name.startswith("Create") or o.path.rstrip("/").endswith("/create")) and not o.output_entity:
            target = o.name.removeprefix("Create")
            first = o.path.strip("/").split("/")[0]
            guess = target if target in entities else next((e for e in entities if n.route(e) == first), None)
            if guess:
                o.output_entity = guess
                fixes.append(f"операция {o.name} возвращает {guess}")
    for ev in spec.events:
        names = {p.name for p in ev.payload}
        for ph in re.findall(r"\{(\w+)\}", ev.notify or ""):
            if ph not in names:
                ev.payload.append(Field(name=ph, title=ph, type="string", max_length=200))
                names.add(ph)
                fixes.append(f"событие {ev.name}: в payload добавлено поле {ph} для уведомления")
    return fixes


def _fix_demo_events(spec: AppSpec) -> list[str]:
    """An expected event that nothing publishes is attached to the action right before the check."""
    if not spec.demo:
        return []
    fixes = []
    ops = {o.name: o for o in spec.operations}
    emitted = {ev for o in spec.operations for ev in o.emits} | {ev for j in spec.jobs for ev in j.emits} | {
        t.emits for e in spec.entities if e.lifecycle for t in e.lifecycle.transitions if t.emits}
    events = {e.name for e in spec.events}
    for i, st in enumerate(spec.demo.steps):
        ev = st.args.get("event") if st.action == "expect" else None
        if not ev or ev not in events or ev in emitted:
            continue
        prev = next((p for p in reversed(spec.demo.steps[:i]) if p.action != "expect"), None)
        if prev and prev.action in ("operation", "route") and prev.operation in ops:
            ops[prev.operation].emits.append(ev)
            fixes.append(f"событие {ev} публикует операция {prev.operation}")
        elif prev and prev.action == "advance_time" and len(spec.jobs) == 1:
            spec.jobs[0].emits.append(ev)
            fixes.append(f"событие {ev} публикует задача {spec.jobs[0].name}")
        else:
            continue
        emitted.add(ev)
    return fixes


def _array_paths(node, path: str = "") -> list[tuple[str, list[dict]]]:
    """Arrays of objects inside a JSON document: (dot path, records), nested arrays included (objects.meters)."""
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            found += _array_paths(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        records = [x for x in node if isinstance(x, dict)]
        if records:
            found.append((path, records))
            nested: dict[str, list[dict]] = {}
            for r in records[:200]:
                for k, v in r.items():
                    if isinstance(v, list) and any(isinstance(x, dict) for x in v):
                        nested.setdefault(k, []).extend(x for x in v if isinstance(x, dict))
            for k, recs in nested.items():
                found.append((f"{path}.{k}", recs))
    return found


def auto_datasets(spec: AppSpec, data_dir) -> list[str]:
    """Arrays of the attached JSON files that no dataset maps get mapped to the entity whose fields match their keys."""
    import json
    from pathlib import Path
    from .ir import Dataset
    if not data_dir or not Path(data_dir).exists():
        return []
    fixes = []
    for file in sorted(Path(data_dir).glob("*.json")):
        try:
            doc = json.loads(file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for path, records in _array_paths(doc):
            if any(d.file == file.name and d.records_path == path for d in spec.datasets):
                continue
            keys = {k.lower(): k for r in records[:50] for k, v in r.items() if not isinstance(v, (list, dict))}
            mapped = {d.entity for d in spec.datasets}
            best, best_map = None, {}
            for e in spec.entities:
                if e.name in mapped:
                    continue
                m = {f.name: keys[f.name.lower()] for f in e.fields if f.name.lower() in keys and f.type not in ("ref",)}
                if len(m) > len(best_map):
                    best, best_map = e, m
            if best is not None and len(best_map) >= 2:
                spec.datasets.append(Dataset(name=f"{file.stem}-{path.replace('.', '-')}", file=file.name, records_path=path,
                                             entity=best.name, mapping=best_map, geometry_format="none"))
                fixes.append(f"данные {file.name}:{path} сопоставлены сущности {best.name} ({', '.join(best_map)})")
    return fixes


def _fix_mobile(spec: AppSpec) -> list[str]:
    """A required mobile app without screens gets a default screen for the field role."""
    if not spec.analysis.components.get("mobile") or any(s.platform == "mobile" for s in spec.screens):
        return []
    admins = set(admin_roles(spec)) if spec.permissions else set()
    field_roles = [r.name for r in spec.roles if r.name not in admins] or [spec.roles[-1].name]
    widgets = []
    if any(e.geo_fields for e in spec.entities):
        widgets.append(Widget(type="map", title="Карта"))
    owned = next((p.entity for p in spec.permissions if p.role in field_roles and "read_own" in p.actions), None)
    if owned:
        widgets.append(Widget(type="list", title="Мои записи", entity=owned))
    widgets.append(Widget(type="notifications", title="Уведомления"))
    spec.screens.append(Screen(id="SCR-M1", name="MobileHome", title="Главный экран", platform="mobile",
                               roles=field_roles, widgets=widgets))
    return ["добавлен мобильный экран по умолчанию"]


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


LAT = ("lat", "latitude")
LON = ("lon", "lng", "longitude")


def _routes_from_coordinates(spec: AppSpec) -> list[str]:
    """Demo steps that post fixed coordinates to a position operation become route legs over the geo layer:
    fixed coordinates rarely hit a zone, a planned route always does."""
    geo_layers = [e.name for e in spec.entities if e.geo_fields]
    if not geo_layers:
        return []
    ops = {o.name: o for o in spec.operations}
    fixes = []
    entered = False
    for st in spec.demo.steps:
        op = ops.get(st.operation or "")
        if not op or st.action not in ("operation", "route"):
            continue
        names = {f.name.lower() for f in op.input}
        if not (names & set(LAT) and names & set(LON)):
            continue
        title = st.title.lower()
        if "leg" in st.args and st.action == "route":
            entered = entered or st.args["leg"] >= 1
            continue
        if re.search(r"покида|выезж|выезд|уезжа", title):
            leg = 3
        elif re.search(r"друг|втор|переезж|сосед|смен", title):
            leg = 2
        elif re.search(r"въезж|въезд|заезж|прибыв|приезж|в зон", title):
            leg = 2 if entered else 1
        else:
            leg = 0
        entered = entered or leg >= 1
        st.action = "route"
        st.args = {"leg": leg, "zones": 2}
        fixes.append(f"демо {st.id}: координаты заменены движением по маршруту (участок {leg})")
    return fixes


def autofix(spec: AppSpec, data_dir=None) -> list[str]:
    """Deterministic repairs that do not need a model call. Returns a log of fixes."""
    fixes: list[str] = _fix_names(spec) + normalize_enums(spec) + auto_datasets(spec, data_dir)
    if spec.demo:
        fixes += _routes_from_coordinates(spec)
        events = {e.name for e in spec.events}
        before = len(spec.demo.steps)
        spec.demo.steps = [st for st in spec.demo.steps if st.action != "expect" or st.args.get("event") in events]
        if len(spec.demo.steps) != before:
            fixes.append(f"демо: убраны проверки без известного события ({before - len(spec.demo.steps)})")
        for st in spec.demo.steps:
            if st.action == "create" and st.entity and re.search(r"отображ|видит|просмотр|истори|список|показыва", st.title.lower()) \
                    and not re.search(r"созда|добав|регистр|оформ|принима", st.title.lower()):
                st.action, st.args = "view", {}
                fixes.append(f"демо {st.id}: просмотр записей вместо создания")
                continue
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
    for p in spec.permissions:
        e = next((x for x in spec.entities if x.name == p.entity), None)
        if e is None or not ({"read_own", "update_own"} & set(p.actions)):
            p.owner_field = None
            continue
        user_refs = [f for f in e.fields if f.type == "ref" and f.ref == "User"]
        if p.owner_field and not any(f.name == p.owner_field for f in user_refs):
            p.owner_field = None
        if not p.owner_field and len(user_refs) > 1:
            named = next((f.name for f in user_refs if p.role.lower() in f.name.lower()), None)
            creator = next((f.name for f in user_refs if "create" in p.actions and
                            re.search(r"creat|author|request|report|submit", f.name.lower())), None)
            if named or creator:
                p.owner_field = named or creator
                if p.owner_field != e.owner_field:
                    fixes.append(f"{p.role} видит свои {e.name} по полю {p.owner_field}")
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
    fixes += _fix_entities(spec) + _fix_operations(spec) + _fix_demo_events(spec) + _fix_mobile(spec)
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
        if name in n.reserved_entity_names():
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
        if own and not e.owner_field and not all(p.owner_field for p in own):
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
            elif p.endswith("Id") and not any(p in id_aliases(spec, e) for e in spec.entities):
                names = ", ".join("{" + n.camel(e.name) + "Id}" for e in spec.entities)
                errors.append(f"Операция {o.name}: параметр {{{p}}} не соответствует ни одной сущности — используй один из: {names}")
        if (o.name.startswith("Create") or o.path.rstrip("/").endswith("/create")) and not o.output_entity:
            errors.append(f"Операция {o.name} создаёт запись — укажи output_entity (вернуть созданную запись с id)")
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
        emitters: dict[str, set[str]] = {}
        for o in spec.operations:
            for ev in o.emits:
                emitters.setdefault(ev, set()).add(o.name)
        job_events = {ev for j in spec.jobs for ev in j.emits}
        transition_events = {t.emits for e in spec.entities if e.lifecycle for t in e.lifecycle.transitions if t.emits}
        seen_ops: set[str] = set()
        for st in spec.demo.steps:
            if st.action in ("operation", "route") and st.operation:
                seen_ops.add(st.operation)
            if st.action == "expect":
                ev = st.args.get("event")
                ops_for = emitters.get(ev, set())
                if ev and not ops_for and ev not in job_events and ev not in transition_events:
                    errors.append(f"Демо {st.id}: событие {ev} не публикует ни одна операция, задача или переход — "
                                  f"добавь его в emits той операции (или задачи), которая его порождает")
                if ev and ev not in job_events and ev not in transition_events and ops_for and not (ops_for & seen_ops):
                    errors.append(f"Демо {st.id}: событие {ev} публикует операция {', '.join(sorted(ops_for))}, но её нет "
                                  f"в предыдущих шагах — добавь шаг action=operation с этой операцией перед {st.id}")
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
