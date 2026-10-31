# Развёртывание фабрики на сервере (VM хакатона)

Цель: фабрика работает на VM, ходит в модель организаторов `qwen3.8-27b` по адресу `http://10.10.10.10:4000/v1`,
жюри открывает веб-интерфейс, вставляет кейс 2 и получает развёрнутое приложение.

Время: ~30–60 минут (основное — скачивание образов и прогрев кэшей).

---

## 0. Что нужно заранее

| Что | Требование |
|---|---|
| VM | Linux x86_64, Docker 24+ с плагином compose, пользователь в группе `docker` |
| Ресурсы | от 8 ГБ RAM (лучше 16), от 40 ГБ свободного диска, 4+ CPU |
| Сеть VM | доступ к `10.10.10.10:4000` (модель), GitHub, Docker-реестрам (Docker Hub, mcr.microsoft.com, ghcr.io), api.nuget.org, registry.npmjs.org. Google (dl.google.com) **не нужен** — сборщик Android берётся готовым из ghcr.io |
| Доступ с ноутбука | SSH `team23@85.143.113.230 -p 10023` (VPN на ноутбуке выключить) |
| Ключ модели | выдан организаторами (`sk-...`) |

Подключение (VPN выключен):

```bash
ssh -p 10023 team23@85.143.113.230
```

## 0.1. Мало памяти (4 ГБ)

Замеры на кейсе 1: работающий стенд (PostGIS, Kafka, backend, mocks, nginx) — ~0,7 ГБ; сборка .NET — до ~1 ГБ;
сборка образа web — до ~1 ГБ; запись видео — ~0,4 ГБ; **сборка APK (Gradle) — до 3 ГБ**.

На хосте с ОЗУ < 6 ГБ фабрика сама включает **режим малой памяти**: Kafka ограничена 256 МБ, одновременно работает один
стенд (предыдущие останавливаются), APK собирается после всех проверок с урезанной кучей (`-Xmx1g`, 1 поток).
Принудительно: `FACTORY_LOW_MEMORY=1` (или `0`), пропустить APK: `FACTORY_APK=off`.

Обязательно добавьте swap, если есть sudo:

```bash
sudo fallocate -l 6G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

Без sudo и без swap: попросите организаторов увеличить ОЗУ до 8 ГБ, либо используйте вариант «фабрика на ноутбуке,
модель через VM» (раздел 9) — на Mac 24 ГБ ОЗУ, всё проверено, образы и кэши уже прогреты.

## 1. Проверить VM

```bash
ssh -p 10023 team23@85.143.113.230
docker version && docker compose version          # оба должны ответить
groups | grep -q docker && echo "в группе docker" || echo "нужен sudo usermod -aG docker $USER и перелогин"
curl -s -o /dev/null -w "model:%{http_code}\n" http://10.10.10.10:4000/v1/models -H "Authorization: Bearer <КЛЮЧ>"
curl -s -o /dev/null -w "nuget:%{http_code}\n" https://api.nuget.org/v3/index.json
curl -s -o /dev/null -w "npm:%{http_code}\n" https://registry.npmjs.org/
df -h ~ && free -g && nproc
```

Ожидаемо: `model:200`, `nuget:200`, `npm:200`. Если интернета нет — см. раздел 9.

## 2. Код: через GitHub

На Mac (из `/Users/lenya/rwb`) отправить последние изменения:

```bash
git add -A && git commit -m "Фабрика: обновление" && git push
```

На VM — первый раз:

```bash
git clone https://github.com/lenyagrigorenko/rwb.git ~/rwb
```

и далее при каждом обновлении:

```bash
cd ~/rwb && git pull
```

Если репозиторий приватный — git спросит логин и токен GitHub (Settings → Developer settings → Personal access
tokens, право `repo`). `factory/.env`, `out/`, `runs/` в git не попадают — `.env` создаётся на VM (раздел 3).

## 3. Ключ модели: локальный API вместо OpenRouter

На VM создайте `~/rwb/factory/.env` (ключ подставьте свой):

```bash
cd ~/rwb/factory
cat > .env <<'EOF'
LLM_BASE_URL=http://10.10.10.10:4000/v1
LLM_MODEL=qwen3.8-27b
LLM_API_KEY=ВАШ_КЛЮЧ
LLM_PROVIDER=
LLM_THINKING=auto
LLM_CONCURRENCY=3
LLM_TPM=30000
LLM_TIMEOUT=900
EOF
chmod 600 .env
```

| Переменная | Значение |
|---|---|
| `LLM_BASE_URL` | адрес OpenAI-совместимого API организаторов |
| `LLM_MODEL` | `qwen3.8-27b` (без префикса `qwen/`, он только для OpenRouter) |
| `LLM_PROVIDER` | пусто (используется только с OpenRouter) |
| `LLM_THINKING` | `auto` — выключать «размышления» Qwen3 параметром шаблона; если сервер его не принимает, клиент сам переключится на `soft` (`/no_think` в запросе). Можно задать `soft` сразу |
| `LLM_STRUCTURED` | по умолчанию `auto`; `off` — не просить у сервера JSON-схему (схема в промпте + проверка) |
| `LLM_CONCURRENCY` | число параллельных запросов. У ключа организаторов лимит **3** (ответ 429 «max_parallel_requests») |
| `LLM_TPM` | лимит токенов в минуту на ключ. У ключа организаторов **30000** (ответ 429 «Limit type: tokens»). Клиент сам планирует запросы под лимит (запрос резервирует промпт + max_tokens) и не получает отказов. Если у демонстрационного ключа другие лимиты, клиент узнает их из первого ответа 429 и подстроится; ожидание по 429 не тратит попытки генерации |
| `LLM_RATE_WAIT` | сколько секунд суммарно можно ждать лимитов в одном вызове (по умолчанию 900) |

Вернуть OpenRouter (для отладки на Mac): `LLM_BASE_URL=https://openrouter.ai/api/v1`, `LLM_MODEL=qwen/qwen3.8-27b`,
`LLM_PROVIDER=cerebras/fp16`, ключ OpenRouter.

