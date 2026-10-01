# 術語候選審核

課堂總結完成後，開啟該堂課的「術語候選」分頁。畫面會按節數、出現次數及近似寫法排序未定義的術語。每組可標為正確、指定正式寫法，或永久忽略；前兩種會把同組近似寫法存為 `aka`。術語說明可在儲存前修改。若希望下次轉錄也參考正式術語，可勾選加入 Whisper；畫面會顯示 prompt 與術語的 200 字預算。
聽錯項目可指定已有的正式術語，此時會把候選與近似寫法加入該術語的 `aka`，並保留原有說明。

「套用到課程設定」會先重新讀取課程檔，合併原有的 `whisper.terms`、`summary.glossary` 和 `summary.ignored_terms`，然後使用既有的 vocabulary PUT 寫入。若課程檔從畫面載入後已有變動，畫面會要求重新載入。兩次讀取與寫入之間仍可能發生同時編輯；目前沒有 ETag 或 `If-Match` 鎖。

## CLI 與 API 契約

`lec terms <session> --json` 唯讀取用該 session 的 `notes.jsonl`，不呼叫模型；`--course <課名或 TOML 路徑>` 指定比對課程，`--similarity 0..1` 調整分組門檻，預設 `0.5`。沒有 `notes.jsonl` 或候選時回傳空陣列並以狀態碼 0 結束。session 資料夾不存在或門檻不合法時以狀態碼 1 結束。

JSON `schema_version` 為 1，包含 `session`、`course`、`course_id`、`course_file`、`whisper_prompt_base`、`defined`（`terms` 與 `glossary` 數量）以及 `candidates`。每個候選包含 `term`、`count`、`sections`、`explain`、`asr_original`、`verified`、`flags` 及 `variants`。`flags` 可為 `hedged`、`asr_corrected`、`unverified`、`variant_group`；變體包含術語、次數、節次與相似度。同一 label 重做時採最後一筆，無法解析的行會跳過。

UI 呼叫 `GET /api/v1/sessions/{session_id}/term-candidates` 取得同一 JSON。路徑由 `SessionStore.path_for` 驗證，不能跳出 output root。`PUT /api/v1/courses/{course_id}/vocabulary` 可額外帶 `ignored_terms: string[]`；省略時保留課程檔現有忽略清單。`summary.ignored_terms` 是新增的課程設定鍵，預設空陣列；舊版 `lec` 讀取含此鍵的課程檔時可能產生未知鍵警告。
