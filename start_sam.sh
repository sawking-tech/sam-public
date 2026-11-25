#!/usr/bin/env bash
# SAM на Linux и macOS: базы в Docker, агрегатор MCP, шлюз. Вывод - logs/<компонент>.log, повторный запуск перезапускает.
#   ./start_sam.sh         поднять
#   ./start_sam.sh --stop  остановить
# PYTHON - интерпретатор, GATEWAY_HOST, GATEWAY_PORT и MCP_PORT - адрес и порты. Голос запускается отдельно (README).
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
GATEWAY_PORT="${GATEWAY_PORT:-8000}"
GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"   # 0.0.0.0 - доступ из сети, тогда задай SAM_API_KEY
MCP_PORT="${MCP_PORT:-3000}"
COMPOSE_FILE="docker/docker-compose.dev.yml"
LOGS="logs"
STOP_TRIES=10     # старый процесс освобождает порт: без ожидания проверка здоровья ответила бы от него
HEALTH_TRIES=30   # шлюз загружает модель эмбеддингов: первый старт идёт до 30 с

stop_component() {
  local pidfile="$LOGS/$1.pid"
  [ -f "$pidfile" ] || return 0
  local pid
  pid="$(cat "$pidfile")"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq "$STOP_TRIES"); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$pidfile"
}

start_component() {
  local name="$1"; shift
  stop_component "$name"
  nohup "$@" >"$LOGS/$name.log" 2>&1 &
  echo $! >"$LOGS/$name.pid"
}

mkdir -p "$LOGS"
if [ "${1:-}" = "--stop" ]; then
  stop_component gateway
  stop_component mcp
  docker compose -f "$COMPOSE_FILE" stop
  exit 0
fi

[ -f .env ] || { echo "Нет .env: выполни cp .env.example .env и впиши ключ поставщика LLM"; exit 1; }
docker compose -f "$COMPOSE_FILE" up -d
start_component mcp "$PYTHON" mcp/mcp_aggregator.py --port "$MCP_PORT"
start_component gateway "$PYTHON" -m uvicorn gateway.main:app --host "$GATEWAY_HOST" --port "$GATEWAY_PORT"

for _ in $(seq "$HEALTH_TRIES"); do
  if curl -fs "http://127.0.0.1:$GATEWAY_PORT/health" >/dev/null 2>&1; then
    echo "SAM запущена: http://127.0.0.1:$GATEWAY_PORT/monitor"
    exit 0
  fi
  sleep 1
done
echo "Шлюз не ответил за $HEALTH_TRIES с: смотри $LOGS/gateway.log"
exit 1
