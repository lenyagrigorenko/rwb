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
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; }
# Процессы интерфейса этого пользователя на этом порту (uv и python), в том числе запущенные прежними версиями скрипта.
kill_servers() { pkill "-$1" -u "$(id -u)" -f "factory serve --port $PORT" 2>/dev/null || true; }

start() {
  if running; then echo "уже запущен (pid $(cat "$PID_FILE")), http://${PUBLIC_HOST:-localhost}:$PORT"; return; fi
  [ -x "$UV" ] || { echo "нет uv: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
  if port_busy; then
    echo "порт $PORT занят — остановите прежний процесс: $0 stop"; (ss -ltnp 2>/dev/null || true) | grep ":$PORT " || true; exit 1
  fi
  local host="${PUBLIC_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
  # PUBLIC_URL — внешний адрес интерфейса, если порт 8000 проброшен наружу под другим номером (можно и в factory/.env).
  local public_url=""; [ -n "${PUBLIC_URL:-}" ] && public_url="PUBLIC_URL='$PUBLIC_URL'"
  # setsid: своя группа процессов, чтобы stop останавливал и uv, и python под ним.
  local detach=""; command -v setsid >/dev/null && detach="setsid"
  local from=1; [ -f "$LOG" ] && from=$(( $(wc -l < "$LOG") + 1 ))
  # В фоне — одна простая команда (не список с cd), иначе $! — промежуточная оболочка, а не uv.
  local cmd="cd '$ROOT/factory' || exit 1; PUBLIC_HOST='$host' $public_url PORT='$PORT' nohup $detach '$UV' run factory serve --port '$PORT' >> '$LOG' 2>&1 & echo \$! > '$PID_FILE'"
  if docker info >/dev/null 2>&1; then
    bash -c "$cmd"
  else
    echo "нет прав на Docker в этой сессии — запуск с группой docker (sudo). Чтобы не нужно было: перелогиньтесь по SSH"
    sudo -u "$USER" -g docker -H bash -c "$cmd"
  fi
  for _ in $(seq 1 60); do running || break; port_busy && break; sleep 0.5; done
  if running && port_busy; then
    echo "запущен: http://$host:$PORT (лог: $LOG)"
  else
    echo "не запустился, см. $LOG"; tail -n "+$from" "$LOG" | tail -20; exit 1
  fi
}

stop() {
  local pid="" was=""
  [ -f "$PID_FILE" ] && pid="$(cat "$PID_FILE")"
  { running || port_busy; } && was=1
  for sig in TERM KILL; do
    [ -n "$pid" ] && { kill "-$sig" -- "-$pid" 2>/dev/null || kill "-$sig" "$pid" 2>/dev/null || true; }
    kill_servers "$sig"
    for _ in $(seq 1 20); do port_busy || break; sleep 0.5; done
    port_busy || break
  done
  rm -f "$PID_FILE"
  if port_busy; then
    echo "порт $PORT занят другим процессом:"; (ss -ltnp 2>/dev/null || true) | grep ":$PORT " || true
    echo "  если это старый интерфейс, запущенный от root: sudo pkill -f 'factory serve'"; return 1
  fi
  [ -n "$was" ] && echo "остановлен" || echo "не запущен"
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) running && echo "работает (pid $(cat "$PID_FILE")), порт $PORT" || echo "не запущен" ;;
  logs) tail -f "$LOG" ;;
  *) echo "использование: $0 start|stop|restart|status|logs"; exit 1 ;;
esac
