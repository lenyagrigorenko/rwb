#!/usr/bin/env bash
# Развёртывание фабрики на VM: зависимости, образы, прогрев кэшей, запуск веб-интерфейса.
# Использование (на VM, из корня репозитория): ./scripts/vm-setup.sh
# Ключ модели: factory/.env (LLM_BASE_URL=http://10.10.10.10:4000/v1, LLM_MODEL=qwen3.8-27b, LLM_API_KEY=..., LLM_PROVIDER=)
set -euo pipefail
cd "$(dirname "$0")/.."
test -f factory/.env || { echo "Нет factory/.env — создайте его (DEPLOY.md, раздел 3)"; exit 1; }

echo "== Docker"
docker version --format '{{.Server.Version}}' || { echo "Docker недоступен"; exit 1; }
docker compose version

echo "== uv / Python"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
(cd factory && uv sync -q)

echo "== Образы стека"
for img in mcr.microsoft.com/dotnet/sdk:10.0 mcr.microsoft.com/dotnet/aspnet:10.0 postgis/postgis:17-3.5 \
           node:22-alpine nginx:1.27-alpine apache/kafka:3.9.1 eclipse-temurin:17-jdk \
           mcr.microsoft.com/playwright:v1.64.1-noble; do
  docker pull -q "$img"
done
docker volume create nuget-cache >/dev/null
docker volume create gradle-cache >/dev/null

echo "== Сборщик Android"
docker build -q -t factory/android-builder:1 factory/docker/android-builder

echo "== Прогрев кэшей на эталонной модели (NuGet, npm, Gradle)"
(cd factory && uv run python - <<'PY'
import asyncio, yaml
from pathlib import Path
from factory.ir import AppSpec
from factory.gen import backend, frontend, infra, mobile
from factory import builder
spec = AppSpec.model_validate(yaml.safe_load(open("../cases/case1/golden_spec.yaml")))
out = Path("../out/_warmup")
backend.generate(spec, out, data_dir=Path("../cases/case1/data")); frontend.generate(spec, out); infra.generate(spec, out); mobile.generate(spec, out)
ok, errors, _ = asyncio.run(builder.build_backend(out / "backend", f"{spec.namespace}.slnx"))
print("backend warmup:", ok)
import os
from factory.runtime import low_memory
if os.environ.get("FACTORY_APK", "on") != "off":
    ok, apk, _ = asyncio.run(builder.build_apk(out / "mobile", low_memory=low_memory()))
    print("android warmup:", ok)
PY
)
(cd out/_warmup && COMPOSE_PARALLEL_LIMIT=1 docker compose build -q backend && COMPOSE_PARALLEL_LIMIT=1 docker compose build -q frontend)

echo "== Проверка хоста и модели"
(cd factory && uv run factory doctor)

echo "== Запуск веб-интерфейса на :8000 (PUBLIC_HOST — адрес, по которому VM видна жюри)"
PUBLIC_HOST="${PUBLIC_HOST:-$(hostname -I | awk '{print $1}')}"
export PUBLIC_HOST
mkdir -p runs && cd factory && nohup uv run factory serve --port 8000 > ../runs/server.log 2>&1 &
echo "Готово: http://$PUBLIC_HOST:8000"
