#!/usr/bin/env bash
# Стресс-прогон конвейера на наборе кейсов: ./scripts/stress.sh [параллельность] [каталог кейсов...]
# Итоги — в runs/stress-<время>/summary.txt (по одному кейсу в строке).
set -uo pipefail
cd "$(dirname "$0")/.."
PAR="${1:-2}"; shift || true
export FACTORY_APK="${FACTORY_APK:-off}"
CASES=("$@"); [ ${#CASES[@]} -eq 0 ] && CASES=(cases/stress/*/)
DIR="runs/stress-$(date +%m%d-%H%M)"; mkdir -p "$DIR"
run_one() {
  local case="$1" dir="$2" name; name="$(basename "$case")"
  (cd factory && uv run factory run "../$case" > "../$dir/$name.log" 2>&1)
  local code=$?
  # Стенд после проверки не нужен: освобождаем память Docker для следующих кейсов.
  local project; project="$(grep -o '"project": "[^"]*"' "$dir/$name.log" | tail -1 | cut -d'"' -f4)"
  [ -n "$project" ] && [ -f "$project/docker-compose.yml" ] && (cd "$project" && docker compose down -v --remove-orphans >/dev/null 2>&1)
  python3 - "$dir/$name.log" "$name" "$code" >> "$dir/summary.txt" <<'PY'
import json, re, sys
log, name, code = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(log, encoding="utf-8", errors="ignore").read()
m = re.search(r'\{\s*"status".*\}\s*$', text, re.S)
if m:
    r = json.loads(m.group(0)); mt = r.get("metrics", {})
    print(f"{name}: {r['status']} calls={mt.get('calls')} tokens={mt.get('prompt_tokens')}+{mt.get('completion_tokens')} "
          f"wall={mt.get('wall_seconds')}s project={r.get('project')}")
else:
    print(f"{name}: CRASH exit={code} :: {text.strip().splitlines()[-1][:200] if text.strip() else ''}")
PY
}
export -f run_one
printf '%s\n' "${CASES[@]}" | xargs -P "$PAR" -I{} bash -c 'run_one "$@"' _ {} "$DIR"
cat "$DIR/summary.txt"
