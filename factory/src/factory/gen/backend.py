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


def login_of(spec: AppSpec, role: str) -> str:
    return next(r.demo_login for r in spec.roles if r.name == role)


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
    for r in spec.roles:
        logins = [r.demo_login] + ([r.demo_login + "2"] if r.name in own else [])
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


def _sample(spec: AppSpec, e: Entity, f: Field, i: int):
    center = getattr(spec, "map_center", None) or [55.7558, 37.6173]
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
        return (f.default if i == 0 and f.default in vals else vals[i % len(vals)]) if vals else None
    if t in ("string",):
        if f.regex:
            return _regex_sample(f, i)
        return f"{f.title} {i + 1}"[: f.max_length or 255]
    if t == "text":
        return f"{f.title}: тестовая запись {i + 1}"
    if t == "int":
        lo = int(f.min) if f.min is not None else 1
        hi = int(f.max) if f.max is not None else lo + 1000
        return min(lo + i * 5, hi)
    if t in ("decimal", "money"):
        lo = float(f.min) if f.min is not None else 10.0
        hi = float(f.max) if f.max is not None else lo + 10000
        return round(min(lo + 12.5 * (i + 1), hi), 2)
    if t == "bool":
        return not re.search(r"delet|block|archiv|disabl|cancel|remov|banned|hidden", f.name.lower())
    if t == "date":
        return f"2026-10-{(i % 27) + 1:02d}"
    if t == "datetime":
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
    blocks = []
    for e in _topo(spec):
        if e.name in dataset_entities or e.seed_count <= 0:
            continue
        own = own_roles(spec, e.name)
        owners = [u["id"] for u in users if u["role"] in own] or [u["id"] for u in users]
        rows = []
        count = e.seed_count
        for f in e.fields:
            if f.unique and f.examples and not f.regex:
                count = min(count, len(set(f.examples)))
        used: dict[str, set] = {}
        for i in range(count):
            row: dict = {"id": _uid(spec.slug, e.name, str(i))}
            for f in e.fields:
                if e.lifecycle and f.name == e.lifecycle.field:
                    row[f.name] = e.lifecycle.initial
                elif f.name == e.owner_field:
                    row[f.name] = owners[i % len(owners)]
                elif f.type == "ref":
                    if f.ref == "User":
                        row[f.name] = users[i % len(users)]["id"] if f.required else None
                    elif ids.get(f.ref):
                        row[f.name] = ids[f.ref][i % len(ids[f.ref])]
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
            rows.append(row)
        if not rows:
            continue
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
    times = sorted(_iso(v) for st in spec.demo.steps for v in st.args.values() if _iso(v))
    base = times[0] if times else None
    for s in spec.demo.steps:
        st: dict = {"id": s.id, "title": s.title, "action": s.action, "expectError": s.expect_error}
        op = ops.get(s.operation or "")
        if op:
            st["method"] = op.method
            st["path"] = "api/v1/" + op.path.lstrip("/")
            st["login"] = login_of(spec, op.roles[0])
        if s.action == "route" and op:
            path_entities = {p[:-2].lower() for p in op.path_params}
            layer = s.args.get("layer") or next(
                (g for g in datasets_geo + geo_entities if g.lower() not in path_entities), None)
            st["layer"] = layer
            st["zones"] = int(s.args.get("zones", 2))
            st["leg"] = int(s.args.get("leg", 0))
            names = {f.name.lower(): f.name for f in op.input}
            st["latField"] = next((names[k] for k in ("lat", "latitude") if k in names), "lat")
            st["lonField"] = next((names[k] for k in ("lon", "lng", "longitude") if k in names), "lon")
            st["extra"] = {f.name: 25 for f in op.input
                           if f.name not in (st["latField"], st["lonField"])
                           and f.type in ("int", "decimal") and "speed" in f.name.lower()}
        elif s.action == "operation" and op:
            st["args"] = s.args
            st["resolve"] = {**demo_resolvers(spec, op, s.args), **_time_resolvers(s.args, base)}
            if op.output_entity:
                st["outputVar"] = n.camel(op.output_entity) + "Id"
        elif s.action == "create" and s.entity and spec.entity(s.entity):
            e = spec.entity(s.entity)
            creators = [p.role for p in spec.permissions if p.entity == e.name and "create" in p.actions]
            st["method"] = "POST"
            st["path"] = f"api/v1/{n.route(e)}"
            st["login"] = login_of(spec, creators[0]) if creators else spec.roles[0].demo_login
            st["args"] = s.args
            fake = Operation(id="X", name="Create", title="", description="", path="/x", roles=[creators[0] if creators else spec.roles[0].name],
                             input=[f for f in e.fields])
            st["resolve"] = {**demo_resolvers(spec, fake, s.args), **_time_resolvers(s.args, base)}
            st["outputVar"] = n.camel(e.name) + "Id"
            st["action"] = "operation"
        elif s.action == "transition" and s.entity:
            e = spec.entity(s.entity)
            t = next((t for t in (e.lifecycle.transitions if e and e.lifecycle else [])
                      if t.name == s.transition), None)
            st["path"] = f"api/v1/{n.route(s.entity)}/{{{n.camel(s.entity)}Id}}/{n.kebab(s.transition or '')}"
            st["login"] = login_of(spec, t.roles[0]) if t else spec.roles[0].demo_login
        elif s.action == "advance_time":
            st["minutes"] = s.minutes or int(s.args.get("minutes", 10))
        elif s.action == "expect":
            st["event"] = s.args.get("event", "")
        steps.append(st)
    return {"title": spec.demo.title, "timeBase": base is not None, "steps": steps}


