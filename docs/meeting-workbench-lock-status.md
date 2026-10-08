# 單工作鎖與狀態相容性

2026-10-08 實作里程碑：課堂 run、summary、會議 run 與會後辨識共用一個工作鎖。此文件描述已落地的容器與 reader 行為；會議工作編排仍待後續里程碑接線。

## 鎖的生命週期

`<state_dir>/run.lock` 是持續存在的空 sidecar 檔，使用平台原生非阻塞 OS 鎖。取得鎖的行程會一直持有檔案 handle 到工作結束，並在成功取得後發布 `<state_dir>/run.json`。sidecar 不會被刪除或替換，避免刪除 locked inode 造成第二個工作誤取得鎖。程序異常退出時由作業系統釋放鎖；下一個工作可回收仍顯示舊 PID 的 `run.json`。

`run.json` 是公開 metadata，不是鎖本身。writer 使用 schema 2，只增加 `work_type`；未知或缺值正規化為 `lecture`。鎖已取得但 metadata 尚未發布的短暫狀態會回傳 schema 2、`pid=null`、`session=null`、`work_type="unknown"`，讀者必須視為 busy。舊 schema 1 的活躍 PID 仍受尊重且不會被 reader 回寫。

`status.json` writer 使用 schema 2，只增加 `work_type`、`stop_reason`、`diarization`；`events.jsonl` 維持 schema 1。schema 1 reader 只在記憶體補上 lecture 預設與缺少欄位，不重寫舊檔，並保留未知附加欄位與未知非空工作類型。

`lec stop` 只在鎖記錄的 session 目錄寫入 `stop` 或 `stop_force`。若工作尚未發布 session，命令以既有錯誤退出 1 要求稍後重試，不送 OS signal；既有 CLI 語法與 exit code 不變。API 在啟動前檢查失敗後若 CLI 回報競爭，會重新讀取鎖並將仍活躍的工作映射為 HTTP 409。

## 限制與驗證

鎖是本機檔案系統的 advisory lock；所有 writer 都必須升級到此 sidecar 協定，舊程式在同時啟動時仍可能只依賴舊 PID 檢查。網路檔案系統、PID reuse 與不支援原生鎖的檔案系統不在此里程碑的保證範圍；原生鎖錯誤會向上拋出，不會假裝 idle。

自動測試涵蓋四種工作跨行程競跑只產生一個 owner、同 PID 物件互斥、metadata 發布前 busy、異常退出後重新取得、舊 schema 讀取、寫入失敗釋放鎖與 status 發布排序。這些是契約／mock 測試，未啟動音訊引擎、麥克風或真實辨識。
