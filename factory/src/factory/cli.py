"""Command line entry point: `factory run <case_dir>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    parser = argparse.ArgumentParser(prog="factory", description="Агентский конвейер: требования → приложение")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="Прогнать конвейер для кейса (каталог с requirements.md и data/)")
    run.add_argument("case", type=Path)
    run.add_argument("--out", type=Path, default=ROOT / "out")
    run.add_argument("--runs", type=Path, default=ROOT / "runs")
    run.add_argument("--skip-verify", action="store_true")
    sub.add_parser("doctor", help="Проверка хоста и модели перед прогоном")
    serve = sub.add_parser("serve", help="Веб-интерфейс конвейера")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()

    if args.cmd == "run":
        from .pipeline import Pipeline
        p = Pipeline(args.case.resolve(), args.out.resolve(), args.runs.resolve(), skip_verify=args.skip_verify)
        state = asyncio.run(p.run())
        print(json.dumps({"status": state.status, "project": state.result.get("project_dir"),
                          "metrics": {k: v for k, v in state.metrics.items() if k != "by_stage"}}, ensure_ascii=False, indent=1))
    elif args.cmd == "doctor":
        from .doctor import main as doctor
        doctor()
    elif args.cmd == "serve":
        import uvicorn
        # Открытые SSE-потоки браузера не должны задерживать остановку (иначе порт остаётся занят при перезапуске).
        uvicorn.run("factory.server:app", host="0.0.0.0", port=args.port, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
