#!/usr/bin/env bash
# DevArena in-place, data-preserving upgrade from a separate new-code directory.
# Usage: bash upgrade.sh /opt/devarena [port]
set -Eeuo pipefail
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET="${1:-}"
PORT="${2:-8080}"
if [[ -z "$TARGET" ]]; then
  echo "用法: bash upgrade.sh /path/to/current/devarena [port]" >&2
  exit 2
fi
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT>=1 && PORT<=65535 )) || { echo "端口不合法" >&2; exit 2; }
[[ -d "$TARGET" ]] || { echo "原项目目录不存在: $TARGET" >&2; exit 2; }
TARGET="$(cd "$TARGET" && pwd -P)"
[[ "$SOURCE" != "$TARGET" ]] || { echo "请从另一份新版源码运行升级脚本；不要直接对正在运行的目录进行 git pull 覆盖。" >&2; exit 2; }
for file in server.py index.html start.sh start-background.sh; do
  [[ -f "$SOURCE/$file" ]] || { echo "新版缺少 $file" >&2; exit 2; }
done
[[ -f "$TARGET/server.py" ]] || { echo "旧项目目录中缺少 server.py" >&2; exit 2; }

# Validate persisted state BEFORE service restart or modifications.
if [[ -f "$TARGET/data/state.json" ]]; then
  python3 - "$TARGET/data/state.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f: state=json.load(f)
if not isinstance(state, dict) or any(not isinstance(state.get(k, []),list) for k in ('exams','submissions','files','attempts')):
    raise SystemExit('state.json 结构异常，拒绝升级')
print('旧 JSON 数据检查通过: 考试 %s 场、答卷 %s 份、附件 %s 个' % (
    len(state.get('exams',[])),len(state.get('submissions',[])),len(state.get('files',[]))))
PY
fi

BACKUP_PARENT="${TARGET}-backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$BACKUP_PARENT/$STAMP"
mkdir -p "$BACKUP/code"
echo "备份位置: $BACKUP"

# Stop a service managed by start-background.sh. Do not kill unknown services.
WAS_RUNNING=0
if [[ -f "$TARGET/devarena.pid" ]] && [[ -f "$TARGET/start-background.sh" ]]; then
  if (cd "$TARGET" && bash start-background.sh status >/dev/null 2>&1); then
    WAS_RUNNING=1
    (cd "$TARGET" && bash start-background.sh stop)
    sleep 2
  fi
fi

# Save old program and settings; copy uploaded data with hardlinks where supported.
for f in server.py index.html start.sh start-background.sh README.md app_config.json oauth_config.json oauth_config.example.json; do
  if [[ -f "$TARGET/$f" ]]; then cp -p "$TARGET/$f" "$BACKUP/code/$f"; fi
done
if [[ -d "$TARGET/data" ]]; then
  # Uploaded files are immutable; hardlinked snapshot avoids duplicating multi-GB core dumps.
  cp -al "$TARGET/data" "$BACKUP/data" || {
    echo "硬链接备份失败，将改用完整拷贝（大型文件可能需要额外磁盘空间）" >&2
    rm -rf "$BACKUP/data"
    cp -a "$TARGET/data" "$BACKUP/data"
  }
fi

rollback() {
  echo "升级检查或启动失败，正在恢复旧程序..." >&2
  for f in "$BACKUP"/code/*; do
    [[ -f "$f" ]] || continue
    cp -p "$f" "$TARGET/$(basename "$f")"
  done
  if (( WAS_RUNNING )); then
    (cd "$TARGET" && bash start-background.sh start "$PORT") || true
  fi
  echo "数据未删除；备份保留于: $BACKUP" >&2
}
trap rollback ERR

# Replace code only; never overwrite data/, oauth_config.json or an existing app_config.json.
for f in server.py index.html start.sh start-background.sh; do
  cp -p "$SOURCE/$f" "$TARGET/$f"
done
for f in README.md oauth_config.example.json; do
  if [[ -f "$SOURCE/$f" ]]; then cp -p "$SOURCE/$f" "$TARGET/$f"; fi
done
if [[ ! -e "$TARGET/app_config.json" && -f "$SOURCE/app_config.json" ]]; then
  cp -p "$SOURCE/app_config.json" "$TARGET/app_config.json"
fi

python3 -m py_compile "$TARGET/server.py"
if [[ -f "$TARGET/data/state.json" ]]; then
  python3 -m json.tool "$TARGET/data/state.json" >/dev/null
fi

(cd "$TARGET" && bash start-background.sh start "$PORT")
trap - ERR
echo "升级成功！访问地址: http://服务器IP:$PORT"
echo "旧数据、现有 app_config.json 和 oauth_config.json 已保留"
echo "备份位置: $BACKUP"
echo "提示: 对 nginx 反向代理的上传体积和超时限制需另行配置。"
