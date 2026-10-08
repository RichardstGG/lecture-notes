#!/bin/sh
# Finder 雙擊就能跑（.command 是 macOS 的慣例）。邏輯全部在 setup.py。
cd "$(dirname "$0")" || exit 1
echo "先檢查依賴。有 NVIDIA 時會詢問要不要使用 CUDA。"
python3 setup.py "$@"
status=$?
echo
echo "按 Enter 關閉這個視窗。"
read -r _
exit $status
