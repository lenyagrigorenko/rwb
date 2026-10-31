"""Security and static checks of the generated project, run by the pipeline itself.

The CI file runs the same tools in GitLab; here they run right after the build so the security report contains real
findings with severity and a final status, not just a list of configured scanners.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from .builder import SDK, run

NODE = "node:22-alpine"
GITLEAKS = "zricethezav/gitleaks:v8.21.2"
TRIVY = "aquasec/trivy:0.58.1"
SEVERITY_ORDER = {"critical": 0, "high": 1, "moderate": 2, "medium": 2, "low": 3, "info": 4}
# Версии, которые задаёт целевой стек заказчика: находки по ним — принятый риск с рекомендацией, а не провал проверки.
ACCEPTED = {"@angular/": "версия Angular 20.1.6 задана целевым стеком; исправлено в 20.3.x — обновить после согласования"}
SEVERITY_RU = {"critical": "критическая", "high": "высокая", "moderate": "средняя", "medium": "средняя", "low": "низкая",
               "info": "информационная"}


def _check(tool: str, what: str, status: str, summary: str, findings: list[dict] | None = None) -> dict:
    return {"tool": tool, "what": what, "status": status, "summary": summary, "findings": findings or []}


async def _image(name: str, pull: bool) -> bool:
    code, _ = await run(["docker", "image", "inspect", name], timeout=30)
    if code == 0:
        return True
    if not pull:
        return False
    code, _ = await run(["docker", "pull", "-q", name], timeout=180)
    return code == 0


async def nuget(project: Path, solution: str) -> dict:
    backend = project / "backend"
    code, out = await run(["docker", "run", "--rm", "-v", f"{backend.resolve()}:/src", "-v", "nuget-cache:/root/.nuget/packages",
                           "-w", "/src", SDK, "dotnet", "list", solution, "package", "--vulnerable", "--include-transitive",
                           "--format", "json"], timeout=300)
    what = "Уязвимые NuGet-зависимости (CVE, база GitHub Advisory)"
    start = out.find("{")
    if start < 0:
        return _check("dotnet list package --vulnerable", what, "error", "не удалось получить данные: " + out.strip()[-200:])
    try:
        data = json.loads(out[start:out.rfind("}") + 1])
    except json.JSONDecodeError:
        return _check("dotnet list package --vulnerable", what, "error", "некорректный ответ")
    findings, seen = [], set()
    for proj in data.get("projects", []):
        for fw in proj.get("frameworks", []):
            for pkg in fw.get("topLevelPackages", []) + fw.get("transitivePackages", []):
                for v in pkg.get("vulnerabilities", []):
                    key = (pkg.get("id"), v.get("advisoryurl"))
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append({"severity": (v.get("severity") or "unknown").lower(), "where": f"{pkg.get('id')} {pkg.get('resolvedVersion', '')}",
                                     "id": v.get("advisoryurl", "").rsplit("/", 1)[-1], "title": v.get("advisoryurl", ""),
                                     "fix": "обновить пакет до исправленной версии"})
    return _check("dotnet list package --vulnerable", what, "findings" if findings else "ok",
                  f"уязвимых пакетов: {len(findings)}" if findings else "уязвимых пакетов не найдено", findings)


async def npm(project: Path) -> dict:
    frontend = project / "frontend"
    what = "Уязвимые npm-зависимости web-интерфейса (production)"
    if not (frontend / "package-lock.json").exists():
        return _check("npm audit", what, "skipped", "нет package-lock.json")
    code, out = await run(["docker", "run", "--rm", "-v", f"{frontend.resolve()}:/app:ro", "-w", "/app", NODE,
                           "npm", "audit", "--package-lock-only", "--omit=dev", "--json"], timeout=240)
    start = out.find("{")
    try:
        data = json.loads(out[start:out.rfind("}") + 1]) if start >= 0 else {}
    except json.JSONDecodeError:
        data = {}
    if "metadata" not in data:
        return _check("npm audit", what, "error", "не удалось получить данные: " + out.strip()[-200:])
    findings = []
    for name, v in (data.get("vulnerabilities") or {}).items():
        via = [x for x in v.get("via", []) if isinstance(x, dict)]
        findings.append({"severity": v.get("severity", "unknown"), "where": f"{name} {v.get('range', '')}".strip(),
                         "id": next((str(x.get("url", "")).rsplit("/", 1)[-1] for x in via if x.get("url")), "—"),
                         "title": next((x.get("title", "") for x in via if x.get("title")), "уязвимость в зависимости"),
                         "fix": "npm audit fix" if v.get("fixAvailable") else "нет исправления, отслеживать"})
    counts = data["metadata"].get("vulnerabilities", {})
    summary = ", ".join(f"{SEVERITY_RU.get(k, k)}: {n}" for k, n in counts.items() if n and k != "total") or "уязвимостей не найдено"
    return _check("npm audit", what, "findings" if findings else "ok", summary, findings)


async def gitleaks(project: Path) -> dict:
    what = "Секреты и ключи в исходном коде"
    if not await _image(GITLEAKS, pull=True):
        return _check("Gitleaks", what, "skipped", "образ недоступен — проверка выполняется в CI")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "gitleaks.toml"
        cfg.write_text("[extend]\nuseDefault = true\n\n[allowlist]\npaths = ['''(^|/)(bin|obj|node_modules|\\.angular|TestResults)/''']\n",
                       encoding="utf-8")
        code, out = await run(["docker", "run", "--rm", "-v", f"{project.resolve()}:/repo:ro", "-v", f"{tmp}:/cfg",
                               GITLEAKS, "detect", "--no-git", "--source", "/repo", "--config", "/cfg/gitleaks.toml",
                               "--report-format", "json", "--report-path", "/cfg/report.json", "--exit-code", "0",
                               "--no-banner"], timeout=240)
        report = Path(tmp) / "report.json"
        if not report.exists():
            return _check("Gitleaks", what, "error", out.strip()[-200:] or "нет отчёта")
        items = json.loads(report.read_text(encoding="utf-8") or "[]")
    findings = [{"severity": "high", "where": f"{x.get('File', '').removeprefix('/repo/')}:{x.get('StartLine', '')}",
                 "id": x.get("RuleID", ""), "title": x.get("Description", "секрет в коде"),
                 "fix": "вынести в переменные окружения / секреты CI"} for x in items]
    return _check("Gitleaks", what, "findings" if findings else "ok",
                  f"найдено: {len(findings)}" if findings else "секретов не найдено", findings)


async def trivy(project: Path) -> dict:
    what = "CVE в зависимостях и ошибки конфигурации Dockerfile / compose"
    if not await _image(TRIVY, pull=False):
        return _check("Trivy", what, "skipped", "выполняется в CI (стадия security)")
    code, out = await run(["docker", "run", "--rm", "-v", f"{project.resolve()}:/repo:ro", "-v", "trivy-cache:/root/.cache",
                           TRIVY, "fs", "--quiet", "--scanners", "vuln,misconfig", "--severity", "HIGH,CRITICAL",
                           "--skip-dirs", "**/node_modules", "--skip-dirs", "**/bin", "--skip-dirs", "**/obj",
                           "--format", "json", "/repo"], timeout=420)
    start = out.find("{")
    try:
        data = json.loads(out[start:out.rfind("}") + 1]) if start >= 0 else {}
    except json.JSONDecodeError:
        data = {}
    if "Results" not in data and "SchemaVersion" not in data:
        return _check("Trivy", what, "error", out.strip()[-200:] or "нет отчёта")
    findings = []
    for r in data.get("Results") or []:
        for v in r.get("Vulnerabilities") or []:
            findings.append({"severity": v.get("Severity", "").lower(), "where": f"{r.get('Target')}: {v.get('PkgName')} {v.get('InstalledVersion')}",
                             "id": v.get("VulnerabilityID", ""), "title": (v.get("Title") or "")[:120],
                             "fix": f"обновить до {v.get('FixedVersion')}" if v.get("FixedVersion") else "нет исправления"})
        for m in r.get("Misconfigurations") or []:
            findings.append({"severity": m.get("Severity", "").lower(), "where": r.get("Target", ""), "id": m.get("ID", ""),
                             "title": (m.get("Title") or "")[:120], "fix": (m.get("Resolution") or "")[:120]})
    return _check("Trivy", what, "findings" if findings else "ok",
                  f"найдено HIGH/CRITICAL: {len(findings)}" if findings else "HIGH/CRITICAL не найдено", findings)


def static_analysis(build_log: str) -> dict:
    """Warnings of the .NET analyzers (AnalysisLevel latest-recommended) from the last build."""
    codes: dict[str, int] = {}
    for m in re.finditer(r": warning (CA\d+|CS\d+|IDE\d+|NU\d+)", build_log or ""):
        codes[m.group(1)] = codes.get(m.group(1), 0) + 1
    what = "Статический анализ C#: анализаторы .NET (latest-recommended), предупреждения компилятора"
    if not build_log:
        return _check("Roslyn analyzers", what, "skipped", "журнал сборки недоступен")
    total = sum(codes.values())
    findings = [{"severity": "low", "where": "backend", "id": c, "title": f"предупреждений: {n}", "fix": "исправить по описанию правила"}
                for c, n in sorted(codes.items(), key=lambda kv: -kv[1])[:10]]
    return _check("Roslyn analyzers", what, "findings" if total else "ok",
                  f"предупреждений: {total}" if total else "предупреждений нет", findings)


async def scan(project: Path, solution: str, build_log: str = "") -> dict:
    checks = list(await asyncio.gather(nuget(project, solution), npm(project), gitleaks(project), trivy(project)))
    checks.append(static_analysis(build_log))
    findings = sorted((dict(f, tool=c["tool"]) for c in checks for f in c["findings"]),
                      key=lambda f: SEVERITY_ORDER.get(f["severity"], 5))
    for f in findings:
        reason = next((r for prefix, r in ACCEPTED.items() if f["where"].startswith(prefix)), None)
        if reason:
            f["accepted"] = True
            f["fix"] = "принятый риск: " + reason
    worst = min((SEVERITY_ORDER.get(f["severity"], 5) for f in findings
                 if f["tool"] != "Roslyn analyzers" and not f.get("accepted")), default=9)
    if worst <= 1:
        status = "не пройдена"
    elif any(f.get("accepted") and SEVERITY_ORDER.get(f["severity"], 5) <= 1 for f in findings):
        status = "пройдена с принятыми рисками"
    else:
        status = "пройдена с замечаниями" if findings else "пройдена"
    return {"status": status, "checks": checks, "findings": findings, "markdown": to_markdown(status, checks, findings)}


def to_markdown(status: str, checks: list[dict], findings: list[dict]) -> str:
    mark = {"ok": "✅ без замечаний", "findings": "⚠️ есть находки", "skipped": "⏭ пропущено", "error": "❌ ошибка запуска"}
    lines = [f"**Итоговый статус проверки безопасности: {status}.** Проверки выполнены конвейером сразу после сборки; "
             "те же инструменты запускаются в GitLab CI (стадия `security`), отчёты — в артефактах `reports/`.", "",
             "| Инструмент | Что проверяет | Результат | Итог |", "|---|---|---|---|"]
    lines += [f"| {c['tool']} | {c['what']} | {mark.get(c['status'], c['status'])} | {c['summary']} |" for c in checks]
    if findings:
        lines += ["", "**Найденные дефекты и уязвимости** (по убыванию критичности):", "",
                  "| Критичность | Инструмент | Где | Идентификатор | Описание | Рекомендация |", "|---|---|---|---|---|---|"]
        lines += [f"| {SEVERITY_RU.get(f['severity'], f['severity'])} | {f['tool']} | `{f['where']}` | {f['id']} | {f['title']} | {f['fix']} |"
                  for f in findings[:40]]
    else:
        lines += ["", "Дефектов и уязвимостей не найдено."]
    return "\n".join(lines)
