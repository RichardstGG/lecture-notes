# 工作台導航與總結方式

課堂與會議工作台各有「歷史紀錄」「內網共享」及設定子選單。子選單先切換到所屬工作台，再捲動到該頁區塊；「本機設定」是獨立頁面。切換頁面保留尚未送出的表單、課程編輯內容及會議表單與停止請求狀態，不呼叫啟動、停止或關閉分享 API。背景狀態仍可見。

課堂依序顯示處理控制、原有逐字稿／課堂筆記／術語候選與右側歷史紀錄、內網分享、課程設定。缺少 `work_type` 的舊場次歸入課堂；`work_type=meeting` 不進入課堂歷史、文件選取或術語請求；明確的未知類型不歸入任何工作台。會議使用淡藍色主題，真實歷史與結果從現有 session API 讀取原稿和帶代號稿，帶代號稿可透過 session SSE 更新。正式會議頁已移除 mock，透過會議 API 建立／編輯 meetings TOML、錄音／匯入、正常停止／強停、啟動辨識／取消／重跑。預計人數必填 1..30；雙來源選項停用，沒有摘要模型與筆記欄。10/4 收折使用真實 speakers.json 資料。

會議 status 若含 `diarization`，會議頁顯示目前階段、已處理與總音訊秒數、要求與找到的代號數；總長未知時不顯示百分比。跨頁提示也顯示階段與秒數。切回課堂或本機設定只關閉會議文件 SSE 連線，不呼叫停止作業；重新進入會議頁會重讀 session。會議結果只呈現真實 session，無示範資料進入歷史或內網分享。

## 分頁與區塊網址

| 頁面 | 頂端 | 歷史紀錄 | 內網共享 | 設定 |
| --- | --- | --- | --- | --- |
| 課堂 | `#live` | `#history` | `#lecture-sharing` | `#courses` |
| 會議 | `#meeting-live` | `#meeting-history` | `#meeting-sharing` | `#meeting-settings` |
| 本機設定 | `#local-settings` | — | — | — |

支援重新載入與瀏覽器上一頁／下一頁；既有 `#live`、`#history`、`#courses` 連結保留。未知 fragment 回到課堂頂端。小螢幕仍顯示文字子選單。

## 課堂總結方式契約

開始處理的「LLM總結方式」依序列出「不總結」、已安裝的本機模型、PR #53 提供的已設定 API。當課程預設本機模型未安裝，保留一個不可選的目前預設項目並標示「未安裝」；預設 API 不在清單時標示「設定不可用」，不默默改選其他方式。API 選項只讀取公開的 ID、名稱與類型，不讀取連線資訊或認證。

`lec courses --json` 與 `/api/v1/courses` 增補布林欄位 `summary_enabled`，來自 default → local → course 合併後的 `summary.enabled`；API schema 可接受舊 CLI 缺少此欄位，UI 以啟用總結相容舊資料。CLI 格式、API v1、status／phase schema 與停止契約保持不變。

- 初次選取與切換課程：依課程的 `summary_enabled`、`upstream`、`model` 選取；未手動變更時不送出摘要覆寫，仍由後端使用完整課程設定。
- 手動選「不總結」：使用既有 `overrides: {"summary.enabled": false}`，不送出之前選擇的模型或 API。
- 手動選本機模型：送出 `upstream: "local"`、該 `model` 與 `overrides: {"summary.enabled": true}`，能覆寫原本停用總結或使用 API 的課程。
- 手動選 API：送出已儲存的 `upstream` ID 與 `overrides: {"summary.enabled": true}`，不送本機模型。

選擇只影響這次處理，不寫入課程檔。右側補做／重做摘要維持 PR #53 的介面。會議沒有總結選單或模型操作。

會議啟動期間按鈕停用；API 確認本次 CLI 已持鎖並發布 session 才回 202，配置前錯誤顯示 envelope 訊息。前端不推測錄音狀態。檔案選擇器沿用 upload staging，重跑不重傳音檔；正常停止／取消後仍可強停，換頁不重送停止。跨頁提示包含目前唯一工作的類型、session、phase、進度。session SSE 在原稿／帶代號稿更新時 append／replace，來源或發言者資料改變時 snapshot 更新結果。

前端檢查：`npm run lint` 使用 TypeScript 的 noUnusedLocals／noUnusedParameters（沒有額外 ESLint 相依）；`npm run typecheck`、`npm run build`、`npm test`。
