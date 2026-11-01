#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PORT="${1:-8080}"
python3 server.py --host 0.0.0.0 --port "$PORT"
