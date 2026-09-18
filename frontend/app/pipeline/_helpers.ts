import type { Node, Edge } from '@xyflow/react'

// ── 資料型別 ─────────────────────────────────────────────────────────────────

/** 腳本節點：執行用戶寫好的腳本或指令 */
export interface StepData extends Record<string, unknown> {
  name: string
  batch: string
  workingDir: string
  outputPath: string
  expect: string
  skillMode?: boolean   // optional — 僅在 YAML 序列化時使用，節點類型由 node.type 決定
  // 「output 層的 skill_mode」：用 deep 驗證（validate_step_with_skill）。
  // 純 skill 節點不需要這個（後端看 expect 有沒有填來決定深淺）；
  // 只有 script 節點 + AI 驗證節點勾「Skill 模式」時才會被設成 true。
  expectSkillMode?: boolean
  readonly?: boolean    // optional — skill 唯讀驗證模式
  skill?: string        // optional — 掛載的 Claude Code skill 名稱
  askMode?: boolean     // optional — 詢問模式（LLM 積極問使用者）
  humanConfirm?: boolean           // optional — 人工確認步驟
  humanConfirmMessage?: string     // optional — 確認訊息
  humanConfirmNotifyTelegram?: boolean  // optional — 是否 Telegram 通知
  humanConfirmScreenshot?: boolean     // optional — 是否自動截圖
  humanConfirmPreview?: boolean        // optional — 是否 render 上一步驟輸出檔案預覽
  humanConfirmSendPrevOutput?: boolean // optional — 抵達節點時自動把上一步輸出檔當 document 傳到 TG
  hcOnTimeout?: 'wait' | 'pass' | 'reject' | 'abort'  // 超時後行動,預設 wait = 永遠等
  // 背景模式(Script 開 daemon / GUI 用):啟動後不等 exit、立即下一步、subprocess 由 runner 接管
  background?: boolean
  background_keep?: boolean   // 背景模式下:workflow 結束「不」自動 kill、進程留在桌面供手動操作
  readyAfterSeconds?: number   // 背景啟動後等 N 秒讓 daemon ready 再下一步,預設 0
  // 桌面自動化節點（computer_use）
  computerUse?: boolean                  // optional — 桌面自動化步驟
  computerUseActions?: ComputerUseAction[]  // optional — 動作序列
  computerUseAssetsDir?: string          // optional — 錨點圖片資料夾
  computerUseFailFast?: boolean          // optional — 遇錯立即中止
  cvThreshold?: number                   // CV 比對門檻：0.50 寬鬆 / 0.80 標準 / 0.90 嚴格
  cvSearchOnlyNear?: boolean             // true = 只搜錄製座標附近
  cvSearchRadius?: number                // 附近搜尋半徑（px），預設 400
  cvTriggerHover?: boolean               // true = 比對前先觸發 hover 效果（匹配錄製時的 hover 狀態）
  cvHoverWaitMs?: number                 // hover 等待時間（ms）：200 或 400
  cvCoordFallback?: boolean              // true = CV 失敗時退回錄製座標硬點（預設 false = 失敗就停）
  ocrThreshold?: number                  // OCR 最小 conf 門檻（預設 0.6）
  ocrCvFallback?: boolean                // true = OCR 失敗接著 CV 比對（預設 false = 失敗就停）
  // VLM 把關 Phase 1（每動作後驗證 expected outcome）
  cuVlmCheckStrategy?: 'off' | 'after_each' | 'critical_only'   // 預設 off
  cuOnMismatch?: 'stop_notify' | 'retry_once' | 'skip_and_continue'  // 預設 stop_notify
  cuVlmMaxRetries?: number                // retry_once 模式重試上限（預設 1）
  // UIA 模式
  cuMode?: 'pixel' | 'uia'                // 預設 pixel
  uiaWindow?: string                       // 視窗 title pattern(支援 *)
  // 視覺驗證節點（visual_validation）
  visualValidation?: boolean             // optional — 視覺驗證步驟
  vvSource?: 'prev_output' | 'current_screen'
  vvPrompt?: string
  vvSearchRegion?: number[]              // [left, top, width, height]，空陣列 = 看整個螢幕
  // Subagent 節點（subagent）
  subagent?: boolean                     // optional — Subagent 步驟（多輪 LLM agent loop）
  subagentRole?: string                  // data_analyst | coder | researcher | critic | planner
  subagentMaxIter?: number               // 最多 LLM 輪數上限（預設 5）
  // MCP 節點(mcp)— 呼叫已安裝 MCP server 的 tool
  mcp?: boolean
  mcpServer?: string                     // 註冊名(設定頁 MCP 專區安裝的 server)
  mcpTool?: string
  mcpToolArgsText?: string               // inline JSON 或多行 YAML 塊(原樣保真)
  // 輸出 JSON Schema 合約(output.json_schema、inline JSON 字串)
  jsonSchemaText?: string
  // Condition 節點(Ticket 2 控制流)— 純 metadata、runner 跳轉用、不執行命令
  condition?: boolean
  expression?: string                    // IF mode
  onTrue?: string
  onFalse?: string
  switch?: string                        // Switch mode
  cases?: Record<string, string>
  default?: string
  // 跳轉:任意 step 跑完跳指定下一步(end / __end__ / 空 = 線性)
  next?: string
  // 此節點用哪份 LLM 設定:"primary"(預設、走主模型)或 "secondary"(走副模型)
  // 副模型在設定頁設;副模型未設則自動 fallback 主
  llmRole?: 'primary' | 'secondary'
  // Outlook 自動化節點（outlook_automation）
  outlookAutomation?: boolean            // optional — Outlook 自動化步驟
  outlookTemplate?: string               // 選單模板 ID（空字串 = 自由輸入需求）
  outlookFreeText?: string               // 自由輸入需求（template 為空時用）
  outlookParams?: Record<string, unknown> // 模板參數（subject/sender/since/until/to ...）
  // 網頁爬蟲節點（web_crawler）
  webCrawler?: boolean                   // optional — 網頁爬蟲步驟
  wcMode?: 'web' | 'video'               // 模式：web=Crawl4AI / video=yt-dlp
  wcUrl?: string                         // [web mode] 向後相容單 URL；wcUrls 為空時用這個
  wcUrls?: string[]                      // [web mode] 多 URL 列表；非空時走多檔輸出
  wcJsRender?: boolean                   // [web mode] 啟用 JS 渲染（SPA 必須開）
  wcWaitForSelector?: string             // [web mode] 等指定 CSS selector 出現再抓
  wcCloudflareFallback?: boolean         // [web mode] CF 偵測到時 fallback FlareSolverr
  wcCookies?: string                     // 登入 cookies（多種格式可接，兩個模式共用）
  wcInteractions?: WebCrawlerAction[]    // [web mode] JS 互動序列
  wcDownloadAssets?: boolean             // [web mode] 把附件下載到 assets/
  // 智慧滾動（取代寫死的「滾 2 次」）— 預設自動偵測 scrollHeight 不變才停
  wcScrollCount?: number                 // [web mode] 0=自動 / >0=固定滾 N 次
  wcTargetPostCount?: number             // [web mode] 0=不設目標 / >0=滾到至少 N 個貼文連結後停
  // 論壇 / 列表模式（自動抓子頁）
  wcWithChildren?: boolean               // [web mode] 開啟「列表頁 → 抽連結 → 並行抓子頁 → 合併」
  wcChildLinkPattern?: string            // 子頁 URL pattern（空 = auto 內建 12 種）
  wcMaxChildren?: number                 // 最多抓幾個子頁（預設 10）
  // 影片模式
  wcVideoUrl?: string                    // [video mode] YouTube/Vimeo/Bilibili URL
  wcVideoQuality?: string                // [video mode] best / 1080p / 720p / 480p / 360p
  wcVideoMaxFilesizeMb?: number          // [video mode] 檔案大小上限（MB）
  wcVideoMaxDurationMin?: number         // [video mode] 影片長度上限（分鐘，0=不限）
  wcVideoSubs?: boolean                  // [video mode] 是否下載字幕
  wcVideoSubsLangs?: string              // [video mode] 字幕語言偏好（逗號分隔）
  wcVideoSaveInfoJson?: boolean          // [video mode] 是否寫 video.info.json（預設 OFF）
  timeout: number
  retry: number
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

/** Subagent 節點：多輪 LLM agent loop、role-based、跳過 recipe + validator */
export interface SubagentData extends Record<string, unknown> {
  name: string
  taskDescription: string
  workingDir: string
  outputPath: string
  role: string             // data_analyst | coder | researcher | critic | planner
  maxIter: number          // 最多 LLM 輪數上限（1-10、預設 5）
  timeout: number
  retry: number
  llmRole?: 'primary' | 'secondary'
  // 驗證閘(output.expect / output.json_schema):面板可編輯,YAML↔canvas 全程保留
  expectText?: string
  jsonSchemaText?: string
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

/** 技能節點：LLM 自動撰寫並執行程式碼 */
export interface SkillData extends Record<string, unknown> {
  name: string
  taskDescription: string
  workingDir: string
  outputPath: string
  expectedOutput: string
  jsonSchemaText?: string  // 輸出 JSON Schema 合約(inline JSON 字串、選填)
  readonly: boolean
  skill: string         // 掛載的 Claude Code skill 名稱（空字串 = 不掛載）
  askMode: boolean      // 詢問模式:LLM 遇到任何不確定就主動 ask_user 問用戶
  timeout: number
  retry: number
  next?: string         // 跳轉:跑完跳指定 step name(end / 留空 = 線性、用於 condition 分支)
  llmRole?: 'primary' | 'secondary'  // 用主/副模型(預設主)
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

/** AI 驗證節點：輕量 LLM 快速驗證前一步輸出 */
export interface AiValidationData extends Record<string, unknown> {
  expectText: string
  targetPath: string
  skillMode: boolean   // 保留：控制驗證時是否可執行程式碼
  llmRole?: 'primary' | 'secondary'  // 此驗證用主/副模型(會覆寫前一步的 llm_role)
  // 輸出 JSON Schema 合約(選填、inline JSON 字串):輸出 .json 時先跑確定性驗證(0 token),
  // 缺欄位/型別錯直接擋 + 具體錯誤;生成端也會把 schema 塞進任務要求(弱模型穩定核心)
  jsonSchemaText?: string
  index: number
}

/** Condition 節點(Ticket 2):IF / Switch 控制流。
 *  純 metadata、不執行命令。runner 求值表達式後跳到目標 step name。 */
export interface ConditionData extends Record<string, unknown> {
  name: string
  mode: 'if' | 'switch'      // UI 顯示用、後端只看 expression / switch 哪個非空
  expression: string         // IF 模式:Jinja2 boolean expression
  onTrue: string             // IF 模式:條件為真跳的 step name
  onFalse: string            // IF 模式:條件為假跳的 step name(留空 = end)
  switch: string             // Switch 模式:Jinja2 expression、求值後 str(value)
  cases: Record<string, string>  // Switch 模式:case_value → step_name
  default: string            // Switch 模式:沒命中時跳的 step name(留空 = end)
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

/** 人工確認節點：暫停 Pipeline 等待人為確認 */
export interface HumanConfirmData extends Record<string, unknown> {
  name: string
  message: string          // 自訂確認訊息
  notifyTelegram: boolean  // 是否透過 Telegram 通知
  screenshot: boolean      // 是否自動截圖並傳送到 Telegram
  previewPrevOutput: boolean  // 是否 render 上一步驟輸出檔案成 PNG 傳 TG
  sendPrevOutput: boolean  // 是否自動把上一步輸出檔當 document 傳到 TG（手機可下載）
  timeout: number          // 超時秒數(超時行動 != wait 時有效)
  hcOnTimeout: 'wait' | 'pass' | 'reject' | 'abort'   // 超時後的行動,預設 'wait' = 永遠等
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

// 桌面自動化動作（對應 backend ComputerUseAction）
export interface ComputerUseAction {
  type: 'click_image' | 'click_at' | 'type_text' | 'hotkey' | 'wait' | 'wait_image' | 'screenshot' | 'scroll' | 'drag'
      | 'assert_image' | 'assert_text' | 'activate_window' | 'if_image_found' | 'retry_until' | 'vlm_check'
      | 'uia_click' | 'uia_send_keys' | 'uia_get_text' | 'uia_get_table_rowcount' | 'uia_click_cell'
      | 'uia_wait_enabled' | 'uia_assert_state' | 'uia_close_window' | 'uia_set_clipboard'
      | 'uia_select' | 'uia_wait'
      | 'if_element_found' | 'wait_download' | 'wait_text' | 'if_text_found' | 'for_each' | 'uia_get_clipboard'
      | 'ocr_get_text'
  image?: string
  image2?: string        // 次錨點（多錨點驗證）
  dx2?: number           // 次錨點相對點擊點的 X 位移
  dy2?: number           // 次錨點相對點擊點的 Y 位移
  x?: number
  y?: number
  x2?: number
  y2?: number
  dy?: number
  text?: string
  keys?: string[]
  seconds?: number
  timeout_sec?: number
  confidence?: number
  button?: 'left' | 'right' | 'middle'
  clicks?: number
  description?: string
  // 三層 fallback toggle (UIA / CV / 強制座標), 預設全 True = UIA → CV → 強制座標
  // 對應 backend ComputerUseAction.{use_uia, use_cv, use_coord}; 細節見 panel 註解
  use_uia?: boolean     // UIA element 結構定位(預設 True)
  use_cv?: boolean      // CV 圖像比對(預設 True)
  use_coord?: boolean   // 強制座標最終 fallback(預設 True、舊欄位、語意改成「最終座標 fallback 啟用」)
  coord_fallback?: boolean  // 純 CV 模式:CV 找不到退錄製座標(action 層級;錄製預設 true)
  hold_sec?: number     // click 長按時間（>0 時回放走 mouseDown→sleep→mouseUp）
  modifiers?: string[]  // click 時按著的修飾鍵（如 ["ctrl"]、["ctrl","shift"]）
  use_ocr?: boolean     // click_image 顯式 OCR 啟用（勾選才跑 OCR，避免 silent 填字但沒觸發）
  ocr_text?: string     // OCR 目標文字（跟 use_ocr=true 搭配才生效）
  // ── ocr_get_text 專用：讀「標籤旁邊的值」存成變數 ──
  label?: string        // 要找的標籤文字（例：總計金額）
  direction?: 'right' | 'below'   // 值在標籤的哪一側
  kind?: 'amount' | 'ident' | 'taxid' | 'any'  // 值的格式約束；taxid 會驗檢查碼（唯一能證偽的）
  max_gap?: number      // 標籤與值的最大距離（px）
  // save_as 沿用下方 UIA action 的同名欄位（同樣是「值存進哪個變數」）
  // OCR 搜尋範圍（藍框，絕對桌面座標；width=0 = 未設定，回退 near_xy+cv_search_radius）
  ocr_box_left?: number
  ocr_box_top?: number
  ocr_box_width?: number
  ocr_box_height?: number
  // 嚴格鎖定範圍：true = 框內找不到立即 fail（不退附近、不退全螢幕）
  ocr_strict_region?: boolean
  // VLM 把關 Phase 1：動作後預期狀態（自然語言）+ 是否標記為 critical（critical_only 模式下才驗）
  expected?: string
  verify_critical?: boolean
  // UIA action 專用(uia_click / uia_send_keys / uia_get_text / uia_get_table_rowcount / uia_click_cell / uia_wait_enabled / uia_assert_state)
  control?: { type?: string; name?: string; auto_id?: string; depth?: number }
  save_as?: string
  row?: number | string                                    // 字串支援 {{var}} 替換
  column?: number | string
  check?: 'exists' | 'enabled' | 'focused' | 'checked'
  window?: string                                          // action 層級 window 覆寫(空 → 用 step.uiaWindow)
  rect?: number[]                                          // UIA picker 抓到的 element rect[x,y,w,h]、給 backend ControlFromPoint fallback 用
  anchor_off_x?: number // 點擊相對錨點影像中心的偏移 x
  anchor_off_y?: number // 點擊相對錨點影像中心的偏移 y
  full_image?: string   // 全螢幕截圖檔名（手動圈選編輯錨點時用）
  full_left?: number    // 全螢幕截圖對應的虛擬桌面原點 X（可能是負值）
  full_top?: number     // 全螢幕截圖對應的虛擬桌面原點 Y
  // search_region：CV / OCR / VLM 搜尋矩形（紅框，絕對桌面座標 [l,t,w,h]）
  search_region?: number[]
  // CV 嚴格鎖定範圍：true = 紅框內找不到立即 fail（不退附近、不退全螢幕、不退錄製座標）
  cv_strict_region?: boolean
  // VLM 相關欄位（vlm_check / click_image vlm_mode / 視覺判斷模板用）
  vlm_prompt?: string   // vlm_check 判斷條件、或 vlm_mode=description 的目標描述
  // grounding：地端 GUI 定位模型直接給座標（唯一讓 VLM 決定座標的模式；
  // 失敗或與錨點對不上時自動退回 CV）
  vlm_mode?: 'off' | 'description' | 'anchor_pick' | 'grounding'
  vlm_anchors?: string[] // vlm_mode=anchor_pick 用的多張變體錨點圖檔名
  // 控制流：if_image_found / retry_until 用（unknown[] 因為遞迴 dict 巢狀）
  then?: ComputerUseAction[]
  else?: ComputerUseAction[]
  do?: ComputerUseAction[]
  dir?: string      // wait_download：下載資料夾（空 = Downloads）
  pattern?: string  // wait_download：檔名 glob
  items?: string | string[]  // for_each：清單或逗號/換行分隔字串
  continue_on_error?: boolean  // for_each：某筆失敗跳下一筆
  split_as?: string  // for_each：一筆多欄位,變數名用 | 分隔
  split_sep?: string // for_each：欄位分隔符(預設 -)
  click_method?: string // uia_click：'mouse' 強制滑鼠點
  until?: ComputerUseAction | string  // retry_until 巢狀動作 / uia_wait 條件字串(appear/disappear/text_contains/text_equals)
  max_attempts?: number
  wait_between_sec?: number
  // activate_window 用
  title?: string
  title_contains?: string
}

export interface ComputerUseData extends Record<string, unknown> {
  name: string
  actions: ComputerUseAction[]
  assetsDir: string         // 錨點圖片資料夾（相對工作流）
  failFast: boolean         // 遇錯立即中止
  cvThreshold: number       // CV 比對門檻：0.50 寬鬆 / 0.80 標準 / 0.90 嚴格
  cvSearchOnlyNear: boolean // true = 只搜錄製座標附近（找不到直接 FAIL）
  cvSearchRadius: number    // 附近搜尋半徑（px），預設 400
  cvTriggerHover: boolean   // true = 比對前先 moveTo 錄製座標觸發 hover
  cvHoverWaitMs: number     // hover 等待 ms：200（快）/ 400（保險）
  cvCoordFallback: boolean  // true = CV 失敗時退回錄製座標硬點。預設 false（失敗就停，不亂點）
  ocrThreshold: number      // OCR 最小 conf 門檻（1.0/0.9/0.8/0.6 分級；預設 0.6）
  ocrCvFallback: boolean    // true = OCR 失敗時繼續試 CV 比對鏈。預設 false（失敗就停）
  // VLM 把關 Phase 1（節點層級設定）
  cuVlmCheckStrategy: 'off' | 'after_each' | 'critical_only'        // 預設 off
  cuOnMismatch: 'stop_notify' | 'retry_once' | 'skip_and_continue'  // 預設 stop_notify
  cuVlmMaxRetries: number   // retry_once 重試上限（預設 1）
  // UIA 模式(預設 pixel、向後相容)
  cuMode: 'pixel' | 'uia'   // 'pixel' = 錄製座標(現況);'uia' = UIA tree 控制
  uiaWindow: string         // UIA 模式視窗 title pattern(支援 *)、空字串 = foreground
  timeout: number           // 秒（執行上限）
  retry: number
  llmRole?: 'primary' | 'secondary'  // VLM 把關 / 視覺輔助用主/副模型
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

/** 視覺驗證節點：用 Settings 主模型（必須支援視覺）判斷某個圖像是否符合預期 */
export interface VisualValidationData extends Record<string, unknown> {
  name: string
  source: 'prev_output' | 'current_screen'   // 上一步輸出檔 / 目前螢幕畫面
  prompt: string                              // 描述「應該看到什麼」的判斷條件
  // current_screen 來源時可選的螢幕區域（虛擬桌面絕對座標）。空陣列 = 看整個螢幕
  searchRegion: number[]   // [left, top, width, height]
  llmRole?: 'primary' | 'secondary'
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

/** 網頁爬蟲動作（panel 互動序列；對應 backend 的 wc_interactions list[dict]）*/
export interface WebCrawlerAction {
  type: 'click' | 'scroll' | 'wait' | 'wait_for' | 'type'
  selector?: string             // click / wait_for / type 用
  to?: 'top' | 'bottom' | 'pixels'  // scroll 用
  pixels?: number               // scroll to=pixels 時
  seconds?: number              // wait 用
  text?: string                 // type 用
}

/** 網頁爬蟲節點：丟一個 URL 進去，吐 markdown + frontmatter 出來給後續 skill 解析 */
export interface WebCrawlerData extends Record<string, unknown> {
  name: string
  mode: 'web' | 'video'            // 模式：web=Crawl4AI 抓網頁 / video=yt-dlp 抓影片
  // 網頁模式
  url: string                      // 向後相容單 URL；建議改用 urls
  urls: string[]                   // 多 URL；> 1 個時 outputPath 解讀為資料夾、自動命名
  jsRender: boolean
  waitForSelector: string
  cloudflareFallback: boolean
  cookies: string                  // 登入 cookies（兩模式共用）
  interactions: WebCrawlerAction[]
  downloadAssets: boolean
  // 智慧滾動（進階設定）
  scrollCount: number              // 0=自動偵測 / >0=固定滾 N 次
  targetPostCount: number          // 0=不設目標 / >0=滾到至少 N 個貼文連結
  // 論壇 / 列表模式（自動抓子頁）
  withChildren: boolean            // 開啟「列表頁 → 抽連結 → 並行抓子頁 → 合併」
  childLinkPattern: string         // 空字串 = auto 內建 pattern；可填 regex 客製
  maxChildren: number              // 最多抓幾個子頁（預設 10）
  // 影片模式
  videoUrl: string
  videoQuality: string             // best / 1080p / 720p / 480p / 360p
  videoMaxFilesizeMb: number       // 預設 500
  videoMaxDurationMin: number      // 預設 30；0 = 不限
  videoSubs: boolean               // 預設 true
  videoSubsLangs: string           // 預設 ''（後端 fallback 'zh-TW,zh-Hant,zh-CN,zh-Hans,en'）
  videoSaveInfoJson: boolean       // 預設 false；勾起來才存 video.info.json
  // 共用
  outputPath: string
  retry: number
  timeout: number
  // 驗證閘(output.expect / output.json_schema):面板可編輯,YAML↔canvas 全程保留
  expectText?: string
  jsonSchemaText?: string
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

/** Outlook 自動化節點：透過 pywin32 + Outlook COM 處理寄信 / 收信 / 行事曆 / 附件 */
export interface OutlookData extends Record<string, unknown> {
  name: string
  // 選單模板 ID（前端選了哪個模板；空字串 = 自由輸入需求模式）
  // 例：daily_todo / search_summary / send_mail / send_with_attachment
  //    download_attachments / 等
  template: string
  // 自由輸入：模板沒勾時，使用者直接打字描述需求（agent 限定 win32 工具集）
  freeText: string
  // 模板參數：依模板而定的鍵值對（subject / sender / since / until / to / folder ...）
  params: Record<string, unknown>
  // 輸出檔路徑（可選；整理結果如 xlsx / md 報告會寫到這）
  outputPath: string
  retry: number
  // 整個步驟的執行上限（秒）。Outlook COM 對巨型收信夾 search_mail 可能跑 4-5 分鐘，
  // 預設 600 秒；30k+ 信箱建議 1800-3600。
  timeout: number
  llmRole?: 'primary' | 'secondary'
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

export type ScriptNode = Node<StepData>
export type SkillNode = Node<SkillData>
export type SubagentNode = Node<SubagentData>
export type AiValidationNode = Node<AiValidationData>
export type HumanConfirmNode = Node<HumanConfirmData>
export type ComputerUseNode = Node<ComputerUseData>
export type VisualValidationNode = Node<VisualValidationData>
export type OutlookNode = Node<OutlookData>
export type WebCrawlerNode = Node<WebCrawlerData>
export type McpNode = Node<McpData>
export type ConditionNode = Node<ConditionData>
export type AppNode = Node<StepData | AiValidationData | SkillData | SubagentData | HumanConfirmData | ComputerUseData | VisualValidationData | OutlookData | WebCrawlerData | ConditionData | McpData>

export function newAiValidationData(index = 0): AiValidationData {
  return { expectText: '', targetPath: '', skillMode: false, llmRole: 'primary', index }
}

let _webCrawlerCounter = 0
export function newWebCrawlerData(index = 0): WebCrawlerData {
  _webCrawlerCounter++
  return {
    name: `網頁爬蟲_${_webCrawlerCounter}`,
    mode: 'web',
    url: '',
    urls: [],
    jsRender: true,
    waitForSelector: '',
    cloudflareFallback: true,
    cookies: '',
    interactions: [],
    downloadAssets: false,
    scrollCount: 0,
    // 預設「達到 10 篇就停」— 對大多「抓清單做摘要」的場景剛好；
    // 想要「全部撈不限」就清空欄位（變 0 = 不設目標、走預設 2 滾）
    targetPostCount: 10,
    // 論壇 / 列表模式預設關閉；要爬「列表 → N 篇詳細頁」才打開
    withChildren: false,
    childLinkPattern: '',
    maxChildren: 10,
    videoUrl: '',
    videoQuality: '720p',
    videoMaxFilesizeMb: 500,
    videoMaxDurationMin: 30,
    videoSubs: true,
    videoSubsLangs: '',
    videoSaveInfoJson: false,
    outputPath: '',
    // 爬蟲節點 default 設 2（其他節點是 1）。理由：失敗多半是暫時性（CF challenge、
    // timeout、503），重抓很便宜（純 deterministic 重跑、零 LLM token），retry 2 的
    // CP 值高。不需要的話 panel 改 0/1 即可。
    retry: 2,
    timeout: 600,  // 影片下載常需要 5-10 分鐘；網頁模式不會用到那麼久也沒影響
    index,
    status: 'idle',
    errorMsg: '',
  }
}

let _outlookCounter = 0
export function newOutlookData(index = 0): OutlookData {
  _outlookCounter++
  return {
    name: `Outlook自動化_${_outlookCounter}`,
    template: '',
    freeText: '',
    params: {},
    outputPath: '',
    retry: 0,
    timeout: 600,
    index,
    status: 'idle',
    errorMsg: '',
  }
}

let _visualValidationCounter = 0
export function newVisualValidationData(index = 0): VisualValidationData {
  _visualValidationCounter++
  return {
    name: `視覺驗證_${_visualValidationCounter}`,
    source: 'prev_output',
    prompt: '',
    searchRegion: [],
    index,
    status: 'idle',
    errorMsg: '',
  }
}

let _conditionCounter = 0
export function newConditionData(index = 0): ConditionData {
  _conditionCounter++
  return {
    name: `條件_${_conditionCounter}`,
    mode: 'if',
    expression: '',
    onTrue: '',
    onFalse: '',
    switch: '',
    cases: {},
    default: '',
    index,
    status: 'idle',
    errorMsg: '',
  }
}

let _confirmCounter = 0
export function newHumanConfirmData(index = 0): HumanConfirmData {
  _confirmCounter++
  return {
    name: `人工確認_${_confirmCounter}`,
    message: '',
    notifyTelegram: true,
    screenshot: false,
    previewPrevOutput: false,
    sendPrevOutput: false,
    timeout: 3600,
    hcOnTimeout: 'wait',
    index,
    status: 'idle',
    errorMsg: '',
  }
}

/**
 * 視窗標題 → 穩定的關鍵字。瀏覽器標題的尾巴會變:「和其他 N 個頁面」隨分頁數變、
 * 「- 公司 - Microsoft Edge」帶設定檔名,Edge 還藏零寬字元 —— 整串拿去比對,
 * 多開一個分頁就找不到視窗。只留頁面本身的標題。
 */
export function windowKeyword(title: string): string {
  return (title || '')
    .replace(/[\u200b\u200c\u200d\ufeff]/g, '')
    .replace(/\*/g, '')
    .replace(/\s+(?:和其他\s*\d+\s*個頁面|and \d+ more pages?)[\s\S]*$/i, '')
    .replace(/\s+-\s+(?:[^-]+\s+-\s+)?(?:Microsoft\s*Edge|Google Chrome|Mozilla Firefox)$/i, '')
    .replace(/\s+-\s+[^-]+\s+-\s+Microsoft(?:\s*E\w*)?$/i, '')
    .trim()
}

/** 視窗標題 → UIA 目標視窗 pattern(頭尾萬用字元)。 */
export function windowPatternOf(title: string): string {
  const k = windowKeyword(title)
  return k ? `*${k}*` : ''
}

// 防呆:新增桌面自動化節點時,確保名稱不與現有節點撞名(計數器頁面重整後會歸零、
// 撞名會讓兩節點共用同一個 _assets 夾 → 互相覆蓋錨點圖)。回傳一個目前沒被用到的 桌面自動化_N。
export function dedupeComputerUseName(name: string, existing: Set<string>): string {
  if (!existing.has(name)) return name
  let k = 1
  while (existing.has(`桌面自動化_${k}`)) k++
  return `桌面自動化_${k}`
}

let _computerUseCounter = 0
export function newComputerUseData(index = 0): ComputerUseData {
  _computerUseCounter++
  return {
    name: `桌面自動化_${_computerUseCounter}`,
    actions: [],
    assetsDir: '',
    failFast: true,
    cvThreshold: 0.5,
    cvSearchOnlyNear: false,
    cvSearchRadius: 400,
    cvTriggerHover: true,
    cvHoverWaitMs: 200,
    cvCoordFallback: false,
    ocrThreshold: 0.6,
    ocrCvFallback: false,
    cuVlmCheckStrategy: 'off',
    cuOnMismatch: 'stop_notify',
    cuVlmMaxRetries: 1,
    cuMode: 'pixel',
    uiaWindow: '',
    timeout: 300,
    retry: 0,
    index,
    status: 'idle',
    errorMsg: '',
  }
}

let _counter = 0
export function newStepData(index = 0): StepData {
  _counter++
  return {
    name: `Python腳本_${_counter}`,
    batch: '',
    workingDir: '',
    outputPath: '',
    expect: '',
    timeout: 300,
    // 與 backend models.py PipelineStep.retry default 對齊；
    // 讓失敗有一次自我修正的機會（節點失敗後 LLM 會看到 reason 重試）。
    // 不想要重試的步驟在 UI 改成 0 即可。
    retry: 1,
    index,
    status: 'idle',
    errorMsg: '',
  }
}

let _skillCounter = 0
let _subagentCounter = 0
export function newSubagentData(index = 0): SubagentData {
  _subagentCounter++
  return {
    name: `Subagent_${_subagentCounter}`,
    taskDescription: '',
    workingDir: '',
    outputPath: '',
    role: 'data_analyst',
    maxIter: 5,
    timeout: 600,
    retry: 1,
    index,
    status: 'idle',
    errorMsg: '',
  }
}

/** MCP 節點:呼叫已安裝 MCP server 的 tool(設定頁 MCP 專區安裝) */
export interface McpData extends Record<string, unknown> {
  name: string
  server: string          // 已安裝 server 的註冊名(如 filesystem / memory)
  tool: string            // 該 server 的 tool 名
  toolArgsText: string    // tool 參數:inline JSON({"path":"x"})或多行 YAML 塊(原樣保真)
  outputPath: string
  timeout: number
  retry: number
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

let _mcpCounter = 0
export function newMcpData(index = 0): McpData {
  _mcpCounter++
  return {
    name: `MCP_${_mcpCounter}`,
    server: '',
    tool: '',
    toolArgsText: '',
    outputPath: '',
    timeout: 60,
    retry: 1,
    index,
    status: 'idle',
    errorMsg: '',
  }
}

export function newSkillData(index = 0): SkillData {
  _skillCounter++
  return {
    name: `AI技能_${_skillCounter}`,
    taskDescription: '',
    workingDir: '',
    outputPath: '',
    expectedOutput: '',
    readonly: false,
    skill: '',
    askMode: false,
    timeout: 300,
    // 與 backend default 對齊。skill 節點失敗常見是 LLM 程式碼瑕疵，
    // retry 1 給它看到 reason 自我修正一次的機會。
    retry: 1,
    index,
    status: 'idle',
    errorMsg: '',
  }
}

// ── 節點顏色（依 index 循環）──────────────────────────────────────────────────
const COLORS = ['#6366f1','#0ea5e9','#10b981','#f59e0b','#ec4899','#8b5cf6','#14b8a6','#f97316']
export const stepColor = (index: number) => COLORS[index % COLORS.length]

// ── 扇形版面 ──────────────────────────────────────────────────────────────────
// condition(IF / Switch)分支會「扇形」攤開:主線一排、分支對稱往上下展、
// 每條分支各自往右接成一列。讓使用者一眼看懂「這裡分流、各走各的」。
const FAN_COL_W = 360
const FAN_ROW_H = 200
const FAN_Y0 = 160

// 從 steps 算出每個節點的出邊。
// condition → 依 cases(Switch)/ onTrue·onFalse(IF) 連到具名目標;
// 其他節點:next: end → 不連(分支終點);next: <名稱> → 連該節點;無 next → 線性 i→i+1。
function buildFlowGraph(steps: StepData[]): { out: number[][]; edges: { i: number; j: number }[] } {
  const n = steps.length
  const nameToIdx = new Map<string, number>()
  steps.forEach((s, i) => { if (s.name && !nameToIdx.has(s.name)) nameToIdx.set(s.name, i) })
  const out: number[][] = steps.map(() => [])
  const edges: { i: number; j: number }[] = []
  steps.forEach((s, i) => {
    const targets: number[] = []
    if (s.condition) {
      if (s.switch) {
        for (const tgt of Object.values(s.cases || {})) {
          const j = nameToIdx.get(String(tgt))
          if (j !== undefined) targets.push(j)
        }
        if (s.default && nameToIdx.has(s.default)) targets.push(nameToIdx.get(s.default)!)
      } else {
        if (s.onTrue && nameToIdx.has(s.onTrue)) targets.push(nameToIdx.get(s.onTrue)!)
        if (s.onFalse && nameToIdx.has(s.onFalse)) targets.push(nameToIdx.get(s.onFalse)!)
      }
    } else {
      const nxt = s.next
      if (nxt && nxt.trim().toLowerCase() === 'end') {
        // 分支終點、不連
      } else if (nxt) {
        const j = nameToIdx.get(nxt)
        if (j !== undefined) targets.push(j)
      } else if (i + 1 < n) {
        targets.push(i + 1)
      }
    }
    // 同一目標去重:default 與某 case 指向同一節點時只連一條(避免畫面重複箭頭)
    const seen = new Set<number>()
    for (const j of targets) { if (seen.has(j)) continue; seen.add(j); out[i].push(j); edges.push({ i, j }) }
  })
  return { out, edges }
}

// 算出每個節點的扇形座標:欄(x)= 最長路徑深度;列(y)= DFS,condition 多出邊對稱攤開。
function fanLayout(n: number, out: number[][]): { x: number; y: number }[] {
  if (n === 0) return []
  const col = new Array(n).fill(0)
  for (let pass = 0; pass < n; pass++) {
    let changed = false
    for (let i = 0; i < n; i++) for (const j of out[i]) if (col[j] < col[i] + 1) { col[j] = col[i] + 1; changed = true }
    if (!changed) break
  }
  const lane = new Map<number, number>()
  const dfs = (i: number, l: number) => {
    if (lane.has(i)) return
    lane.set(i, l)
    const ch = out[i]
    if (ch.length <= 1) { for (const c of ch) dfs(c, l) }
    else { const k = ch.length; ch.forEach((c, idx) => dfs(c, l + (idx - (k - 1) / 2))) }
  }
  dfs(0, 0)
  for (let i = 0; i < n; i++) if (!lane.has(i)) lane.set(i, 0)
  // 匯流置中:多條分支收斂回同一節點時,擺在各前驅的垂直中點、其後單線子節點跟著移。
  const preds: number[][] = new Array(n).fill(0).map(() => [])
  for (let i = 0; i < n; i++) for (const j of out[i]) preds[j].push(i)
  const byCol = [...Array(n).keys()].sort((a, b) => col[a] - col[b])
  for (const j of byCol) {
    if (preds[j].length >= 2) {
      lane.set(j, preds[j].reduce((s, p) => s + lane.get(p)!, 0) / preds[j].length)
      let cur = j
      while (out[cur].length === 1 && preds[out[cur][0]].length < 2) {
        const nx = out[cur][0]; lane.set(nx, lane.get(cur)!); cur = nx
      }
    }
  }
  return [...Array(n).keys()].map(i => ({ x: col[i] * FAN_COL_W, y: FAN_Y0 + lane.get(i)! * FAN_ROW_H }))
}

// ── Steps → ReactFlow nodes + edges ──────────────────────────────────────────
export function stepsToFlow(steps: StepData[]): { nodes: AppNode[]; edges: Edge[] } {
  const nodes: AppNode[] = steps.map((s, i) => {
    if (s.computerUse) {
      return {
        id: `step-${i}`,
        type: 'computerUse' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          actions: s.computerUseActions || [],
          assetsDir: s.computerUseAssetsDir || '',
          failFast: s.computerUseFailFast ?? true,
          cvThreshold: s.cvThreshold ?? 0.5,
          cvSearchOnlyNear: s.cvSearchOnlyNear ?? false,
          cvSearchRadius: s.cvSearchRadius ?? 400,
          cvTriggerHover: s.cvTriggerHover ?? true,
          cvHoverWaitMs: s.cvHoverWaitMs ?? 200,
          cvCoordFallback: s.cvCoordFallback ?? false,
          ocrThreshold: s.ocrThreshold ?? 0.6,
          ocrCvFallback: s.ocrCvFallback ?? false,
          cuVlmCheckStrategy: s.cuVlmCheckStrategy ?? 'off',
          cuOnMismatch: s.cuOnMismatch ?? 'stop_notify',
          cuVlmMaxRetries: s.cuVlmMaxRetries ?? 1,
          cuMode: s.cuMode ?? 'pixel',
          uiaWindow: s.uiaWindow ?? '',
          timeout: s.timeout,
          retry: s.retry,
          llmRole: (s.llmRole === 'secondary' ? 'secondary' : 'primary') as 'primary' | 'secondary',
          // expect / json_schema:節點 data 必須帶著,否則「套用 YAML → 畫布 → autosave」
          // 這一圈就把 YAML 寫好的驗證閘洗掉(與 flowToSteps 讀回端成對)
          expectText: s.expect || '',
          jsonSchemaText: s.jsonSchemaText || '',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as ComputerUseData,
      }
    }
    if (s.visualValidation) {
      return {
        id: `step-${i}`,
        type: 'visualValidation' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          source: (s.vvSource === 'current_screen' ? 'current_screen' : 'prev_output') as 'prev_output' | 'current_screen',
          prompt: s.vvPrompt || '',
          searchRegion: Array.isArray(s.vvSearchRegion) ? s.vvSearchRegion : [],
          llmRole: (s.llmRole === 'secondary' ? 'secondary' : 'primary') as 'primary' | 'secondary',
          expectText: s.expect || '',
          jsonSchemaText: s.jsonSchemaText || '',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as VisualValidationData,
      }
    }
    if (s.outlookAutomation) {
      return {
        id: `step-${i}`,
        type: 'outlookAutomation' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          template: s.outlookTemplate || '',
          freeText: s.outlookFreeText || '',
          params: (s.outlookParams as Record<string, unknown>) || {},
          outputPath: s.outputPath,
          timeout: s.timeout,
          retry: s.retry,
          llmRole: (s.llmRole === 'secondary' ? 'secondary' : 'primary') as 'primary' | 'secondary',
          expectText: s.expect || '',
          jsonSchemaText: s.jsonSchemaText || '',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as OutlookData,
      }
    }
    if (s.webCrawler) {
      return {
        id: `step-${i}`,
        type: 'webCrawler' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          mode: (s.wcMode === 'video' ? 'video' : 'web') as 'web' | 'video',
          url: s.wcUrl || '',
          urls: Array.isArray(s.wcUrls) ? s.wcUrls : [],
          jsRender: s.wcJsRender ?? true,
          waitForSelector: s.wcWaitForSelector || '',
          cloudflareFallback: s.wcCloudflareFallback ?? true,
          cookies: s.wcCookies || '',
          interactions: Array.isArray(s.wcInteractions) ? s.wcInteractions : [],
          downloadAssets: s.wcDownloadAssets ?? false,
          scrollCount: s.wcScrollCount ?? 0,
          targetPostCount: s.wcTargetPostCount ?? 0,
          withChildren: s.wcWithChildren ?? false,
          childLinkPattern: s.wcChildLinkPattern || '',
          maxChildren: s.wcMaxChildren ?? 10,
          videoUrl: s.wcVideoUrl || '',
          videoQuality: s.wcVideoQuality || '720p',
          videoMaxFilesizeMb: s.wcVideoMaxFilesizeMb ?? 500,
          videoMaxDurationMin: s.wcVideoMaxDurationMin ?? 30,
          videoSubs: s.wcVideoSubs ?? true,
          videoSubsLangs: s.wcVideoSubsLangs || '',
          videoSaveInfoJson: s.wcVideoSaveInfoJson ?? false,
          outputPath: s.outputPath,
          retry: s.retry,
          timeout: s.timeout || 600,
          expectText: s.expect || '',
          jsonSchemaText: s.jsonSchemaText || '',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as WebCrawlerData,
      }
    }
    if (s.humanConfirm) {
      return {
        id: `step-${i}`,
        type: 'humanConfirmation' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          message: s.humanConfirmMessage || '',
          notifyTelegram: s.humanConfirmNotifyTelegram ?? true,
          screenshot: s.humanConfirmScreenshot ?? false,
          previewPrevOutput: s.humanConfirmPreview ?? false,
          sendPrevOutput: s.humanConfirmSendPrevOutput ?? false,
          timeout: s.timeout || 3600,
          hcOnTimeout: (s.hcOnTimeout as HumanConfirmData['hcOnTimeout']) ?? 'wait',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as HumanConfirmData,
      }
    }
    if (s.condition) {
      // YAML 來源:有寫 expression → IF 模式;有寫 switch → Switch 模式
      const inferredMode: 'if' | 'switch' = s.switch ? 'switch' : 'if'
      return {
        id: `step-${i}`,
        type: 'condition' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          mode: inferredMode,
          expression: s.expression || '',
          onTrue: s.onTrue || '',
          onFalse: s.onFalse || '',
          switch: s.switch || '',
          cases: (s.cases as Record<string, string>) || {},
          default: s.default || '',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as ConditionData,
      }
    }
    if (s.subagent) {
      return {
        id: `step-${i}`,
        type: 'subagent' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          taskDescription: s.batch,
          workingDir: s.workingDir || '',
          outputPath: s.outputPath,
          role: s.subagentRole || 'data_analyst',
          maxIter: s.subagentMaxIter ?? 5,
          timeout: s.timeout,
          retry: s.retry,
          llmRole: (s.llmRole === 'secondary' ? 'secondary' : 'primary') as 'primary' | 'secondary',
          expectText: s.expect || '',
          jsonSchemaText: s.jsonSchemaText || '',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as SubagentData,
      }
    }
    if (s.mcp) {
      return {
        id: `step-${i}`,
        type: 'mcpStep' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          server: s.mcpServer || '',
          tool: s.mcpTool || '',
          toolArgsText: s.mcpToolArgsText || '',
          outputPath: s.outputPath,
          timeout: s.timeout || 60,
          retry: s.retry,
          expectText: s.expect || '',
          jsonSchemaText: s.jsonSchemaText || '',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as McpData,
      }
    }
    if (s.skillMode) {
      // 向後相容:舊格式 skillMode=true → skillStep 節點
      return {
        id: `step-${i}`,
        type: 'skillStep' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          taskDescription: s.batch,
          workingDir: s.workingDir,
          outputPath: s.outputPath,
          expectedOutput: s.expect,
          jsonSchemaText: s.jsonSchemaText || '',
          readonly: s.readonly || false,
          skill: s.skill || '',
          askMode: s.askMode || false,
          timeout: s.timeout,
          retry: s.retry,
          llmRole: (s.llmRole === 'secondary' ? 'secondary' : 'primary') as 'primary' | 'secondary',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as SkillData,
      }
    }
    return {
      id: `step-${i}`,
      type: 'scriptStep' as const,
      position: { x: i * 320, y: 160 },
      data: { ...s, index: i, skillMode: undefined },
    }
  })

  // 分支感知:condition 依 cases/onTrue·onFalse 連線、其餘照 next / 線性。
  // 同步把節點排成扇形(主線一排、分支對稱攤開),不再全擠成單排線性鏈。
  const { out, edges: rawEdges } = buildFlowGraph(steps)
  const positions = fanLayout(steps.length, out)
  nodes.forEach((node, i) => { node.position = positions[i] })

  // 用 insertable type — hover 出 + / 🗑️；箭頭由 ReactFlow defaultEdgeOptions 統一處理
  const edges: Edge[] = rawEdges.map(({ i, j }) => ({
    id: `e-${i}-${j}`,
    source: `step-${i}`,
    target: `step-${j}`,
    type: 'insertable',
    animated: steps[i].status === 'running',
    style: { stroke: stepColor(i), strokeWidth: 2 },
    markerEnd: { type: 'arrowclosed' as any, color: stepColor(i), width: 18, height: 18 },
  }))

  return { nodes, edges }
}

// ── ReactFlow nodes → ordered steps（只包含有邊連接的節點）──────────────────────
export function flowToSteps(nodes: AppNode[], edges: Edge[]): StepData[] {
  // 收集 AI 驗證節點，建立 predecessor → aiData 映射
  const aiNodeIds = new Set<string>()
  const aiDataByPredecessor = new Map<string, AiValidationData>()

  for (const n of nodes) {
    if (n.type === 'aiValidation') {
      aiNodeIds.add(n.id)
      const inEdge = edges.find(e => e.target === n.id)
      if (inEdge) aiDataByPredecessor.set(inEdge.source, n.data as AiValidationData)
    }
  }

  // 過濾出可執行節點(scriptStep + skillStep + humanConfirmation + computerUse + visualValidation + condition + ...)
  // condition 雖然不執行命令但要進 YAML、由 runner 求值跳轉
  const execNodeIds = new Set<string>()
  const execNodes: AppNode[] = []
  for (const n of nodes) {
    if (n.type === 'scriptStep' || n.type === 'skillStep' || n.type === 'subagent'
        || n.type === 'humanConfirmation' || n.type === 'condition'
        || n.type === 'computerUse' || n.type === 'visualValidation'
        || n.type === 'outlookAutomation' || n.type === 'webCrawler' || n.type === 'mcpStep') {
      execNodeIds.add(n.id)
      execNodes.push(n)
    }
  }
  if (execNodes.length === 0) return []

  // 建立虛擬邊（跳過 AI 驗證節點）
  const virtualEdges: Edge[] = []
  for (const e of edges) {
    if (aiNodeIds.has(e.source)) continue
    if (aiNodeIds.has(e.target)) {
      const aiOutEdge = edges.find(e2 => e2.source === e.target)
      if (aiOutEdge && execNodeIds.has(aiOutEdge.target)) {
        virtualEdges.push({ ...e, target: aiOutEdge.target, id: `v-${e.id}` })
      }
      continue
    }
    if (execNodeIds.has(e.source) && execNodeIds.has(e.target)) {
      virtualEdges.push(e)
    }
  }

  // 找起點（無入邊的節點）
  const hasIncoming = new Set(virtualEdges.map(e => e.target))
  const starts = execNodes.filter(n => !hasIncoming.has(n.id))
  if (!starts.length) return []

  // 沿邊走、收集有連接的節點。
  // 之前用 Map<source,target>（單一 target）→ 同 source 多條出邊只保留最後一條、
  // 後寫覆蓋前寫；使用者「插入中間節點忘記刪舊邊」會看運氣決定走不走中間節點，
  // 而且中間節點會被當「孤立節點」靜默丟掉（user 收不到任何警告）。
  // 改成 multimap + DFS 找最長路徑：插入新節點即使保留舊邊、新路徑也會被選中。
  const adjMulti = new Map<string, string[]>()
  for (const e of virtualEdges) {
    if (!adjMulti.has(e.source)) adjMulti.set(e.source, [])
    adjMulti.get(e.source)!.push(e.target)
  }
  // DFS 找最長路徑；visited 防 cycle、子探索用 set copy 不互相污染
  const longestFrom = (node: string, visited: Set<string>): string[] => {
    if (visited.has(node)) return []
    const next = new Set(visited); next.add(node)
    const targets = adjMulti.get(node) || []
    if (targets.length === 0) return [node]
    let best: string[] = []
    for (const t of targets) {
      const sub = longestFrom(t, next)
      if (sub.length > best.length) best = sub
    }
    return [node, ...best]
  }
  const orderIds = longestFrom(starts[0].id, new Set<string>())
  const ordered: AppNode[] = []
  const seen = new Set<string>()
  for (const id of orderIds) {
    const node = execNodes.find(n => n.id === id)
    if (node) { ordered.push(node); seen.add(id) }
  }

  // 主路徑外的「分支子節點」也要進 YAML — condition 節點會分多條路、
  // 不在最長路徑上的分支(例如 onFalse 那條)若被丟掉、runner 就跳不到、
  // 會報「找不到 step」。所以 BFS 把所有從起點可達的 exec 節點都收進來、
  // 附在主路徑後面(順序不影響 — runner 靠 name 跳轉、不靠陣列順序)。
  const reachable: string[] = []
  {
    const queue = [starts[0].id]
    const visited = new Set<string>()
    while (queue.length) {
      const cur = queue.shift()!
      if (visited.has(cur)) continue
      visited.add(cur)
      for (const t of adjMulti.get(cur) || []) {
        if (!visited.has(t)) queue.push(t)
      }
    }
    for (const id of visited) {
      if (!seen.has(id)) reachable.push(id)
    }
  }
  for (const id of reachable) {
    const node = execNodes.find(n => n.id === id)
    if (node) ordered.push(node)
  }

  // 孤立節點不加入（邊驅動執行）

  const result = ordered.map((n, i) => {
    const aiData = aiDataByPredecessor.get(n.id)

    if (n.type === 'computerUse') {
      const d = n.data as ComputerUseData
      return {
        name: d.name,
        batch: '',
        workingDir: '',
        outputPath: '',
        // expect / json_schema 由後端 yaml_to_canvas 放進 base_data,這裡必須帶回,
        // 否則 YAML 寫好的驗證閘會在 canvas round-trip(autosave)時被洗掉
        // aiData 優先:讓「後掛 AI 驗證節點」對此節點也生效(原本只有 script/skill 吃)
        expect: aiData?.expectText || (d as any).expectText || '',
        jsonSchemaText: aiData?.jsonSchemaText || (d as any).jsonSchemaText || '',
        computerUse: true,
        computerUseActions: d.actions,
        computerUseAssetsDir: d.assetsDir,
        computerUseFailFast: d.failFast,
        cvThreshold: d.cvThreshold,
        cvSearchOnlyNear: d.cvSearchOnlyNear,
        cvSearchRadius: d.cvSearchRadius,
        cvTriggerHover: d.cvTriggerHover,
        cvHoverWaitMs: d.cvHoverWaitMs,
        ocrThreshold: d.ocrThreshold,
        ocrCvFallback: d.ocrCvFallback,
        cvCoordFallback: d.cvCoordFallback,
        cuVlmCheckStrategy: d.cuVlmCheckStrategy,
        cuOnMismatch: d.cuOnMismatch,
        cuVlmMaxRetries: d.cuVlmMaxRetries,
        cuMode: d.cuMode,
        uiaWindow: d.uiaWindow,
        timeout: d.timeout,
        retry: d.retry,
        llmRole: d.llmRole || 'primary',
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }

    if (n.type === 'visualValidation') {
      const d = n.data as VisualValidationData
      return {
        name: d.name,
        batch: '',
        workingDir: '',
        outputPath: '',
        expect: aiData?.expectText || (d as any).expectText || '',
        jsonSchemaText: aiData?.jsonSchemaText || (d as any).jsonSchemaText || '',
        visualValidation: true,
        vvSource: d.source,
        vvPrompt: d.prompt,
        vvSearchRegion: d.searchRegion && d.searchRegion.length === 4 ? d.searchRegion : [],
        timeout: 120,
        retry: 0,
        llmRole: d.llmRole || 'primary',
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }
    if (n.type === 'outlookAutomation') {
      const d = n.data as OutlookData
      return {
        name: d.name,
        batch: d.freeText || '',          // batch 欄位塞自由輸入;agent 跑時會優先看 outlookTemplate
        workingDir: '',
        outputPath: d.outputPath,
        expect: aiData?.expectText || (d as any).expectText || '',
        jsonSchemaText: aiData?.jsonSchemaText || (d as any).jsonSchemaText || '',
        outlookAutomation: true,
        outlookTemplate: d.template,
        outlookFreeText: d.freeText,
        outlookParams: d.params,
        timeout: typeof d.timeout === 'number' && d.timeout > 0 ? d.timeout : 600,
        retry: d.retry,
        llmRole: d.llmRole || 'primary',
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }
    if (n.type === 'webCrawler') {
      const d = n.data as WebCrawlerData
      return {
        name: d.name,
        batch: '',
        workingDir: '',
        outputPath: d.outputPath,
        expect: aiData?.expectText || (d as any).expectText || '',
        jsonSchemaText: aiData?.jsonSchemaText || (d as any).jsonSchemaText || '',
        webCrawler: true,
        wcMode: d.mode,
        wcUrl: d.url,
        wcUrls: d.urls,
        wcJsRender: d.jsRender,
        wcWaitForSelector: d.waitForSelector,
        wcCloudflareFallback: d.cloudflareFallback,
        wcCookies: d.cookies,
        wcInteractions: d.interactions,
        wcDownloadAssets: d.downloadAssets,
        wcScrollCount: d.scrollCount ?? 0,
        wcTargetPostCount: d.targetPostCount ?? 0,
        wcWithChildren: d.withChildren ?? false,
        wcChildLinkPattern: d.childLinkPattern || '',
        wcMaxChildren: d.maxChildren ?? 10,
        wcVideoUrl: d.videoUrl,
        wcVideoQuality: d.videoQuality,
        wcVideoMaxFilesizeMb: d.videoMaxFilesizeMb,
        wcVideoMaxDurationMin: d.videoMaxDurationMin,
        wcVideoSubs: d.videoSubs,
        wcVideoSubsLangs: d.videoSubsLangs,
        wcVideoSaveInfoJson: d.videoSaveInfoJson,
        timeout: typeof d.timeout === 'number' && d.timeout > 0 ? d.timeout : 600,
        retry: d.retry,
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }
    if (n.type === 'humanConfirmation') {
      const d = n.data as HumanConfirmData
      return {
        name: d.name,
        batch: '',
        workingDir: '',
        outputPath: '',
        expect: '',
        humanConfirm: true,
        humanConfirmMessage: d.message,
        humanConfirmNotifyTelegram: d.notifyTelegram,
        humanConfirmScreenshot: d.screenshot,
        humanConfirmPreview: d.previewPrevOutput,
        humanConfirmSendPrevOutput: d.sendPrevOutput,
        timeout: d.timeout,
        hcOnTimeout: d.hcOnTimeout || 'wait',
        retry: 0,
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }

    if (n.type === 'condition') {
      const d = n.data as ConditionData
      return {
        name: d.name,
        batch: '',
        workingDir: '',
        outputPath: '',
        expect: '',
        condition: true,
        // IF 跟 Switch 用同一份 model 欄位、依 d.mode 把該寫的寫進去、不該寫的留空
        expression: d.mode === 'if' ? (d.expression || '') : '',
        onTrue: d.mode === 'if' ? (d.onTrue || '') : '',
        onFalse: d.mode === 'if' ? (d.onFalse || '') : '',
        switch: d.mode === 'switch' ? (d.switch || '') : '',
        cases: d.mode === 'switch' ? (d.cases || {}) : {},
        default: d.mode === 'switch' ? (d.default || '') : '',
        timeout: 5,
        retry: 0,
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }

    if (n.type === 'skillStep') {
      const d = n.data as SkillData
      return {
        name: d.name,
        batch: d.taskDescription,
        workingDir: d.workingDir || '',
        outputPath: d.outputPath,
        expect: aiData?.expectText || d.expectedOutput || (d as any).expectText || '',
        jsonSchemaText: aiData?.jsonSchemaText || (d as SkillData).jsonSchemaText || '',
        skillMode: true,
        // 對 skill 節點不設 expectSkillMode — 後端用 has_expect 自動判斷深淺
        readonly: d.readonly || false,
        skill: d.skill || '',
        askMode: d.askMode || false,
        timeout: d.timeout,
        retry: d.retry,
        next: d.next || '',
        llmRole: d.llmRole || 'primary',
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }

    if (n.type === 'subagent') {
      const d = n.data as SubagentData
      return {
        name: d.name,
        batch: d.taskDescription,
        workingDir: d.workingDir || '',
        outputPath: d.outputPath,
        expect: aiData?.expectText || (d as any).expectText || '',
        jsonSchemaText: aiData?.jsonSchemaText || (d as any).jsonSchemaText || '',
        subagent: true,
        subagentRole: d.role || 'data_analyst',
        subagentMaxIter: d.maxIter ?? 5,
        timeout: d.timeout,
        retry: d.retry,
        llmRole: d.llmRole || 'primary',
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }

    if (n.type === 'mcpStep') {
      const d = n.data as McpData
      return {
        name: d.name,
        batch: '',
        workingDir: '',
        outputPath: d.outputPath,
        expect: aiData?.expectText || (d as any).expectText || '',
        jsonSchemaText: aiData?.jsonSchemaText || (d as any).jsonSchemaText || '',
        mcp: true,
        mcpServer: d.server || '',
        mcpTool: d.tool || '',
        mcpToolArgsText: d.toolArgsText || '',
        timeout: d.timeout || 60,
        retry: d.retry,
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }

    const d = n.data as StepData
    // AI 驗證節點如果設了 llmRole(且非預設 primary)→ 覆寫此 script 的 llmRole
    // 因為實際驗證 LLM 是用此 step 的 llm_role 跑、AI 驗證節點是修飾此 step 的驗證行為
    const effectiveLlmRole = (aiData?.llmRole === 'secondary')
      ? 'secondary'
      : (d.llmRole || 'primary')
    return {
      name: d.name,
      batch: d.batch,
      workingDir: d.workingDir || '',
      outputPath: (aiData?.targetPath && !d.outputPath) ? aiData.targetPath : d.outputPath,
      expect: aiData?.expectText || d.expect || (d as any).expectText || '',
      jsonSchemaText: aiData?.jsonSchemaText || (d as StepData).jsonSchemaText || '',
      skillMode: false,  // script / 其他節點:step-level 永不是 skill
      // AI 驗證節點若勾「Skill 模式」→ expectSkillMode=true → 走 deep 驗證
      expectSkillMode: !!aiData?.skillMode,
      timeout: d.timeout,
      retry: d.retry,
      next: d.next || '',
      llmRole: effectiveLlmRole,
      // 背景模式(daemon / GUI app):不等 exit、立刻下一步
      background: !!(d as { background?: boolean }).background,
      background_keep: (d as { background_keep?: boolean }).background_keep !== false,  // 預設 true(保留)
      readyAfterSeconds: (d as { readyAfterSeconds?: number }).readyAfterSeconds || 0,
      index: i,
      status: d.status,
      errorMsg: d.errorMsg,
    } as StepData
  })

  // ── 依「邊 + 最終陣列順序」重算每步的 next,確保 YAML round-trip 不掉拍 ──
  // 排序是 [最長路徑]+[分支節點],可能把「終點節點」排在分支節點前面。若不顯式標 next,
  // 重新匯入時終點會「順序 fallthrough」掉到後面的分支節點 → 多一條錯誤箭頭(merge 工作流必踩)。
  // 規則(condition 走 cases/onTrue/onFalse、不動):
  //   - 0 出邊(終點):不是陣列最後一個 → next: end;是最後一個 → 留空。
  //   - 1 出邊:目標剛好是陣列下一個 → 線性留空;否則顯式 next: <目標步驟名>。
  const nameById = new Map(execNodes.map(n => [n.id, ((n.data as { name?: string }).name) || n.id]))
  result.forEach((step, i) => {
    const node = ordered[i]
    if (node.type === 'condition') return
    const outs = adjMulti.get(node.id) || []
    if (outs.length === 0) {
      step.next = (i < result.length - 1) ? 'end' : ''
    } else if (outs.length === 1) {
      step.next = (outs[0] === ordered[i + 1]?.id) ? '' : (nameById.get(outs[0]) || '')
    }
  })
  return result
}

// ── Steps → YAML string ───────────────────────────────────────────────────────
/** 序列化 output 區塊(path + expect + skill_mode)— 所有節點型別共用。
 * 修:web_crawler / subagent / outlook / mcp 分支原本只寫 path、把 expect 默默丟掉
 */
function emitOutputBlock(lines: string[], s: StepData) {
  if (!(s.outputPath || s.expect || s.jsonSchemaText)) return
  lines.push(`    output:`)
  if (s.outputPath) lines.push(`      path: ${s.outputPath}`)
  if (s.expect) {
    if (s.expect.includes('\n') || s.expect.length > 80) {
      lines.push(`      expect: |`)
      for (const dl of s.expect.split('\n')) {
        lines.push(`        ${dl}`)
      }
    } else {
      lines.push(`      expect: "${s.expect.replace(/"/g, '\\"')}"`)
    }
  }
  if (s.expectSkillMode) lines.push(`      skill_mode: true`)
  // 輸出 JSON Schema 合約(inline JSON = 合法 YAML flow style,單行輸出)
  if (s.jsonSchemaText && s.jsonSchemaText.trim()) {
    let oneLine = s.jsonSchemaText.trim()
    try { oneLine = JSON.stringify(JSON.parse(oneLine)) } catch { oneLine = oneLine.split('\n').map(x => x.trim()).join(' ') }
    lines.push(`      json_schema: ${oneLine}`)
  }
}

export function stepsToYaml(name: string, steps: StepData[]): string {
  // 自動判斷 validate：有 skill 步驟或任何步驟有 expect → 啟用
  const needsValidate = steps.some(s => s.skillMode || !!s.expect)
  const lines: string[] = [
    `name: ${name || 'my-pipeline'}`,
    `validate: ${needsValidate}`,
    ``,
    `steps:`,
  ]
  for (const s of steps) {
    lines.push(`  - name: ${s.name}`)
    if (s.humanConfirm) {
      lines.push(`    human_confirm: true`)
      if (s.humanConfirmMessage) lines.push(`    message: "${s.humanConfirmMessage.replace(/"/g, '\\"')}"`)
      if (s.humanConfirmNotifyTelegram === false) lines.push(`    notify_telegram: false`)
      if (s.humanConfirmScreenshot) lines.push(`    screenshot: true`)
      if (s.humanConfirmPreview) lines.push(`    preview_prev_output: true`)
      if (s.humanConfirmSendPrevOutput) lines.push(`    send_prev_output: true`)
      if (s.timeout && s.timeout !== 3600) lines.push(`    timeout: ${s.timeout}`)
      if (s.hcOnTimeout && s.hcOnTimeout !== 'wait') lines.push(`    hc_on_timeout: ${s.hcOnTimeout}`)
      if (s.next) lines.push(`    next: ${s.next}`)
      continue
    }
    if (s.condition) {
      // Condition 節點:純 metadata、不需 batch / output / timeout
      lines.push(`    condition: true`)
      if (s.expression) lines.push(`    expression: "${s.expression.replace(/"/g, '\\"')}"`)
      if (s.onTrue) lines.push(`    on_true: ${s.onTrue}`)
      if (s.onFalse) lines.push(`    on_false: ${s.onFalse}`)
      if (s.switch) lines.push(`    switch: "${s.switch.replace(/"/g, '\\"')}"`)
      if (s.cases && Object.keys(s.cases).length > 0) {
        const inline = Object.entries(s.cases)
          .map(([k, v]) => `"${k}": ${v}`)
          .join(', ')
        lines.push(`    cases: { ${inline} }`)
      }
      if (s.default) lines.push(`    default: ${s.default}`)
      if (s.next) lines.push(`    next: ${s.next}`)
      continue
    }
    if (s.visualValidation) {
      lines.push(`    visual_validation: true`)
      lines.push(`    vv_source: ${s.vvSource || 'prev_output'}`)
      const vvp = s.vvPrompt || ''
      if (vvp) {
        if (vvp.includes('\n') || vvp.length > 80) {
          lines.push(`    vv_prompt: |`)
          for (const dl of vvp.split('\n')) {
            lines.push(`      ${dl}`)
          }
        } else {
          lines.push(`    vv_prompt: "${vvp.replace(/"/g, '\\"')}"`)
        }
      }
      if (s.vvSearchRegion && s.vvSearchRegion.length === 4) {
        lines.push(`    vv_search_region: [${s.vvSearchRegion.join(', ')}]`)
      }
      emitOutputBlock(lines, s)
      if (s.timeout && s.timeout !== 120) lines.push(`    timeout: ${s.timeout}`)
      if (s.llmRole === 'secondary') lines.push(`    llm_role: secondary`)
      continue
    }
    if (s.webCrawler) {
      lines.push(`    web_crawler: true`)
      if (s.wcMode && s.wcMode !== 'web') lines.push(`    wc_mode: ${s.wcMode}`)
      // 共用 cookies
      const ck = s.wcCookies || ''
      if (ck) {
        if (ck.includes('\n') || ck.length > 80) {
          lines.push(`    wc_cookies: |`)
          for (const dl of ck.split('\n')) lines.push(`      ${dl}`)
        } else {
          lines.push(`    wc_cookies: "${ck.replace(/"/g, '\\"')}"`)
        }
      }
      if (s.wcMode === 'video') {
        if (s.wcVideoUrl) lines.push(`    wc_video_url: "${s.wcVideoUrl.replace(/"/g, '\\"')}"`)
        if (s.wcVideoQuality && s.wcVideoQuality !== '720p') lines.push(`    wc_video_quality: ${s.wcVideoQuality}`)
        if (s.wcVideoMaxFilesizeMb !== undefined && s.wcVideoMaxFilesizeMb !== 500) lines.push(`    wc_video_max_filesize_mb: ${s.wcVideoMaxFilesizeMb}`)
        if (s.wcVideoMaxDurationMin !== undefined && s.wcVideoMaxDurationMin !== 30) lines.push(`    wc_video_max_duration_min: ${s.wcVideoMaxDurationMin}`)
        if (s.wcVideoSubs === false) lines.push(`    wc_video_subs: false`)
        if (s.wcVideoSubsLangs) lines.push(`    wc_video_subs_langs: "${s.wcVideoSubsLangs.replace(/"/g, '\\"')}"`)
        if (s.wcVideoSaveInfoJson === true) lines.push(`    wc_video_save_info_json: true`)
      } else {
        // 過濾使用者貼上的空行 / # 註解、只序列化有效 URL
        const validUrls = (s.wcUrls || [])
          .map(u => u.trim())
          .filter(u => u && !u.startsWith('#'))
        if (validUrls.length > 0) {
          lines.push(`    wc_urls:`)
          for (const u of validUrls) {
            lines.push(`      - "${u.replace(/"/g, '\\"')}"`)
          }
        } else if (s.wcUrl) {
          lines.push(`    wc_url: "${s.wcUrl.replace(/"/g, '\\"')}"`)
        }
        if (s.wcJsRender === false) lines.push(`    wc_js_render: false`)
        if (s.wcWaitForSelector) lines.push(`    wc_wait_for_selector: "${s.wcWaitForSelector.replace(/"/g, '\\"')}"`)
        if (s.wcCloudflareFallback === false) lines.push(`    wc_cloudflare_fallback: false`)
        if (s.wcInteractions && s.wcInteractions.length > 0) {
          lines.push(`    wc_interactions:`)
          for (const a of s.wcInteractions) {
            lines.push(`      - ${JSON.stringify(a)}`)
          }
        }
        if (s.wcDownloadAssets === true) lines.push(`    wc_download_assets: true`)
        // 智慧滾動：只在使用者有指定（非預設 0）時才寫入 yaml — 預設行為已在後端處理
        if (typeof s.wcScrollCount === 'number' && s.wcScrollCount > 0) {
          lines.push(`    wc_scroll_count: ${s.wcScrollCount}`)
        }
        if (typeof s.wcTargetPostCount === 'number' && s.wcTargetPostCount > 0) {
          lines.push(`    wc_target_post_count: ${s.wcTargetPostCount}`)
        }
        // 論壇 / 列表模式：只在開啟時寫；max_children 跟 pattern 也只在非預設時寫
        if (s.wcWithChildren === true) {
          lines.push(`    wc_with_children: true`)
          if (s.wcChildLinkPattern) {
            lines.push(`    wc_child_link_pattern: "${s.wcChildLinkPattern.replace(/"/g, '\\"')}"`)
          }
          if (typeof s.wcMaxChildren === 'number' && s.wcMaxChildren !== 10) {
            lines.push(`    wc_max_children: ${s.wcMaxChildren}`)
          }
        }
      }
      emitOutputBlock(lines, s)
      if (s.timeout && s.timeout !== 600) lines.push(`    timeout: ${s.timeout}`)
      if (s.retry !== undefined && s.retry !== 1) lines.push(`    retry: ${s.retry}`)
      if (s.next) lines.push(`    next: ${s.next}`)
      continue
    }
    if (s.subagent) {
      lines.push(`    subagent: true`)
      if (s.subagentRole && s.subagentRole !== 'data_analyst') {
        lines.push(`    subagent_role: ${s.subagentRole}`)
      } else {
        lines.push(`    subagent_role: ${s.subagentRole || 'data_analyst'}`)
      }
      if (s.subagentMaxIter !== undefined && s.subagentMaxIter !== 5) {
        lines.push(`    subagent_max_iter: ${s.subagentMaxIter}`)
      }
      if (s.batch) {
        if (s.batch.includes('\n') || s.batch.length > 80) {
          lines.push(`    batch: |`)
          for (const bl of s.batch.split('\n')) lines.push(`      ${bl}`)
        } else {
          lines.push(`    batch: ${s.batch}`)
        }
      }
      if (s.workingDir) lines.push(`    working_dir: ${s.workingDir}`)
      emitOutputBlock(lines, s)
      if (s.timeout && s.timeout !== 600) lines.push(`    timeout: ${s.timeout}`)
      if (s.retry !== undefined && s.retry !== 1) lines.push(`    retry: ${s.retry}`)
      if (s.next) lines.push(`    next: ${s.next}`)
      if (s.llmRole === 'secondary') lines.push(`    llm_role: secondary`)
      continue
    }
    if (s.mcp) {
      lines.push(`    mcp: true`)
      if (s.mcpServer) lines.push(`    mcp_server: ${s.mcpServer}`)
      if (s.mcpTool) lines.push(`    mcp_tool: ${s.mcpTool}`)
      const argsText = (s.mcpToolArgsText || '').trim()
      if (argsText) {
        if (argsText.startsWith('{')) {
          // inline JSON(JSON 是 YAML flow-style 的子集,直接單行輸出)
          lines.push(`    mcp_tool_args: ${argsText.replace(/\n\s*/g, ' ')}`)
        } else {
          // 多行 YAML 塊:原樣保真、每行加 6 空格縮排
          lines.push(`    mcp_tool_args:`)
          for (const al of argsText.split('\n')) lines.push(`      ${al}`)
        }
      }
      emitOutputBlock(lines, s)
      if (s.timeout && s.timeout !== 60) lines.push(`    mcp_timeout: ${s.timeout}`)
      if (s.retry !== undefined && s.retry !== 1) lines.push(`    retry: ${s.retry}`)
      if (s.next) lines.push(`    next: ${s.next}`)
      continue
    }
    if (s.outlookAutomation) {
      lines.push(`    outlook_automation: true`)
      if (s.outlookTemplate) lines.push(`    outlook_template: ${s.outlookTemplate}`)
      const ft = s.outlookFreeText || ''
      if (ft) {
        if (ft.includes('\n') || ft.length > 80) {
          lines.push(`    batch: |`)
          for (const dl of ft.split('\n')) lines.push(`      ${dl}`)
        } else {
          lines.push(`    batch: "${ft.replace(/"/g, '\\"')}"`)
        }
      }
      // outlook_params：序列化成 JSON 一行 — 最簡單也保守
      if (s.outlookParams && Object.keys(s.outlookParams).length > 0) {
        lines.push(`    outlook_params: ${JSON.stringify(s.outlookParams)}`)
      }
      emitOutputBlock(lines, s)
      if (s.timeout && s.timeout !== 600) lines.push(`    timeout: ${s.timeout}`)
      if (s.retry && s.retry !== 0) lines.push(`    retry: ${s.retry}`)
      if (s.llmRole === 'secondary') lines.push(`    llm_role: secondary`)
      continue
    }
    if (s.computerUse) {
      lines.push(`    computer_use: true`)
      if (s.computerUseAssetsDir) lines.push(`    assets_dir: ${s.computerUseAssetsDir}`)
      if (s.computerUseFailFast === false) lines.push(`    fail_fast: false`)
      if (s.cvThreshold !== undefined && s.cvThreshold !== 0.5) lines.push(`    cv_threshold: ${s.cvThreshold}`)
      if (s.cvSearchOnlyNear) lines.push(`    cv_search_only_near: true`)
      if (s.cvSearchRadius !== undefined && s.cvSearchRadius !== 400) lines.push(`    cv_search_radius: ${s.cvSearchRadius}`)
      if (s.cvTriggerHover === false) lines.push(`    cv_trigger_hover: false`)
      if (s.cvHoverWaitMs !== undefined && s.cvHoverWaitMs !== 200) lines.push(`    cv_hover_wait_ms: ${s.cvHoverWaitMs}`)
      // cv_coord_fallback 預設 false → 只在 true 時寫入
      if (s.cvCoordFallback === true) lines.push(`    cv_coord_fallback: true`)
      if (s.ocrThreshold !== undefined && s.ocrThreshold !== 0.6) lines.push(`    ocr_threshold: ${s.ocrThreshold}`)
      if (s.ocrCvFallback === true) lines.push(`    ocr_cv_fallback: true`)
      // VLM 把關 Phase 1 — 預設 off / stop_notify / 1、只在不同預設時寫入
      if (s.cuVlmCheckStrategy && s.cuVlmCheckStrategy !== 'off') lines.push(`    cu_vlm_check_strategy: ${s.cuVlmCheckStrategy}`)
      if (s.cuOnMismatch && s.cuOnMismatch !== 'stop_notify') lines.push(`    cu_on_mismatch: ${s.cuOnMismatch}`)
      if (s.cuVlmMaxRetries !== undefined && s.cuVlmMaxRetries !== 1) lines.push(`    cu_vlm_max_retries: ${s.cuVlmMaxRetries}`)
      // UIA 模式 — 預設 pixel、空 window
      if (s.cuMode && s.cuMode !== 'pixel') lines.push(`    cu_mode: ${s.cuMode}`)
      if (s.uiaWindow) lines.push(`    uia_window: ${JSON.stringify(s.uiaWindow)}`)
      if (s.computerUseActions && s.computerUseActions.length > 0) {
        // 以 JSON 陣列寫入 actions（一行一動作，夠精簡又能 yaml parse）
        lines.push(`    actions:`)
        for (const a of s.computerUseActions) {
          // 用 flow 寫法把每個 action 壓成一行 JSON
          const compact = JSON.stringify(a)
          lines.push(`      - ${compact}`)
        }
      }
      emitOutputBlock(lines, s)
      if (Number.isFinite(Number(s.timeout)) && Number(s.timeout) > 0 && Number(s.timeout) !== 300) lines.push(`    timeout: ${Number(s.timeout)}`)
      // computer_use 一定寫 retry(即使是 0),因為 backend PipelineStep 預設 retry=1
      // 對 UI 自動化來說 retry 從動作 #1 重跑會重複點擊造成副作用,所以預期是 retry=0
      lines.push(`    retry: ${Number(s.retry) || 0}`)
      if (s.llmRole === 'secondary') lines.push(`    llm_role: secondary`)
      continue
    }
    if (s.workingDir) lines.push(`    working_dir: ${s.workingDir}`)
    if (s.batch) {
      if (s.batch.includes('\n') || s.batch.length > 80) {
        lines.push(`    batch: |`)
        for (const bl of s.batch.split('\n')) {
          lines.push(`      ${bl}`)
        }
      } else {
        lines.push(`    batch: ${s.batch}`)
      }
    }
    if (s.skillMode) lines.push(`    skill_mode: true`)
    if (s.skill) lines.push(`    skill: ${s.skill}`)
    if (s.readonly) lines.push(`    readonly: true`)
    if (s.askMode) lines.push(`    ask_mode: true`)
    if (s.background) {
      lines.push(`    background: true`)
      lines.push(`    background_keep: ${s.background_keep === false ? 'false' : 'true'}`)  // 預設 true(保留)
      if (s.readyAfterSeconds && s.readyAfterSeconds > 0) {
        lines.push(`    ready_after_seconds: ${s.readyAfterSeconds}`)
      }
    }
    if (s.outputPath || s.expect || s.jsonSchemaText) {
      lines.push(`    output:`)
      if (s.outputPath) lines.push(`      path: ${s.outputPath}`)
      if (s.expect) {
        if (s.expect.includes('\n') || s.expect.length > 80) {
          lines.push(`      expect: |`)
          for (const dl of s.expect.split('\n')) {
            lines.push(`        ${dl}`)
          }
        } else {
          lines.push(`      expect: "${s.expect.replace(/"/g, '\\"')}"`)
        }
      }
      // output.skill_mode 只在 script 節點 + AI 驗證節點勾深度時寫；skill 節點不寫
      if (s.expectSkillMode) lines.push(`      skill_mode: true`)
      // 輸出 JSON Schema 合約(inline JSON = 合法 YAML flow style,單行輸出)
      if (s.jsonSchemaText && s.jsonSchemaText.trim()) {
        lines.push(`      json_schema: ${s.jsonSchemaText.trim().replace(/\n\s*/g, ' ')}`)
      }
    }
    if (Number.isFinite(Number(s.timeout)) && Number(s.timeout) > 0 && Number(s.timeout) !== 300) lines.push(`    timeout: ${Number(s.timeout)}`)
    // retry 的後端 default 是 1，只要不等於 1 都得寫出來（包含使用者明確設 0）
    if (String(s.retry ?? '') !== '' && Number.isFinite(Number(s.retry)) && Number(s.retry) !== 1) lines.push(`    retry: ${Number(s.retry)}`)
    // next 跳轉(condition 分支用、空字串 = 線性、不寫)
    if (s.next)            lines.push(`    next: ${s.next}`)
    // llm_role(預設 primary、不寫;只在 secondary 時寫)
    if (s.llmRole === 'secondary') lines.push(`    llm_role: secondary`)
  }
  return lines.join('\n')
}

// ── YAML string → steps ───────────────────────────────────────────────────────
export function parseYaml(raw: string): { name: string; validate: boolean; steps: StepData[] } | null {
  try {
    const lines = raw.split('\n')
    let stepIndent = 2
    for (const line of lines) {
      const m = line.match(/^(\s*)- name:/)
      if (m) { stepIndent = m[1].length; break }
    }

    let name = 'my-pipeline'
    let validate = false
    const steps: StepData[] = []
    let cur: Partial<StepData> | null = null
    let inOutput = false
    let multilineTarget: 'batch' | 'expect' | 'vv_prompt' | 'wc_cookies' | null = null
    let multilineIndent = 0
    let multilineLines: string[] = []

    const flushMultiline = () => {
      if (multilineTarget && cur && multilineLines.length > 0) {
        const text = multilineLines.join('\n').replace(/\n+$/, '')
        if (multilineTarget === 'batch') cur.batch = text
        else if (multilineTarget === 'vv_prompt') cur.vvPrompt = text
        else if (multilineTarget === 'wc_cookies') cur.wcCookies = text
        else cur.expect = text
      }
      multilineTarget = null
      multilineLines = []
      multilineIndent = 0
    }

    for (let li = 0; li < lines.length; li++) {
      const line = lines[li]
      const t = line.trim()

      if (multilineTarget) {
        if (t === '') { multilineLines.push(''); continue }
        const leadingSpaces = line.match(/^(\s*)/)?.[1].length ?? 0
        if (leadingSpaces >= multilineIndent) {
          multilineLines.push(line.slice(multilineIndent))
          continue
        }
        flushMultiline()
      }

      if (!t || t.startsWith('#') || t === 'pipeline:' || t === 'steps:') continue

      if (/^name:/.test(t) && !cur) {
        name = t.replace(/^name:\s*/, '')
      } else if (/^validate:/.test(t) && !cur) {
        validate = /true/.test(t)
      } else if (/^- name:/.test(t)) {
        flushMultiline()
        if (cur) steps.push(buildStep(cur, steps.length))
        cur = { name: t.replace(/^-\s*name:\s*/, '') }
        inOutput = false
      } else if (/^working_dir:/.test(t) && cur) {
        cur.workingDir = t.replace(/^working_dir:\s*/, '')
        inOutput = false
      } else if (/^batch:/.test(t) && cur) {
        const val = t.replace(/^batch:\s*/, '')
        if (val === '|' || val === '>') {
          multilineTarget = 'batch'
          const nextLine = lines[li + 1]
          multilineIndent = nextLine ? (nextLine.match(/^(\s*)/)?.[1].length ?? 0) : 0
        } else {
          cur.batch = val
        }
        inOutput = false
      } else if (/^output:/.test(t) && cur) {
        inOutput = true
      } else if (/^path:/.test(t) && cur && inOutput) {
        cur.outputPath = t.replace(/^path:\s*/, '')
      } else if (/^(expect|description):/.test(t) && cur && inOutput) {
        const val = t.replace(/^(expect|description):\s*/, '').replace(/^"|"$/g, '')
        if (val === '|' || val === '>') {
          multilineTarget = 'expect'
          const nextLine = lines[li + 1]
          multilineIndent = nextLine ? (nextLine.match(/^(\s*)/)?.[1].length ?? 0) : 0
        } else {
          cur.expect = val
        }
      } else if (/^json_schema:/.test(t) && cur && inOutput) {
        // 輸出 JSON Schema 合約:inline JSON(單行)直接存;block 巢狀原樣收集(同 mcp_tool_args)
        const rawJs = t.replace(/^json_schema:\s*/, '').trim()
        if (rawJs) {
          cur.jsonSchemaText = rawJs
        } else {
          const baseIndent = line.match(/^(\s*)/)?.[1].length ?? 0
          const collected: string[] = []
          let lj = li + 1
          let minIndent = Infinity
          while (lj < lines.length) {
            const sub = lines[lj]
            if (!sub.trim()) { collected.push(''); lj++; continue }
            const subIndent = sub.match(/^(\s*)/)?.[1].length ?? 0
            if (subIndent <= baseIndent) break
            collected.push(sub)
            if (subIndent < minIndent) minIndent = subIndent
            lj++
          }
          const strip = minIndent === Infinity ? 0 : minIndent
          cur.jsonSchemaText = collected.map(cl => cl ? cl.slice(strip) : '').join('\n').replace(/\s+$/, '')
          li = lj - 1
        }
      } else if (/^ai_validation:/.test(t) && cur && inOutput) {
        // ai_validation 是後端 model 上的死欄位，這裡單純忽略；
        // 解析時不再以它觸發任何狀態（避免「YAML 寫但行為不變」的假設）
      } else if (/^skill_mode:/.test(t) && cur) {
        // 區分 step-level（cur.skillMode）跟 output.skill_mode（cur.expectSkillMode）
        if (inOutput) {
          cur.expectSkillMode = /true/.test(t)
        } else {
          cur.skillMode = /true/.test(t)
        }
      } else if (/^skill:/.test(t) && cur) {
        cur.skill = t.replace(/^skill:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^readonly:/.test(t) && cur) {
        cur.readonly = /true/.test(t)
      } else if (/^ask_mode:/.test(t) && cur) {
        cur.askMode = /true/.test(t)
      } else if (/^human_confirm:/.test(t) && cur) {
        cur.humanConfirm = /true/.test(t)
      } else if (/^message:/.test(t) && cur) {
        cur.humanConfirmMessage = t.replace(/^message:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^notify_telegram:/.test(t) && cur) {
        cur.humanConfirmNotifyTelegram = /true/.test(t)
      } else if (/^screenshot:/.test(t) && cur) {
        cur.humanConfirmScreenshot = /true/.test(t)
      } else if (/^preview_prev_output:/.test(t) && cur) {
        cur.humanConfirmPreview = /true/.test(t)
      } else if (/^send_prev_output:/.test(t) && cur) {
        cur.humanConfirmSendPrevOutput = /true/.test(t)
      } else if (/^hc_on_timeout:/.test(t) && cur) {
        const v = t.replace(/^hc_on_timeout:\s*/, '').replace(/^"|"$/g, '').trim()
        if (v === 'wait' || v === 'pass' || v === 'reject' || v === 'abort') cur.hcOnTimeout = v
      } else if (/^background_keep:/.test(t) && cur) {
        cur.background_keep = /true/.test(t)
      } else if (/^background:/.test(t) && cur) {
        cur.background = /true/.test(t)
      } else if (/^ready_after_seconds:/.test(t) && cur) {
        cur.readyAfterSeconds = parseInt(t.replace(/^ready_after_seconds:\s*/, '')) || 0
      } else if (/^web_crawler:/.test(t) && cur) {
        cur.webCrawler = /true/.test(t)
      } else if (/^wc_mode:/.test(t) && cur) {
        const v = t.replace(/^wc_mode:\s*/, '').replace(/^"|"$/g, '').trim()
        cur.wcMode = (v === 'video' ? 'video' : 'web')
      } else if (/^wc_url:/.test(t) && cur) {
        cur.wcUrl = t.replace(/^wc_url:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^wc_urls:/.test(t) && cur) {
        cur.wcUrls = []
      } else if (/^- ".*"$/.test(t) && cur && Array.isArray(cur.wcUrls)) {
        // wc_urls 的列表項目（每行 "url"）
        cur.wcUrls.push(t.replace(/^-\s*"/, '').replace(/"$/, ''))
      } else if (/^wc_js_render:/.test(t) && cur) {
        cur.wcJsRender = /true/.test(t)
      } else if (/^wc_wait_for_selector:/.test(t) && cur) {
        cur.wcWaitForSelector = t.replace(/^wc_wait_for_selector:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^wc_cloudflare_fallback:/.test(t) && cur) {
        cur.wcCloudflareFallback = /true/.test(t)
      } else if (/^wc_download_assets:/.test(t) && cur) {
        cur.wcDownloadAssets = /true/.test(t)
      } else if (/^wc_scroll_count:/.test(t) && cur) {
        cur.wcScrollCount = parseInt(t.replace(/^wc_scroll_count:\s*/, '')) || 0
      } else if (/^wc_target_post_count:/.test(t) && cur) {
        cur.wcTargetPostCount = parseInt(t.replace(/^wc_target_post_count:\s*/, '')) || 0
      } else if (/^wc_with_children:/.test(t) && cur) {
        cur.wcWithChildren = /true/.test(t)
      } else if (/^wc_child_link_pattern:/.test(t) && cur) {
        cur.wcChildLinkPattern = t.replace(/^wc_child_link_pattern:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^wc_max_children:/.test(t) && cur) {
        cur.wcMaxChildren = parseInt(t.replace(/^wc_max_children:\s*/, '')) || 10
      } else if (/^wc_video_url:/.test(t) && cur) {
        cur.wcVideoUrl = t.replace(/^wc_video_url:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^wc_video_quality:/.test(t) && cur) {
        cur.wcVideoQuality = t.replace(/^wc_video_quality:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^wc_video_max_filesize_mb:/.test(t) && cur) {
        cur.wcVideoMaxFilesizeMb = parseInt(t.replace(/^wc_video_max_filesize_mb:\s*/, '')) || 500
      } else if (/^wc_video_max_duration_min:/.test(t) && cur) {
        cur.wcVideoMaxDurationMin = parseInt(t.replace(/^wc_video_max_duration_min:\s*/, '')) || 0
      } else if (/^wc_video_subs:/.test(t) && cur) {
        cur.wcVideoSubs = /true/.test(t)
      } else if (/^wc_video_subs_langs:/.test(t) && cur) {
        cur.wcVideoSubsLangs = t.replace(/^wc_video_subs_langs:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^wc_video_save_info_json:/.test(t) && cur) {
        cur.wcVideoSaveInfoJson = /true/.test(t)
      } else if (/^wc_cookies:/.test(t) && cur) {
        const val = t.replace(/^wc_cookies:\s*/, '').replace(/^"|"$/g, '')
        if (val === '|' || val === '>') {
          multilineTarget = 'wc_cookies'
          const nextLine = lines[li + 1]
          multilineIndent = nextLine ? (nextLine.match(/^(\s*)/)?.[1].length ?? 0) : 0
        } else {
          cur.wcCookies = val
        }
      } else if (/^wc_interactions:/.test(t) && cur) {
        // 後面跟著一串 - { JSON } 行；抓下面到 indent 縮回去為止
        cur.wcInteractions = []
      } else if (/^- \{/.test(t) && cur && Array.isArray(cur.wcInteractions)) {
        // wc_interactions 的 JSON 陣列項目
        try {
          const obj = JSON.parse(t.replace(/^-\s*/, ''))
          cur.wcInteractions.push(obj)
        } catch { /* ignore */ }
      // ── computer_use 節點回讀(稽查 F)──────────────────────────────
      // stepsToYaml 會寫出這些欄位、但 parseYaml 原本一個都沒讀回 → 在 YAML 編輯器
      // 手改 computer_use 節點後按「套用」會把 17 個欄位(含 actions)全靜默丟掉。
      // 以下逐欄回讀,鏡像對應 stepsToYaml 的 computer_use 區塊。
      } else if (/^computer_use:/.test(t) && cur) {
        cur.computerUse = /true/.test(t)
      } else if (/^assets_dir:/.test(t) && cur) {
        cur.computerUseAssetsDir = t.replace(/^assets_dir:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^fail_fast:/.test(t) && cur) {
        cur.computerUseFailFast = /true/.test(t)
      } else if (/^cv_threshold:/.test(t) && cur) {
        const v = parseFloat(t.replace(/^cv_threshold:\s*/, '')); if (!isNaN(v)) cur.cvThreshold = v
      } else if (/^cv_search_only_near:/.test(t) && cur) {
        cur.cvSearchOnlyNear = /true/.test(t)
      } else if (/^cv_search_radius:/.test(t) && cur) {
        const v = parseInt(t.replace(/^cv_search_radius:\s*/, '')); if (!isNaN(v)) cur.cvSearchRadius = v
      } else if (/^cv_trigger_hover:/.test(t) && cur) {
        cur.cvTriggerHover = /true/.test(t)
      } else if (/^cv_hover_wait_ms:/.test(t) && cur) {
        const v = parseInt(t.replace(/^cv_hover_wait_ms:\s*/, '')); if (!isNaN(v)) cur.cvHoverWaitMs = v
      } else if (/^cv_coord_fallback:/.test(t) && cur) {
        cur.cvCoordFallback = /true/.test(t)
      } else if (/^ocr_threshold:/.test(t) && cur) {
        const v = parseFloat(t.replace(/^ocr_threshold:\s*/, '')); if (!isNaN(v)) cur.ocrThreshold = v
      } else if (/^ocr_cv_fallback:/.test(t) && cur) {
        cur.ocrCvFallback = /true/.test(t)
      } else if (/^cu_vlm_check_strategy:/.test(t) && cur) {
        const v = t.replace(/^cu_vlm_check_strategy:\s*/, '').replace(/^"|"$/g, '').trim()
        if (v === 'off' || v === 'after_each' || v === 'critical_only') cur.cuVlmCheckStrategy = v
      } else if (/^cu_on_mismatch:/.test(t) && cur) {
        const v = t.replace(/^cu_on_mismatch:\s*/, '').replace(/^"|"$/g, '').trim()
        if (v === 'stop_notify' || v === 'retry_once' || v === 'skip_and_continue') cur.cuOnMismatch = v
      } else if (/^cu_vlm_max_retries:/.test(t) && cur) {
        const v = parseInt(t.replace(/^cu_vlm_max_retries:\s*/, '')); if (!isNaN(v)) cur.cuVlmMaxRetries = v
      } else if (/^cu_mode:/.test(t) && cur) {
        const v = t.replace(/^cu_mode:\s*/, '').replace(/^"|"$/g, '').trim()
        cur.cuMode = (v === 'uia' ? 'uia' : 'pixel')
      } else if (/^uia_window:/.test(t) && cur) {
        const raw = t.replace(/^uia_window:\s*/, '').trim()
        // 寫出端用 JSON.stringify → 多半帶引號,剝掉;也容忍裸字串
        try { cur.uiaWindow = JSON.parse(raw) } catch { cur.uiaWindow = raw.replace(/^"|"$/g, '') }
      } else if (/^actions:/.test(t) && cur) {
        cur.computerUseActions = []
      } else if (/^- \{/.test(t) && cur && Array.isArray(cur.computerUseActions)) {
        // actions 的 JSON 陣列項目(一行一動作)
        try {
          const obj = JSON.parse(t.replace(/^-\s*/, ''))
          cur.computerUseActions.push(obj)
        } catch { /* ignore */ }
      } else if (/^visual_validation:/.test(t) && cur) {
        cur.visualValidation = /true/.test(t)
      } else if (/^vv_source:/.test(t) && cur) {
        const v = t.replace(/^vv_source:\s*/, '').replace(/^"|"$/g, '').trim()
        // 相容舊值 prev_output_file / rendered_preview → 一律轉 prev_output
        cur.vvSource = (v === 'current_screen' ? 'current_screen' : 'prev_output')
      } else if (/^vv_prompt:/.test(t) && cur) {
        const val = t.replace(/^vv_prompt:\s*/, '').replace(/^"|"$/g, '')
        if (val === '|' || val === '>') {
          multilineTarget = 'vv_prompt'
          const nextLine = lines[li + 1]
          multilineIndent = nextLine ? (nextLine.match(/^(\s*)/)?.[1].length ?? 0) : 0
        } else {
          cur.vvPrompt = val
        }
      } else if (/^vv_search_region:/.test(t) && cur) {
        // 格式 [l, t, w, h]
        const m = t.match(/\[([^\]]+)\]/)
        if (m) {
          const arr = m[1].split(',').map(x => parseInt(x.trim()) || 0)
          if (arr.length === 4) cur.vvSearchRegion = arr
        }
      } else if (/^subagent:/.test(t) && cur) {
        cur.subagent = /true/.test(t)
      } else if (/^mcp:/.test(t) && cur) {
        cur.mcp = /true/.test(t)
      } else if (/^mcp_server:/.test(t) && cur) {
        cur.mcpServer = t.replace(/^mcp_server:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^mcp_tool:/.test(t) && cur) {
        cur.mcpTool = t.replace(/^mcp_tool:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^mcp_timeout:/.test(t) && cur) {
        cur.timeout = parseInt(t.replace(/^mcp_timeout:\s*/, '')) || 60
      } else if (/^mcp_tool_args:/.test(t) && cur) {
        // 兩種格式:inline JSON(單行)或多行 YAML 塊(原樣收集、保真存文字)
        const raw = t.replace(/^mcp_tool_args:\s*/, '').trim()
        if (raw) {
          cur.mcpToolArgsText = raw
        } else {
          const baseIndent = line.match(/^(\s*)/)?.[1].length ?? 0
          const collected: string[] = []
          let lj = li + 1
          let minIndent = Infinity
          while (lj < lines.length) {
            const sub = lines[lj]
            if (!sub.trim()) { collected.push(''); lj++; continue }
            const subIndent = sub.match(/^(\s*)/)?.[1].length ?? 0
            if (subIndent <= baseIndent) break
            collected.push(sub)
            if (subIndent < minIndent) minIndent = subIndent
            lj++
          }
          // 去共同前綴縮排(保留相對縮排)→ panel 顯示乾淨、輸出時再統一補縮排
          const strip = minIndent === Infinity ? 0 : minIndent
          cur.mcpToolArgsText = collected.map(cl => cl ? cl.slice(strip) : '').join('\n').replace(/\s+$/, '')
          li = lj - 1
        }
      } else if (/^subagent_role:/.test(t) && cur) {
        cur.subagentRole = t.replace(/^subagent_role:\s*/, '').replace(/^"|"$/g, '').trim() || 'data_analyst'
      } else if (/^subagent_max_iter:/.test(t) && cur) {
        cur.subagentMaxIter = parseInt(t.replace(/^subagent_max_iter:\s*/, '')) || 5
      } else if (/^condition:/.test(t) && cur) {
        cur.condition = /true/.test(t)
      } else if (/^expression:/.test(t) && cur) {
        cur.expression = t.replace(/^expression:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^on_true:/.test(t) && cur) {
        cur.onTrue = t.replace(/^on_true:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^on_false:/.test(t) && cur) {
        cur.onFalse = t.replace(/^on_false:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^switch:/.test(t) && cur) {
        cur.switch = t.replace(/^switch:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^cases:/.test(t) && cur) {
        // inline JSON-ish:cases: { "200": ok, "404": retry }
        const m = t.match(/cases:\s*\{(.+)\}\s*$/)
        if (m) {
          const cases: Record<string, string> = {}
          // 切 key/value pairs:`"200": ok, "404": retry` → entries
          for (const pair of m[1].split(',')) {
            const p = pair.trim()
            const kv = p.match(/^"?([^"]*?)"?\s*:\s*(.+)$/)
            if (kv) cases[kv[1].trim()] = kv[2].trim()
          }
          cur.cases = cases
        }
      } else if (/^default:/.test(t) && cur) {
        cur.default = t.replace(/^default:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^next:/.test(t) && cur) {
        cur.next = t.replace(/^next:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^llm_role:/.test(t) && cur) {
        const v = t.replace(/^llm_role:\s*/, '').replace(/^"|"$/g, '').trim()
        cur.llmRole = (v === 'secondary' ? 'secondary' : 'primary')
      } else if (/^outlook_automation:/.test(t) && cur) {
        cur.outlookAutomation = /true/.test(t)
      } else if (/^outlook_template:/.test(t) && cur) {
        cur.outlookTemplate = t.replace(/^outlook_template:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^outlook_params:/.test(t) && cur) {
        // 兩種格式都接：
        //   ✓ inline JSON：outlook_params: {"to":"x@y.com",...}
        //   ✓ multi-line YAML：outlook_params:↵  to: x@y.com↵  subject: ...
        // AI 助手本來指示用 inline JSON 但偶爾仍會吐 multi-line、這裡兜底
        const raw = t.replace(/^outlook_params:\s*/, '').trim()
        if (raw) {
          try { cur.outlookParams = JSON.parse(raw) } catch { /* 壞 JSON 就略過 */ }
        } else {
          // multi-line YAML：往下讀比 outlook_params 縮排更深的 key: value 行
          const baseIndent = line.match(/^(\s*)/)?.[1].length ?? 0
          const params: Record<string, unknown> = {}
          let lj = li + 1
          while (lj < lines.length) {
            const sub = lines[lj]
            const subTrim = sub.trim()
            if (!subTrim) { lj++; continue }
            const subIndent = sub.match(/^(\s*)/)?.[1].length ?? 0
            if (subIndent <= baseIndent) break  // 縮排回去了 → 結束
            const m = subTrim.match(/^([\w\-]+)\s*:\s*(.*)$/)
            if (!m) { lj++; continue }
            const key = m[1]
            let val: string = m[2].trim()
            // 剝引號 — "x" / 'x'
            if ((val.startsWith('"') && val.endsWith('"')) ||
                (val.startsWith("'") && val.endsWith("'"))) {
              val = val.slice(1, -1)
            }
            // 嘗試 boolean / number 推論
            if (val === 'true') params[key] = true
            else if (val === 'false') params[key] = false
            else if (/^-?\d+$/.test(val)) params[key] = parseInt(val)
            else params[key] = val
            lj++
          }
          if (Object.keys(params).length > 0) cur.outlookParams = params
          li = lj - 1  // 跳過已消化的子行（外層 for 會 ++）
        }
      } else if (/^timeout:/.test(t) && cur) {
        cur.timeout = parseInt(t.replace(/^timeout:\s*/, '')) || 300
        inOutput = false
      } else if (/^retry:/.test(t) && cur) {
        cur.retry = parseInt(t.replace(/^retry:\s*/, '')) || 0
        inOutput = false
      } else if (cur && t && !t.startsWith('-')) {
        // 不匹配任何 key 的行 → 追加到 batch（處理長文字被換行的情況）
        if (cur.batch && !inOutput) {
          cur.batch += ' ' + t
        } else if (cur.expect && inOutput) {
          cur.expect += ' ' + t
        }
      }
    }
    flushMultiline()
    if (cur) steps.push(buildStep(cur, steps.length))
    return { name, validate, steps }
  } catch { return null }
}

function buildStep(partial: Partial<StepData>, index: number): StepData {
  // 防呆:弱 LLM 常把「桌面自動化步驟」漏寫 `computer_use: true`、只留一個名字
  //（如「RPA操作」「自動化點擊」）→ 解析後會變成空白 script 節點、不是 computer_use。
  // 若某步「完全沒有任何類型欄位、也沒有 batch」且名字明顯是桌面自動化/RPA 意圖,
  // 就補正成空白 computer_use 節點(actions 留給使用者在畫布錄製)。
  // 對齊 system prompt「桌面自動化節點」與「啟動既有專案第 0 點」的設計。
  const _hasAnyType = !!(
    (partial.batch && partial.batch.trim())
    || partial.skillMode || partial.humanConfirm || partial.visualValidation
    || partial.webCrawler || partial.outlookAutomation || partial.subagent
    || partial.condition || partial.computerUse || partial.mcp
  )
  if (!_hasAnyType && partial.name
      && /RPA|computer[\s_-]?use|點擊|點按|滑鼠|鍵盤|錄製|桌面自動化|UI\s?自動化|自動化操作|自動化點擊|操作工具|操作視窗|視窗操作/i.test(partial.name)) {
    partial.computerUse = true
  }
  // Outlook 節點的編碼器把 outlookFreeText 寫進 batch:，這裡把它還原回 freeText、
  // 避免畫布上 freeText 欄空白而 batch 帶著一段使用者不會用到的描述
  if (partial.outlookAutomation && partial.batch && !partial.outlookFreeText) {
    partial.outlookFreeText = partial.batch
    partial.batch = ''
  }
  return {
    name: partial.name ?? `步驟 ${index + 1}`,
    batch: partial.batch ?? '',
    workingDir: partial.workingDir ?? '',
    outputPath: partial.outputPath ?? '',
    expect: partial.expect ?? '',
    skillMode: partial.skillMode ?? false,
    expectSkillMode: partial.expectSkillMode ?? false,
    readonly: partial.readonly ?? false,
    skill: partial.skill ?? '',
    humanConfirm: partial.humanConfirm ?? false,
    humanConfirmMessage: partial.humanConfirmMessage ?? '',
    humanConfirmNotifyTelegram: partial.humanConfirmNotifyTelegram ?? true,
    humanConfirmScreenshot: partial.humanConfirmScreenshot ?? false,
    humanConfirmPreview: partial.humanConfirmPreview ?? false,
    humanConfirmSendPrevOutput: partial.humanConfirmSendPrevOutput ?? false,
    visualValidation: partial.visualValidation ?? false,
    vvSource: partial.vvSource ?? 'prev_output',
    vvPrompt: partial.vvPrompt ?? '',
    vvSearchRegion: partial.vvSearchRegion ?? [],
    webCrawler: partial.webCrawler ?? false,
    wcMode: partial.wcMode ?? 'web',
    wcUrl: partial.wcUrl ?? '',
    wcUrls: partial.wcUrls ?? [],
    wcJsRender: partial.wcJsRender ?? true,
    wcWaitForSelector: partial.wcWaitForSelector ?? '',
    wcCloudflareFallback: partial.wcCloudflareFallback ?? true,
    wcCookies: partial.wcCookies ?? '',
    wcInteractions: partial.wcInteractions ?? [],
    wcDownloadAssets: partial.wcDownloadAssets ?? false,
    wcScrollCount: partial.wcScrollCount ?? 0,
    wcTargetPostCount: partial.wcTargetPostCount ?? 0,
    wcWithChildren: partial.wcWithChildren ?? false,
    wcChildLinkPattern: partial.wcChildLinkPattern ?? '',
    wcMaxChildren: partial.wcMaxChildren ?? 10,
    wcVideoUrl: partial.wcVideoUrl ?? '',
    wcVideoQuality: partial.wcVideoQuality ?? '720p',
    wcVideoMaxFilesizeMb: partial.wcVideoMaxFilesizeMb ?? 500,
    wcVideoMaxDurationMin: partial.wcVideoMaxDurationMin ?? 30,
    wcVideoSubs: partial.wcVideoSubs ?? true,
    wcVideoSubsLangs: partial.wcVideoSubsLangs ?? '',
    wcVideoSaveInfoJson: partial.wcVideoSaveInfoJson ?? false,
    outlookAutomation: partial.outlookAutomation ?? false,
    outlookTemplate: partial.outlookTemplate ?? '',
    outlookFreeText: partial.outlookFreeText ?? '',
    outlookParams: partial.outlookParams ?? {},
    subagent: partial.subagent ?? false,
    subagentRole: partial.subagentRole ?? 'data_analyst',
    subagentMaxIter: partial.subagentMaxIter ?? 5,
    // computer_use 節點(稽查 F)— 跟 condition 一樣的漏洞:buildStep 原本完全沒列這些欄位,
    // parseYaml 設好 cur.computerUse=true 等、進 buildStep 後全被丟掉 → YAML round-trip 丟失桌面自動化節點。
    // 預設值對齊 newComputerUseData。
    computerUse: partial.computerUse ?? false,
    computerUseActions: partial.computerUseActions ?? [],
    computerUseAssetsDir: partial.computerUseAssetsDir ?? '',
    computerUseFailFast: partial.computerUseFailFast ?? true,
    cvThreshold: partial.cvThreshold ?? 0.5,
    cvSearchOnlyNear: partial.cvSearchOnlyNear ?? false,
    cvSearchRadius: partial.cvSearchRadius ?? 400,
    cvTriggerHover: partial.cvTriggerHover ?? true,
    cvHoverWaitMs: partial.cvHoverWaitMs ?? 200,
    cvCoordFallback: partial.cvCoordFallback ?? false,
    ocrThreshold: partial.ocrThreshold ?? 0.6,
    ocrCvFallback: partial.ocrCvFallback ?? false,
    cuVlmCheckStrategy: partial.cuVlmCheckStrategy ?? 'off',
    cuOnMismatch: partial.cuOnMismatch ?? 'stop_notify',
    cuVlmMaxRetries: partial.cuVlmMaxRetries ?? 1,
    cuMode: partial.cuMode ?? 'pixel',
    uiaWindow: partial.uiaWindow ?? '',
    // condition / 分支控制(YAML 來源:condition: true + expression+on_true/on_false 或 switch+cases)
    // 之前 buildStep 漏寫這 8 個欄位 → parseYaml 設好 cur.condition=true 等、進 buildStep 後全被丟掉、
    // stepsToYaml 一看 s.condition===undefined 就完全不寫 condition 區塊 → YAML round-trip 丟失條件節點
    condition: partial.condition ?? false,
    expression: partial.expression ?? '',
    onTrue: partial.onTrue ?? '',
    onFalse: partial.onFalse ?? '',
    switch: partial.switch ?? '',
    cases: partial.cases ?? {},
    default: partial.default ?? '',
    next: partial.next ?? '',
    llmRole: partial.llmRole ?? 'primary',
    // MCP 節點(同 condition/computer_use 的教訓:buildStep 白名單漏列 = YAML round-trip 丟節點)
    mcp: partial.mcp ?? false,
    mcpServer: partial.mcpServer ?? '',
    mcpTool: partial.mcpTool ?? '',
    mcpToolArgsText: partial.mcpToolArgsText ?? '',
    // 輸出 JSON Schema 合約
    jsonSchemaText: partial.jsonSchemaText ?? '',
    // 同類漏寫補齊:human_confirm 超時行為(wait/pass/reject/abort)、skill 主動問用戶模式
    hcOnTimeout: partial.hcOnTimeout ?? 'wait',
    askMode: partial.askMode ?? false,
    timeout: partial.timeout ?? (partial.humanConfirm ? 3600 : (partial.visualValidation ? 120 : (partial.webCrawler ? 600 : (partial.outlookAutomation ? 600 : (partial.subagent ? 600 : (partial.mcp ? 60 : 300)))))),
    // YAML 沒寫 retry 時的 fallback — 跟 newSkillData / newStepData 跟 backend
    // PipelineStep.retry default 一致（都是 1）。讓「貼 YAML 進來」跟「拉新節點」
    // 看到的預設值相同，避免使用者疑惑。
    retry: partial.retry ?? 1,
    index,
    status: 'idle',
    errorMsg: '',
  }
}
