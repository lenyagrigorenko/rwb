"""Web UI and API of the factory: start runs, watch agents live, answer questions, deploy results."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse

from .builder import run as run_cmd
from .pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[3]
CASES = ROOT / "cases"
RUNS = ROOT / "runs"
OUT = ROOT / "out"
INPUTS = RUNS / "_inputs"
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Factory")
ACTIVE: dict[str, dict] = {}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/presets")
def presets() -> list[dict]:
    out = []
    for d in sorted(CASES.iterdir()):
        req = d / "requirements.md"
        if req.exists():
            text = req.read_text(encoding="utf-8")
            title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), d.name)
            files = [p.name for p in (d / "data").iterdir()] if (d / "data").exists() else []
            out.append({"id": d.name, "title": title, "requirements": text, "files": files})
    return out


@app.post("/api/runs")
async def start_run(requirements: str = Form(...), preset: str = Form(""), answer_timeout: int = Form(90),
                    files: list[UploadFile] = File(default=[])) -> dict:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    case_dir = INPUTS / run_id
    data_dir = case_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "requirements.md").write_text(requirements, encoding="utf-8")
    if preset and (CASES / preset / "data").exists():
        for p in (CASES / preset / "data").iterdir():
            (data_dir / p.name).write_bytes(p.read_bytes())
    for f in files:
        if f.filename:
            (data_dir / Path(f.filename).name).write_bytes(await f.read())

    loop = asyncio.get_running_loop()
    answers_future: asyncio.Future = loop.create_future()

    async def provide_answers() -> dict:
        return await answers_future

    pipeline = Pipeline(case_dir, OUT, RUNS, run_id=run_id, answer_timeout=answer_timeout, answers=provide_answers)
    ACTIVE[run_id] = {"pipeline": pipeline, "answers": answers_future}
    asyncio.create_task(_run(pipeline))
    return {"run_id": run_id}


async def _run(pipeline: Pipeline) -> None:
    try:
        await pipeline.run()
    except Exception:
        pass


@app.post("/api/runs/{run_id}/answers")
def answer(run_id: str, answers: dict) -> dict:
    entry = ACTIVE.get(run_id)
    if not entry:
        raise HTTPException(404)
    if not entry["answers"].done():
        entry["answers"].set_result(answers)
    return {"ok": True}


@app.get("/api/runs")
def list_runs() -> list[dict]:
    items = []
    for d in sorted(RUNS.iterdir(), reverse=True):
        st = d / "state.json"
        if st.exists():
            s = json.loads(st.read_text(encoding="utf-8"))
            items.append({"run_id": s["run_id"], "status": s["status"], "case": s["case"],
                          "project": s.get("result", {}).get("project_dir")})
    return items[:30]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    st = RUNS / run_id / "state.json"
    if not st.exists():
        raise HTTPException(404)
    return json.loads(st.read_text(encoding="utf-8"))


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str) -> StreamingResponse:
    path = RUNS / run_id / "state.json"

    async def events():
        last = 0.0
        for _ in range(60 * 60 * 2):
            if path.exists() and path.stat().st_mtime != last:
                last = path.stat().st_mtime
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    await asyncio.sleep(0.2)
                    continue
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                if data.get("status") in ("done", "failed"):
                    break
            await asyncio.sleep(0.5)

    return StreamingResponse(events(), media_type="text/event-stream")


def _project(run_id: str) -> Path:
    state = get_run(run_id)
    p = state.get("result", {}).get("project_dir")
    if not p:
        raise HTTPException(409, "Проект ещё не сгенерирован")
    return Path(p)


@app.get("/api/runs/{run_id}/docs")
def list_docs(run_id: str) -> list[str]:
    project = _project(run_id)
    files = ["README.md"] + sorted(str(p.relative_to(project)) for p in (project / "docs").glob("*.md"))
    return [f for f in files if (project / f).exists()]


@app.get("/api/runs/{run_id}/file", response_class=PlainTextResponse)
def read_file(run_id: str, path: str) -> str:
    project = _project(run_id).resolve()
    target = (project / path).resolve()
    if project not in target.parents and target != project:
        raise HTTPException(400)
    if not target.is_file():
        raise HTTPException(404)
    return target.read_text(encoding="utf-8")


@app.post("/api/runs/{run_id}/deploy")
async def deploy(run_id: str) -> dict:
    project = _project(run_id)
    index = abs(hash(project.name)) % 50
    ports = {"WEB_PORT": 8088 + index * 10, "API_PORT": 8080 + index * 10, "DB_PORT": 15432 + index,
             "MOCKS_PORT": 8081 + index * 10}
    env = (project / ".env.example").read_text(encoding="utf-8")
    for k, v in ports.items():
        env = re.sub(rf"^{k}=.*$", f"{k}={v}", env, flags=re.M)
    (project / ".env").write_text(env, encoding="utf-8")
    code, out = await run_cmd(["docker", "compose", "up", "-d", "--build", "--wait"], cwd=project, timeout=1500)
    host = os.environ.get("PUBLIC_HOST", "localhost")
    return {"ok": code == 0, "web": f"http://{host}:{ports['WEB_PORT']}", "api": f"http://{host}:{ports['API_PORT']}/scalar",
            "log": out[-3000:]}


@app.get("/api/runs/{run_id}/archive")
async def archive(run_id: str) -> FileResponse:
    project = _project(run_id)
    target = RUNS / run_id / f"{project.name}.zip"
    await run_cmd(["zip", "-qr", str(target), ".", "-x", "*/node_modules/*", "*/bin/*", "*/obj/*", ".env"], cwd=project)
    return FileResponse(target, filename=target.name)


@app.get("/api/runs/{run_id}/apk")
def apk(run_id: str) -> FileResponse:
    path = get_run(run_id).get("result", {}).get("apk")
    if not path or not Path(path).exists():
        raise HTTPException(404, "APK не собран")
    return FileResponse(path, filename=f"{_project(run_id).name}.apk", media_type="application/vnd.android.package-archive")


@app.get("/api/runs/{run_id}/apk-qr")
def apk_qr(run_id: str) -> HTMLResponse:
    import io

    import qrcode
    import qrcode.image.svg

    host = os.environ.get("PUBLIC_HOST", "localhost")
    port = os.environ.get("PORT", "8000")
    img = qrcode.make(f"http://{host}:{port}/api/runs/{run_id}/apk", image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return HTMLResponse(buf.getvalue().decode(), media_type="image/svg+xml")


@app.get("/api/runs/{run_id}/media")
def media(run_id: str, path: str) -> FileResponse:
    project = _project(run_id).resolve()
    target = (project / path).resolve()
    if project not in target.parents or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)
