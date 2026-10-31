"""Runtime verification: deploy the generated app, execute the demo scenario, collect failures with server-side causes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx

from .builder import run
from .gen.backend import admin_roles, login_of
from .ir import AppSpec

DEMO_PASSWORD = "Demo2026!"


def total_memory_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            return int(f.readline().split()[1]) / 2**20
    except OSError:
        import subprocess
        try:
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()) / 2**30
        except Exception:
            return 16.0


def low_memory() -> bool:
    """FACTORY_LOW_MEMORY=1/0 forces the mode; otherwise auto: less than 6 GB RAM."""
    flag = os.environ.get("FACTORY_LOW_MEMORY", "").strip()
    return flag == "1" if flag else total_memory_gb() < 6


async def stop_other_stands(project: Path) -> None:
    """On small hosts keep only one generated stand running."""
    for other in project.parent.iterdir():
        if other != project and (other / "docker-compose.yml").exists() and (other / ".env").exists():
            await run(["docker", "compose", "down"], cwd=other, timeout=120)


def ports_for(slug: str) -> dict[str, int]:
    index = sum(ord(c) for c in slug) % 40
    return {"WEB_PORT": 8100 + index * 10, "API_PORT": 8101 + index * 10, "MOCKS_PORT": 8102 + index * 10,
            "DB_PORT": 15500 + index}


def public_base() -> str | None:
    """Внешний адрес интерфейса фабрики: PUBLIC_URL (если порт проброшен под другим номером) или PUBLIC_HOST:PORT."""
    if url := os.environ.get("PUBLIC_URL", "").strip().rstrip("/"):
        return url
    host = os.environ.get("PUBLIC_HOST")
    return f"http://{host}:{os.environ.get('PORT', '8000')}" if host else None


def stand_api_url(name: str) -> str | None:
    """API стенда через прокси интерфейса фабрики (/stand/<проект>): снаружи нужен только один порт."""
    base = public_base()
    return f"{base}/stand/{name}" if base else None


def write_env(project: Path, slug: str) -> dict[str, int]:
    ports = ports_for(project.name)
    env = (project / ".env.example").read_text(encoding="utf-8")
    for k, v in ports.items():
        env = re.sub(rf"^{k}=.*$", f"{k}={v}", env, flags=re.M)
    (project / ".env").write_text(env, encoding="utf-8")
    return ports


async def deploy(project: Path, slug: str, services: list[str] | None = None) -> tuple[bool, str, dict[str, int]]:
    if low_memory() and not services:
        await stop_other_stands(project)
    ports = write_env(project, slug)
    code, out = await run(["docker", "compose", "up", "-d", "--build", "--wait", *(services or [])], cwd=project, timeout=1800)
    return code == 0, out[-4000:], ports


async def redeploy_clean(project: Path, slug: str) -> tuple[bool, str, dict[str, int]]:
    """Fresh database and rebuilt backend: every demo round starts from the same state."""
    await run(["docker", "compose", "down", "-v", "--remove-orphans"], cwd=project, timeout=300)
    return await deploy(project, slug)


async def redeploy_backend(project: Path, slug: str) -> tuple[bool, str, dict[str, int]]:
    """Between demo rounds: fresh schema and rebuilt backend; Kafka, Postgres and the web container keep running."""
    ports = write_env(project, slug)
    await run(["docker", "compose", "stop", "backend"], cwd=project, timeout=120)
    code, out = await run(["docker", "compose", "exec", "-T", "db", "psql", "-U", "app", "-d", "app", "-v", "ON_ERROR_STOP=1", "-c",
                           "DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE EXTENSION IF NOT EXISTS postgis;"],
                          cwd=project, timeout=120)
    if code != 0:
        return await redeploy_clean(project, slug)
    code, out = await run(["docker", "compose", "up", "-d", "--build", "--wait", "backend"], cwd=project, timeout=1800)
    if code != 0:
        return await redeploy_clean(project, slug)
    return True, out[-4000:], ports


async def logs_since(project: Path, since: float) -> str:
    _, out = await run(["docker", "compose", "logs", "backend", "--no-color", "--since", str(int(since))], cwd=project, timeout=60)
    return out


def exception_summaries(log_text: str, limit: int = 6) -> list[str]:
    """Short causes of failed requests from the JSON console log."""
    found = []
    for line in log_text.splitlines():
        if "Request failed" not in line and "Request rejected" not in line and "unhandled exception" not in line:
            continue
        raw = line.split("|", 1)[1].strip() if "|" in line else line
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ex = entry.get("Exception") or entry.get("Message") or ""
        lines = [x.strip() for x in ex.splitlines() if x.strip()]
        inner = next((x for x in lines if "Exception (" in x or x.startswith("--->") or "Npgsql" in x), "")
        at = next((x for x in lines if ".Application/Operations/" in x or ".Application/Jobs/" in x), "")
        found.append(" | ".join(x for x in (lines[0] if lines else "", inner, at) if x)[:600])
    return found[-limit:]


async def run_demo(spec: AppSpec, api: str, timeout: float = 240) -> dict:
    admin = login_of(spec, admin_roles(spec)[0])
    async with httpx.AsyncClient(base_url=api, timeout=30) as client:
        token = (await client.post("/api/v1/auth/login", json={"login": admin, "password": DEMO_PASSWORD})).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        await client.post("/api/v1/demo/run", headers=headers)
        started = time.time()
        status: dict = {}
        while time.time() - started < timeout:
            await asyncio.sleep(2)
            status = (await client.get("/api/v1/demo", headers=headers)).json()
            if not status.get("running"):
                break
        return status


async def save_openapi(api: str, dest: Path) -> bool:
    """Copy of the stand's OpenAPI document for the repository (docs/api/openapi.json)."""
    try:
        async with httpx.AsyncClient(base_url=api, timeout=30) as client:
            resp = await client.get("/openapi/v1.json")
            resp.raise_for_status()
            doc = resp.json()
    except (httpx.HTTPError, ValueError):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def failed_targets(spec: AppSpec, status: dict) -> dict[str, list[str]]:
    """Map failed demo steps to the operation/job responsible for them."""
    if not spec.demo:
        return {}
    by_id = {s.id: s for s in spec.demo.steps}
    emitters: dict[str, str] = {}
    for op in spec.operations:
        for ev in op.emits:
            emitters.setdefault(ev, op.name)
    for job in spec.jobs:
        for ev in job.emits:
            emitters.setdefault(ev, job.name)
    targets: dict[str, list[str]] = {}
    last_op_ok: dict[str, bool] = {}
    first_failure_seen = False
    flow = "Ход демо-сценария: " + "; ".join(
        f"{st['title']} — {st.get('status')}" + (f" ({st.get('detail')})" if st.get("action") == "advance_time" and st.get("detail") else "")
        for st in status.get("steps", []))
    for st in status.get("steps", []):
        step = by_id.get(st["id"])
        if not step:
            continue
        if step.operation:
            last_op_ok[step.operation] = st.get("status") == "done"
        if st.get("status") != "failed":
            continue
        detail = st.get("detail") or ""
        if step.expect_error:
            continue
        if step.action in ("operation", "route") and step.operation:
            # Only real HTTP failures are logic bugs; missing data is not.
            if re.search(r": 5\d\d ", detail):
                # 500 — всегда ошибка кода, а не каскад: чиним независимо от первопричины.
                targets.setdefault(step.operation, []).append(f"Шаг «{step.title}»: {detail[:700]}")
                continue
            if first_failure_seen:
                continue
            if re.search(r": (400|403|404|409|422) ", detail) and not detail.startswith("Не найдено"):
                targets.setdefault(step.operation, []).append(f"Шаг «{step.title}»: {detail[:700]}")
                # Следующие шаги обычно падают каскадом от этого — сначала чиним первопричину.
                first_failure_seen = True
        elif step.action == "expect":
            ev = step.args.get("event", "")
            owner = emitters.get(ev)
            if owner and owner not in targets and last_op_ok.get(owner, owner in {j.name for j in spec.jobs}):
                targets.setdefault(owner, []).append(
                    f"Шаг «{step.title}»: событие {ev} не было опубликовано, хотя предыдущие шаги выполнены")
    return {k: v + [flow] for k, v in targets.items()}
