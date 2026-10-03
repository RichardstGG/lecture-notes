# 內網唯讀分享

主控 UI 仍只監聽 `127.0.0.1`。在「內網唯讀分享」選定場次、輸入這台電腦的內網 IPv4（`10/8`、`172.16/12` 或 `192.168/16`）與埠（預設 `8766`）後，才會另啟一個分享 HTTP 應用。主控與分享服務由同一個後端程序管理；分享應用不掛載主控 API，也不提供編輯、留言、錄音控制或其他場次的路由。若防火牆封鎖該埠，需在本機允許同一內網的連線。

連結含隨機邀請碼，放在 URL fragment，瀏覽器不會把它送到伺服器的 GET 日誌。參與者填寫 1–40 字暱稱後才取得 `HttpOnly`、`SameSite=Strict` 訪客 cookie。主控端每約 2 秒更新參與者名單；連續 30 秒未請求則顯示離線並釋放席位。同時最多 20 個在線訪客，訪客紀錄最多保留 200 筆，超過時淘汰最久的離線紀錄。重新連線以原訪客憑證認領席位；如果當時已有 20 人在線，會收到 `share_full`。同一瀏覽器重新加入不增加席位。

分享固定在開啟時選定的場次，不受主控頁後續切換影響。訪客每約 2 秒輪詢逐字稿與筆記，未變更時回傳 `304`；內容從本機輸出檔讀取與快取最多 2 秒。下載只提供這兩份 Markdown，檔名及內容標示「目前版本」、擷取時間與版本雜湊。即使錄音停止，分享仍持續，直到手動關閉或主控服務結束。補做總結未必更新原場次的 phase，因此第一版**所有下載**一律標示目前版本，避免誤稱最終版。關閉會立即撤銷邀請碼與訪客憑證；重新開啟產生新連結。記憶體中的分享狀態不跨主控程序重啟保留。

資料只在本機與選定內網介面傳輸，沒有外部 CDN、雲端服務或執行時網路資源。分享使用一般 HTTP，適合受信任的內網；持有有效連結的人可填暱稱閱讀和下載，暱稱不是身分驗證。瀏覽器已取得的資料無法遠端收回。

## API 契約（v1）

主控服務：`GET /api/v1/sharing` 回傳 `{api_version, active, participants, session_id?, url?, max_online?, error?}`。`POST /api/v1/sharing/open` 的 JSON 為 `{session_id, host, port}`，成功回傳同一狀態；已分享時為 `409 share_already_open`，不允許的 host／port 為 `400`，綁定失敗為 `409 share_bind_failed`。`POST /api/v1/sharing/close` 使用 JSON `{}` 並回傳關閉後狀態。這三個路由只供 loopback 主控 UI 使用。既有 `lec` CLI、`stop`／`stop_force` 檔案、phase 名稱與 `status.json` 等檔案契約沒有更動。

分享服務：`POST /share/v1/join` 接受 `{invitation, nickname}` 並設定訪客 cookie；`GET /share/v1/snapshot` 回傳 `{api_version, course, phase, transcript, notes, version, version_label, captured_at, poll_seconds}`，可帶 `If-None-Match` 得到 `304`；`GET /share/v1/download/{transcript|notes}` 下載目前版本；`POST /share/v1/leave` 使用 JSON `{}` 並標記離線。除了加入外，內容端點都需要訪客 cookie。`401 join_required`、`403 invalid_invitation`、`409 share_full`、`410 share_closed` 皆以 `{error:{code,message}}` 回傳（已停止監聽時，舊連結直接連線失敗）。下載／快照不包含輸出資料夾路徑或其他場次 ID。
