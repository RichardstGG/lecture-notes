# 單來源會議錄音與匯入（2026-10-09）

本里程碑提供同步 CLI `lec meeting run`、來源保存與原逐字稿。會後辨識、meeting API 與正式錄音 UI 尚未接線；UI 控制仍是預覽。會議流程保持「實驗中」，本機合成檔案測試不代表麥克風或辨識準確度驗收。

## CLI

先以 `meetings/examples/template.toml` 建立 `meetings/<id>.toml`，再使用：

```bash
./lec meeting run 設計 --speakers 10 --file /path/to/input.wav --json
# 以下會開啟真實麥克風，只在使用者同意錄音時執行
./lec meeting run 設計 --speakers 10 --source chosen-device --json
./lec status --json
./lec stop
./lec stop --force
```

`--speakers` 必填且為 1..30；它最後覆寫 `diarization.num_speakers`（包括 `--set`）。`--file` 與 `--source` 不可同時指定。會議 id 必須有獨立設定檔，不自動建立或讀取同名課程。`--set section.key=value` 沿用會議設定驗證。dual 仍在鎖、目錄與引擎啟動前回 `capture_unavailable`。

會議執行私有設定副本強制 `summary.enabled=false`、`audio.keep_recording=true`；絕不建立 LlamaServer、Summarizer 或自動辨識。沿用轉錄器的 Markdown/SRT 格式；它使用的舊 `course` 標籤帶會議名稱，`config.used.toml` 中 `[work] type="meeting"` 是工作類型依據，不建立或修改課程檔。

`--json` 只在工作結束時輸出一個 JSON；進度讀 `lec status --json`。引擎的文字日誌留在 session.log，CLI stderr 只回報安全的操作錯誤。成功格式：

```json
{"schema_version":1,"accepted":true,"work_type":"meeting","operation":"run","session":"/absolute/session","pid":1234,"outcome":"done"}
```

取消的 `outcome` 為 `aborted`、exit 130。錯誤格式為 `{schema_version:1,accepted:false,error:{code,message}}`，包括 argparse 參數錯誤。既有課堂指令的 JSON／退出碼不改。

| 退出碼 | code 或結果 |
| --- | --- |
| 0 | `done`（正常停止現場錄音亦為 done） |
| 2 | `input_invalid`、找不到指定匯入檔的 `source_missing` |
| 3 | `run_active`；任何工作類型都佔同一把鎖 |
| 1 | `dependency_missing`、`capture_unavailable`、`source_copy_failed`、`source_invalid`、`capture_unverified`、`capture_loss`、`capture_failed`、`decode_failed`、`asr_failed`、`engine_failed`、`write_failed`，或錄音未留下來源的 `source_missing` |
| 130 | 使用者取消／強制停止 |

## 來源與狀態

每次工作使用排他建立的新 session。匯入先複製到 `.source_audio.<suffix>.tmp`，flush/fsync 後原子 rename 為 `source_audio.<suffix>`；副檔名轉小寫，只接受 1..12 個英數字，原檔名不構成磁碟路徑。轉錄僅使用這份複本。刪除 upload staging 或外部原檔不影響已保存來源。

現場錄音由既有轉錄器保存 `recording_HHMMSS.ogg`，完成後移為 `source_audio.ogg`。封存先取得有限正數音長、以 ffmpeg 全檔解碼驗證，再串流計算 SHA-256，最後原子寫入 `source.json`。匯入在啟動 Whisper 前完成以上步驟。需要 ffmpeg 和 ffprobe，不安裝或下載引擎。

`source.json` schema 1 的精確欄位：

| 欄位 | 語義 |
| --- | --- |
| `schema_version` | 1 |
| `kind` | `import` 或 `recording` |
| `original_name` | 匯入原檔名，僅供顯示；錄音為 `source_audio.ogg` |
| `path` | session 相對檔名 `source_audio.<suffix>` |
| `size_bytes`、`sha256` | 已保存來源的 byte size、64 字小寫十六進位 SHA-256 |
| `duration_seconds`、`created_at` | ffprobe 音長及帶時區的 metadata 建立時間 |
| `requested_speakers` | 本次預計人數；不是實際辨識人數 |
| `complete` | 解碼通過、擷取報告可驗證、遺失比例小於 2%，且擷取未失敗 |
| `capture` | 原樣保留引擎 `run()["capture"]` 報告；匯入用相同 `probe_capture` 計算，無法驗證為 null |

2% 使用既有引擎 `CAPTURE_LOSS_ERROR`，不是辨識準確度門檻。`complete=true` 也不表示音質或辨識品質合格。高遺失或未知完整性會保留音檔及 `complete=false` metadata，工作 failed 且訊息明示不可用於辨識；解碼或寫入失敗不發布成功 manifest。後續重跑仍必須重新驗證 size、hash、路徑和 complete，不能只信舊 manifest。

status schema 2 增量寫入可選 `capture={complete,report}`，不改既有欄位型別；events schema 1 增加 `source_saved {path,complete}`，並保留引擎的 `capture_integrity` 與錯誤事件。轉錄有缺口時 `asr_failed`，原稿及已驗證來源保留供後續重跑；不以有部分文字宣稱完整成功。

生命週期為 `starting → loading → recording|transcribing → finishing → done|failed|aborted`。正常停止現場錄音會排空轉錄、封存來源，done 且 `stop_reason=user`；匯入取消或尚在 loading 時取消為 aborted/130。stop watcher 只讀 session 內 `stop`／`stop_force`，不對 CLI 傳 OS signal；Ctrl+C 沿用相同語義。強停會終止已知擷取／驗證子行程與自己擁有的 Whisper、寫 aborted、回單一最終 JSON、釋放鎖並退出。強停留下的錄音若尚無經驗證 manifest，不能用於辨識；已完整保存的匯入來源可以保留。

## 驗證範圍與限制

- Automated / Mock：CLI 參數、JSON、0/1/2/3/130、來源先保存再處理、外部來源刪除、不可覆寫、複製失敗、擷取遺失、ASR 缺口、單鎖 busy、正常 stop、獨立子行程強停、訊號 handler 還原、無 LLM。
- Automated integration：合成 WAV 使用真實 ffmpeg/ffprobe 解碼；真實 Transcriber 配模擬 ASR 回應產生 Markdown/SRT。不是 whisper 模型或辨識品質驗證。
- Hardware / Not tested：真實麥克風、正常與強停的音訊保存品質、長錄音、macOS／Windows 錄音皆待使用者同意後在真機驗證。Windows 既有引擎正常停止會 terminate ffmpeg，若產物未完整封存，本流程會拒絕將它視為可用來源。
- 辨識準確度、雙音源硬體、模型門檻與 S00 策略仍待乾淨多人樣本；不使用既有 20.2% 遺失錄音作為品質證據。Windows basic 只驗程式契約，不取代錄音硬體驗收。

手動檔案流程可在已配置 Whisper 的 Linux 環境用第一段 `--file` 指令驗證；另開終端測 stop／force，檢查 status、source manifest 與原稿。麥克風與 Windows hardware profile 需使用者另外授權／開啟時段，agent 不自行執行。
