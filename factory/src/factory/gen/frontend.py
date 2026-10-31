"""Frontend generator: IR -> Angular 20 + Angular Material admin (metadata-driven screens)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..builder import remove_tree
from ..ir import AppSpec
from . import naming as n
from .backend import admin_roles, id_aliases
from .render import TEMPLATES, Renderer

SKIP = {"node_modules", "dist", ".angular"}


def _field(f) -> dict:
    return {"name": f.name, "title": f.title, "type": f.type, "required": f.required, "maxLength": f.max_length,
            "min": f.min, "max": f.max, "regex": f.regex, "enumValues": f.enum_values, "enumTitles": f.enum_titles,
            "ref": f.ref, "listVisible": f.list_visible, "readonly": f.readonly}


def app_spec(spec: AppSpec) -> dict:
    perms: dict[str, dict[str, list[str]]] = {}
    for p in spec.permissions:
        perms.setdefault(p.role, {})[p.entity] = list(p.actions)
    entities = []
    for e in spec.entities:
        entities.append({
            "name": e.name, "title": e.title, "titlePlural": e.title_plural, "route": n.route(e),
            "description": e.description, "labelField": e.label_field, "ownerField": e.owner_field,
            "fields": [_field(f) for f in e.fields],
            "lifecycleField": e.lifecycle.field if e.lifecycle else None,
            "transitions": [{"name": t.name, "path": n.kebab(t.name), "title": t.title,
                             "from": t.from_states, "to": t.to, "roles": t.roles}
                            for t in (e.lifecycle.transitions if e.lifecycle else [])],
            "geo": bool(e.geo_fields),
            "covers": e.covers,
        })
    return {
        "slug": spec.slug,
        "productName": spec.product_name,
        "summary": spec.analysis.summary,
        "mapCenter": spec.map_center,
        "roles": [{"name": r.name, "title": r.title, "login": r.demo_login} for r in spec.roles],
        "adminRoles": admin_roles(spec),
        "entities": entities,
        "permissions": perms,
        "screens": [s.model_dump(include={"id", "name", "title", "roles", "widgets", "covers"})
                    for s in spec.screens if s.platform == "web"],
        "eventTitles": {ev.name: ev.title for ev in spec.events},
        "operations": [{
            "id": op.id, "name": op.name, "title": op.title, "description": op.description, "method": op.method,
            "path": op.path, "roles": op.roles,
            "entity": next((e.name for p in op.path_params for e in spec.entities if p in id_aliases(spec, e)), None),
            "inputs": [_field(f) for f in op.input],
        } for op in spec.operations],
        "hasDemo": bool(spec.demo and spec.demo.steps),
    }


def generate(spec: AppSpec, out: Path) -> list[Path]:
    dest = out / "frontend"
    if dest.exists():
        remove_tree(dest)
    shutil.copytree(TEMPLATES / "frontend" / "base", dest, ignore=lambda d, names: [x for x in names if x in SKIP])
    r = Renderer("frontend", spec)
    r.render("app-spec.ts.j2", dest / "src" / "app" / "generated" / "app-spec.ts",
             app_spec_json=json.dumps(app_spec(spec), ensure_ascii=False, indent=2))
    r.render("models.ts.j2", dest / "src" / "app" / "generated" / "models.ts")
    index = dest / "src" / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8")
        .replace('<html lang="en">', '<html lang="ru">')
        .replace("<title>Admin</title>", f"<title>{spec.product_name}</title>"),
        encoding="utf-8",
    )
    return r.written + [index]
