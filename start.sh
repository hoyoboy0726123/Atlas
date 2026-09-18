#!/usr/bin/env bash
# Atlas 啟動腳本(macOS / Linux)。桌面自動化節點(UIA)只支援 Windows。
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"

echo "啟動 Atlas..."

# ── 後端 ─────────────────────────────────────────────────────────
cd "$BACKEND"
if [ ! -d ".venv" ]; then
  if command -v uv &> /dev/null; then
    echo "  用 uv 安裝後端依賴..."
    uv sync
  else
    echo "  建立虛擬環境並用 pip 安裝後端依賴..."
    python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
  fi
fi
[ -f .env ] || cp .env.example .env

# 後端預設只綁 127.0.0.1。API 沒有認證、而且會執行 AI 產生的程式碼,
# 要開放給區網(自負風險)再: export ATLAS_HOST=0.0.0.0
.venv/bin/uvicorn main:app --host "${ATLAS_HOST:-127.0.0.1}" --port 8014 &
BACKEND_PID=$!

# ── 前端 ─────────────────────────────────────────────────────────
cd "$FRONTEND"
if [ ! -d "node_modules" ]; then
  echo "  安裝前端依賴(第一次需要幾分鐘)..."
  npm install --silent
fi
BACKEND_PORT=8014 NEXT_PUBLIC_BACKEND_PORT=8014 npx next dev --port 3012 &
FRONTEND_PID=$!

echo ""
echo "Atlas 已啟動"
echo "   介面：http://localhost:3012"
echo "   後端：http://localhost:8014"
echo "按 Ctrl+C 停止"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" INT TERM
wait
