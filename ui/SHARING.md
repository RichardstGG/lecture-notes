# 內網唯讀分享

主控 UI 仍只監聽 `127.0.0.1`。在「內網唯讀分享」選定場次與埠（預設 `8766`）後，才會另啟一個分享 HTTP 應用。介面預設勾選「監聽所有 IPv4 介面」，使分享服務綁定 `0.0.0.0`；也可取消勾選，限定在指定的內網 IPv4（`10/8`、`172.16/12` 或 `192.168/16`）。主控與分享服務由同一個後端程序管理；分享應用不掛載主控 API，也不提供編輯、留言、錄音控制或其他場次的路由。若防火牆封鎖該埠，需在本機允許同一內網的連線。

連結含隨機邀請碼，放在 URL fragment，瀏覽器不會把它送到伺服器的 GET 日誌。參與者填寫 1–40 字暱稱後才取得 `HttpOnly`、`SameSite=Strict` 訪客 cookie。主控端每約 2 秒更新參與者名單；連續 30 秒未請求則顯示離線並釋放席位。同時最多 20 個在線訪客，訪客紀錄最多保留 200 筆，超過時淘汰最久的離線紀錄。重新連線以原訪客憑證認領席位；如果當時已有 20 人在線，會收到 `share_full`。同一瀏覽器重新加入不增加席位。

分享固定在開啟時選定的場次，不受主控頁後續切換影響。課堂分享逐字稿與筆記；會議分享原逐字稿與帶發言者逐字稿，不分享筆記。訪客每約 2 秒輪詢，未變更時回傳 `304`；內容從本機輸出檔讀取與快取最多 2 秒。下載只提供該類型的兩份 Markdown，檔名及內容標示「目前版本」、擷取時間與版本雜湊。即使錄音停止，分享仍持續，直到手動關閉或主控服務結束。補做總結或重跑辨識未必更新原場次的 phase，因此**所有下載**一律標示目前版本，避免誤稱最終版。關閉會立即撤銷邀請碼與訪客憑證；重新開啟產生新連結。記憶體中的分享狀態不跨主控程序重啟保留。

資料在本機與分享服務的監聽介面傳輸；`0.0.0.0` 包含所有 IPv4 介面（含 VPN／公網介面，如有），實際可達範圍取決於本機路由與防火牆。沒有外部 CDN、雲端服務或執行時網路資源。分享使用一般 HTTP，適合受信任的內網；持有有效連結的人可填暱稱閱讀和下載，暱稱不是身分驗證。瀏覽器已取得的資料無法遠端收回。

主控頁會以本機 `ip -j -4 route show default` 與 `ip -j -4 addr show dev <interface>` 取得預設路由介面的 IPv4。多條路由優先採 metric 最小者，並優先使用該介面的 `prefsrc`。不使用 shell，也不向外部伺服器探測。沒有預設路由、沒有 `ip` 工具（例如部分 macOS／Windows 環境）或偵測失敗時，改由操作者手動填寫連結 IP。已開啟的分享不自動換連結；切換網路後請重新載入主控頁，確認 IP 並重新開啟分享。

監聽位址與連結位址分開：連結及 QR code 永遠使用可連線的 IPv4，不能使用 `0.0.0.0`。QR code 在主控前端本機產生 SVG，包含完整邀請碼，不呼叫任何外部 QR 服務。其他介面的實際本機 IP 也能搭配同一埠與邀請碼存取；仍驗證實際 socket 的目的 IP、Host 與同來源 Origin。`0.0.0.0` 僅代表 IPv4，不包含 IPv6。

## API 契約（v1）

主控服務：`GET /api/v1/sharing` 回傳 `{api_version, active, participants, session_id?, work_type?, url?, max_online?, bind_host?, advertise_host?, error?}`。`POST /api/v1/sharing/open` 的 JSON 為 `{session_id, host, port, advertise_host?}`，成功回傳同一狀態；已分享時為 `409 share_already_open`，不允許的 host 為 `400`（欄位型別／port 範圍驗證為 `422`），綁定失敗為 `409 share_bind_failed`。`POST /api/v1/sharing/close` 使用 JSON `{}` 並回傳關閉後狀態。這三個路由只供 loopback 主控 UI 使用。既有 `lec` CLI、`stop`／`stop_force` 檔案、phase 名稱與 `status.json` 等檔案契約沒有更動。

分享服務：`POST /share/v1/join` 接受 `{invitation, nickname}` 並設定訪客 cookie；`GET /share/v1/snapshot` 對課堂回傳 `{api_version, work_type:"lecture", course, phase, transcript, notes, version, version_label, captured_at, poll_seconds}`，對會議回傳同形狀但 `work_type:"meeting"` 且以 `speaker_transcript` 取代 `notes`。可帶 `If-None-Match` 得到 `304`。`GET /share/v1/download/transcript` 供兩類使用；`/notes` 僅課堂、`/speaker-transcript` 僅會議，另一類回 `404`。`POST /share/v1/leave` 使用 JSON `{}` 並標記離線。除了加入外，內容端點都需要訪客 cookie。`401 join_required`、`403 invalid_invitation`、`409 share_full`、`410 share_closed` 皆以 `{error:{code,message}}` 回傳（已停止監聽時，舊連結直接連線失敗）。下載／快照不包含輸出資料夾路徑或其他場次 ID。

新增 `GET /api/v1/sharing/network`（只限主控）：回傳 `{api_version, interface, advertise_host, error}`；偵測失敗仍回 `200`，IP 與介面為 `null` 並提示手動輸入。`host="0.0.0.0"` 開啟時可提供 `advertise_host`，未提供則重新偵測；沒有可用 IP 時回 `400 invalid_advertise_host`，不啟動監聽。具體 `host` 的既有請求仍有效，連結使用同一 IP；提供不同 `advertise_host` 會被拒絕。主控狀態新增 `bind_host` 與 `advertise_host`，既有欄位不變。
