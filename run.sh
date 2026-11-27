#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "未找到 uv。请先安装 uv，再运行 uv sync --locked。" >&2
  exit 1
fi

exec uv run --locked python "$ROOT/feishu_bot.py" "$@"
