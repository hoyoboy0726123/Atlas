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

    "computer_use": """\
# computer_use 節點(桌面自動化)— 動作全集與組合寫法

節點層設定:
```yaml
- name: 操作表單
  computer_use: true
  cu_mode: uia                 # uia = 讀 GUI 結構(推薦);pixel = 錄製座標 + CV/OCR
  uia_window: "*E-Quote*"      # 目標視窗(支援 * 萬用;動作可用自己的 window 覆蓋)
  fail_fast: true              # 任一動作失敗立即中止
  actions: [ ... ]
```
- **視窗 pattern 用 `*關鍵字*`**,別綁完整標題 —— Edge 標題含「和其他 N 個頁面」,
  分頁數一變就找不到。
- 跨視窗流程:各動作自帶 `window`(讀 A 視窗、填 B 視窗),不必拆成兩個節點。
- `control` 用 `{auto_id: "..."}` 最穩;只有 `name` 也可以,`name` 支援萬用字元
  (`name: "資料處理中*"`)。auto_id / name **必須由使用者在 UIA Inspector 挑選取得,你猜不到**。

## 動作型別(全部,不要發明清單外的名字)
| 動作 | 做什麼 | 關鍵欄位 |
|---|---|---|
| `uia_click` | 點控制項 | `control`;Tk 等假接受點擊的按鈕加 `click_method: "mouse"` |
| `uia_send_keys` | 填文字 / 送按鍵 | `text`(可含 `{{var}}`)或 `keys` |
| `uia_select` | 下拉選單選項 | `text`(選項文字,一字不差,可含 `{{var}}`) |
| `uia_get_text` | 讀控制項的值 → 變數 | `save_as` |
| `uia_wait` | 等畫面狀態 | `until`: appear / disappear / text_contains / text_equals、`timeout_sec` |
| `if_element_found` | 元素在不在 → 走 then / else | `control`、`timeout_sec`(探測秒數)、`then: [...]`、`else: [...]` |
| `wait_download` | 等下載資料夾出現寫完的新檔 | `pattern`(如 `PP_*.xlsx`)、`timeout_sec`、`save_as`(存完整路徑) |
| `for_each` | 清單逐筆迴圈 | `items`、`save_as`、`continue_on_error`、`split_as`、`do: [...]` |
| `uia_get_clipboard` / `uia_set_clipboard` | 讀 / 寫剪貼簿 | `save_as` / `text` |
| `activate_window` | 把視窗拉到前景(喚醒睡眠分頁) | `title_contains`(關鍵字,可含 `*`) |
| `wait_text` / `if_text_found` | OCR 版的等待 / 分歧 | `text`、`until`、`then`/`else` |
| `wait_image` / `if_image_found` | CV 版的等待 / 分歧 | `image`、`until: disappear` 可等它消失 |
| `ocr_get_text` | OCR 讀標籤旁的值 | `label`、`direction`、`kind`、`save_as` |
| `uia_get_table_rowcount` / `uia_click_cell` | 表格列數 / 點格子 | `save_as` / `row`、`column` |
| `uia_wait_enabled` / `uia_assert_state` / `uia_close_window` | 等可用 / 驗狀態 / 關視窗 | `timeout_sec` / `check` / `window` |
| `wait` / `type_text` / `hotkey` / `click_image` / `click_at` | 固定等待 / 打字 / 快捷鍵 / 錄製點擊 | `seconds` / `text` / `keys` / `image` / `x`,`y` |
⚠ 填值用 `uia_send_keys`,**沒有** `uia_set_text` / `uia_set_value`。

## 「查詢時間不固定」→ uia_wait,不要寫死 wait 秒數
```yaml
- {type: uia_wait, control: {type: Text, name: "資料處理中*"}, until: appear, timeout_sec: 10}
- {type: uia_wait, control: {type: Text, name: "資料處理中*"}, until: disappear, timeout_sec: 300}
```
- `until` 條件不成立會等到逾時才誠實失敗,所以「**不一定會出現**的東西一律用 `if_element_found`
  探測,不要用 `uia_wait` 等它出現」—— 查無資料時遮罩根本不出現,uia_wait 會判失敗、後面連環壞。
- 只等 disappear 有競態(按下按鈕後遮罩還沒 render 就檢查 → 誤判已消失);保險寫法是
  「探測出現 → 出現才等消失」:
```yaml
- type: if_element_found
  control: {type: Text, name: "資料處理中*"}
  timeout_sec: 3
  then:
    - {type: uia_wait, control: {type: Text, name: "資料處理中*"}, until: disappear, timeout_sec: 300}
  else: []
```
- `timeout_sec` 在 `if_element_found` 上是「最多探測幾秒」:元素一出現就馬上走 then,
  沒出現就等滿再走 else;**探測不到不算失敗,else 為空就直接繼續下一個動作**。

## 「可能跳對話框、可能直接下載」的分歧
```yaml
- {type: uia_click, control: {name: "匯出/Export"}}
- type: if_element_found              # 「查無資料」對話框有出現嗎?
  control: {type: Button, name: "確定"}
  timeout_sec: 3
  then:                                # 出現 → 按掉、繼續下一筆
    - {type: uia_click, control: {type: Button, name: "確定"}}
    # 剛彈出的對話框會吃掉點擊(回報成功卻沒關)—— 點完必須等它消失驗證
    - {type: uia_wait, control: {type: Button, name: "確定"}, until: disappear, timeout_sec: 5}
  else:                                # 沒出現 → 等下載完成
    - {type: wait_download, pattern: "PP_Component*.xlsx", timeout_sec: 300, save_as: 下載檔}
```
`wait_download` 只認「動作開始後新出現、且寫完」的檔案(排除 .crdownload 半成品、大小穩定才算);
`dir` 空值 = 使用者的 Downloads 資料夾。對話框的「確定」在 Inspector 要**取消「只看網頁內容」**才找得到。

## 多筆逐一查詢 → for_each,不要複製 N 份動作
```yaml
- type: for_each
  items: "UX3407%, RC71L%, GU605%"   # 逗號或換行分隔;或 {{ input.品規清單 }};或畫面讀下來的變數
  save_as: 品規                       # 每輪把當前值放進 {{品規}};{{品規_序號}} 是第幾筆(1 起算)
  continue_on_error: true            # 某筆失敗跳下一筆(預設 false = 整個中斷)
  do:
    - {type: uia_send_keys, control: {auto_id: spec}, text: "{{品規}}"}
    - {type: uia_click, control: {name: "匯出/Export"}}
    - ...(等待 → 分歧)
```
一筆帶多個值(當月 + 上月的年月對,跨年時年份不同)用 `split_as` 拆欄位,月份當外層、品規當內層:
```yaml
- type: for_each
  items: "{{ now.year }}-{{ now.month }}, {{ now.next_month_year }}-{{ now.next_month }}"
  save_as: 期間
  split_as: "查詢年|查詢月"          # 每筆按 split_sep(預設 -)拆開依序存進變數;最後一欄吃剩餘部分
  do:
    - {type: uia_select, control: {auto_id: year},  text: "{{查詢年}}"}
    - {type: uia_select, control: {auto_id: month}, text: "{{查詢月}}"}
    - {type: for_each, items: "{{清單原文}}", save_as: 品規, continue_on_error: true, do: [...]}
```
巢狀迴圈裡的 `save_as` 變數可以用雙序號命名:`save_as: "下載檔{{期間_序號}}_{{品規_序號}}"`。

## 清單在 Tkinter 工具裡 → 剪貼簿交接
Tk 沒有 UIA provider,內部控件讀不到(整個視窗是空白 Pane)。工具端做一顆「複製清單」鈕
(勾選項目一行一筆放進剪貼簿),Atlas 端:
```yaml
- {type: activate_window, title_contains: "那隻工具"}
- {type: uia_click, control: {type: Button}, rect: [x, y, w, h], click_method: "mouse"}   # 或 click_image
- {type: wait, seconds: 0.5}
- {type: uia_get_clipboard, save_as: 清單原文}   # 剪貼簿空的會誠實報錯
- {type: for_each, items: "{{清單原文}}", save_as: 品規, do: [...]}
```
Tk 的按鈕會**假接受**一般 UIA 點擊(回報成功、實際沒觸發),所以 `uia_click` 要加
`click_method: "mouse"` 強制滑鼠真點(執行時會自動把目標視窗拉到最上層)。

## 瀏覽器「睡眠分頁」與喚醒
目標網頁閒置太久(小時級)Edge 會卸載整頁,UIA 找得到視窗卻讀不到元素。系統已有自動救援:
填值 / 點擊 / 讀值找不到元素時會自動把視窗拉到前景再試一次。但 **`uia_wait` 等待、`if_*` 探測、
CV 點擊、OCR 讀字**對睡死的視窗會誤判(空樹被當成「元素不在」走錯分支)——流程碰該視窗的
第一個動作是這幾種時,前面加 `activate_window` + `wait 1.5`。`title_contains` 填頁面標題關鍵字
(如 `SCM Portal`),不要貼完整標題。

## UIA 讀不到的畫面(Canvas 繪製、遠端桌面)
用 CV / OCR 替代:`wait_image`(等錨點圖出現 / `until: disappear`)、`wait_text`(OCR 等文字)、
`if_image_found` / `if_text_found`(同樣 then / else)。能用 UIA 就用 UIA —— 快、準、不受遮擋。
""",

    "variables": """\
# 變數與傳值(computer_use 動作 / 跨節點 / 日期)

## 同一個 computer_use 節點內
取值動作設 `save_as: 金額` → 之後的動作用 `{{金額}}`。**取值動作必須排在使用它的動作之前**,
順序錯了變數是空的、而且不報錯。`for_each` 的 `save_as` 同理,並多一個 `{{<變數>_序號}}`(第幾筆,1 起算)。

## 跨節點
`{{ steps.<步驟名>.output.<變數名> }}` —— computer_use 的 `save_as` 變數會自動晉升成
`steps.<步驟名>.output.<變數名>`;步驟名一字不差(改名會全斷)。

## 日期命名空間 `{{ now.* }}`(排程報表選當月 / 上月用)
全部是**補零字串**,下拉選單的選項文字直接對得上:
- `now.year`(2026)、`now.month`(09)、`now.day`、`now.date`(2026-09-18)
- `now.prev_month`(08)、`now.prev_month_year`、`now.next_month`(10)、`now.next_month_year`
  (跨年時給對應年份:12 月的 next_month 是 01、next_month_year 是明年)
可直接寫在動作的 text / items 裡:`{type: uia_select, control: {auto_id: month}, text: "{{ now.month }}"}`。

## 其他來源
- `{{ input.名稱 }}`:啟動工作流時傳入的參數(執行對話框會列出要填的欄位;排程 / webhook 帶 input_params)。
- `{{ secrets.名稱 }}`:設定頁 Secrets 保險箱,值不會出現在 log。

## 常見錯誤
- 變數名只能中英數與底線;單層大括號 `{金額}` 無效,會被字面填進欄位。
- 同節點內不要用跨節點語法(該步驟還沒跑完、output 不存在)。
- 不確定有哪些變數 → 先呼叫 `list_workflow_variables`,不要猜名字。
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
            "  - cancel  : 中止跑中的子代理(cancel_subagent_task)\n"
            "  - computer_use : 桌面自動化動作全集與組合寫法(等待 / 分歧 / 迴圈 / 剪貼簿 / 喚醒)\n"
            "  - variables : 變數與傳值(now.* 日期 / _序號 / 跨節點 / input / secrets)\n\n"
            "使用: read_help_doc('chain')"
        )
    valid = ", ".join(HELP_DOCS.keys())
    return f"❌ 未知 topic={t!r}。可選: {valid}"
