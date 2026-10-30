"""Jinja2 rendering with IR-aware helpers."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..ir import AppSpec
from . import naming as n

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"


class Renderer:
    def __init__(self, group: str, spec: AppSpec, extra: dict | None = None):
        self.spec = spec
        self.env = Environment(
            loader=FileSystemLoader(TEMPLATES / group),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            undefined=StrictUndefined,
        )
        self.env.globals.update(
            spec=spec,
            N=spec.namespace,
            pascal=n.pascal,
            camel=n.camel,
            plural=n.plural,
            kebab=n.kebab,
            snake=n.snake,
            route=n.route,
            table=n.table,
            column=n.column,
            sql_type=n.sql_type,
            ts_type=n.ts_type,
            kt_type=n.kt_type,
            cs_string=n.cs_string,
            cs_type=lambda e, f, **kw: n.cs_type(spec, e, f, **kw),
            cs_default=lambda e, f: n.cs_default(spec, e, f),
            enum_name=lambda e, f: n.enum_name_for(spec, e, f),
            navigations=lambda e: n.navigations(spec, e),
            **(extra or {}),
        )
        self.env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
        self.written: list[Path] = []

    def text(self, template: str, **ctx) -> str:
        return self.env.get_template(template).render(**ctx)

    def render(self, template: str, dest: Path, **ctx) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self.text(template, **ctx), encoding="utf-8")
        self.written.append(dest)
        return dest
