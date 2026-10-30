#!/usr/bin/env bash
# Развёртывание фабрики на VM: зависимости, образы, прогрев кэшей, запуск веб-интерфейса.
# Использование (на VM, из корня репозитория): ./scripts/vm-setup.sh
# Лучше запускать БЕЗ sudo, от пользователя в группе docker (sudo usermod -aG docker $USER, затем перелогиниться).
# Ключ модели: factory/.env (LLM_BASE_URL=http://10.10.10.10:4000/v1, LLM_MODEL=qwen3.8-27b, LLM_API_KEY=..., LLM_PROVIDER=)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
test -f factory/.env || { echo "Нет factory/.env — создайте его (DEPLOY.md, раздел 3)"; exit 1; }

# При запуске через sudo в конце вернём владельца файлов обычному пользователю.
OWNER="${SUDO_USER:-}"
fix_owner() { [ -n "$OWNER" ] && chown -R "$OWNER":"$OWNER" "$ROOT" 2>/dev/null || true; }
trap fix_owner EXIT
[ -n "$OWNER" ] && echo "ВНИМАНИЕ: скрипт запущен через sudo; файлы будут возвращены пользователю $OWNER"

set_env() {  # set_env KEY VALUE — записать/заменить переменную в factory/.env
  grep -q "^$1=" factory/.env && sed -i "s|^$1=.*|$1=$2|" factory/.env || echo "$1=$2" >> factory/.env
}

echo "== Docker"
docker version --format '{{.Server.Version}}' || { echo "Docker недоступен"; exit 1; }
docker compose version

echo "== Сеть (таймаут 15 с на адрес)"
for url in https://api.nuget.org/v3/index.json https://registry.npmjs.org/ https://repo.maven.apache.org/maven2/ \
           https://dl.google.com/android/repository/repository2-3.xml https://services.gradle.org/distributions/ \
           https://plugins.gradle.org/m2/; do
  printf "  %-60s " "$url"; curl -s -m 15 -o /dev/null -w "%{http_code} %{time_total}s\n" "$url" || echo "недоступен"
done

echo "== uv / Python"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
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

echo "== Сборщик Android (необязательно)"
if docker image inspect factory/android-builder:1 >/dev/null 2>&1; then
  echo "  образ factory/android-builder:1 уже есть"
elif docker build -q -t factory/android-builder:1 factory/docker/android-builder; then
  echo "  образ собран"
else
  echo "  НЕ СОБРАН: нет доступа к dl.google.com / services.gradle.org. APK отключён (FACTORY_APK=off)."
  echo "  Можно перенести готовый образ с ноутбука — DEPLOY.md, раздел «Android без доступа к Google»."
  set_env FACTORY_APK off
fi

echo "== Прогрев кэшей на эталонной модели (NuGet, npm, Gradle)"
(cd factory && uv run python - <<'PY'
import asyncio, os, yaml
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(".env")
from factory.ir import AppSpec
from factory.gen import backend, frontend, infra, mobile
from factory import builder
from factory.runtime import low_memory
spec = AppSpec.model_validate(yaml.safe_load(open("../cases/case1/golden_spec.yaml")))
out = Path("../out/_warmup")
backend.generate(spec, out, data_dir=Path("../cases/case1/data")); frontend.generate(spec, out); infra.generate(spec, out); mobile.generate(spec, out)
ok, errors, log = asyncio.run(builder.build_backend(out / "backend", f"{spec.namespace}.slnx"))
print("backend warmup:", ok, "" if ok else log[-1500:])
if os.environ.get("FACTORY_APK", "on") != "off":
    ok, apk, log = asyncio.run(builder.build_apk(out / "mobile", low_memory=low_memory()))
    print("android warmup:", ok, "" if ok else log[-1500:])
PY
) || echo "  прогрев завершился с ошибкой — см. вывод выше"
(cd out/_warmup && COMPOSE_PARALLEL_LIMIT=1 docker compose build -q backend && COMPOSE_PARALLEL_LIMIT=1 docker compose build -q frontend) \
  || echo "  сборка образов стенда не удалась — см. вывод выше"

echo "== Проверка хоста и модели"
(cd factory && uv run factory doctor) || true

echo "== Запуск веб-интерфейса на :8000 (PUBLIC_HOST — адрес, по которому VM видна жюри)"
PUBLIC_HOST="${PUBLIC_HOST:-$(hostname -I | awk '{print $1}')}"
export PUBLIC_HOST
mkdir -p runs
if [ -n "$OWNER" ]; then
  fix_owner
  echo "  интерфейс запустите от $OWNER (не через sudo): cd ~/rwb/factory && PUBLIC_HOST=$PUBLIC_HOST uv run factory serve --port 8000"
else
  (cd factory && nohup uv run factory serve --port 8000 > ../runs/server.log 2>&1 &)
  echo "Готово: http://$PUBLIC_HOST:8000"
fi
