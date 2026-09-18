"""測試用：確保可以 import core.* 與 setup_engines（repo 根目錄不一定在 sys.path 上）。

用法：在每個測試檔最上面 `from . import _pathfix  # noqa: F401`。
只依賴標準函式庫，不需要安裝 pytest（可用 `python3 -m unittest discover -s tests` 執行）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
