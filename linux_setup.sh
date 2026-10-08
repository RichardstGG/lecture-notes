#!/bin/sh
# 只是個入口：實際邏輯全部在 setup.py（三個平台共用一份，不要在這裡加判斷）。
cd "$(dirname "$0")" || exit 1
echo "先檢查依賴。有顯卡時會詢問：使用本機 GPU（NVIDIA 可選 CUDA）或只用外部摘要 API。"
exec python3 setup.py "$@"
