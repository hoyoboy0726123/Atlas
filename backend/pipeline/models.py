"""
Pipeline YAML 設定模型。

範例 YAML：
  pipeline:
    name: 每日資料處理
    steps:
      - name: 資料抓取
        batch: python fetch_data.py
        timeout: 300
        output:
          path: /data/raw.csv
          expect: "CSV 檔，至少 100 列，含 date、price 欄位"
        retry: 2

      - name: 資料分析
        batch: python analyze.py
        timeout: 600
        output:
          path: /data/report.xlsx
          expect: "Excel 檔，大小大於 10KB"
        retry: 1
"""
from typing import Optional, Union
import yaml
from pydantic import field_validator, BaseModel, ConfigDict, Field


class StepOutput(BaseModel):
    """輸出檔案的預期描述（用自然語言，LLM 負責驗證）"""
    path: Optional[str] = None
    expect: str = ""
    description: str = ""  # 同 expect 的別名，YAML 可用 description 代替
    ai_validation: bool = True  # YAML 可用 ai_validation: true 明確啟用
    skill_mode: bool = False  # True = 使用 Skill agent 主動驗證
    # 輸出 JSON Schema 合約(選填):輸出檔為 .json 時,先跑「確定性 schema 驗證」
    # (0 token、比 LLM 驗證更硬)。不過 → 直接 fail + 具體欄位錯誤(自癒可讀懂修正)。
    # 這是「弱模型也能穩」的結構化保證:生成端也會把 schema 塞進任務要求。
    json_schema: dict = {}

    @field_validator("json_schema", mode="before")
    @classmethod
    def _coerce_json_schema(cls, v):
        # 模型常把 schema 寫成帶引號的 JSON 字串(YAML 習慣),不該因此整份 YAML 被拒 → 自動解析
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return {}
            import json
            try:
                parsed = json.loads(v)
            except Exception as e:
                raise ValueError(f"json_schema 不是合法的 JSON 字串:{e}")
            if not isinstance(parsed, dict):
                raise ValueError("json_schema 必須是 JSON 物件")
            return parsed
        return v if v is not None else {}

    def get_expect(self) -> str:
        """取得驗證描述（優先 expect，fallback 到 description）"""
        return self.expect or self.description


