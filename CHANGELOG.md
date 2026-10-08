# 版本歷史

每個已合併的 PR 對應一個版本號，新的在上面。

## 版本號規則

格式是 `v主.次.修`，修訂號固定兩位數。

| 部分 | 意思 |
|---|---|
| 主版本 | 產品里程碑：**V1.0** 課程紀錄與初版 UI、**V2.0** 會議紀錄與會議工作台 UI、**V3.0** 打包成應用程式 |
| 次版本 | 一批同主題的 PR |
| 修訂號 | 每合併一個 PR 加 1，從 `00` 開始，跨次版本時歸零 |

- 修訂號照 **PR 合併時間** 排，不照 PR 編號。有些 PR 是後開先合的，照合併時間排，版本號才會跟 git 歷史一樣往前走。
- 只列已合併的 PR；關閉但沒合併的不算。
- 日期是台北時間（UTC+8）的合併日期。
- 這份檔案由 maintainer 累積一批 PR 後交給 Antigravity 更新，再由 Claude 復核。流程和提示詞見 [docs/changelog-update.md](docs/changelog-update.md)。

## v1.6：會議雙音源與 Windows 安裝

| 版本 | 合併日期 | PR | 內容 |
|---|---|---|---|
| v1.6.05 | 2026-10-08 | [#67](https://github.com/RichardstGG/lecture-notes/pull/67) | 會議設定與資料層契約 |
| v1.6.04 | 2026-10-08 | [#66](https://github.com/RichardstGG/lecture-notes/pull/66) | 發言者辨識評分器（DER／JER） |
| v1.6.03 | 2026-10-08 | [#65](https://github.com/RichardstGG/lecture-notes/pull/65) | Windows 一鍵安裝 |
| v1.6.02 | 2026-10-08 | [#64](https://github.com/RichardstGG/lecture-notes/pull/64) | Windows 測試站 |
| v1.6.01 | 2026-10-08 | [#63](https://github.com/RichardstGG/lecture-notes/pull/63) | 雙音源錄音引擎（Linux） |
| v1.6.00 | 2026-10-08 | [#62](https://github.com/RichardstGG/lecture-notes/pull/62) | 雙音源契約 |

## v1.5：總結 API 上游與發言者辨識引擎

| 版本 | 合併日期 | PR | 內容 |
|---|---|---|---|
| v1.5.08 | 2026-10-06 | [#61](https://github.com/RichardstGG/lecture-notes/pull/61) | UI 管理 API 上游 |
| v1.5.07 | 2026-10-06 | [#60](https://github.com/RichardstGG/lecture-notes/pull/60) | 課程表單對齊 |
| v1.5.06 | 2026-10-06 | [#59](https://github.com/RichardstGG/lecture-notes/pull/59) | doctor 認得 Metal 裝置名稱 |
| v1.5.05 | 2026-10-06 | [#58](https://github.com/RichardstGG/lecture-notes/pull/58) | 會議 UI 唯讀顯示 |
| v1.5.04 | 2026-10-06 | [#57](https://github.com/RichardstGG/lecture-notes/pull/57) | gitignore |
| v1.5.03 | 2026-10-06 | [#56](https://github.com/RichardstGG/lecture-notes/pull/56) | 發言者辨識安裝（含 SHA-256 驗證） |
| v1.5.02 | 2026-10-06 | [#55](https://github.com/RichardstGG/lecture-notes/pull/55) | 發言者辨識引擎 |
| v1.5.01 | 2026-10-06 | [#54](https://github.com/RichardstGG/lecture-notes/pull/54) | 工作台導覽 |
| v1.5.00 | 2026-10-06 | [#53](https://github.com/RichardstGG/lecture-notes/pull/53) | 課程總結可用 API 上游 |

## v1.4：會議工作台規劃與穩定性加固

#44 是 V2.0（會議紀錄）的起點。

| 版本 | 合併日期 | PR | 內容 |
|---|---|---|---|
| v1.4.08 | 2026-10-05 | [#52](https://github.com/RichardstGG/lecture-notes/pull/52) | 錄音不再因下游停頓而掉音訊 |
| v1.4.07 | 2026-10-05 | [#51](https://github.com/RichardstGG/lecture-notes/pull/51) | util 加固 |
| v1.4.06 | 2026-10-05 | [#49](https://github.com/RichardstGG/lecture-notes/pull/49) | 回報空解碼失敗 |
| v1.4.05 | 2026-10-05 | [#50](https://github.com/RichardstGG/lecture-notes/pull/50) | Unicode 比對一致 |
| v1.4.04 | 2026-10-05 | [#47](https://github.com/RichardstGG/lecture-notes/pull/47) | servers 擁有權紀錄寫回狀態資料夾 |
| v1.4.03 | 2026-10-05 | [#48](https://github.com/RichardstGG/lecture-notes/pull/48) | transcribe 純邏輯測試 |
| v1.4.02 | 2026-10-05 | [#46](https://github.com/RichardstGG/lecture-notes/pull/46) | Codex 所有權同步 |
| v1.4.01 | 2026-10-05 | [#45](https://github.com/RichardstGG/lecture-notes/pull/45) | CLAUDE.md 所有權 |
| v1.4.00 | 2026-10-04 | [#44](https://github.com/RichardstGG/lecture-notes/pull/44) | 會議工作台契約 |

## v1.3：內網唯讀分享

| 版本 | 合併日期 | PR | 內容 |
|---|---|---|---|
| v1.3.02 | 2026-10-04 | [#43](https://github.com/RichardstGG/lecture-notes/pull/43) | 關閉分享按鈕 |
| v1.3.01 | 2026-10-04 | [#42](https://github.com/RichardstGG/lecture-notes/pull/42) | 分享到所有介面、預設路由網址、QR code |
| v1.3.00 | 2026-10-04 | [#41](https://github.com/RichardstGG/lecture-notes/pull/41) | 內網唯讀分享 |

## v1.2：轉錄可靠性與術語

| 版本 | 合併日期 | PR | 內容 |
|---|---|---|---|
| v1.2.02 | 2026-10-02 | [#40](https://github.com/RichardstGG/lecture-notes/pull/40) | 術語候選 |
| v1.2.01 | 2026-10-01 | [#39](https://github.com/RichardstGG/lecture-notes/pull/39) | 轉錄失敗的段落不再被吞掉 |
| v1.2.00 | 2026-10-01 | [#38](https://github.com/RichardstGG/lecture-notes/pull/38) | A/B 比較記錄模型來源 |

## v1.1：品質評估與安裝體驗

| 版本 | 合併日期 | PR | 內容 |
|---|---|---|---|
| v1.1.09 | 2026-09-24 | [#37](https://github.com/RichardstGG/lecture-notes/pull/37) | Windows 驗證手冊 |
| v1.1.08 | 2026-09-24 | [#36](https://github.com/RichardstGG/lecture-notes/pull/36) | 所有權邊界 |
| v1.1.07 | 2026-09-24 | [#35](https://github.com/RichardstGG/lecture-notes/pull/35) | 互動式 setup.py |
| v1.1.06 | 2026-09-24 | [#34](https://github.com/RichardstGG/lecture-notes/pull/34) | 樣本直接比對 |
| v1.1.05 | 2026-09-24 | [#32](https://github.com/RichardstGG/lecture-notes/pull/32) | macOS 驗證手冊 |
| v1.1.04 | 2026-09-24 | [#33](https://github.com/RichardstGG/lecture-notes/pull/33) | devices 測試 |
| v1.1.03 | 2026-09-24 | [#31](https://github.com/RichardstGG/lecture-notes/pull/31) | 輸出檔變動時 SSE 不斷線 |
| v1.1.02 | 2026-09-24 | [#30](https://github.com/RichardstGG/lecture-notes/pull/30) | doctor 工具鏈檢查 |
| v1.1.01 | 2026-09-24 | [#29](https://github.com/RichardstGG/lecture-notes/pull/29) | 樣本品質評估 |
| v1.1.00 | 2026-09-24 | [#28](https://github.com/RichardstGG/lecture-notes/pull/28) | Agent 分工文件 |

## v1.0：課程紀錄與初版 UI

**V1.0 里程碑：v1.0.26。** 到這一版，課程錄音、從檔案轉錄、總結、UI 的設定、裝置與模型選擇，以及 `lec --start-ui` 都已完成。

| 版本 | 合併日期 | PR | 內容 |
|---|---|---|---|
| v1.0.26 | 2026-09-23 | [#27](https://github.com/RichardstGG/lecture-notes/pull/27) | UI 音檔選擇器（**V1.0 里程碑**） |
| v1.0.25 | 2026-09-23 | [#26](https://github.com/RichardstGG/lecture-notes/pull/26) | CLAUDE.md |
| v1.0.24 | 2026-09-21 | [#24](https://github.com/RichardstGG/lecture-notes/pull/24) | UI 裝置設定與診斷 |
| v1.0.23 | 2026-09-21 | [#25](https://github.com/RichardstGG/lecture-notes/pull/25) | dshow 裝置清單解析修正 |
| v1.0.22 | 2026-09-21 | [#23](https://github.com/RichardstGG/lecture-notes/pull/23) | Windows README |
| v1.0.21 | 2026-09-21 | [#22](https://github.com/RichardstGG/lecture-notes/pull/22) | UI 安裝與升級文件統一 |
| v1.0.20 | 2026-09-21 | [#20](https://github.com/RichardstGG/lecture-notes/pull/20) | `lec --start-ui` |
| v1.0.19 | 2026-09-21 | [#21](https://github.com/RichardstGG/lecture-notes/pull/21) | Windows subprocess 改用 UTF-8 |
| v1.0.18 | 2026-09-21 | [#19](https://github.com/RichardstGG/lecture-notes/pull/19) | macOS 安裝文件 |
| v1.0.17 | 2026-09-21 | [#18](https://github.com/RichardstGG/lecture-notes/pull/18) | 一鍵升級 |
| v1.0.16 | 2026-09-21 | [#17](https://github.com/RichardstGG/lecture-notes/pull/17) | README：只裝 whisper 的說明 |
| v1.0.15 | 2026-09-21 | [#14](https://github.com/RichardstGG/lecture-notes/pull/14) | UI 模型探索 |
| v1.0.14 | 2026-09-21 | [#16](https://github.com/RichardstGG/lecture-notes/pull/16) | 只轉錄模式 |
| v1.0.13 | 2026-09-21 | [#15](https://github.com/RichardstGG/lecture-notes/pull/15) | 只轉錄模式的 doctor 檢查 |
| v1.0.12 | 2026-09-21 | [#13](https://github.com/RichardstGG/lecture-notes/pull/13) | 巢狀輸出路徑 |
| v1.0.11 | 2026-09-21 | [#12](https://github.com/RichardstGG/lecture-notes/pull/12) | 結構化詞彙編輯 |
| v1.0.10 | 2026-09-21 | [#11](https://github.com/RichardstGG/lecture-notes/pull/11) | Windows MSVC 偵測 |
| v1.0.09 | 2026-09-21 | [#10](https://github.com/RichardstGG/lecture-notes/pull/10) | 課程設定編輯器 |
| v1.0.08 | 2026-09-20 | [#9](https://github.com/RichardstGG/lecture-notes/pull/9) | UI 關閉時串流正常收尾 |
| v1.0.07 | 2026-09-20 | [#8](https://github.com/RichardstGG/lecture-notes/pull/8) | 課程總結詞彙表 |
| v1.0.06 | 2026-09-20 | [#7](https://github.com/RichardstGG/lecture-notes/pull/7) | 自動 PR 流程文件 |
| v1.0.05 | 2026-09-20 | [#6](https://github.com/RichardstGG/lecture-notes/pull/6) | UI frontend 基礎 |
| v1.0.04 | 2026-09-18 | [#5](https://github.com/RichardstGG/lecture-notes/pull/5) | UI 行程控制 |
| v1.0.03 | 2026-09-18 | [#4](https://github.com/RichardstGG/lecture-notes/pull/4) | Session API |
| v1.0.02 | 2026-09-18 | [#3](https://github.com/RichardstGG/lecture-notes/pull/3) | UI backend 基礎 |
| v1.0.01 | 2026-09-18 | [#2](https://github.com/RichardstGG/lecture-notes/pull/2) | UI 狀態契約 |
| v1.0.00 | 2026-09-18 | [#1](https://github.com/RichardstGG/lecture-notes/pull/1) | 三平台適配 |
