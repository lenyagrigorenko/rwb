#!/usr/bin/env bash
# Управление веб-интерфейсом фабрики без tmux: start | stop | restart | status | logs
# Работает от обычного пользователя; если нет прав на Docker в текущей сессии — запускает с группой docker через sudo.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PORT="${PORT:-8000}"
PID_FILE="$ROOT/runs/server.pid"
LOG="$ROOT/runs/server.log"
UV="$HOME/.local/bin/uv"
command -v uv >/dev/null && UV="$(command -v uv)"
mkdir -p "$ROOT/runs"

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

start() {
  if running; then echo "уже запущен (pid $(cat "$PID_FILE")), http://${PUBLIC_HOST:-localhost}:$PORT"; return; fi
  [ -x "$UV" ] || { echo "нет uv: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
  local host="${PUBLIC_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
  local cmd="cd '$ROOT/factory' && PUBLIC_HOST='$host' PORT='$PORT' nohup '$UV' run factory serve --port '$PORT' >> '$LOG' 2>&1 & echo \$! > '$PID_FILE'"
  if docker info >/dev/null 2>&1; then
    bash -c "$cmd"
  else
    echo "нет прав на Docker в этой сессии — запуск с группой docker (sudo)"
    sudo -u "$USER" -g docker -H bash -c "$cmd"
  fi
  sleep 3
  if running; then echo "запущен: http://$host:$PORT (лог: $LOG)"; else echo "не запустился, см. $LOG"; tail -20 "$LOG"; exit 1; fi
}

stop() {
  if running; then kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE" && echo "остановлен"; else echo "не запущен"; fi
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop || true; start ;;
  status) running && echo "работает (pid $(cat "$PID_FILE")), порт $PORT" || echo "не запущен" ;;
  logs) tail -f "$LOG" ;;
  *) echo "использование: $0 start|stop|restart|status|logs"; exit 1 ;;
esac