class ComputerUseAction(BaseModel):
    """單一桌面自動化動作。
    type 決定其餘欄位的解讀方式：
      - click_image：image 指定要尋找的錨點圖（相對 assets_dir 的檔名），點中心
      - click_at：x/y 絕對座標點擊（少用，僅當錨點失效時備援）
      - type_text：text 輸入純文字
      - hotkey：keys 為組合鍵陣列（如 ["ctrl", "c"]）
      - wait：seconds 靜態等待
      - wait_image：等某張圖出現（含 timeout），常用於等載入完成
      - screenshot：存一張截圖到 assets_dir（方便事後除錯，不影響流程）
      - drag / scroll：拖曳 / 捲動
      - assert_image：驗證某錨點圖「當下」必須可見，否則步驟失敗（短 timeout）
      - assert_text：OCR 驗證螢幕上必須有某段文字，否則步驟失敗
      - activate_window：把指定標題的視窗切到前景（解決錄製回放時視窗不在前的常見問題）
      - if_image_found：條件分支 — 找到 image 就跑 then: 動作清單，否則跑 else:
      - retry_until：重複跑 do: 清單直到 until: 動作成功（處理按鈕要按多次、網路抖動等）
      - vlm_check：用 Settings 主模型（必須支援視覺）判斷螢幕當下是否符合 vlm_prompt 描述。
        純判斷不點擊；pass=False 步驟即失敗。可選 search_region 把截圖裁成關鍵區域省 token。
    """
    # YAML 會用 else: 這個 Python 保留字當 key，靠 pydantic alias 接回 Python 端的 else_
    model_config = ConfigDict(populate_by_name=True)

    type: str  # click_image | click_at | type_text | hotkey | wait | wait_image | screenshot | scroll | drag | assert_image | assert_text | activate_window | if_image_found | retry_until | vlm_check
    image: str = ""       # 主錨點圖檔名（相對 assets_dir）
    image2: str = ""      # 次錨點圖檔名（多錨點驗證用，選填）
    dx2: int = 0          # 次錨點相對點擊點的位移 x
    dy2: int = 0          # 次錨點相對點擊點的位移 y
    anchor_off_x: int = 0 # 點擊位置相對錨點影像中心的偏移 x（螢幕邊緣擷取時非 0）
    anchor_off_y: int = 0 # 點擊位置相對錨點影像中心的偏移 y
    # 全螢幕截圖（錄製當下的虛擬桌面全景，供手動圈選參考）
    full_image: str = ""  # full_NNN.png 檔名
    full_left: int = 0    # 虛擬桌面原點 X（副螢幕在左側時會是負值）
    full_top: int = 0     # 虛擬桌面原點 Y
    x: int = 0
    y: int = 0
    x2: int = 0           # drag 終點 X
    y2: int = 0           # drag 終點 Y
    text: str = ""
    keys: list[str] = []
    seconds: float = 0.0
    timeout_sec: float = 10.0  # wait_image 的最大等待秒數
    dy: int = 0                # scroll 動作：滾輪缺口數（正數上、負數下）
    confidence: float = 0.5    # 圖像比對相似度門檻 (0.0-1.0)；跟步驟層級 cv_threshold 寬鬆 tier 一致
                               # 實測錄製情境 0.5 對 DPI / 主題色 / hover 差異容忍度好，誤判仍可接受
    button: str = "left"       # click 按鈕：left/right/middle
    clicks: int = 1            # click 次數：1=單擊, 2=雙擊
    description: str = ""      # 使用者可讀的動作描述（給 UI 顯示）
    # ── 三層 fallback toggle (預設全 True = UIA → CV → 強制座標) ─────
    # 給「不會設定的使用者」最高命中率,進階使用者可關掉某層、自定義單一或組合:
    #   全勾 (預設):  UIA → CV → 強制座標 (三層自動 fallback)
    #   只勾 UIA:    UIA, 找不到立即 fail (嚴格 UIA 模式)
    #   只勾 CV:     CV, 找不到立即 fail (嚴格 CV 模式)
    #   只勾 座標:   直接點記錄座標 (= 舊 use_coord=True 短路行為)
    #   UIA+CV:      UIA → CV, 都失敗 fail
    #   UIA+座標:    UIA → 強制座標 (CV 跳過)
    #   CV+座標:     CV → 強制座標 (UIA 跳過、跟 OCR/VLM mode 一樣的尊重原則)
    # use_ocr=True 或 vlm_mode!=off 啟用時, OCR/VLM 自帶 primary 邏輯, 三個欄位不適用
    use_uia: bool = True       # UIA element 結構定位(錄製時 mouse-down 抓的 ui 欄位)
    use_cv: bool = True        # CV template matching(用錄製的 img_xxx.png 模板比對)
    use_coord: bool = True     # 強制座標最終 fallback(用錄製的 x,y 直接點)
    # 純 CV 模式(use_uia=False)下「CV 找不到 → 退錄製座標」的 action 層級開關。
    # 節點層級 cv_coord_fallback 預設 False 且只在 true 時寫進 YAML,改它的預設
    # 會讓所有舊節點的純 CV 動作一起開始亂點座標;錄製新動作改帶這個欄位。
    coord_fallback: bool = False
                               # 注意: 跟舊 use_coord 同欄位名、但語意改成「最終座標 fallback 啟用」
    hold_sec: float = 0.0      # click 按住不放的持續時間（> 0 會在回放時 mouseDown-sleep-mouseUp 取代瞬擊）
    modifiers: list[str] = []  # click 時按著的修飾鍵（如 ["ctrl"] 或 ["ctrl","shift"]）
    use_ocr: bool = False      # click_image 專用：顯式 OCR 啟用旗標。True 且 ocr_text 有值才跑 OCR
    ocr_text: str = ""         # OCR 目標文字（要跟 use_ocr=True 搭配才會生效）
    # OCR 搜尋範圍（per-action 藍框，虛擬桌面絕對座標）。width=0 表示沒自訂，
    # 回退使用 cv_search_radius 以紅十字為中心的預設區域
    ocr_box_left: int = 0
    ocr_box_top: int = 0
    ocr_box_width: int = 0
    ocr_box_height: int = 0
    # OCR 嚴格鎖定範圍：True = 框內找不到立即 fail（不退 phase2 附近、不退 phase3 全螢幕）
    # 用於「目標必須在固定位置才合法」的場景（例：通知必須在右下角才能點）
    # 預設 False = 寬容三階段 fallback（適用多數場景）
    ocr_strict_region: bool = False
    # activate_window 專用：至少要填 title 或 title_contains 其一
    title: str = ""              # 精確視窗標題比對
    title_contains: str = ""     # 視窗標題子字串比對（大小寫不敏感）
    # CV 搜尋矩形（per-action 紅框，虛擬桌面絕對座標）。格式 [left, top, width, height]。
    # 給定時覆蓋預設的「錄製座標 ±cv_search_radius」範圍搜尋，適用於：
    #   1. 目標區域大、半徑 400 不夠；2. 有多個相似 UI 元素要精準定位；3. 加速（更小區域 = 更快）
    # click_image / wait_image / assert_image 都支援
    search_region: list[int] = []
    # CV 嚴格鎖定範圍：True = 紅框內找不到立即 fail（不退附近、不退全螢幕、不退錄製座標）
    # 用於「目標必須在固定區域才合法」的場景；預設 False = 寬容三階段 fallback
    cv_strict_region: bool = False
    # ── 控制流巢狀動作（if_image_found / retry_until 用）─────────
    # 這些欄位刻意保留為 list[dict] / Optional[dict]，不做遞迴 pydantic 模型驗證，
    # 因為 execute_action 接收的是 dict；巢狀動作在執行時才逐一 .get() 讀取並驗證。
    # 優點：避免 pydantic 自我遞迴引用的 model_rebuild 麻煩；YAML 原始結構直通。
    then: list[dict] = []                       # if_image_found：找到時跑的子動作清單
    else_: list[dict] = Field(default_factory=list, alias="else")  # 找不到時跑的子動作清單
    do: list[dict] = []                          # retry_until：要反覆執行的動作清單
    until: Optional[Union[dict, str]] = None  # retry_until：檢查條件(dict) / uia_wait：appear、disappear、text_contains、text_equals(str)
    max_attempts: int = 3                        # retry_until：最多試幾輪
    wait_between_sec: float = 1.0                # retry_until：每輪之間等待秒數
    # ── vlm_check 專用：給 Settings 主模型（視覺）判斷的 prompt ──────
    # 不是讓 VLM 決定座標、不是讓它執行動作；只回傳 {"pass": bool, "reason": str}
    # 模型本身不支援視覺時，呼叫會直接報錯（不靜默 fallback）
    vlm_prompt: str = ""
    # ── UIA-first 三層 fallback(click_image / click_at 用)─────────
    # 錄製時(recorder.py)在 mouse-down 同時呼 uiautomation.ControlFromPoint 抓的元素資訊。
    # 回放時 computer_use Phase 0 先用這資訊找元素、找到就點(跨機台、跨 scaling 最穩);
    # 找不到才退到 CV 模板比對 → OCR → 強制座標的下層 fallback。
    # 跟下面 UIA 純粹型 action(uia_click/uia_send_keys)那組 control/save_as 等是兩回事 —
    # 那組是進階使用者手動透過 UIA Inspector 設定的、這組是錄製時自動抓的(無感)。
    ui: dict = {}                 # {"name", "control_type", "automation_id", "window_title", "rect"} 任一/全有
    # ── ocr_get_text 專用欄位(螢幕 OCR 抓「標籤旁邊的值」)──
    # ⚠ 這些欄位一定要在這裡宣告。ComputerUseAction 沒設 extra="allow",pydantic v2
    #   預設 extra="ignore" —— 未宣告的欄位在 runner 的 model_dump() 會**靜默消失**,
    #   面板試抓正常、YAML 存得下去,正式跑卻回「缺 label 欄位」;更糟的是 direction /
    #   kind 掉光會降回預設值(right / amount),讀到旁邊完全不同的數字還回報成功。
    label: str = ""              # 要找的標籤文字(例:總計金額)
    direction: str = "right"     # right 同列右側 / below 表格欄位在下方
    kind: str = "amount"         # amount 金額 / ident 單號統編 / any 只要含數字
    max_gap: int = 600           # 標籤與值的最大距離(px)
    lang_tag: str = ""           # OCR 語言標籤、留空走預設 zh-Hant-TW
    # ── type_text 專用 ──
    type_method: str = ""        # clipboard(預設、IME 免疫)/ keys(逐字打,某些欄位擋貼上時用)

    # ── UIA action 專用欄位(uia_click / uia_send_keys / uia_get_text 等用)──
    # control 識別:by Name / AutomationId / ControlType、可組合
    control: dict = {}           # {"type": "Button", "name": "儲存", "auto_id": "save-btn", "depth": 10}
    save_as: str = ""            # uia_get_text / uia_get_table_rowcount 把值存到此變數名、後續 step 可用 {{...}}
    # wait_download 用
    dir: str = ""                # 下載資料夾；空 = 使用者的 Downloads
    pattern: str = "*"           # 檔名 glob（例：PP_Component*.xlsx）
    # for_each 用
    items: Optional[Union[str, list]] = None  # 清單、或逗號/換行分隔字串、或 {{變數}}
    continue_on_error: bool = False           # 某筆失敗時跳下一筆繼續（預設整個中斷）
    split_as: str = ""                        # 一筆多欄位：變數名用 | 分隔（例：查詢年|查詢月）
    split_sep: str = "-"                      # 欄位分隔符（最後一個欄位吃剩餘部分）
    click_method: str = ""                    # uia_click："mouse" 跳過 pattern 強制滑鼠點（Tk 匿名鈕假接受 Invoke 用）
    row: int | str = 0           # uia_click_cell 用、可填字串(如 "{{row_count + 1}}")延後解析
    column: int | str = 0        # uia_click_cell 用
    check: str = ""              # uia_assert_state 用:exists / enabled / focused / checked
    window: str = ""              # action 層級 window 覆寫(無填走 step.uia_window)、支援 wildcard *
    rect: list[int] = []         # UIA picker 抓到的 element rect[x,y,w,h]、絕對桌面座標
                                  # control 沒 Name 也沒 AutomationId(如 generic PaneControl/GroupControl)時、
                                  # 用 ControlFromPoint(rect 中心)當 fallback 找元素

    # ── VLM 把關 Phase 1:每動作執行後驗證(跟 vlm_check 不同) ──────────
    # vlm_check 是「動作序列裡的 explicit 檢查步」、不點擊純判斷;
    # 這兩個欄位是「click/type/hotkey 等動作執行**之後**自動把前後截圖送 VLM 看
    # 是否符合 expected」、用來抓「點空了」「對話框沒開」等錄製座標漂移問題。
    # 觸發條件由 expected(有沒填) + step.cu_vlm_check_strategy + verify_critical 三個一起決定:
    #   strategy=off          → 永遠不驗
    #   strategy=after_each   → 每個 expected 非空的動作都驗
    #   strategy=critical_only → 只驗 expected 非空 AND verify_critical=True 的動作
    expected: str = ""           # 動作後預期狀態的自然語言描述(例「另存新檔對話框已開啟」)
                                  # 空字串 = 不對此動作做 VLM 驗證
    verify_critical: bool = False # True = strategy=critical_only 時也會驗
                                  # (讓使用者錄製時標出哪幾步絕對不能漂)
    # ── click_image 專用：VLM 輔助模式 ─────────────────────────────
    # 設計核心：永遠不讓 VLM 給座標 — 它只負責「決定要找的東西」，
    # 真正的點擊位置由既有的確定性管線（OCR / CV）算出
    #   "off"           → 不啟用 VLM（預設，走原本 OCR / 座標 / CV 三模）
    #   "description"   → 把 vlm_prompt 給 VLM，VLM 回螢幕上目標的實際文字 → OCR 找這段文字 → 點中心
    #   "anchor_pick"   → 把 vlm_anchors 列出的多張變體 + 螢幕送 VLM，VLM 挑哪張最像 → 用該張錨點走標準 CV 比對
    #   "grounding"     → 地端 GUI 定位模型（Mano-CUA 系）直接回座標並點擊。
    #                     上面兩種刻意不讓 VLM 給座標，因為通用雲端模型定位不準；
    #                     這個模式用的是**專門訓練過 GUI 定位**的地端模型，
    #                     2026-08-03 實測 14/14 命中、誤差中位數 4.5px（網頁 / Excel ribbon /
    #                     檔案總管都涵蓋）。失敗自動退回 CV → 座標，不會讓整步掛掉。
    #                     注意：同一顆模型「讀畫面文字」會編造，所以只拿它定位、不拿它讀字。
    vlm_mode: str = "off"
    # anchor_pick 模式的候選錨點圖檔名清單（每張都相對 assets_dir）。off 模式不用。
    vlm_anchors: list[str] = []

    # ── 多形態錨點（純 CV，不需要任何模型）─────────────────────────
    # 同一顆按鈕會隨狀態換樣子時（最大化↔還原、播放↔暫停、亮↔暗主題），
    # 錄製時把每種樣子各存一張，執行時**每張都比一次、取分數最高的**。
    # 取最高分而不是「第一張過門檻就用」：兩張候選都可能勉強過門檻，
    # 只有分數差距分得出來哪張是畫面當下真正的樣子。
    #
    # 2026-08-06 記事本標題列實測（UIA 給真值）：
    #   還原態找「最大化」鈕：單張錯形態誤差 311px(conf 0.63) → 多形態 0px(conf 1.00)
    #   最大化態找「還原」鈕：單張錯形態 conf 只有 0.59       → 多形態 0px(conf 1.00)
    # 這正是 anchor_pick 要解的問題，但純 CV 更快更準，而且不用打雲端 API。
    image_variants: list[str] = []