## 4. Установка и прогрев

```bash
cd ~/rwb
./scripts/vm-setup.sh
```

Скрипт: ставит `uv` и зависимости Python, скачивает образы стека (.NET SDK/ASP.NET, PostGIS, Node, nginx, Kafka,
Playwright, Gitleaks — проверка секретов, PlantUML — рендер диаграмм ТЗ), собирает образ сборщика Android, прогревает
кэши NuGet / npm / Gradle на эталонном проекте, проверяет модель и запускает веб-интерфейс на `:8000`. Повторный запуск
безопасен.

Обновление после `git pull` (без полного прогрева): докачать новые образы и перезапустить интерфейс:

```bash
cd ~/rwb && git pull
docker pull -q zricethezav/gitleaks:v8.21.2 && docker pull -q plantuml/plantuml:1.2024.8
./scripts/factory-server.sh restart
```

## 5. Проверка готовности

```bash
cd ~/rwb/factory && uv run factory doctor
```

Все строки должны быть ✅ (⚠️ «порт 8000 занят» — нормально, если интерфейс уже запущен). Обратите внимание на:

- **режим размышлений выключен** — если reasoning tokens > 0, поставьте `LLM_THINKING=soft` в `.env`;
- **JSON по схеме** — «на стороне сервера: нет» допустимо, просто чуть больше токенов;
- **скорость** — на сервере площадки ожидаем ~60 ток/с; полный прогон кейса ≈ 10–15 минут.

## 6. Запуск веб-интерфейса (постоянно)

`vm-setup.sh` в конце запускает интерфейс сам. Управление (работает без tmux, переживает отключение SSH):

```bash
cd ~/rwb
./scripts/factory-server.sh status     # работает ли
./scripts/factory-server.sh restart    # после git pull
./scripts/factory-server.sh logs       # журнал (Ctrl+C — выйти из просмотра)
./scripts/factory-server.sh stop
```

Открыть интерфейс с ноутбука через SSH-туннель (VPN выключен):

```bash
ssh -p 10023 -L 8000:localhost:8000 team23@85.143.113.230
# открыть http://localhost:8000
```

### 6.1. Один порт на всё: интерфейс, APK и API для телефона

