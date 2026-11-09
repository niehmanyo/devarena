#!/usr/bin/env bash
# Usage: bash start-background.sh [start|stop|restart|status|logs] [port]
set -euo pipefail
cd "$(dirname "$0")"
ACTION="${1:-start}"
PORT="${2:-8080}"
PIDFILE="devarena.pid"
LOGFILE="devarena.log"

read_pid() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid
  pid="$(cat "$PIDFILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o args= 2>/dev/null | grep -q '[s]erver.py' || return 1
  echo "$pid"
}
start_service() {
  if pid="$(read_pid)"; then
    echo "DevArena 已运行 (PID $pid)"
    return
  fi
  rm -f "$PIDFILE"
  nohup python3 -u server.py --host 0.0.0.0 --port "$PORT" >>"$LOGFILE" 2>&1 </dev/null &
  pid=$!
  echo "$pid" >"$PIDFILE"
  sleep 1
  if ! read_pid >/dev/null; then
    rm -f "$PIDFILE"
    echo "启动失败，请查看 $LOGFILE" >&2
    exit 1
  fi
  echo "DevArena 后台启动成功 (PID $pid, 端口 $PORT)"
  echo "日志：$PWD/$LOGFILE"
}
stop_service() {
  if pid="$(read_pid)"; then
    kill "$pid"
    echo "已发送停止信号 (PID $pid)"
  else
    echo "服务未运行或 PID 文件已失效"
  fi
  rm -f "$PIDFILE"
}
case "$ACTION" in
  start) start_service ;;
  stop) stop_service ;;
  restart) stop_service; start_service ;;
  status) if pid="$(read_pid)"; then echo "运行中 (PID $pid)"; else echo "未运行"; exit 1; fi ;;
  logs) touch "$LOGFILE"; tail -n 100 -f "$LOGFILE" ;;
  *) echo "用法: bash start-background.sh [start|stop|restart|status|logs] [port]" >&2; exit 2 ;;
esac
