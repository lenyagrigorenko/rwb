"""Naming and type-mapping helpers shared by all generators."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from ..ir import AppSpec, Entity, Field

TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"

# Типы .NET / NetTopologySuite / ASP.NET, с которыми сущность не должна совпадать по имени.
FRAMEWORK_TYPES = {
    "Task", "Action", "Func", "Type", "Object", "String", "Thread", "Timer", "Path", "File", "Directory", "Index",
    "Range", "Attribute", "Exception", "Guid", "Version", "Encoding", "Environment", "Math", "Convert", "Console",
    "Random", "Uri", "Json", "Http", "Results", "Endpoint", "Controller", "ControllerBase", "Activity", "Monitor",
    "Process", "Point", "Polygon", "LineString", "MultiPolygon", "MultiPoint", "MultiLineString", "Geometry",
    "Coordinate", "Envelope", "Feature", "DateTime", "TimeSpan", "Decimal", "Enum", "Array", "List", "Dictionary",
    "Startup", "Assembly", "Delegate", "Nullable", "Lazy", "Tuple", "ValueTask", "Claim", "HttpContext", "Role",
    "Permission", "User", "Session", "Notification", "Event", "Entity", "Model", "Order", "Status", "State",
}


def snake(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    s = re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"_\1", s)
    return s.lower()


def kebab(name: str) -> str:
    return snake(name).replace("_", "-")


def pascal(name: str) -> str:
    return name[:1].upper() + name[1:]


def camel(name: str) -> str:
    return name[:1].lower() + name[1:]


def plural(word: str) -> str:
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", word):
        return word + "es"
    return word + "s"


def table(entity: Entity | str) -> str:
    name = entity if isinstance(entity, str) else entity.name
    return snake(plural(name))


def singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if re.search(r"(s|x|z|ch|sh)es$", word):
        return word[:-2]
    return word[:-1] if word.endswith("s") else word


@lru_cache
def declared_types() -> frozenset[str]:
    """Type names declared by our backend and mock templates (the generated code must not redeclare them)."""
    names: set[str] = set()
    for root in (TEMPLATES_DIR / "backend", TEMPLATES_DIR / "mocks"):
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in (".j2", ".cs"):
                names.update(re.findall(r"\b(?:class|record|interface|enum|struct)\s+([A-Z][A-Za-z0-9]+)(?![{\w])",
                                        p.read_text(encoding="utf-8", errors="ignore")))
    return frozenset(names)


@lru_cache
def reserved_entity_names() -> frozenset[str]:
    """Entity names whose generated classes (X, XDto, XService, XFilter, XUpsertRequest, XsController) would clash."""
    out = set(declared_types()) | FRAMEWORK_TYPES
    for t in declared_types():
        for sfx in ("Dto", "Service", "Filter", "UpsertRequest"):
            if t.endswith(sfx) and len(t) > len(sfx):
                out.add(t[:-len(sfx)])
        if t.endswith("Controller") and len(t) > len("Controller"):
            out.add(singular(t[:-len("Controller")]))
    return frozenset(out)


@lru_cache
def reserved_member_names() -> frozenset[str]:
    """Operation / job / event names whose generated classes (XRequest, XResponse, XHandler, XJob, XPayload) would clash."""
    out = set(declared_types()) | FRAMEWORK_TYPES
    for t in declared_types():
        for sfx in ("Request", "Response", "Handler", "Job", "Payload"):
            if t.endswith(sfx) and len(t) > len(sfx):
                out.add(t[:-len(sfx)])
    return frozenset(out)


def route(entity: Entity | str) -> str:
    name = entity if isinstance(entity, str) else entity.name
    return kebab(plural(name))


def column(f: Field | str) -> str:
    name = f if isinstance(f, str) else f.name
    return snake(name)


GENERIC_ENUM_NAMES = {"status", "type", "kind", "state", "category", "level", "priority"}
# Types visible in generated files through usings: an enum with such a name would be ambiguous.
RESERVED_TYPES = {
    "EntityState", "Task", "Action", "Type", "Point", "Geometry", "Environment", "Thread", "Timer", "Path", "File",
    "Directory", "Index", "Range", "Attribute", "Exception", "Guid", "Version", "Encoding", "Formatting", "Order",
    "Status", "State", "Role", "Permission", "Permissions", "Event", "Notification", "User", "Session", "Mode",
    "Priority", "Kind", "Level", "Category", "Channel", "Message", "Group", "Activity", "Model", "Entity",
}


def enum_type(entity: Entity, f: Field) -> str:
    name = pascal(f.name)
    if f.name in GENERIC_ENUM_NAMES or name in RESERVED_TYPES or name == entity.name:
        return entity.name + name
    return name


def all_enums(spec: AppSpec) -> list[tuple[str, Entity, Field]]:
    seen: dict[str, tuple[str, Entity, Field]] = {}
    for e in spec.entities:
        for f in e.fields:
            if f.type == "enum":
                name = enum_type(e, f)
                if name in seen and seen[name][2].enum_values != f.enum_values:
                    name = e.name + pascal(f.name)
                seen.setdefault(name, (name, e, f))
    return list(seen.values())


def enum_name_for(spec: AppSpec, entity: Entity, f: Field) -> str:
    for name, e, ff in all_enums(spec):
        if ff.enum_values == f.enum_values and (e.name == entity.name and ff.name == f.name
                                                 or name == enum_type(entity, f)):
            return name
    return enum_type(entity, f)


_CS = {
    "string": "string", "text": "string", "email": "string", "phone": "string", "url": "string",
    "int": "int", "decimal": "decimal", "money": "decimal", "bool": "bool",
    "date": "DateOnly", "datetime": "DateTimeOffset", "ref": "Guid", "json": "string",
    "geo_point": "Point", "geo_line": "Geometry", "geo_polygon": "Geometry", "geometry": "Geometry",
}
_REFERENCE_TYPES = {"string", "Point", "Geometry"}


def cs_type(spec: AppSpec, entity: Entity | None, f: Field, *, force_nullable: bool = False) -> str:
    if f.type == "enum":
        base = enum_name_for(spec, entity, f) if entity else "string"
    else:
        base = _CS[f.type]
    nullable = force_nullable or not f.required
    if base in _REFERENCE_TYPES:
        return base + "?" if nullable else base
    return base + "?" if nullable else base


def cs_default(spec: AppSpec, entity: Entity | None, f: Field) -> str:
    """Initializer for non-nullable reference properties."""
    t = cs_type(spec, entity, f)
    if t == "string":
        return ' = "";'
    if t in ("Point", "Geometry"):
        return " = null!;"
    if f.type == "enum" and f.required and entity is not None:
        val = f.default if f.default in (f.enum_values or []) else (f.enum_values or [""])[0]
        return f" = {enum_name_for(spec, entity, f)}.{val};"
    return ""


def sql_type(f: Field) -> str:
    return {
        "string": f"varchar({f.max_length or 255})", "text": "text", "email": "varchar(255)",
        "phone": "varchar(32)", "url": "varchar(2048)", "int": "integer",
        "decimal": "numeric(18,6)", "money": "numeric(18,2)", "bool": "boolean", "date": "date",
        "datetime": "timestamptz", "ref": "uuid", "json": "jsonb", "enum": "varchar(64)",
        "geo_point": "geometry(Point,4326)", "geo_line": "geometry(Geometry,4326)",
        "geo_polygon": "geometry(Geometry,4326)", "geometry": "geometry(Geometry,4326)",
    }[f.type]


def ts_type(f: Field) -> str:
    if f.type == "enum":
        return " | ".join(f"'{v}'" for v in f.enum_values or []) or "string"
    return {
        "int": "number", "decimal": "number", "money": "number", "bool": "boolean",
        "json": "unknown", "geo_point": "GeoJsonGeometry", "geo_line": "GeoJsonGeometry",
        "geo_polygon": "GeoJsonGeometry", "geometry": "GeoJsonGeometry",
    }.get(f.type, "string")


def kt_type(f: Field) -> str:
    base = {
        "int": "Int", "decimal": "Double", "money": "Double", "bool": "Boolean",
        "json": "JsonElement", "geo_point": "JsonElement", "geo_line": "JsonElement",
        "geo_polygon": "JsonElement", "geometry": "JsonElement",
    }.get(f.type, "String")
    return base + "?"


def cs_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def cs_verbatim(s: str) -> str:
    return '@"' + s.replace('"', '""') + '"'


def navigations(spec: AppSpec, e: Entity) -> list[tuple[str, str, Field]]:
    """EF navigation properties for ref fields: (name, type, field), e.g. roomId -> Room: MeetingRoom."""
    taken = {pascal(f.name) for f in e.fields} | {e.name}
    out = []
    for f in e.fields:
        if f.type != "ref" or not f.name.endswith("Id") or len(f.name) <= 2:
            continue
        name = pascal(f.name[:-2])
        target = "AppUser" if f.ref == "User" else f.ref
        if name in taken or (f.ref != "User" and spec.entity(f.ref) is None):
            continue
        taken.add(name)
        out.append((name, target, f))
    return out