def _iso(v):
    from datetime import datetime
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", v):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _time_resolvers(args: dict, base) -> dict:
    """Absolute demo times become offsets from a base aligned to the app clock (start ≈ now + 60 min)."""
    out = {}
    for k, v in args.items():
        t = _iso(v)
        if t is not None and base is not None:
            out[k] = {"kind": "time", "offsetMinutes": int((t - base).total_seconds() // 60)}
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
            named = next((r.name for r in spec.roles if r.name.lower() in lname), None)
            assignees = [p.role for p in spec.permissions if path_entity and p.entity == path_entity.name
                         and {"read_own", "update_own"} & set(p.actions)]
            role = named or next((r for r in assignees if r != acting), None) or next(
                (r.name for r in spec.roles if r.name != acting and r.name not in admin_roles(spec)), acting)
            resolve[key] = {"kind": "user", "login": login_of(spec, role)}
        else:
            resolve[key] = {"kind": "entity", "var": n.camel(target) + "Id"}
            if key != n.camel(target) + "Id":
                resolve[key]["alias"] = key
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
             dataset_vms=[dataset_vm(spec, ds) for ds in spec.datasets])
    r.render("Infrastructure/Services.cs.j2", i / "Services.cs")
    write_json(i / "Persistence" / "seed.json", seed_data(spec))

    api = src / f"{N}.Api"
    r.render("Api/Api.csproj.j2", api / f"{N}.Api.csproj")
    r.render("Api/Program.cs.j2", api / "Program.cs")
    r.render("Api/appsettings.json.j2", api / "appsettings.json", db_name=n.snake(N))
    r.render("Api/ApiInfrastructure.cs.j2", api / "Infrastructure" / "ApiInfrastructure.cs")
    r.render("Api/PlatformControllers.cs.j2", api / "Controllers" / "PlatformControllers.cs")
    r.render("Api/DemoScenarioRunner.cs.j2", api / "Demo" / "DemoScenarioRunner.cs")
    write_json(api / "Demo" / "demo.json", demo_script(spec))
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
            expr = f'G(r, {k}, "{ds.geometry_format}")'
            if f.type == "geo_point":
                expr = f"({expr} as Point)"
            if f.required:
                expr += "!"
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
            "records_path": ds.records_path, "assignments": assignments}


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
