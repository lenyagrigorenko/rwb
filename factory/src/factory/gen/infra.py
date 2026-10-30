"""Infrastructure generator: docker compose, env, GitLab CI, helper scripts."""

from __future__ import annotations

from pathlib import Path

from ..ir import AppSpec
from .render import Renderer


def generate(spec: AppSpec, out: Path) -> list[Path]:
    from .backend import admin_roles, login_of
    r = Renderer("infra", spec, extra={"admin_login": login_of(spec, admin_roles(spec)[0])})
    r.render("docker-compose.yml.j2", out / "docker-compose.yml")
    r.render("env.example.j2", out / ".env.example")
    for name, dest in (("gitlab-ci.yml.j2", ".gitlab-ci.yml"), ("gitignore.j2", ".gitignore"),
                       ("Makefile.j2", "Makefile"), ("smoke.sh.j2", "scripts/smoke.sh")):
        if (Path(r.env.loader.searchpath[0]) / name).exists():
            r.render(name, out / dest)
    smoke = out / "scripts" / "smoke.sh"
    if smoke.exists():
        smoke.chmod(0o755)
    return r.written
