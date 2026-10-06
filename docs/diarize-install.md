# 會議發言者辨識：安裝、授權與驗證狀態

會議工作台的「會後發言者辨識」（`core/diarize.py`）需要一個第三方套件與兩個模型。
**課堂功能完全不需要它們**；沒裝時只有會議辨識會得到 `engine_unavailable`，其餘不受影響。

## 一行安裝

```bash
python3 upgrade.py
```

`upgrade.py` 預設會處理這一項（`--skip-diarization` 可略過）。實際做的事：

| 步驟 | 內容 | 大小 |
|---|---|---|
| 1 | 確保專案 `.venv` 存在 | — |
| 2 | `.venv/bin/python -m pip install -r requirements-diarize.txt` | 約 15 MB（`sherpa-onnx` 4.4 MB ＋ `sherpa-onnx-core` 10.6 MB） |
| 3 | `python3 setup_engines.py diarize` 取得兩個模型到 `models/` | 約 35 MB |

只要模型、不碰 pip：`python3 setup_engines.py diarize`。

**為什麼放 `.venv` 而不是系統 Python：** `lec` 只用標準函式庫，而 Debian 13 等發行版的 PEP 668
不讓人 `pip install` 進系統 Python。分群在子行程（`core/diarize_worker.py`）跑，由 `core/diarize.py`
依序找有裝 sherpa 的直譯器：呼叫端指定 → 環境變數 `LEC_DIARIZE_PYTHON` → 目前直譯器 → 專案 `.venv`。

## 模型與授權

| 模型 | 檔案 | 授權 | 來源 |
|---|---|---|---|
| speaker segmentation | `models/sherpa-onnx-pyannote-segmentation-3-0.onnx`（約 6 MB） | **MIT**（CNRS 2022；壓縮檔內附 LICENSE，安裝時存成 `models/sherpa-onnx-pyannote-segmentation-3-0.LICENSE.txt`） | sherpa-onnx 由 [`pyannote/segmentation-3.0`](https://huggingface.co/pyannote/segmentation-3.0) 轉換成 ONNX |
| speaker embedding | `models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx`（約 28 MB） | **Apache-2.0**（依 3D-Speaker 專案；模型檔本身不附授權文字，這一點是依官方專案頁的說明，沒有逐檔查證） | [3D-Speaker](https://github.com/modelscope/3D-Speaker) 的中英 code-switch 模型，經 sherpa-onnx 發佈 |
| `sherpa-onnx` 套件 | `pip install sherpa-onnx==1.13.8` | **Apache-2.0** | [PyPI](https://pypi.org/project/sherpa-onnx/) |

兩個模型都從 `https://github.com/k2-fsa/sherpa-onnx/releases/download/…` 下載，**不需要註冊、不需要 token、
不需要接受任何條款**（對照：`pyannote.audio` 的 community-1 要在 Hugging Face 填聯絡資料取得 token）。

## 下載一律驗證 SHA-256

GitHub release 的資產 tag 是**可變的**，下載到什麼不能直接信任，所以 `setup_engines.py` 把雜湊寫死：

- 壓縮檔先驗，過了才解；解出的 `model.onnx` 再驗一次，過了才原子改名成最終檔名。
- 任一步驗證失敗：刪掉下載物、整個流程失敗，**不留半成品**，也不會用來路不明的檔案。
- 只從壓縮檔取**指定的那一個成員**（完整名稱精確比對），不解其他檔案，也不信任成員路徑。
- 目標檔已存在但雜湊不符：**拒絕覆蓋**（可能是你自己換的模型），要你自己決定。
- 已存在且雜湊相符：直接略過，所以重複執行 `upgrade.py` 不會重下載。

| 檔案 | SHA-256 |
|---|---|
| segmentation 壓縮檔 | `24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488` |
| segmentation 模型 | `220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079` |
| embedding 模型 | `aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2` |

## 離線與隱私

- **只有安裝時需要網路**（pip 與兩個模型）。之後會議辨識完全在本機執行。
- **已實測**：把真的 sherpa-onnx 辨識流程放進**沒有任何網路介面**的 Linux network namespace
  （`unshare -rn`，並先確認在裡面連外會 `Network is unreachable`），仍能完整跑完。所以它**不依賴**網路。
- **沒有驗證**：它**是否曾嘗試連線**。這台機器沒有 `strace`，沒辦法觀察 socket。文件與程式碼都沒有看到
  遙測，但「沒看到」不等於「沒有」。若這點對你很重要，請在可以抓封包的環境自行確認。

## 驗證狀態

| 項目 | 層級 | 結果 |
|---|---|---|
| 模型下載驗證、拒絕覆蓋、損毀／被換檔案、路徑穿越、不留半成品 | Automated test（mock） | 通過，`tests/test_diarize_setup.py` |
| `upgrade.py` 的步驟順序、venv 建立、`--skip-diarization` | Automated test（mock） | 通過 |
| 真的建 `.venv`、`pip install`、下載兩個模型、第二次略過 | Manual test（Linux） | 通過 |
| 安裝出來的 `.venv` 與 `models/` 被引擎靠預設探索找到並跑完真 sherpa 冒煙測試（不設任何環境變數） | Manual test（Linux） | 通過 |
| 在無網路介面的環境執行 | Manual test（Linux） | 通過 |
| macOS、Windows | **Not tested** | 依 PyPI 頁面所列有這兩個平台的 `sherpa-onnx` wheel（macOS arm64／x86-64、Windows x64／arm64），但**沒有在真機裝過或跑過**。`.venv` 路徑與 `python.exe` 位置的邏輯有單元測試，其餘未驗證。維持「實驗中」。 |
| 辨識準確度 | **Not tested** | 唯一可用的真實會議錄音有 20.2% 音訊遺失，不能當依據。見 `docs/meeting-workbench-implementation.md`。 |

## 疑難排解

| 現象 | 原因與處理 |
|---|---|
| `engine_unavailable：找不到安裝了 sherpa-onnx 的 Python` | 跑 `python3 upgrade.py`；或手動 `.venv/bin/python -m pip install -r requirements-diarize.txt`；或把 `LEC_DIARIZE_PYTHON` 指到已安裝的直譯器 |
| `model_missing` | 跑 `python3 setup_engines.py diarize` |
| `setup_engines.py diarize` 說「SHA-256 不符，已刪除」 | 下載中斷或來源被更換。重跑一次；仍失敗就**不要**使用那個來源 |
| 說「已存在但 SHA-256 與預期不符，不覆蓋」 | `models/` 裡有一個同名但內容不同的檔案。若是自己換的就忽略；否則刪掉它重跑 |
| 分群行程被系統結束（結束代碼 -9／137，訊息提到記憶體） | 記憶體不足。一場 87.7 分鐘的會議實測峰值約 1 GB；先關掉其他吃記憶體的程式 |

## 移除

```bash
.venv/bin/python -m pip uninstall -y sherpa-onnx sherpa-onnx-core
rm models/sherpa-onnx-pyannote-segmentation-3-0.* models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx
```
