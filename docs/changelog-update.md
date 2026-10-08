# 更新 CHANGELOG.md

[CHANGELOG.md](../CHANGELOG.md) 不會每個 PR 都更新，以免同時開著的 PR 在同一個位置衝突。改成這個流程：

1. maintainer 累積一批已合併的 PR 後，把下面的提示詞交給 Antigravity。
2. Antigravity 開 `antigravity/changelog-<最新版本>` 分支，只改 `CHANGELOG.md`，開 PR。
3. maintainer 請 Claude 依照最後一節的清單復核。
4. maintainer 合併。

檔案所有權：`CHANGELOG.md` 由 Claude 與 Antigravity 共有，本檔歸 Claude。
Antigravity 平常只做審查（見 `.agents/agents/code-reviewer/agent.md`），更新 changelog 是 maintainer 另外交付的工作，範圍只限 `CHANGELOG.md`。

## 交給 Antigravity 的提示詞

複製下面整段。maintainer 有特別指示的話，填在最後的「本次指示」，沒有就寫「無」。

````text
你這次的工作是更新 lecture-notes 的 CHANGELOG.md，不是審查 PR。
依照 docs/changelog-update.md 的版本規則，把上次更新後合併的 PR 補進去。

範圍限制：
- 只改 CHANGELOG.md，其他檔案一律不動。
- 從 origin/main 開新分支 antigravity/changelog-<這批最後一個版本號>，例如 antigravity/changelog-v1.7.03。
- 推上去後開 PR 指向 main，標題用「docs(changelog): 補上 <起始版本>–<結束版本>」。
- 不要 approve、不要 merge，也不要推到別人的分支。

步驟：
1. 讀 CHANGELOG.md，找出最上面那一列的版本號與 PR 編號，這是上次更新到的位置。
2. 列出所有已合併 PR 的合併順序：
     gh pr list --state merged --limit 500 --json number,mergedAt,title \
       --jq 'sort_by(.mergedAt) | .[] | "\(.mergedAt)\t#\(.number)\t\(.title)"'
   取上一步那個 PR 之後合併的全部 PR，就是這一批。
3. 決定這一批的版本號（規則見下）。
4. 每個 PR 讀標題和描述（gh pr view <編號>），寫一句繁體中文內容摘要，
   講使用者看得到的效果，大約 20 字以內，不要照抄英文標題。
5. 新的次版本就在最上面新增一個「## v主.次：主題」區塊；延續目前次版本就把新列加在該區塊表格最上面。
   每列格式：| v1.7.00 | 2026-10-10 | [#68](https://github.com/RichardstGG/lecture-notes/pull/68) | 內容 |
   日期是 mergedAt 轉成台北時間（UTC+8）的日期。
6. PR 描述裡列出：這批的 PR 編號、版本範圍、為什麼開新次版本或延續舊的、主題名稱的理由。

版本規則：
- 修訂號：照合併時間排序，每個 PR 加 1，兩位數，跨次版本歸零（.00 起）。
- 次版本：這批有新功能（feat）時，開下一個次版本並取一個主題名稱；
  這批只有修正、文件、測試、雜項時，延續最上面那個次版本的修訂號。
  一批裡明顯有兩個不相干的主題時可以拆成兩個次版本，在 PR 描述說明理由。
- 主版本：只有 maintainer 在「本次指示」明確說某個 PR 完成了 V2.0 或 V3.0 才能升。
  被指定的那個 PR 是 v2.0.00（或 v3.0.00），之後的 PR 接著 .01、.02。不要自己判斷升主版本。
- 「本次指示」和上面規則衝突時，以本次指示為準。

本次指示：
<maintainer 填寫，例如「#70 完成 V2.0」「這批的主題叫做會議資料層」，沒有就寫「無」>
````

## Claude 復核清單

在 Antigravity 開的 PR 上逐項檢查，結果用 AGENTS.md 的驗證層級回報。

1. **只動了 `CHANGELOG.md`**：`gh pr diff <編號> --name-only`。
2. **順序和範圍正確**：在 PR 分支的 checkout 上跑下面的指令，CHANGELOG 裡由舊到新的 PR 編號必須完全等於合併順序，沒有漏列也沒有多列。

   ```bash
   diff <(grep -oE '^\| v[0-9]+\.[0-9]+\.[0-9]{2} \| [0-9-]+ \| \[#[0-9]+\]' CHANGELOG.md | grep -oE '#[0-9]+' | tac) <(gh pr list --state merged --limit 500 --json number,mergedAt --jq 'sort_by(.mergedAt) | .[] | "#\(.number)"')
   ```

   沒有輸出才算通過。這個 PR 本身還沒合併，不會出現在右邊。
3. **版本號連續**：同一個次版本的修訂號從 `00` 起連續，新次版本從 `00` 歸零，修訂號兩位數。
4. **次版本與主版本判斷**：對照「版本規則」與 maintainer 的「本次指示」。主版本只能依 maintainer 指示升。
5. **日期**：抽幾列跟 `mergedAt` 換成 UTC+8 的日期比對。
6. **內容摘要**：讀 PR 描述確認沒有寫錯功能，用語是繁體中文、看得懂。
7. **既有列沒被改動**：diff 只有新增，舊的版本號不能重編。
