#!/bin/bash
# Genie 影片處理面板 — 雙擊啟動(從 repo checkout 執行,git pull 即更新)
PY="$HOME/proj_genie/.venv/bin/python3"
if curl -s -m 1 http://127.0.0.1:5250/api/state > /dev/null 2>&1; then
  open "http://127.0.0.1:5250"   # 已在跑,直接開頁面
  exit 0
fi
exec "$PY" "$HOME/proj_genie/genie-panel/dashboard.py"
