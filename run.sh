#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"

pick_python() {
  local candidate
  for candidate in "$ROOT/.venv/bin/python" \
    "/home/dev/.openclaw/workspace/skills/FastInsight/venv/bin/python" \
    "python3"; do
    if [[ -x "$candidate" ]] && "$candidate" -c "import lark_oapi" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
    if [[ "$candidate" == "python3" ]] && python3 -c "import lark_oapi" >/dev/null 2>&1; then
      echo "python3"
      return 0
    fi
  done
  return 1
}

PY="$(pick_python || true)"
if [[ -z "${PY}" ]]; then
  echo "未找到已安装 lark-oapi 的 Python。请先执行：" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

echo "Using $PY" >&2
echo "注意：同一飞书应用通常只能有一条长连接。启动前请停掉 OpenClaw 飞书通道和旧的 feishu_bridge.py。" >&2
exec "$PY" "$ROOT/feishu_bot.py" "$@"