Интерфейс фабрики проксирует API развёрнутого стенда: `http://<адрес>:8000/stand/<проект>/...` → порт API стенда
на VM. Поэтому наружу достаточно **одного порта 8000**: через него открываются интерфейс, скачивание APK по QR-коду
и API мобильного приложения. В APK зашивается адрес `<внешний адрес>/stand/<проект>`, он же показан в интерфейсе
рядом с APK («API для приложения»). На экране входа приложения адрес можно поменять вручную.

Внешний адрес интерфейса:

| Как жюри попадает на VM | Что задать |
|---|---|
| из внутренней сети, `http://10.10.1.23:8000` | ничего (по умолчанию `PUBLIC_HOST` = внутренний IP VM) |
| организаторы пробросили порт 8000 на `85.143.113.230:<порт>` | `PUBLIC_URL=http://85.143.113.230:<порт> ./scripts/factory-server.sh restart` (или строка `PUBLIC_URL=...` в `factory/.env`) |

Адрес зашивается в APK при сборке, поэтому `PUBLIC_URL` задайте **до** прогона кейса.

Веб-интерфейс самого стенда (Angular) идёт по своему порту — его тоже надо пробрасывать (у кейса 1 это `8390`;
порт показан в интерфейсе после «Развернуть»).

**Проверить приложение на своём телефоне через ноутбук** (телефон и ноутбук в одной Wi‑Fi сети; если Wi‑Fi площадки
изолирует устройства — раздайте интернет с телефона и подключите к нему ноутбук):

```bash
ssh -p 10023 -L 0.0.0.0:8000:localhost:8000 -L 8390:localhost:8390 team23@85.143.113.230
```

```bash
ipconfig getifaddr en0
```

В приложении «Адрес сервера»: `http://<IP ноутбука>:8000/stand/<проект>` (для кейса 1 — `.../stand/spb-parking-assistant`).
VPN на телефоне выключите: он перехватывает соединение (ошибка «Read timed out»).

## 7. Прогон кейса

- **Веб**: http://<VM>:8000 → выбрать «Помощник управления платными парковками» или вставить текст кейса 2 →
  приложить файлы данных → «Запустить». Вопросы появятся справа; без ответа через 60 с — ответы по умолчанию.
- **Консоль**: `cd ~/rwb/factory && uv run factory run ../cases/case1`

Результат: `~/rwb/out/<slug>/` (проект), `~/rwb/runs/<id>/` (журнал, токены, демо). Стенд каждого проекта
поднимается на своих портах (web 81x0, API 81x1), ссылки — на странице результата.

**Перед сдачей:** прогнать кейс 1 один раз на ключе организаторов — это даст честные метрики в
`docs/pipeline-report.md`.

## 8. GitLab

1. В локальном GitLab создайте два проекта: `factory` (сам конвейер) и `spb-parking-assistant` (результат кейса 1).
2. Фабрика (репозиторий уже есть, добавить второй remote):
   ```bash
   cd ~/rwb && git remote add gitlab <URL проекта factory> && git push gitlab main
   ```
3. Результат кейса 1:
   ```bash
   cd ~/rwb/out/spb-parking-assistant && git init -b main && git add . && git commit -m "Сгенерировано фабрикой"
   git remote add origin <URL проекта> && git push -u origin main
   ```
4. CI сгенерированного проекта (`.gitlab-ci.yml`) требует GitLab Runner с docker executor; для интеграционных
   тестов и сборки образов — `privileged = true` (docker-in-docker). Без runner'а артефакты всё равно есть в
   репозитории: отчёты в `docs/`, видео `docs/demo.webm`.

## 9. Если на VM нет интернета или Docker

**Запасной вариант: фабрика на ноутбуке, модель — через VM.**

```bash
ssh -p 10023 -N -L 4000:10.10.10.10:4000 team23@85.143.113.230      # держать открытым
# в factory/.env на Mac:
# LLM_BASE_URL=http://localhost:4000/v1   LLM_MODEL=qwen3.8-27b   LLM_API_KEY=<ключ>   LLM_PROVIDER=
cd /Users/lenya/rwb/factory && uv run factory doctor && uv run factory serve
```

Жюри открывает `http://<IP ноутбука в сети площадки>:8000`.

