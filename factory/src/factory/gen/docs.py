"""Documentation generator: facts from the IR and run results, prose from the analysis."""

from __future__ import annotations

from pathlib import Path

from ..ir import AppSpec, Field
from .. import validate
from .backend import admin_roles, seed_users
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
        "spec_warnings": spec_warnings or [], "security_findings": "",
        "topics": sorted({ev.topic for ev in spec.events}),
    })
    docs = out / "docs"
    r.render("README.md.j2", out / "README.md")
    r.render("AGENTS.md.j2", out / "AGENTS.md")
    r.render("01-system-analysis.md.j2", docs / "01-system-analysis.md")
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
