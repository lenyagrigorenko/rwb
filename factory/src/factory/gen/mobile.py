"""Mobile generator: IR -> Android (Kotlin 2.2, Jetpack Compose, Material 3)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from ..builder import remove_tree
from ..ir import AppSpec
from .backend import id_aliases
from .render import TEMPLATES, Renderer

SKIP = {".gradle", "build", "local.properties"}


def ks(value: str | None) -> str:
    """Kotlin string literal."""
    return json.dumps(value or "", ensure_ascii=False).replace("$", "\\$")


def generate(spec: AppSpec, out: Path) -> list[Path]:
    dest = out / "mobile"
    if dest.exists():
        remove_tree(dest)
    shutil.copytree(TEMPLATES / "mobile" / "base", dest, ignore=lambda d, names: [x for x in names if x in SKIP])
    screens = [s for s in spec.screens if s.platform == "mobile"]
    roles = [r for r in spec.roles if any(r.name in s.roles for s in screens)] or list(spec.roles)
    package = "ru.factory." + re.sub(r"[^a-z0-9]", "", spec.slug.lower())[:30]
    r = Renderer("mobile", spec, extra={"ks": ks, "mobile_screens": screens, "mobile_roles": roles,
                                        "package": package, "id_aliases": lambda e: id_aliases(spec, e)})
    r.render("app.build.gradle.kts.j2", dest / "app" / "build.gradle.kts")
    r.render("AndroidManifest.xml.j2", dest / "app" / "src" / "main" / "AndroidManifest.xml")
    r.render("AppSpec.kt.j2", dest / "app" / "src" / "main" / "java" / "app" / "mobile" / "AppSpec.kt")
    r.render("README.md.j2", dest / "README.md")
    for script in ("gradlew",):
        p = dest / script
        if p.exists():
            p.chmod(0o755)
    return r.written
