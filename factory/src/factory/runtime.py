"""Runtime verification: deploy the generated app, execute the demo scenario, collect failures with server-side causes."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx

from .builder import run
from .gen.backend import admin_roles, login_of
from .ir import AppSpec

DEMO_PASSWORD = "Demo2026!"


def ports_for(slug: str) -> dict[str, int]:
    index = sum(ord(c) for c in slug) % 40
    return {"WEB_PORT": 8100 + index * 10, "API_PORT": 8101 + index * 10, "MOCKS_PORT": 8102 + index * 10,
            "DB_PORT": 15500 + index}


def write_env(project: Path, slug: str) -> dict[str, int]:
    ports = ports_for(slug)
    env = (project / ".env.example").read_text(encoding="utf-8")
    for k, v in ports.items():
        env = re.sub(rf"^{k}=.*$", f"{k}={v}", env, flags=re.M)
    (project / ".env").write_text(env, encoding="utf-8")
    return ports


async def deploy(project: Path, slug: str, services: list[str] | None = None) -> tuple[bool, str, dict[str, int]]:
    ports = write_env(project, slug)
    code, out = await run(["docker", "compose", "up", "-d", "--build", "--wait", *(services or [])], cwd=project, timeout=1800)
    return code == 0, out[-4000:], ports


async def redeploy_clean(project: Path, slug: str) -> tuple[bool, str, dict[str, int]]:
    """Fresh database and rebuilt backend: every demo round starts from the same state."""
    await run(["docker", "compose", "down", "-v", "--remove-orphans"], cwd=project, timeout=300)
    return await deploy(project, slug)


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
            if re.search(r": (400|5\d\d) ", detail) and not detail.startswith("Не найдено"):
                targets.setdefault(step.operation, []).append(f"Шаг «{step.title}»: {detail[:700]}")
        elif step.action == "expect":
            ev = step.args.get("event", "")
            owner = emitters.get(ev)
            if owner and owner not in targets and last_op_ok.get(owner, owner in {j.name for j in spec.jobs}):
                targets.setdefault(owner, []).append(
                    f"Шаг «{step.title}»: событие {ev} не было опубликовано, хотя предыдущие шаги выполнены")
    return targets