class PipelineStep(BaseModel):
    name: str
    batch: str = ""       # Shell 命令（skill_mode 時可為自然語言描述）
    working_dir: str = ""  # 工作目錄（run_python/run_shell 的 cwd）
    timeout: int = 300    # 秒
    output: Optional[StepOutput] = None
    retry: int = 1        # 自動重試次數（超過才問用戶）

    # 舊版前端把清空的數字欄位序列化成字面 "undefined" / 空值寫進 YAML，
    # 使用者按執行只會看到 pydantic 的 int_parsing 天書。這幾個字串絕不可能是
    # 使用者本意，安靜退回預設值；其他垃圾照樣報錯（誠實失敗）。
    @field_validator("timeout", "retry", mode="before")
    @classmethod
    def _coerce_broken_int(cls, v, info):
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "undefined", "null", "nan")):
            return 300 if info.field_name == "timeout" else 1
        return v
    # 此節點要用哪份 LLM 設定:"primary"(預設、走主模型)或 "secondary"(走副模型)
    # 副模型在設定頁設;若 secondary_provider 為空 → 自動 fallback primary
    llm_role: str = "primary"
    skill_mode: bool = False  # True = batch 為自然語言，由 LLM Skill agent 執行
    skill: str = ""            # 掛載的 Claude Code skill 名稱（~/.agents/skills/ 下的資料夾名）
    readonly: bool = False  # True = 唯讀驗證模式，禁止修改檔案
    ask_mode: bool = False  # True = 詢問模式：LLM 遇到任何不確定就主動用 ask_user 問用戶
    human_confirm: bool = False  # True = 人工確認節點，暫停等待確認
    message: str = ""            # 人工確認時的自訂訊息
    notify_telegram: bool = True  # 人工確認時是否發 Telegram
    screenshot: bool = False     # True = 暫停前自動截圖，附帶到 Telegram
    # True = 人工確認時，把「上一步驟 output.path 的檔案」render 成 PNG 一併傳到 TG
    # 預設 B1 路線：pandas / python-docx / python-pptx / pypdfium2 / PIL，不開真正的 App
    # 後備：若 B1 失敗且 host 裝了 libreoffice，用 libreoffice --headless 轉 PDF 再 render
    preview_prev_output: bool = False
    preview_timeout: int = 30    # 暫時保留欄位（libreoffice 轉檔超時秒數）
    # 人工確認節點：抵達時自動把上一步的輸出檔案傳到 Telegram（手機可下載）
    # False（預設）= 不自動傳；但 inline keyboard 仍有「📎 上一步輸出」按鈕、需要時點來抓
    send_prev_output: bool = False
    # 人工確認超時時自動行動(超時秒數沿用 step.timeout):
    # 'wait'(預設、保留現有行為:永遠等、即使 step.timeout 也忽略)
    # 'pass'(當作通過、繼續下一步)/'reject'(跳回上一步重做)/'abort'(終止 workflow)
    hc_on_timeout: str = "wait"

    # ── 背景 step(daemon / GUI app)──────────────────────────────────
    # True = 啟動 subprocess 後不等它 exit、直接跑下一個 step。
    # 用途:Script 開了一個 GUI / server / daemon 進程、永遠不結束、
    #       但後續 step 需要它持續活著(例如 UI 自動化點按那個視窗)。
    # 設 True 後:
    #   - runner 不等 process exit、繼續下一個 step
    #   - subprocess 由 runner 接管、workflow 結束時自動 kill 釋放資源
    # 預設 False = 跟現有行為一致(等到 exit 才下一步)。
    # 進階(選填):啟動後等 N 秒讓 daemon ready、再下一步(預設 0 = 不等)。
    background: bool = False
    ready_after_seconds: int = 0
    # background_keep:背景模式下,workflow 結束是否「保留」進程(不自動 kill)。
    # 預設 True = 保留進程留在桌面供手動操作;設 False 才在結束時自動 kill。
    # abort(手動中止)一律強制 kill,不受此旗標影響。
    background_keep: bool = True
    # ── 控制流:任意 step 用 next 顯式跳到下一個 step ── Ticket 2 ───
    # 一般 step 跑完依 YAML 順序前進;設了 next 就改跳到指定 step name。
    # 特殊值 "end" / "__end__" / "" → 結束流程(預設行為:空字串 = 線性下一個)
    # 主要用途:condition 節點分支 A、B 各自跑完後跳到 "end" 避免再跑到對方 branch:
    #   - name: branch_true
    #     batch: ...
    #     next: end          ← 跑完直接結束、不會線性掉到 branch_false
    next: str = ""

    # ── 控制流節點:condition(IF / Switch)── Ticket 2 ────────────────
    # 純 metadata 節點、不執行任何命令。runner 求值 expression / switch、
    # 根據結果跳到指定的下游 step。MAX_VISITS=1000 防無限迴圈。
    #
    # IF 模式(填 expression + on_true / on_false):
    #   expression: "{{ steps.X.output.rows | int > 100 }}"   ← Jinja2 boolean 表達式
    #   on_true: bulk_step    ← 條件成立時跳到的 step name
    #   on_false: single_step ← 條件不成立時跳到的 step name(留空 = 流程結束)
    #
    # Switch 模式(填 switch + cases、忽略 expression):
    #   switch: "{{ steps.api.output.status }}"
    #   cases: { "200": ok_step, "404": retry_step, "500": fail_step }
    #   default: fail_step    ← 沒命中任何 case 時的 fallback(留空 = 流程結束)
    condition: bool = False        # True = 此節點為 condition 節點
    expression: str = ""           # IF 模式:Jinja2 boolean expression
    on_true: str = ""              # IF 模式:條件成立要跳的 step name
    on_false: str = ""             # IF 模式:條件不成立要跳的 step name(留空 = end)
    switch: str = ""               # Switch 模式:Jinja2 expression、求值後 str() 比對 cases keys
    cases: dict = {}               # Switch 模式:{case_value: step_name}
    default: str = ""              # Switch 模式:沒命中時跳的 step name(留空 = end)

    # ── 桌面自動化節點(computer_use)────────────────────────────────
    # 此為獨立第 4 種節點，不與 skill / script / human_confirm 混用。
    # 當 computer_use=True 時，runner 走桌面自動化引擎（pyautogui + cv2 比對），
    # 完全跳過 LLM 與 recipe 系統。
    computer_use: bool = False   # True = 桌面自動化節點
    cu_mode: str = "pixel"       # "pixel" = 錄製座標 + CV/OCR/VLM(現況、預設);"uia" = UIA 控制
                                  # 兩種模式 actions[] 共用、實際分派依 action.type 走

    uia_window: str = ""         # cu_mode=uia 時用、視窗 title 比對(支援 wildcard *)、空字串 = foreground
    actions: list[ComputerUseAction] = []  # 錄製/手編的動作序列
    assets_dir: str = ""         # 錨點圖片資料夾（相對路徑掛到工作流目錄下）
    fail_fast: bool = True       # True = 任一動作失敗立即中止；False = 警告後繼續
    # ── CV 比對設定（套用到本節點所有 click_image/drag 動作）──────────
    cv_threshold: float = 0.5    # 比對門檻：0.50 寬鬆 / 0.80 標準 / 0.90 嚴格
    cv_search_only_near: bool = False  # True = 只在錄製座標附近搜尋，不擴大到全螢幕
    cv_search_radius: int = 400  # 附近搜尋半徑（像素）；實際搜尋範圍為 (2r × 2r)
    cv_trigger_hover: bool = True  # True = 比對前先把游標移到錄製座標並等，讓 Windows hover 效果出現
    cv_hover_wait_ms: int = 200    # hover 等待時間：200（快）/ 400（保險，Windows 部分動畫較慢）
    cv_coord_fallback: bool = False # True = CV 完全找不到時退回錄製座標硬點下去；False（預設）= 失敗就 FAIL 不亂點
    # ── VLM 把關 Phase 1:節點層級設定(每動作 expected 走 ComputerUseAction.verify_after)──
    # 設計目的:錄製座標確定性主路徑 + AI 驗證層、99% 失敗從「整套悶著錯」變「立刻發現+人介入」
    cu_vlm_check_strategy: str = "off"   # off / after_each / critical_only
                                          # off          = 完全關 VLM 驗證(現況、預設)
                                          # after_each   = 每個有 expected 的動作都驗
                                          # critical_only = 只驗 verify_critical=True 的動作
    cu_on_mismatch: str = "stop_notify"  # stop_notify / retry_once / skip_and_continue
                                          # stop_notify       = 立即停 + push TG + pipeline awaiting_human(預設、最安全)
                                          # retry_once        = 重試一次同動作、仍失敗才 stop_notify
                                          # skip_and_continue = 警告但繼續(用於非關鍵步、容忍偏離)
    cu_vlm_provider: str = ""            # 空字串 = 跟 settings.model 同(自動推斷);
                                          # 也可指定 "anthropic" / "openai"
    cu_vlm_max_retries: int = 1          # retry_once 模式下最多重試幾次
    # ── OCR 比對設定 ──────────────────────────────────────────────────
    ocr_threshold: float = 0.6     # OCR 最小 confidence：低於這數字視為沒匹配到
                                   # 分級: 1.0 精確 / 0.9 target⊆word / 0.8 跨詞行層級 / 0.6 模糊
    ocr_cv_fallback: bool = False  # True = OCR 失敗時退到 CV 比對鏈（再受 cv_coord_fallback 接棒）；False（預設）= 失敗就 FAIL
    # ── 視覺驗證節點（visual_validation）─────────────────────────────
    # 獨立節點類型：不執行命令，純判斷。3 種來源餵給 Settings 主模型（必須支援視覺）：
    #   prev_output_file → 直接用上一步 output.path 檔案（圖檔直送 VLM；非圖檔先 render_file_preview 轉 PNG）
    #   rendered_preview → 一律走 render_file_preview（多 sheet 的 xlsx 會回多張 PNG，全部送 VLM）
    #   current_screen   → 即時 mss 抓螢幕（搭配 vv_search_region 可裁切關鍵區域）
    # VLM 回 {"pass": bool, "reason": str}；pass=false 步驟即失敗，retry 邏輯沿用既有
    visual_validation: bool = False    # True = 視覺驗證節點
    vv_source: str = "prev_output"     # prev_output | current_screen
                                       # （早期值 prev_output_file / rendered_preview 仍受相容處理）
    vv_prompt: str = ""                # 描述「應該看到什麼」的判斷條件（必填）
    vv_search_region: list[int] = []   # current_screen 用：[left, top, width, height] 絕對桌面座標
    # ── Outlook 自動化節點（outlook_automation）──────────────────────
    # 獨立節點類型：透過 pywin32 + Outlook COM 處理寄信 / 收信 / 行事曆 / 附件等。
    # 強制 host 執行（sandbox 沒 pywin32 + 沒 Outlook profile），由 runner 路由處理。
    # Agent 限定使用 win32_helpers + 允許清單套件（見 win32_agent_config.py）。
    outlook_automation: bool = False   # True = Outlook 自動化節點
    outlook_template: str = ""         # 選單模板 ID（前端選了哪個模板；空字串 = 自由輸入）
                                       # 例：daily_todo / search_summary / send_mail / calendar_list ...
    outlook_params: dict = {}          # 模板參數（subject、sender、since、until、to、folder 等
                                       # 由前端依模板填入，後端組進 prompt 給 agent）
    # ── 網頁爬蟲節點（web_crawler）──────────────────────────────────
    # 獨立節點類型：丟一個 URL 進去，吐 markdown + frontmatter 出來給後續 skill 節點吃。
    # 執行流程：Tier 1 = 沙盒內 Crawl4AI（Playwright + Chromium）；偵測到 Cloudflare
    # 擋下時 fallback Tier 2 = host 端打 FlareSolverr（port 8191、用 Puppeteer 解 CF challenge）。
    # 不進 LLM、不進 recipe；輸出格式為 LLM-friendly markdown，下個 skill 節點直接讀 outputPath。
    # ── Subagent 節點（subagent）────────────────────────────────────
    # 獨立節點類型：跟 skill 一樣是 agent loop（多輪 LLM + tool call），但：
    #   1. system prompt 由 subagent_role 決定（data_analyst / coder / researcher / critic / planner）
    #   2. 工具白名單按 role 過濾（critic 只能 read_file、planner 只能 done）
    #   3. 跳過 recipe cache（多輪結果非確定性、cache 命中率低）
    #   4. 跳過 AI validator（loop 內已自我驗證）
    # 適合：探索性、結構不固定的任務（研究 / debug / 寫稿）
    # 不適合：每天跑相同邏輯的固定任務（用 skill 節點 + recipe 即可、零 token）
    subagent: bool = False
    subagent_role: str = "data_analyst"   # data_analyst | coder | researcher | critic | planner
    subagent_max_iter: int = 10            # 最多 LLM 輪數上限(含 tool call 來回);實測 5 對非簡單任務太低

    # ── MCP 節點（mcp）─────────────────────────────────────────────
    # 獨立節點類型：連到一個 stdio MCP server、呼叫指定 tool，把結果帶回 pipeline。
    # 一個節點就繼承整個 MCP 生態（GitHub / Slack / Notion / DB / filesystem …）。
    # 不進 LLM、不進 recipe（呼叫外部 server、結果非確定性）。
    # mcp_tool_args 的值支援 {{ }} 表達式（引用 input / 上游 step output）。
    mcp: bool = False
    mcp_server: str = ""               # 已安裝 server 的註冊名(建議用法;系統自動帶 command/args/env)
    mcp_command: str = ""              # [進階] 直接指定 server 啟動指令(不走註冊表時用)
    mcp_args: list[str] = []           # [進階] server 參數，如 ["-y","@modelcontextprotocol/server-filesystem","/data"]
    mcp_tool: str = ""                 # 要呼叫的 tool 名稱
    mcp_tool_args: dict = {}           # tool 參數（值可含 {{ steps.x.output }} / {{ input.y }}）
    mcp_env: dict = {}                 # 傳給 server 的環境變數（放 API key 等）
    mcp_timeout: float = 60.0          # 單次呼叫逾時秒數

    web_crawler: bool = False          # True = 網頁爬蟲節點
    # 模式由前端明確選擇（不自動偵測），決定走哪條路徑：
    #   "web"   → Crawl4AI（網頁 → markdown）；填 wc_url + wc_* 欄位
    #   "video" → yt-dlp（YouTube/Vimeo/Bilibili 等 → mp4 + 字幕 + metadata）；填 wc_video_url + wc_video_* 欄位
    # 兩個模式各自獨立的 URL 欄位（user 不會誤把 YT 連結貼到網頁區、反之亦然）
    wc_mode: str = "web"
    # ── 網頁模式 ────────────────────────────────────────────────────
    wc_url: str = ""                   # [向後相容] 單 URL 欄位；wc_urls 為空時用這個
    wc_urls: list[str] = []            # 多 URL 列表；非空時走多 URL 模式（output.path 視為資料夾）
    wc_js_render: bool = True          # 啟用 JS 渲染（SPA 必須 True；純靜態站關掉省時間）
    wc_wait_for_selector: str = ""     # 等指定 CSS selector 出現再抓（避免抓到還沒載完的頁面）
    wc_cloudflare_fallback: bool = True  # Tier 1 被 CF 擋時自動 fallback FlareSolverr
    wc_cookies: str = ""               # 登入 cookies（key=value 一行一個 / 整串 Cookie 標頭 / JSON 陣列）
    wc_interactions: list[dict] = []   # JS 互動序列：[{type:click, selector:".x"} / {type:scroll, to:bottom}
                                       # / {type:wait, seconds:2} / {type:wait_for, selector:".y"}
                                       # / {type:type, selector:"input", text:"foo"}]
    wc_download_assets: bool = False   # True = 把 markdown 裡圖片 / PDF 連結都下載到 assets/
    # ── 智慧滾動（取代以前寫死的「滾 2 次就停」） ────────────────────
    # 預設行為：自動滾到 scrollHeight 不再變大為止（最多 10 次 / 60 秒上限）
    # 進階使用者可指定其中一個（同時填的話 scroll_count 優先）：
    wc_scroll_count: int = 0           # 0 = 自動偵測；> 0 強制滾動 N 次後停
    wc_target_post_count: int = 0      # 0 = 不設目標；> 0 = 滾到頁面出現至少 N 個貼文連結後停（仍受最大次數/時間上限保護）
    # ── 論壇 / 列表模式（同時抓子頁）────────────────────────────────
    # 開啟後 web_crawler 變成「先抓列表頁 → 抽前 N 個子頁連結 → 並行抓子頁 →
    # 合併成單一 markdown」的整套流程。下游 skill 節點直接讀此檔做摘要、
    # 不用自己寫 crawl4ai 程式（之前 LLM 寫程式時容易 hardcode 答案的雷）
    wc_with_children: bool = False     # True = 開啟論壇 / 列表模式（自動抓子頁）
    wc_child_link_pattern: str = ""    # 空字串 = auto（內建 12 種常見 pattern：Reddit/PTT/Dcard/HN 等）
                                       # 也可填 regex（如 r"/articles/\d+"）覆寫
    wc_max_children: int = 10          # 最多抓幾個子頁；對齊 wc_target_post_count 預設值
    # ── 影片模式（yt-dlp）─────────────────────────────────────────
    wc_video_url: str = ""             # YouTube / Vimeo / Bilibili / 等 yt-dlp 支援的影音站 URL
    wc_video_quality: str = "720p"     # best / 1080p / 720p / 480p / 360p（決定 yt-dlp -f 過濾條件）
    wc_video_max_filesize_mb: int = 500   # 單檔上限；超過跳過、不下載半套再 cleanup
    wc_video_max_duration_min: int = 30   # 影片長度上限（分鐘）；0 = 不限
    wc_video_subs: bool = True            # 是否下載字幕（如果有的話；含 auto-generated）
    wc_video_subs_langs: str = ""         # 字幕語言偏好（逗號分隔），空字串 = 預設「zh-TW,zh-Hant,zh-CN,zh-Hans,en」
    wc_video_save_info_json: bool = False  # 是否寫 video.info.json（yt-dlp 完整 metadata dump）；預設 OFF
                                           # 90% 內容是簽名 URL / 格式列表 / headers，6 小時後失效；
                                           # 真要用的（chapters / tags / 全長描述 / 縮圖）勾起來才會落地


class PipelineConfig(BaseModel):
    name: str
    steps: list[PipelineStep]
    validate: bool = True  # False = 跳過 LLM 驗證，僅靠 exit code

    @classmethod
    def from_yaml(cls, path: str) -> "PipelineConfig":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        # 支援頂層有 "pipeline:" 或直接是 {name, steps}
        raw = data.get("pipeline", data)
        filtered = {k: v for k, v in raw.items() if not k.startswith("_")}
        return cls(**filtered)

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineConfig":
        # 過濾掉非 schema 的內部旗標（如 _use_recipe）
        filtered = {k: v for k, v in data.items() if not k.startswith("_")}
        return cls(**filtered)
