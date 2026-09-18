"""Help docs that get loaded **on demand** instead of always in system prompt.

LLM 看 system prompt 只知「有 chain mode / cancel / file 三個 topic、要查就 call
read_help_doc」、實際細節不進每次對話 — 省 ~1500 tok / 輪。
"""

HELP_DOCS: dict[str, str] = {
    "chain": """\
# Chain 模式 — dispatch_subagent_async 的 follow_up 參數

複雜任務含「寫 + 審 + 改」「規劃 + 執行 + 驗證」等多階段時、用 follow_up
讓 backend 自動接力、不必 chat agent 監控也不必使用者每階段再 trigger。

```python
dispatch_subagent_async(
    role="coder",
    task="寫 calculator.py、加基本 test",
    working_dir="ai_output/calc/",
    max_iter=8,
    follow_up=[
        {"role": "critic", "task": "審查 calculator.py 列 3 問題寫 review.md", "max_iter": 10},
        {"role": "coder",  "task": "讀 review.md、修正 calculator.py、跑 test", "max_iter": 10},
    ],
)
```

特性:
- 每階段 backend 自動接力(不靠 chat agent)、共用同一 working_dir
- 上階段 summary + cwd 自動 prepend 進下階段 task prompt
- 任一階段失敗整條 chain 停、TG push「失敗在第 N/M 階段」
- 中間階段完 push「✅ N/M 完、🔁 N+1/M 派出」、最後階段完 push「🎉 整條完成」

各 role 的 max_iter 建議下限(低於這個很容易 exceeded):
- coder 寫小 script (<100 行) + 跑驗證 → 8
- coder 中型/多檔/含 test → 10-12
- critic / planner 讀-寫-思任務 → **10-12、不要給 5**(光「讀檔 + 寫 markdown
  + done」就 4-5 輪、扣 retry 5 輪幾乎一定 exceed)
- researcher → 8-10
- data_analyst → 8-12

何時用 chain vs 單一 dispatch:
- 任務含多階段(多 role 配合) → chain
- 使用者 explicit 說「先 X 再 Y 再 Z」 → chain
- 任務只是寫個 X / 跑一下 → 單一 dispatch 就好

典型 chain 配置:
- coder → critic → coder fix(寫 + 審 + 改)
- planner → coder → critic(規劃 + 執行 + 審)
- researcher → data_analyst(收料 + 整理)
""",

    "files": """\
# 子代理產物的「讀內容」/「傳檔」— 別用 send_file_to_tg

子代理寫的檔在 `chat-adhoc/<timestamp>_<id>/`、**不屬於任何 workflow**、所以
`send_file_to_tg`(那個是給 workflow 用的)會失敗。改用兩個 ad-hoc 專用工具:

## read_subagent_file(task_id, filename) — 讀檔內容貼進 chat
- 使用者問「程式內容是什麼」「貼給我看」「寫了什麼」 → 用這個
- filename 留空 → 先列該 task working_dir 內所有檔
- 50KB 以下 inline 貼回;過大會建議改用 send_subagent_file_to_tg
- 安全:限定 task 的 working_dir 內、不能讀外部

## send_subagent_file_to_tg(task_id, filename, confirm) — 傳檔到 TG
- 使用者要「下載」「傳給我」「把 .py 給我」 → 用這個(走兩步協議)
- 跟 send_file_to_tg 不同:後者要 workflow_query、本工具用 task_id
- 大檔 / binary / 不適合 inline 的都用這個

## 不要做的事
- 不要為了 ad-hoc 子代理產物去建假 workflow 然後 send_file_to_tg(本來就有
  read_subagent_file / send_subagent_file_to_tg 兩個專用工具)
- 不要先 read_subagent_file 再貼到 chat 結尾(訊息巨大、改用 send_*
  傳檔比較好、user 要看就在手機開)
""",

    "cancel": """\
# 中止子代理 — cancel_subagent_task(task_id)

使用者說「停止」「中斷」「不要跑了」「太久了 cancel」「砍掉」→ 用這個。

判斷規則:
- check_subagent_status 顯示 state=running、且使用者明確要停 → cancel
- 已 completed / failed / cancelled 的 → 不用呼叫(回 noop)
- 跑超過 5 分鐘還沒完 + 使用者沒指示 → **主動建議**「要不要 cancel?」、不擅自停

cancel 後 TG 會自動收到「❌ 子代理 X (cancelled)」通知、chat 不必再額外解釋
(push 由 backend 直接發、不繞 chat agent)。

重要警告(別亂提):cancel 不會立即停 docker exec、已 spawn 的 subprocess
會跑完 5-10 秒、但不影響 state — 對使用者來說等同停了。不要因此額外解釋細節、
混淆使用者。
""",
}


def get_help_doc(topic: str) -> str:
    """查 help doc。topic 不在表內 → 回可選 topic 列表 + 簡介。"""
    t = (topic or "").strip().lower()
    if t in HELP_DOCS:
        return HELP_DOCS[t]
    if not t:
        return (
            "可選 topic:\n"
            "  - chain   : 多階段子代理接力(dispatch follow_up 參數)\n"
            "  - files   : 讀子代理產物 / 傳檔到 TG(read_subagent_file / send_*)\n"
            "  - cancel  : 中止跑中的子代理(cancel_subagent_task)\n\n"
            "使用: read_help_doc('chain')"
        )
    valid = ", ".join(HELP_DOCS.keys())
    return f"❌ 未知 topic={t!r}。可選: {valid}"