Без интернета на VM, но с Docker: заранее на ноутбуке `docker save` нужных образов → `scp` → `docker load` на VM;
кэши NuGet/npm/Gradle перенести томами (`docker run --rm -v nuget-cache:/v -v $PWD:/b alpine tar czf /b/nuget.tgz -C /v .`).

## 9.1. Android без доступа к Google

С VM нет доступа к `dl.google.com`, поэтому сборщик Android готовится в **GitHub Actions** и публикуется в ghcr.io:
образ с Android SDK, Gradle и **уже скачанными зависимостями** мобильного шаблона. VM его скачивает, и APK собирается
офлайн. Зависимости у всех генерируемых приложений одинаковые (меняется только код экранов), поэтому кэша достаточно.

1. Отправить код на GitHub (`git push`) — workflow `.github/workflows/android-builder.yml` запустится сам
   (или вручную: GitHub → Actions → android-builder → Run workflow). Длится ~10 минут.
2. Сделать образ доступным VM: GitHub → профиль → Packages → `factory-android-builder` → Package settings →
   Change visibility → **Public**. (Либо на VM: `docker login ghcr.io -u lenyagrigorenko` с токеном, право `read:packages`.)
3. На VM: `./scripts/vm-setup.sh` — скрипт сам скачает `ghcr.io/lenyagrigorenko/factory-android-builder:1`,
   переименует в `factory/android-builder:1` и включит `FACTORY_APK=on`, `FACTORY_GRADLE_OFFLINE=1`.

Проверка вручную:

```bash
docker pull ghcr.io/lenyagrigorenko/factory-android-builder:1
docker tag ghcr.io/lenyagrigorenko/factory-android-builder:1 factory/android-builder:1
```

Запасной путь (если ghcr.io с VM недоступен): на Mac с выключенным VPN
`docker save factory/android-builder:warm-test | gzip > android.tgz && scp -P 10023 android.tgz team23@85.143.113.230:~/`,
на VM `gunzip -c ~/android.tgz | docker load && docker tag factory/android-builder:warm-test factory/android-builder:1`,
в `factory/.env`: `FACTORY_APK=on`, `FACTORY_GRADLE_OFFLINE=1`.

## 9.2. Запуск через sudo

`vm-setup.sh` можно запускать через `sudo`: он сам добавит пользователя в группу `docker`, поставит ему `uv`,
вернёт владельца файлов и запустит интерфейс от его имени. Чтобы `docker` работал без sudo в ваших сессиях — один раз:

```bash
sudo usermod -aG docker team23     # затем выйти из SSH и зайти снова
sudo chown -R team23:team23 ~/rwb  # если уже запускали через sudo
docker ps                          # должно работать без sudo
```

## 10. Частые проблемы

| Симптом | Причина и решение |
|---|---|
| SSH висит на приветствии | на Mac включён VPN — выключить |
| `doctor`: модель недоступна | неверный ключ/адрес в `.env`; проверить `curl .../v1/models` с ключом |
| reasoning tokens > 0, медленно | `LLM_THINKING=soft` |
| 429 / таймауты модели | уменьшить `LLM_CONCURRENCY` до 2, сообщить организаторам |
| сборка .NET: restore error | нет доступа к api.nuget.org; прогреть кэш (раздел 9) |
| стенд не поднимается: порт занят | `docker ps`, остановить старые стенды: `cd ~/rwb/out/<slug> && docker compose down` |
| `vm-setup.sh`: exit code 28 на «Сборщик Android» | таймаут к dl.google.com — готовый образ из ghcr.io (раздел 9.1) |
| В приложении «Read timed out» / нет связи | телефон не видит адрес из APK: задайте на экране входа `http://<доступный адрес>:8000/stand/<проект>` (раздел 6.1), выключите VPN на телефоне |
| APK не собирается | нет образа `factory/android-builder:1` — раздел 9.1; при 4 ГБ без swap — `FACTORY_APK=off` |
| OOM / контейнеры падают с кодом 137 | мало памяти: добавить swap (раздел 0.1), остановить лишние стенды `docker ps` |
| нет видео демо | нет образа Playwright — `docker pull mcr.microsoft.com/playwright:v1.64.1-noble` |
| интерфейс не открывается снаружи | порт 8000 закрыт — SSH-туннель (раздел 6) или попросить организаторов открыть порт |
