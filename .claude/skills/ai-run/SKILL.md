---
name: ai-run
description: Run one ai:ready GitHub issue through Codex in its own worktree, open a draft PR, and apply the user's review comments; from a version's main conversation, plan issues and dispatch each one to its own child session ("派工 #N").
---

> 本 repo：`RichardstGG/lecture-notes`，base branch：`main`。所有 `gh` 指令加 `--repo RichardstGG/lecture-notes`。
> 一律直接用 `./scripts/ai/companion.sh` 呼叫 Codex（不經 codex:codex-rescue subagent）；
> `task` 必須帶 `--cwd <worktree> --write`。push、開 issue/label 需我明確同意；PR 由我自己 merge。
> 若 `.claude/settings.local.json` 不是合法 JSON（`python3 -m json.tool`），先停下來告訴我。

## 主對話：規劃與派子對話（Claude 桌面版）
一個大版本開一個主對話，只負責討論、開 issue、派工、追進度；每個 issue 交給一個子對話跑完，主對話不讀 diff、不跑驗證，以免吃掉 context。
1. 規劃：和我討論技術方案，把決定寫進 issue 本文（**Scope**、**Done when**、技術方案與理由）。子對話從零開始，只看得到 issue。`gh issue create` 要經我同意，新 issue 標 `ai:proposed`。
2. 我說「派工 #N [#M …]」：
   - 先在主對話內做 Dispatch 2 的 Scope 重疊檢查（包含這批 issue 彼此之間的重疊），有重疊就停下來告訴我。
   - 每個 issue 呼叫一次 `start_session`：`initiation: "user_asked"`、`context: "fresh"`、`use_worktree: false`（ai-run 會自己建 `.tmp/wt-issue-N`，子對話要共用這個 checkout）、`name: "issue-N"`、`title: "ai-run #N：<issue 標題>"`、`prompt: "開跑 #N"`、`background`：版本目標，以及這個 issue 和其他 issue 的關係。
   - 沒有 `start_session` 工具時（非桌面版），直接在主對話跑 Dispatch。
3. 子對話結束時，主對話只用一行回報：PR 連結、agy 結論（ready／draft）、待我人工驗證的項目。blocked 時轉述 QUESTIONS。
4. review 意見與 merge 後的 Cleanup 都在該 issue 的子對話裡處理；我在主對話說「PR #X 有 comment」時，用 `SendMessage` 轉給對應的子對話，不在主對話修。

## Dispatch（可並行，一個 issue 一個 worktree）
1. `gh issue view N`。我說「開跑 #N」即視為核准：若缺 `ai:ready` 就補上。
2. 並行檢查：`gh issue list --label ai:in-progress`，Scope 與本 issue 重疊（尤其建置檔、main、CI 等接線點）→ 停下來告訴我。
3. `gh issue edit N --remove-label ai:ready --add-label ai:in-progress`
4. `wt=$(./scripts/ai/new-worktree.sh N main)`
5. 把 issue 全文 +「工作目錄 $wt、依 AGENTS.md 的 Codex worker 規則」寫到 `.tmp/prompt-N.md`，然後：
   `./scripts/ai/companion.sh task --cwd "$wt" --write --background --fresh --json --prompt-file "$PWD/.tmp/prompt-N.md"`
   記下 job id 與 worktree 的對應。
6. `./scripts/ai/codex-wait.sh <job-id> 570`：exit 2（逾時）→ 再呼叫，最多 3 次；exit 0 → Finalize；exit 1/3 → 告訴我 job id 與狀態並停下。

## Finalize
1. `./scripts/ai/codex-result.sh <job-id>`。STATUS 為 blocked，或 partial 但原因不是 ENV_LIMITED → 在 issue 留言貼 QUESTIONS，標 `ai:blocked`。
2. `cd` 到 worktree，**在 host 跑完整** `./scripts/ai/verify.sh`（不信任 sandbox 結果）。失敗 → 以 `--fresh` 重派一次（附 issue 全文、`git diff`、失敗 log）；啟動失敗（job 立刻 failed）不計入次數。
3. 只 `git add -- <FILES 列出的路徑>`，確認 `git status --porcelain` 沒有 FILES 以外的變更；commit（用 COMMIT_MESSAGE）；`git push -u origin ai/issue-N`。
4. `gh pr create --draft --repo RichardstGG/lecture-notes --base main --head ai/issue-N --title ... --body ...`
   body：🤖 標記、`Closes #N`、SUMMARY、TESTS、ENV_LIMITED 與 QUESTIONS（尚待我人工驗證的項目）。
5. `gh issue edit N --remove-label ai:in-progress --add-label ai:review`
6. 在 worktree 內 `./scripts/ai/agy-review.sh <PR URL> main`（Antigravity 唯讀 review，由腳本留 PR comment，約 1 分鐘）。
   結論為 APPROVE 時腳本會把 PR 轉成 ready for review，否則維持 draft；把腳本印出的結果告訴我。
   exit 3（沒裝 agy）或失敗 → 告訴我原因即可，不影響 PR、不重試。

## Apply review（我說「PR #X 有 comment」）
1. 讀取 review comments，整理成 fix brief（逐條列出檔案、行號、要改什麼；我標註「不用改」的略過）。
   「🤖 Antigravity review」的留言只是參考：只納入我在 comment 中點名要改的條目。
2. worktree 不在就 `git worktree add .tmp/wt-issue-N ai/issue-N`。
3. 一律 `--fresh`：prompt 附 issue 全文、目前 `git diff main...`、fix brief。
4. 同 Finalize 2～3（不 force push），在 PR 留言摘要，label 改 `ai:review`，再跑一次 Finalize 6。

## Cleanup（PR merge 後）
`git worktree remove .tmp/wt-issue-N && git branch -d ai/issue-N && git fetch --prune`
