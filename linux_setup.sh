#!/bin/sh
# 只是個入口：實際邏輯全部在 setup.py（三個平台共用一份，不要在這裡加判斷）。
cd "$(dirname "$0")" || exit 1
exec python3 setup.py "$@"
