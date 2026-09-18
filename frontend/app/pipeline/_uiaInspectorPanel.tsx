'use client'
/**
 * UIA inspector panel — 抓當下視窗 element tree、讓使用者選 element + 動作、
 * 自動加進 cu_actions[]。
 *
 * 設計:
 *  - 上方:視窗 title pattern input + foreground 模式 toggle + 「🔍 抓取元素」按鈕
 *  - 中間:可摺的 tree(每個 element 顯示 type / name / auto_id / enabled)
 *    - 滑過/點擊 element → 右側顯示可用 action(uia_click / uia_send_keys 等)
 *  - 點 action button → 把組好的 ComputerUseAction 加進 actions[](onAddAction)
 *  - 不錄製、不需 assets/、不會碰桌面動作
 */
import { useState, useCallback, useRef, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { ChevronDown, ChevronRight, MousePointerClick, Type, Eye, Hash, ListChecks, Clock, CheckCircle, RefreshCcw, Search, AppWindow, Crosshair, X } from 'lucide-react'
import { toast } from 'sonner'
import {
  uiaInspect, uiaHighlight, uiaListWindows,
  uiaPickerStart, uiaPickerPoll, uiaPickerConsume, uiaPickerStop,
  type UiaElement, type UiaInspectResult, type UiaWindowInfo
} from '@/lib/api'
import type { ComputerUseAction } from './_helpers'
import { windowPatternOf } from './_helpers'
import { VariableButton } from './_variablePicker'

interface Props {
  uiaWindow: string
  onUpdateWindow: (w: string) => void
  onAddAction: (action: ComputerUseAction) => void
  workflowId?: string
  /** 本節點的步驟名 —— 變數選單挑到「自己這步」的輸出時要轉成同節點語法 */
  stepName?: string
}

/**
 * 變數選單插入的是跨節點語法 {{ steps.X.output.Y }}，但如果 X 就是**本節點**，
 * 那個語法執行時必炸：步驟還在跑、它的 output 還不存在（steps 命名空間只有
 * 已完成的步驟）。同節點要用 {{Y}}（讀值動作 save_as 存進 step 變數、執行期替換）。
 * 這個坑不該讓使用者記 —— 選到自己這步的輸出就自動轉。
 */
function _localizeVarPath(path: string, stepName?: string): string {
  if (stepName) {
    const m = path.match(/^steps\.(.+?)\.output\.(.+)$/)
    if (m && m[1] === stepName) return m[2]   // {{總計金額}} 而不是 {{ steps.自己.output.總計金額 }}
  }
  return path
}

interface PickerState {
  element: UiaElement
  path: string[]   // 從 root 來的描述路徑(顯示用、不送 backend)
}

// ── 樹的可用性分析:過濾 + 判定 ──────────────────────────────
// 為什麼需要:瀏覽器視窗裡「最小化 / 網址列 / 工具列 / 分頁」全都是 UIA 控制項。
// 實測一個只有 13 個欄位的表單,整個視窗有 324 個節點、其中 277 個是瀏覽器外框。
// 不過濾的話樹根本翻不完,使用者要自己用眼睛找哪個是真正的頁面欄位。

/** 可互動控制項(要填值 / 要點的)。uiautomation 回的是帶 Control 字尾的名稱。 */
// 要「點」或「填」的控制項 —— 可指名率只用這組當分母
const _ACTIONABLE = [
  'Edit', 'ComboBox', 'CheckBox', 'RadioButton', 'Button',
  'List', 'ListItem', 'Spinner', 'Slider', 'Hyperlink', 'Tab', 'TabItem',
  'MenuItem', 'DataGrid', 'Table', 'Tree', 'TreeItem',
]
// 只能「讀」的控制項 —— uia_get_text 的主要目標(訂單編號、狀態列、查詢結果)
// 幾乎都是 TextControl。不列進清單的話,唯讀查詢頁會顯示成一片空白,
// 判定還會紅字說「只能用 CV/OCR」,把使用者推去走比較不可靠的路徑。
const _READABLE = ['Text', 'Document', 'StatusBar', 'Header', 'HeaderItem']
const _mk = (ns: string[]) => new Set<string>(ns.flatMap(n => [n, n + 'Control']))
const ACTIONABLE_TYPES = _mk(_ACTIONABLE)
const INTERACTIVE = new Set<string>([..._mk(_ACTIONABLE), ..._mk(_READABLE)])

/** 單層大括號的變數 typo。實測使用者打了 {總計金額}（單層）——
 * 執行期替換只認 {{變數}}，單層會把字面字元填進欄位而且動作回報成功。
 * 在加入動作的當下擋，比跑完才發現欄位裡有大括號好。 */
function _singleBraceProblem(text: string): string | null {
  // 先把合法的 {{...}} 挖掉，剩下還有 {xxx} 就是 typo
  const rest = text.replace(/\{\{[^{}]*\}\}/g, '')
  const m = rest.match(/\{([^{}]+)\}/)
  return m ? m[1] : null
}

/** 動作序列上的人話描述:一眼看出這步「讀什麼存到哪」或「填什麼進哪」。 */
function describeAction(
  type: string,
  el: UiaElement,
  extra: Partial<ComputerUseAction>,
): string {
  const target = el.name || el.type || '控制項'
  const v = (extra as any).save_as
  const txt = (extra as any).text
  const keys = (extra as any).keys
  switch (type) {
    case 'uia_get_text':
      return v ? `讀「${target}」→ {{${v}}}` : `讀「${target}」(沒設變數、值會被丟掉)`
    case 'uia_get_table_rowcount':
      return v ? `數「${target}」列數 → {{${v}}}` : `數「${target}」列數`
    case 'uia_send_keys':
      if (txt) return `填入「${target}」← ${txt}`
      if (keys) return `對「${target}」按 ${Array.isArray(keys) ? keys.join('+') : keys}`
      return `送鍵到「${target}」`
    case 'uia_click':
      return `點擊「${target}」`
    case 'uia_click_cell': {
      // row 常是 {{rows + 1}} 這種最需要核對的值,落到 default 分支就看不到了
      const r = (extra as any).row
      const c = (extra as any).column
      return `點「${target}」第 ${r ?? '?'} 列第 ${c ?? '?'} 欄`
    }
    case 'uia_wait_enabled':
      return `等「${target}」可用`
    case 'uia_assert_state':
      return `驗證「${target}」${(extra as any).check || ''}`
    case 'uia_close_window':
      return `關閉視窗「${target}」`
    case 'uia_select':
      return `選「${target}」→ ${txt || '?'}`
    case 'uia_wait':
      return `等「${target}」${(extra as any).until === 'disappear' ? '消失' : (extra as any).until === 'text_contains' ? `文字含「${txt}」` : (extra as any).until === 'text_equals' ? `文字=「${txt}」` : '出現'}`
    case 'if_element_found':
      return `若「${target}」出現 → ${(((extra as any).then || []) as unknown[]).length} 個子動作，否則 ${(((extra as any)['else'] || []) as unknown[]).length} 個`
    case 'uia_get_clipboard':
      return `讀剪貼簿 → {{${(extra as any).save_as || '?'}}}`
    case 'uia_set_clipboard':
      return `寫剪貼簿 ← ${txt || ''}`
    default:
      return `${type} ${el.type}${el.name ? ':' + el.name : ''}`
  }
}

const VIEW_BTN = 'px-2.5 py-1 whitespace-nowrap shrink-0'
const VIEW_ON = ' bg-purple-600 text-white'
const VIEW_OFF = ' bg-white text-gray-600 hover:bg-gray-50'
const ROW_BASE = 'flex items-baseline gap-2 px-2 py-1.5 cursor-pointer hover:bg-purple-50 leading-snug'

function isInteractive(el: UiaElement): boolean {
  return INTERACTIVE.has(el.type || '')
}

/** 走訪整棵樹。 */
function walkTree(el: UiaElement | null | undefined, out: UiaElement[] = []): UiaElement[] {
  if (!el) return out
  out.push(el)
  ;(el.children || []).forEach(c => walkTree(c, out))
  return out
}

/**
 * 找「網頁內容」子樹。瀏覽器把頁面塞在 DocumentControl 底下,
 * 只分析它才有意義。取節點最多的那個 Document(避開空的隱藏分頁)。
 */
function findPageRoot(root: UiaElement | null | undefined): UiaElement | null {
  if (!root) return null
  let best: UiaElement | null = null
  let bestSize = 0
  for (const el of walkTree(root)) {
    if ((el.type || '').startsWith('Document')) {
      const size = walkTree(el).length
      if (size > bestSize) { best = el; bestSize = size }
    }
  }
  return bestSize > 3 ? best : null
}

interface TreeStats {
  isWeb: boolean
  totalAll: number        // 整個視窗的節點數
  totalScoped: number     // 分析範圍內的節點數
  interactive: number
  withId: number
  nameOnly: number
  anonymous: number
  score: number           // 0..1 可指名率
}

function analyzeTree(root: UiaElement | null | undefined, scoped: UiaElement | null | undefined): TreeStats {
  const all = walkTree(root)
  const nodes = walkTree(scoped || root)
  // 計分只算「要點/要填」的 —— 唯讀 Text 沒有 auto_id 是常態,
  // 算進分母會把可指名率壓低、誤判成「不適合 UIA」
  const inter = nodes.filter(e => ACTIONABLE_TYPES.has(e.type || ''))
  const withId = inter.filter(e => (e.auto_id || '').trim()).length
  const nameOnly = inter.filter(e => !(e.auto_id || '').trim() && (e.name || '').trim()).length
  const anonymous = inter.length - withId - nameOnly
  // auto_id 給滿分、只有 name 給半分 —— name 是畫面文字,文案一改就失效
  const score = inter.length ? (withId * 2 + nameOnly) / (inter.length * 2) : 0
  return {
    isWeb: !!scoped,
    totalAll: all.length,
    totalScoped: nodes.length,
    interactive: inter.length,
    withId, nameOnly, anonymous, score,
  }
}

function verdictOf(s: TreeStats): { tone: 'good' | 'warn' | 'bad'; text: string } {
  if (!s.interactive) return { tone: 'bad', text: 'UIA 看不到可操作的控制項 → 這個畫面只能用 CV / OCR' }
  const pct = Math.round(s.score * 100)
  if (s.score >= 0.8) return { tone: 'good', text: `很適合 UIA（可指名率 ${pct}%）→ 優先用 UIA，不受解析度與視窗位置影響` }
  if (s.score >= 0.5) return { tone: 'warn', text: `部分可用（可指名率 ${pct}%）→ UIA 為主，抓不到的欄位退 CV / OCR` }
  return { tone: 'bad', text: `可指名率僅 ${pct}% → UIA 幫助有限，建議以 CV / OCR 為主` }
}

export default function UiaInspectorPanel({ uiaWindow, onUpdateWindow, onAddAction, workflowId, stepName }: Props) {
  const [tree, setTree] = useState<UiaInspectResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set([''])) // root 路徑
  const [picker, setPicker] = useState<PickerState | null>(null)
  // hover highlight 節流(避免快速滑過 spam backend)
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastHoverPathRef = useRef<string>('')
  // 「列出視窗」popup
  const [windowsList, setWindowsList] = useState<UiaWindowInfo[]>([])
  const [showWindows, setShowWindows] = useState(false)
  const [loadingWindows, setLoadingWindows] = useState(false)
  // 進階摺疊區(列視窗 + tree 路徑、預設收折、99% 場景用 Live Picker)
  // 滑鼠定位收折(降級):主流程是「列視窗→抓取元素」,實際使用與 SOP 都走樹狀選取
  const [pickerFoldOpen, setPickerFoldOpen] = useState(false)
  // Live Picker(滑鼠 hover 桌面選元素)
  const [pickerActive, setPickerActive] = useState(false)
  const [hoveredEl, setHoveredEl] = useState<UiaElement | null>(null)
  // 預設就聚焦頁面內容 + 只看可操作元素 —— 不過濾的話樹有 300+ 節點、翻不完
  const [pageOnly, setPageOnly] = useState(true)
  const [interactiveOnly, setInteractiveOnly] = useState(true)
  // 預設走「欄位清單」:使用者想的是「找補金額那一格」,不是「第 3 層 Pane 的第 2 個」
  const [view, setView] = useState<'list' | 'tree'>('list')
  const [q, setQ] = useState('')
  const pickerPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const pickerActiveRef = useRef(false)   // unmount cleanup 用、避免 useEffect deps=[pickerActive]
                                          // 在 state 改變時誤觸發 cleanup 把 setInterval 砍掉

  const inspect = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      // ⚠ 深度一定要夠:瀏覽器把頁面內容埋在自己的外框底下,實測 Edge 上一個平常的
      //   表單欄位在**深度 13-14**。深度 6 抓下來全是最小化/網址列/索引標籤,
      //   使用者要找的欄位根本不在樹裡 —— 而且畫面上看不出來,只會覺得「找不到我要的」。
      //   公司入口網站(框架 + iframe 內嵌舊系統)實測 18 層仍整片表單被截掉,只剩頁尾連結。
      const r = await uiaInspect({ window: uiaWindow, max_depth: 60, max_children_per_node: 200 })
      setTree(r)
      setExpanded(new Set(['']))
      setPicker(null)
      toast.success(`已讀取 ${r.window.name || '(foreground)'} 的元素樹`)
    } catch (e) {
      setError((e as Error).message)
      setTree(null)
    } finally {
      setLoading(false)
    }
  }, [uiaWindow])

  const loadWindows = useCallback(async () => {
    setLoadingWindows(true)
    try {
      const r = await uiaListWindows()
      setWindowsList(r.windows || [])
      setShowWindows(true)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setLoadingWindows(false)
    }
  }, [])

  // Live Picker:啟動 + 輪詢 + 確認後 reset、停止 picker
  const startPicker = useCallback(async () => {
    if (pickerActiveRef.current) return
    try {
      await uiaPickerStart()
      pickerActiveRef.current = true
      setPickerActive(true)
      setHoveredEl(null)
      toast.success('🎯 移動滑鼠到目標、按 F8 確認、F9 取消', { duration: 4000 })
      // 輪詢:每 250ms 拿狀態
      pickerPollRef.current = setInterval(async () => {
        try {
          const s = await uiaPickerPoll()
          setHoveredEl(s.hovered)
          if (s.confirmed) {
            const consumed = await uiaPickerConsume()
            if (consumed.element) {
              setPicker({ element: consumed.element, path: ['picked'] })
              toast.success(`已選 ${consumed.element.type}${consumed.element.name ? ': ' + consumed.element.name.slice(0, 40) : ''}`)
            }
            // 確認後 picker 已自停;清 polling
            if (pickerPollRef.current) { clearInterval(pickerPollRef.current); pickerPollRef.current = null }
            pickerActiveRef.current = false
            setPickerActive(false)
            setHoveredEl(null)
          } else if (!s.running) {
            // F9 取消或 picker 自停
            if (pickerPollRef.current) { clearInterval(pickerPollRef.current); pickerPollRef.current = null }
            pickerActiveRef.current = false
            setPickerActive(false)
            setHoveredEl(null)
            if (s.error) toast.error(s.error)
          }
        } catch {
          // 失敗不打擾、下次再 poll
        }
      }, 250)
    } catch (e) {
      toast.error((e as Error).message)
      pickerActiveRef.current = false
      setPickerActive(false)
    }
  }, [])

  const stopPicker = useCallback(async () => {
    if (!pickerActiveRef.current) return
    try { await uiaPickerStop() } catch {}
    if (pickerPollRef.current) { clearInterval(pickerPollRef.current); pickerPollRef.current = null }
    pickerActiveRef.current = false
    setPickerActive(false)
    setHoveredEl(null)
  }, [])

  // unmount cleanup(deps 空、只在 component 真的 unmount 時跑、避免每次 pickerActive 變動誤觸發)
  useEffect(() => () => {
    if (pickerPollRef.current) clearInterval(pickerPollRef.current)
    if (pickerActiveRef.current) uiaPickerStop().catch(() => {})
  }, [])

  const togglePath = (path: string) => {
    setExpanded(s => {
      const next = new Set(s)
      next.has(path) ? next.delete(path) : next.add(path)
      return next
    })
  }

  // 把 element 包成 ComputerUseAction、推給 panel
  const addAction = (type: ComputerUseAction['type'], extra: Partial<ComputerUseAction> = {}) => {
    if (!picker) return
    const el = picker.element
    const control = {
      ...(el.type ? { type: el.type } : {}),
      ...(el.name ? { name: el.name } : {}),
      ...(el.auto_id ? { auto_id: el.auto_id } : {}),
    }
    const action: ComputerUseAction = {
      type,
      control,
      // 記住這個元素是從哪個視窗挑的。backend 的優先序是 action.window > step 的
      // uia_window > foreground,所以帶上之後,同一個節點就能跨視窗
      //(例:從 EAP 讀值 → 填進 BPM 單)。不帶的話使用者一切換「目標視窗」,
      // 先前挑的動作會跑去新視窗找舊欄位、全部失敗。
      ...(uiaWindow.trim() ? { window: uiaWindow.trim() } : {}),
      // 帶上 picker 抓到的 rect、給 backend 在沒 Name/auto_id(generic Pane/GroupControl)時
      // 用 ControlFromPoint(rect 中心)當 fallback、避免 LookupError "searchProperties must not be empty"
      ...(el.rect && el.rect.length === 4 ? { rect: el.rect } : {}),
      // 描述要讓人在序列上直接核對「這步做了什麼」——
      // 只寫 uia_get_text EditControl:營業稅額 的話,看不出存到哪個變數、也看不出填了什麼值,
      // 使用者必須逐一點開才知道對不對。
      description: describeAction(type, el, extra),
      ...extra,
    }
    // 巢狀 then/else 子動作也蓋上視窗 —— 不蓋的話它們落回「步驟層級」的
    // 目標視窗,使用者切去抓別的視窗的元素後,子動作就跑錯視窗找(實測:
    // 目標視窗被切到 Tk,分歧裡的「點確定」跑去 Tk 找、整條炸掉)。
    if (action.window) {
      for (const nf of ['then', 'else'] as const) {
        const lst = (action as unknown as Record<string, unknown>)[nf]
        if (Array.isArray(lst)) {
          (action as unknown as Record<string, unknown>)[nf] =
            lst.map((a: Record<string, unknown>) => ({ window: action.window, ...a }))
        }
      }
    }
    onAddAction(action)
    toast.success(`已加 ${type}(${el.name || el.type})`)
  }

  // hover highlight 節流 + 觸發
  const handleHover = useCallback((path: string, el: UiaElement) => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current)
    if (path === lastHoverPathRef.current) return
    hoverTimerRef.current = setTimeout(() => {
      lastHoverPathRef.current = path
      const [x, y, w, h] = el.rect || [0, 0, 0, 0]
      if (w > 0 && h > 0) {
        uiaHighlight({ x, y, width: w, height: h, ttl_ms: 1500 })
      }
    }, 80)
  }, [])
  const handleHoverEnd = useCallback(() => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current)
  }, [])

  /** 攤平成 (元素, path) 清單 —— path 要跟 renderNode 的算法一致,選取才對得起來。 */
  const flatten = (
    el: UiaElement | null | undefined,
    path = '',
    out: { el: UiaElement; path: string }[] = [],
  ) => {
    if (!el) return out
    out.push({ el, path })
    ;(el.children || []).forEach((c, i) => flatten(c, path ? path + '/' + i : String(i), out))
    return out
  }

  /** 這個節點自己或後代有沒有可操作元素。用來決定「只看可操作」時要不要保留容器。 */
  const keepNode = (el: UiaElement): boolean => {
    if (!interactiveOnly) return true
    if (isInteractive(el)) return true
    return (el.children || []).some(keepNode)
  }

  const renderNode = (el: UiaElement, path: string, depth: number = 0) => {
    if (!keepNode(el)) return null
    const hasKids = el.children && el.children.length > 0
    const isOpen = expanded.has(path)
    const dimmed = !el.enabled || el.offscreen
    const isSelected = picker && picker.path.join('/') === path
    return (
      <div key={path}>
        <div
          className={`flex items-start gap-1 py-0.5 pr-2 hover:bg-purple-50 cursor-pointer ${
            isSelected ? 'bg-purple-100 border-l-2 border-purple-500' : ''
          } ${dimmed ? 'opacity-50' : ''}`}
          style={{ paddingLeft: 4 + depth * 14 }}
          onMouseEnter={() => handleHover(path, el)}
          onMouseLeave={handleHoverEnd}
          onClick={() => {
            setPicker({ element: el, path: path.split('/') })
            // 點擊用較長 ttl(3 秒)強調
            const [x, y, w, h] = el.rect || [0, 0, 0, 0]
            if (w > 0 && h > 0) uiaHighlight({ x, y, width: w, height: h, ttl_ms: 3000 })
          }}
        >
          {hasKids ? (
            <button onClick={(e) => { e.stopPropagation(); togglePath(path) }} className="p-0.5 text-gray-400">
              {isOpen ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            </button>
          ) : (
            <span className="w-4 inline-block" />
          )}
          <div className="flex-1 min-w-0 text-[11px] leading-tight">
            <span className="font-mono text-purple-700">{el.type || '?'}</span>
            {el.name && <span className="text-gray-700 truncate">{' · '}{el.name.length > 60 ? el.name.slice(0, 60) + '…' : el.name}</span>}
            {el.auto_id && <span className="text-gray-400 font-mono ml-1">[{el.auto_id}]</span>}
            {!el.enabled && <span className="text-amber-600 ml-1">(disabled)</span>}
            {el.offscreen && <span className="text-gray-400 ml-1">(offscreen)</span>}
          </div>
        </div>
        {hasKids && isOpen && el.children.map((child, ci) =>
          renderNode(child, path ? `${path}/${ci}` : String(ci), depth + 1)
        )}
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-purple-200 bg-purple-50/30 p-3 space-y-3">
      {/* 🎯 滑鼠定位:降級收折 —— 留給樹太深翻不到/匿名元素認不出/原生 app 樹爆炸的場景 */}
      <div className="rounded-lg border border-gray-300 bg-gray-50 overflow-hidden">
        <button
          type="button"
          onClick={() => setPickerFoldOpen(v => !v)}
          className="w-full flex items-center gap-2 px-2.5 py-1.5 text-left hover:bg-gray-100 transition-colors text-[12px]"
        >
          {(pickerFoldOpen || pickerActive) ? <ChevronDown className="w-3.5 h-3.5 text-gray-500" /> : <ChevronRight className="w-3.5 h-3.5 text-gray-500" />}
          <span className="font-semibold text-gray-700 flex-1">🎯 滑鼠定位元素(指到哪選哪)</span>
          <span className="text-[10px] text-gray-500">樹太深 / 匿名元素時用</span>
        </button>
        {(pickerFoldOpen || pickerActive) && (
        <div className={`m-2 rounded-lg border-2 ${pickerActive ? 'border-emerald-400 bg-emerald-50' : 'border-emerald-200 bg-white'} p-3`}>
        {!pickerActive ? (
          <div>
            <button
              onClick={startPicker}
              className="w-full px-3 py-2.5 bg-emerald-600 text-white rounded-lg text-sm font-medium hover:bg-emerald-700 flex items-center justify-center gap-2"
            >
              <Crosshair className="w-4 h-4" />
              🎯 啟動滑鼠定位
            </button>
            <p className="text-[11px] text-emerald-700 mt-1.5 leading-relaxed">
              啟動後:**滑鼠移到桌面任意 UI 元素**、紅框會跟隨。
              按 <span className="font-mono font-bold">F8</span> 確認當前 hover 元素、
              按 <span className="font-mono font-bold">F9</span> 取消。
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="inline-block w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
              <span className="font-semibold text-emerald-700 text-sm">🎯 定位中…</span>
              <span className="text-[10px] text-gray-500 ml-auto">F8 確認 / F9 取消</span>
            </div>
            {hoveredEl ? (
              <div className="bg-white border border-emerald-200 rounded p-2 text-[11px]">
                <div className="flex items-center gap-1 mb-0.5">
                  <span className="font-mono text-purple-700 font-semibold">{hoveredEl.type || '?'}</span>
                  {!hoveredEl.enabled && <span className="text-amber-600 ml-1">(disabled)</span>}
                </div>
                {hoveredEl.name && <div className="text-gray-700 truncate"><strong>name:</strong> {hoveredEl.name.slice(0, 80)}</div>}
                {hoveredEl.auto_id && <div className="text-gray-500 font-mono truncate"><strong>auto_id:</strong> {hoveredEl.auto_id}</div>}
                {hoveredEl.rect && hoveredEl.rect.length === 4 && (
                  <div className="text-gray-400 font-mono">
                    rect: {hoveredEl.rect[0]},{hoveredEl.rect[1]} {hoveredEl.rect[2]}×{hoveredEl.rect[3]}
                  </div>
                )}
              </div>
            ) : (
              <div className="text-[11px] text-gray-500 italic px-1">移動滑鼠到桌面元素…</div>
            )}
          </div>
        )}
      </div>
        )}
      </div>

      {/* 主要路徑:選視窗 → 抓取元素(列視窗 / 搜尋 / 樹狀選取)、常駐展開 */}
      <div className="rounded-lg border border-purple-200 bg-white overflow-hidden">
        <div className="w-full flex items-center gap-2 px-2.5 py-1.5 text-[12px] bg-purple-50/70 border-b border-purple-100">
          <span className="font-semibold text-purple-700 flex-1">🪟 選視窗 → 🔍 抓取元素</span>
          <span className="text-[10px] text-gray-500">列視窗 / 搜尋 / 樹狀選取</span>
        </div>
        {true && (
          <div className="px-3 pb-3 pt-2 space-y-3">
      {/* 視窗 pattern 輸入 + 抓取按鈕 */}
      <div>
        <label className="text-xs font-semibold text-purple-700 uppercase tracking-wide block mb-1.5">
          🪟 目標視窗(支援 wildcard *、空 = 當前 foreground)
        </label>
        {/* 輸入單獨一行 + 三顆按鈕第二行(等寬、不會被擠變形) */}
        <input
          value={uiaWindow}
          onChange={e => onUpdateWindow(e.target.value)}
          placeholder="例:*檔案總管* / *{{ input.app }}* / 留空用 foreground"
          className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm font-mono outline-none focus:border-purple-400 focus:ring-1 focus:ring-purple-400/20 bg-white mb-1.5"
        />
        <div className="flex items-center gap-1.5">
          <VariableButton
            workflowId={workflowId}
            onPick={(p) => onUpdateWindow(`${uiaWindow || ''}{{ ${p} }}`)}
            className="!flex-shrink-0"
          />
          <button
            onClick={loadWindows}
            disabled={loadingWindows}
            title="列出當下所有 top-level 視窗、選一個自動填 pattern"
            className="flex-1 px-2 py-1.5 bg-white border border-purple-300 text-purple-700 rounded-lg text-xs font-medium hover:bg-purple-50 disabled:bg-gray-100 flex items-center justify-center gap-1 whitespace-nowrap"
          >
            {loadingWindows ? <RefreshCcw className="w-3.5 h-3.5 animate-spin shrink-0" /> : <AppWindow className="w-3.5 h-3.5 shrink-0" />}
            列視窗
          </button>
          <button
            onClick={inspect}
            disabled={loading}
            className="flex-1 px-2 py-1.5 bg-purple-600 text-white rounded-lg text-xs font-medium hover:bg-purple-700 disabled:bg-gray-300 flex items-center justify-center gap-1 whitespace-nowrap"
          >
            {loading ? <RefreshCcw className="w-3.5 h-3.5 animate-spin shrink-0" /> : <Search className="w-3.5 h-3.5 shrink-0" />}
            {loading ? '讀取中…' : '抓取元素'}
          </button>
        </div>
        <p className="text-[10px] text-gray-500 mt-1 leading-relaxed">
          ⚠ 留空會抓「當前 foreground」、會抓到瀏覽器自身;**用「列視窗」選目標** 才能抓到背景的應用(檔案總管 / 公司系統等不必拉到前景)。
        </p>

        {/* 視窗 list popup */}
        {showWindows && (
          <div className="mt-2 rounded-lg border border-purple-300 bg-white max-h-64 overflow-y-auto shadow-md">
            <div className="sticky top-0 flex items-center justify-between bg-purple-50 px-2 py-1 border-b border-purple-200 text-[11px] text-purple-700">
              <span>共 {windowsList.length} 個 top-level 視窗、點一個自動填 pattern</span>
              <button onClick={() => setShowWindows(false)} className="text-purple-500 hover:text-purple-700">關閉</button>
            </div>
            {windowsList.length === 0 && <div className="px-3 py-4 text-xs text-gray-400 text-center">沒有可顯示的視窗</div>}
            {windowsList.map((w, i) => (
              <button
                key={i}
                onClick={() => {
                  const trimmed = windowPatternOf(w.name) || w.name
                  onUpdateWindow(trimmed)
                  setShowWindows(false)
                  toast.success(`已套用 pattern:${trimmed}`)
                }}
                onMouseEnter={() => {
                  if (w.rect && w.rect.length === 4 && w.rect[2] > 0) {
                    uiaHighlight({ x: w.rect[0], y: w.rect[1], width: w.rect[2], height: w.rect[3], ttl_ms: 1500 })
                  }
                }}
                className="w-full text-left px-3 py-1.5 hover:bg-purple-50 border-b border-gray-100 last:border-b-0 flex items-start gap-2 text-xs"
              >
                <AppWindow className="w-3 h-3 text-purple-500 mt-0.5 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-gray-800 truncate" title={w.name}>{w.name}</div>
                  <div className="text-[10px] text-gray-400 font-mono truncate">
                    [{w.class}] {w.rect[0]},{w.rect[1]} {w.rect[2]}×{w.rect[3]}
                    {w.is_offscreen && <span className="text-amber-500 ml-1">(offscreen)</span>}
                  </div>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-2 text-xs text-red-700">
          ⚠ {error}
        </div>
      )}

      {tree && tree.ok && (() => {
        const pageRoot = findPageRoot(tree.tree)
        const scoped = pageOnly ? pageRoot : null
        const stats = analyzeTree(tree.tree, scoped)
        const v = verdictOf(stats)
        const tone = v.tone === 'good'
          ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
          : v.tone === 'warn'
            ? 'bg-amber-50 border-amber-200 text-amber-800'
            : 'bg-rose-50 border-rose-200 text-rose-800'
        return (
        <div className="space-y-2">
          <div className="text-[11px] text-purple-700 bg-purple-100/50 rounded px-2 py-1">
            <strong>{tree.window.name || '(foreground)'}</strong>
            {tree.window.class && <span className="ml-2 text-gray-500">[{tree.window.class}]</span>}
          </div>

          {/* 可用性判定 —— 按下抓取當下就知道該走 UIA 還是 CV,不用自己數 300 個節點 */}
          <div className={`border rounded-lg px-2.5 py-2 text-[11px] ${tone}`}>
            <div className="font-semibold leading-snug">{v.text}</div>
            {stats.interactive > 0 && (
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono opacity-90 [&>span]:whitespace-nowrap">
                <span>可操作 {stats.interactive}</span>
                <span>auto_id {stats.withId}</span>
                <span>僅 name {stats.nameOnly}</span>
                {stats.anonymous > 0 && <span>匿名 {stats.anonymous}</span>}
              </div>
            )}
            {pageRoot && (
              <div className="mt-1 opacity-75">
                偵測到網頁：視窗共 {stats.totalAll} 個節點，
                {pageOnly
                  ? `已排除 ${stats.totalAll - stats.totalScoped} 個瀏覽器外框（網址列 / 工具列 / 分頁）`
                  : '含瀏覽器外框、數字會被灌水'}
              </div>
            )}
            {tree.truncated && (tree.truncated.depth_cut > 0 || tree.truncated.budget_hit) && (
              <div className="mt-1 font-semibold text-red-700">
                ⚠ 元素樹沒讀完（
                {tree.truncated.budget_hit
                  ? `超過 ${tree.truncated.nodes} 個節點上限`
                  : `${tree.truncated.depth_cut} 處超過 ${tree.truncated.max_depth} 層`}
                ）—— 找不到的欄位可能在被截掉的部分，改用 CV / OCR 或縮小目標視窗
              </div>
            )}
          </div>

          {/* 檢視切換 + 搜尋 */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex rounded-md overflow-hidden border border-gray-300 text-[11px] shrink-0">
              <button type="button" onClick={() => setView('list')}
                className={VIEW_BTN + (view === 'list' ? VIEW_ON : VIEW_OFF)}>欄位清單</button>
              <button type="button" onClick={() => setView('tree')}
                className={VIEW_BTN + ' border-l border-gray-300' + (view === 'tree' ? VIEW_ON : VIEW_OFF)}>完整結構</button>
            </div>
            {view === 'list' && (
              <input
                value={q}
                onChange={e => setQ(e.target.value)}
                placeholder="搜尋欄位名稱（例：金額）"
                className="flex-1 min-w-[120px] text-[11px] px-2 py-1 rounded border border-gray-300 outline-none focus:border-purple-500"
              />
            )}
            {pageRoot && (
              <label className="flex items-center gap-1 cursor-pointer text-[11px] text-gray-600 whitespace-nowrap shrink-0">
                <input type="checkbox" className="shrink-0" checked={pageOnly} onChange={e => setPageOnly(e.target.checked)} />
                只看網頁內容
              </label>
            )}
            {view === 'tree' && (
              <label className="flex items-center gap-1 cursor-pointer text-[11px] text-gray-600 whitespace-nowrap shrink-0">
                <input type="checkbox" className="shrink-0" checked={interactiveOnly} onChange={e => setInteractiveOnly(e.target.checked)} />
                只看可操作
              </label>
            )}
          </div>

          {view === 'list' ? (() => {
            const kw = q.trim().toLowerCase()
            const rows = flatten(scoped || tree.tree)
              .filter(r => isInteractive(r.el))
              .filter(r => !kw
                || (r.el.name || '').toLowerCase().includes(kw)
                || (r.el.auto_id || '').toLowerCase().includes(kw)
                || (r.el.type || '').toLowerCase().includes(kw))
            return (
              <div className="border border-gray-200 rounded-lg bg-white overflow-y-auto max-h-[40vh] divide-y divide-gray-100">
                {rows.length === 0 ? (
                  <div className="text-center text-[11px] text-gray-400 py-4">
                    {kw ? '沒有符合的欄位' : '這個範圍內沒有可操作欄位 —— 切到「完整結構」看看'}
                  </div>
                ) : rows.map(({ el, path }) => {
                  const sel = picker && picker.path.join('/') === path
                  const hl = (ttl: number) => {
                    const r = el.rect || [0, 0, 0, 0]
                    if (r[2] > 0 && r[3] > 0) uiaHighlight({ x: r[0], y: r[1], width: r[2], height: r[3], ttl_ms: ttl })
                  }
                  return (
                    <div
                      key={path}
                      onMouseEnter={() => handleHover(path, el)}
                      onClick={() => { setPicker({ element: el, path: path.split('/') }); hl(3000) }}
                      className={ROW_BASE + (sel ? ' bg-purple-100' : '') + (el.enabled ? '' : ' opacity-50')}
                    >
                      <span className="text-[12px] text-gray-800 font-medium truncate flex-1 min-w-0">
                        {el.name || <span className="text-gray-400 italic">(沒有名稱)</span>}
                      </span>
                      <span className="text-[10px] font-mono text-purple-600 shrink-0">
                        {(el.type || '').replace('Control', '')}
                      </span>
                      {el.auto_id
                        ? <span className="text-[10px] font-mono text-gray-400 shrink-0">{el.auto_id}</span>
                        : <span className="text-[10px] text-amber-600 shrink-0">無 id</span>}
                    </div>
                  )
                })}
              </div>
            )
          })() : (
            <div className="border border-gray-200 rounded-lg bg-white overflow-y-auto max-h-[40vh]">
              {renderNode(scoped || tree.tree, '', 0) || (
                <div className="text-center text-[11px] text-gray-400 py-4">
                  這個範圍內沒有可操作元素 —— 取消「只看可操作」看完整結構
                </div>
              )}
            </div>
          )}
          {view === 'list' && (
            <p className="text-[10px] text-gray-500 px-0.5">
              滑過會在畫面上框出位置、點一下選取。找不到欄位就切「完整結構」，或取消「只看網頁內容」。
            </p>
          )}
        </div>
        )
      })()}

      {!tree && !loading && !error && (
        <div className="text-center text-[11px] text-gray-500 py-3">
          按「抓取元素」讀視窗 UIA tree(整棵結構)、適合想看完整層級的場景
        </div>
      )}
          </div>
        )}
      </div>

      {/* 已選 element + 動作選單(獨立區塊、Live Picker / tree 都共用) */}
      {picker && (
        <div className="rounded-lg border-2 border-emerald-400 bg-emerald-50/40 p-2">
          <div className="text-[11px] font-semibold text-emerald-700 mb-1.5 flex items-center gap-1">
            <CheckCircle className="w-3 h-3" /> 已選元素 — 從下面選動作加進序列
            <button onClick={() => setPicker(null)} className="ml-auto text-gray-400 hover:text-gray-700">
              <X className="w-3 h-3" />
            </button>
          </div>
          <UiaActionPicker element={picker.element} onAdd={addAction} workflowId={workflowId} stepName={stepName} />
        </div>
      )}
    </div>
  )
}

/** 已選 element 的動作選擇器:列各 uia_* type 對應的按鈕 + 場景說明 */
function UiaActionPicker({
  element,
  onAdd,
  workflowId,
  stepName,
}: {
  element: UiaElement
  onAdd: (type: ComputerUseAction['type'], extra?: Partial<ComputerUseAction>) => void
  workflowId?: string
  stepName?: string
}) {
  const [textInput, setTextInput] = useState('')
  const [keysInput, setKeysInput] = useState('')
  const [saveAsInput, setSaveAsInput] = useState('')
  const [rowInput, setRowInput] = useState<string>('')
  const [colInput, setColInput] = useState<string>('')
  const [clipboardInput, setClipboardInput] = useState<string>('')
  const [optionInput, setOptionInput] = useState('')
  const [waitUntil, setWaitUntil] = useState('appear')
  const [waitText, setWaitText] = useState('')
  const [waitTimeout, setWaitTimeout] = useState('60')
  const [ifTimeout, setIfTimeout] = useState('3')
  const [ifThen, setIfThen] = useState('click')
  const [ifGoneTimeout, setIfGoneTimeout] = useState('300')
  const [ifElse, setIfElse] = useState('none')
  const [ifDlPattern, setIfDlPattern] = useState('*.xlsx')
  const [ifDlTimeout, setIfDlTimeout] = useState('300')
  const [ifDlSaveAs, setIfDlSaveAs] = useState('下載檔')
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const isGrid = ['DataGrid', 'List', 'Tree', 'Table'].some(s => element.type.includes(s))
  const isEditable = ['Edit', 'Document', 'Combo'].some(s => element.type.includes(s))
  const isCombo = element.type.includes('ComboBox')

  return (
    <div className="space-y-2">
      <div className="text-[11px] text-gray-700 bg-white border border-gray-200 rounded px-2 py-1">
        <strong>已選:</strong> <span className="font-mono text-purple-700">{element.type}</span>
        {element.name && <span className="text-gray-600"> · {element.name.slice(0, 60)}</span>}
        {element.auto_id && <span className="text-gray-400 font-mono ml-1">[{element.auto_id}]</span>}
      </div>

      {/* 主要動作:點擊(最常用)+ 等 enabled */}
      <div className="grid grid-cols-2 gap-1.5">
        <BigActionBtn
          icon={MousePointerClick}
          title="點擊"
          desc="按鈕 / 連結 / cell"
          onClick={() => onAdd('uia_click')}
          help={{
            usage: '點任何可互動元素;優先走 InvokePattern / TogglePattern 等程式介面、視窗在背景也能點、不會搶前景',
            scenario: '點企業系統的「儲存」「送出」「下一步」按鈕、點 checkbox、點 list 項目',
            example: '選 [Save Button] → 點擊\n背景觸發、不必把視窗叫到前面',
          }}
        />
        <BigActionBtn
          icon={Clock}
          title="等就緒"
          desc="等元素出現 + enabled"
          onClick={() => onAdd('uia_wait_enabled')}
          help={{
            usage: '輪詢元素直到 IsEnabled=True 或 timeout;loading 結束後 race condition 防呆',
            scenario: '點完「載入」按鈕、要等下一步按鈕變藍才能繼續;網路慢時尤其重要',
            example: '步驟 1: 點「載入」\n步驟 2: 等就緒(目標:「下一步」按鈕)\n步驟 3: 點「下一步」',
          }}
        />
      </div>

      {/* 讀文字(抓值)—— 升級成一級動作。原本摺在「進階動作」裡，
          使用者選了「找補金額」卻只看得到點擊/等就緒，以為抓值要靠 OCR。
          UIA 讀結構比 OCR 準：ValuePattern 直接拿 value，不受解析度/遮擋影響。 */}
      <div className="bg-emerald-50/60 border border-emerald-200 rounded p-2 space-y-1">
        <div className="text-[11px] font-semibold text-emerald-700">📖 讀文字存變數（抓值）</div>
        <div className="flex gap-1 items-center">
          <input
            value={saveAsInput}
            onChange={e => setSaveAsInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && saveAsInput.trim()) {
                onAdd('uia_get_text', { save_as: saveAsInput.trim() })
              }
            }}
            placeholder="變數名，例：找補金額"
            className="flex-1 min-w-0 border border-gray-200 rounded px-2 py-1 text-xs font-mono"
          />
          <HelpTooltip
            title="讀文字"
            usage="把此控制項的文字 / value 讀出來、存進變數;優先 ValuePattern.Value、退 Name"
            scenario="抓「找補金額」「訂單編號」等欄位值 → 下一個動作用 {{變數}} 填到別處"
            example={'save_as="找補金額"\n→ 後續 type_text text="{{找補金額}}"、或寫剪貼簿跨應用貼'}
          >
            <button
              onClick={() => {
                const v = saveAsInput.trim()
                if (!v) { toast.error('請填變數名'); return }
                // ⚠ 執行期的 {{變數}} 替換只認文字/數字/底線。實測有使用者把
                //   讀到的值「40,425」當變數名 —— {{40,425}} 永遠不會被替換，
                //   欄位裡會出現字面的大括號。當場擋下比事後查好。
                if (!/^[\p{L}\p{N}_]+$/u.test(v)) {
                  toast.error('變數名只能用中英文、數字、底線（這是名字，不是要讀的值）。例：找補金額')
                  return
                }
                onAdd('uia_get_text', { save_as: v })
              }}
              className="shrink-0 whitespace-nowrap px-2 py-1 bg-emerald-600 text-white rounded text-xs flex items-center gap-1 hover:bg-emerald-700 transition-colors"
            >
              <Eye className="w-3 h-3 shrink-0" /> 讀文字
            </button>
          </HelpTooltip>
        </div>
        <div className="text-[10px] text-emerald-700/70">
          UIA 直接讀 GUI 結構、比 OCR 準。之後用 {`{{變數名}}`} 引用；跨節點用 {`{{ steps.步驟名.output.變數名 }}`}。
        </div>
      </div>

      {/* 關閉視窗(WindowPattern.Close、true 背景操作、不必點 X 不必前景) */}
      <BigActionBtn
        icon={X}
        title="關閉視窗"
        desc="不必點 X、不拉前景、被擋住也能關"
        onClick={() => onAdd('uia_close_window')}
        help={{
          usage: '走 WindowPattern.Close()、Windows 程式介面層級、等同送 WM_CLOSE;不靠滑鼠 / 鍵盤、不必前景',
          scenario: '工作流跑完關掉企業系統視窗;或關掉跳出的 popup;或關閉檔案總管',
          example: '選 [視窗任一元素] → 關閉視窗\nbackend 自動往上找 WindowControl 並 Close()',
        }}
      />

      {/* 下拉選單：選指定選項（背景 pattern、不彈清單、支援 {{變數}}） */}
      {isCombo && (
        <div className="bg-violet-50/60 border border-violet-200 rounded p-2 space-y-1">
          <div className="text-[11px] font-semibold text-violet-700">▾ 選擇選項（下拉選單）</div>
          <div className="flex gap-1 items-center">
            <input
              value={optionInput}
              onChange={e => setOptionInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && optionInput.trim()) {
                  onAdd('uia_select', { text: optionInput.trim() })
                  setOptionInput('')
                }
              }}
              placeholder={'選項文字，例：08 或 {{ now.month }}'}
              className="flex-1 min-w-0 border border-gray-200 rounded px-2 py-1 text-xs font-mono"
            />
            <VariableButton
              workflowId={workflowId}
              onPick={(p) => {
                const local = _localizeVarPath(p, stepName)
                setOptionInput(local === p ? `${optionInput}{{ ${p} }}` : `${optionInput}{{${local}}}`)
              }}
            />
            <button
              onClick={() => {
                if (!optionInput.trim()) { toast.error('請填選項文字'); return }
                onAdd('uia_select', { text: optionInput.trim() })
                setOptionInput('')
              }}
              className="shrink-0 whitespace-nowrap px-2 py-1 bg-violet-600 text-white rounded text-xs flex items-center gap-1 hover:bg-violet-700 transition-colors"
            >
              <ListChecks className="w-3 h-3 shrink-0" /> 選取
            </button>
          </div>
          <div className="text-[10px] text-violet-700/70">
            走 UIA pattern 背景選取 + 回讀驗證，不彈清單不動滑鼠。動態月份可用
            {' '}{`{{ now.month }}`}（當月）/ {`{{ now.prev_month }}`} / {`{{ input.月份 }}`}。
            文字要一字不差（08 不是 8）。
          </div>
        </div>
      )}

      {/* 等待條件:查詢時間不固定時,等畫面出現/消失/文字符合才繼續 */}
      <div className="bg-amber-50/60 border border-amber-200 rounded p-2 space-y-1">
        <div className="text-[11px] font-semibold text-amber-700">⏳ 等待這個元素（查詢時間不固定用）</div>
        <div className="flex gap-1 items-center flex-wrap">
          <select
            value={waitUntil}
            onChange={e => setWaitUntil(e.target.value)}
            className="border border-gray-200 rounded px-1.5 py-1 text-xs"
          >
            <option value="appear">出現</option>
            <option value="disappear">消失</option>
            <option value="text_contains">文字包含…</option>
            <option value="text_equals">文字等於…</option>
          </select>
          {(waitUntil === 'text_contains' || waitUntil === 'text_equals') && (
            <input
              value={waitText}
              onChange={e => setWaitText(e.target.value)}
              placeholder="關鍵字，例：已匯出"
              className="flex-1 min-w-[100px] border border-gray-200 rounded px-2 py-1 text-xs"
            />
          )}
          <input
            value={waitTimeout}
            onChange={e => setWaitTimeout(e.target.value)}
            className="w-14 border border-gray-200 rounded px-2 py-1 text-xs text-right"
            title="最多等幾秒"
          />
          <span className="text-[10px] text-gray-400">秒</span>
          <button
            onClick={() => {
              if ((waitUntil === 'text_contains' || waitUntil === 'text_equals') && !waitText.trim()) {
                toast.error('請填要等的關鍵字'); return
              }
              const extra: Record<string, unknown> = { until: waitUntil, timeout_sec: Number(waitTimeout) || 60 }
              if (waitText.trim()) extra.text = waitText.trim()
              onAdd('uia_wait', extra)
            }}
            className="shrink-0 whitespace-nowrap px-2 py-1 bg-amber-600 text-white rounded text-xs hover:bg-amber-700 transition-colors"
          >
            ⏳ 加入等待
          </button>
        </div>
        <div className="text-[10px] text-amber-700/70">
          例：按查詢後等「查詢中」遮罩<b>消失</b>、或等結果區<b>文字包含</b>「已匯出」再做下一步。
          條件不成立會等到逾時才誠實失敗。
        </div>
      </div>

      {/* 條件分歧:元素出現與否走不同路(查無資料對話框 vs 有資料下載) */}
      <div className="bg-sky-50/60 border border-sky-200 rounded p-2 space-y-1">
        <div className="text-[11px] font-semibold text-sky-700">❓ 若這個元素出現…（條件分歧）</div>
        <div className="flex gap-1 items-center flex-wrap text-[10px] text-gray-600">
          探測
          <input value={ifTimeout} onChange={e => setIfTimeout(e.target.value)}
            className="w-10 border border-gray-200 rounded px-1.5 py-1 text-xs text-right" title="最多探測幾秒" />
          秒，出現 →
          <select value={ifThen} onChange={e => setIfThen(e.target.value)}
            className="border border-gray-200 rounded px-1.5 py-1 text-xs">
            <option value="click">點擊這個元素</option>
            <option value="wait_gone">等它消失</option>
            <option value="none">不做動作</option>
          </select>
          {ifThen === 'wait_gone' && (
            <span className="inline-flex items-center gap-1 whitespace-nowrap">
              <input value={ifGoneTimeout} onChange={e => setIfGoneTimeout(e.target.value)}
                className="w-12 border border-gray-200 rounded px-1.5 py-1 text-xs text-right" title="最多等幾秒" />
              秒，
            </span>
          )}
          <span className="whitespace-nowrap">否則 →</span>
          <select value={ifElse} onChange={e => setIfElse(e.target.value)}
            className="border border-gray-200 rounded px-1.5 py-1 text-xs">
            <option value="none">不做動作</option>
            <option value="download">等下載完成</option>
          </select>
        </div>
        {ifElse === 'download' && (
          <div className="flex gap-1 items-center flex-wrap">
            <input value={ifDlPattern} onChange={e => setIfDlPattern(e.target.value)}
              placeholder="檔名樣式，例：*.xlsx"
              className="flex-1 min-w-[110px] border border-gray-200 rounded px-2 py-1 text-xs font-mono" />
            <input value={ifDlTimeout} onChange={e => setIfDlTimeout(e.target.value)}
              className="w-14 border border-gray-200 rounded px-2 py-1 text-xs text-right" title="下載最多等幾秒" />
            <span className="text-[10px] text-gray-400">秒 → 變數</span>
            <input value={ifDlSaveAs} onChange={e => setIfDlSaveAs(e.target.value)}
              placeholder="下載檔"
              className="w-20 border border-gray-200 rounded px-2 py-1 text-xs font-mono" />
          </div>
        )}
        <div className="flex justify-end">
          <button
            onClick={() => {
              const control = {
                ...(element.type ? { type: element.type } : {}),
                ...(element.name ? { name: element.name } : {}),
                ...(element.auto_id ? { auto_id: element.auto_id } : {}),
              }
              const thenActs: ComputerUseAction[] = ifThen === 'click'
                ? [{ type: 'uia_click', control, description: `點擊「${element.name || element.type}」` },
                   // 剛彈出的對話框會吃掉點擊(回報成功卻沒關)——點完必須驗證它真的消失
                   { type: 'uia_wait', control, until: 'disappear', timeout_sec: 5,
                     description: `等「${element.name || element.type}」消失(確認真的按掉)` }]
                // 「處理中」遮罩不一定會出現(真系統查無資料時直接跳對話框)——
                // 出現才等它消失,沒出現不算失敗;單獨用 ⏳ 等出現會卡死整筆。
                : ifThen === 'wait_gone'
                ? [{ type: 'uia_wait', control, until: 'disappear', timeout_sec: Number(ifGoneTimeout) || 300,
                     description: `等「${element.name || element.type}」消失` }]
                : []
              const elseActs: ComputerUseAction[] = ifElse === 'download'
                ? [{ type: 'wait_download', pattern: ifDlPattern.trim() || '*',
                     timeout_sec: Number(ifDlTimeout) || 300,
                     ...(ifDlSaveAs.trim() ? { save_as: ifDlSaveAs.trim() } : {}),
                     description: `等下載完成：${ifDlPattern.trim() || '*'}` }]
                : []
              onAdd('if_element_found', {
                timeout_sec: Number(ifTimeout) || 3,
                then: thenActs,
                else: elseActs,
              })
            }}
            className="shrink-0 whitespace-nowrap px-2 py-1 bg-sky-600 text-white rounded text-xs hover:bg-sky-700 transition-colors"
          >
            ❓ 加入分歧
          </button>
        </div>
        <div className="text-[10px] text-sky-700/70">
          典型：匯出後「查無資料」對話框的「確定」鈕 —— 出現就按掉繼續，沒出現代表有資料、走等下載。
          「處理中」遮罩不一定出現時用「等它消失」。探測不到不算失敗、只是走「否則」分支。
        </div>
      </div>

      {/* 選到唯讀 Text 時要指路 —— 實測使用者選了「找補金額」的 TextControl
          （標籤），找不到填值入口，以為系統做不到。欄位清單裡同名的
          Edit 才是輸入框。 */}
      {!isEditable && element.type.includes('Text') && (
        <div className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1.5 leading-relaxed">
          ⚠ 這是唯讀文字（{element.type}），只能「讀」不能「填」。
          要把值<strong>填進去</strong>，請回欄位清單選同名的 <span className="font-mono">Edit</span> 控制項
          （例：找補金額 → <span className="font-mono">Edit amount</span>）。
        </div>
      )}

      {/* 輸入文字(只有 Edit/Combo/Document 可編輯類型才有意義、其他用送鍵盤) */}
      {isEditable && (
        <div className="bg-emerald-50/50 border border-emerald-200 rounded p-2 space-y-1">
          <div className="text-[11px] font-semibold text-emerald-700">⌨️ 填入文字（貼上一步的 {`{{變數}}`}）</div>
          <div className="flex gap-1 items-center">
            <input
              value={textInput}
              onChange={e => setTextInput(e.target.value)}
              placeholder="文字(可含 {{var}} 或 {{ steps.X.output.Y }})"
              className="flex-1 min-w-0 border border-gray-200 rounded px-2 py-1 text-xs font-mono"
            />
            <VariableButton
              workflowId={workflowId}
              onPick={(p) => {
                const local = _localizeVarPath(p, stepName)
                setTextInput(local === p
                  ? `${textInput}{{ ${p} }}`
                  : `${textInput}{{${local}}}`)   // 同節點變數：{{總計金額}}
              }}
            />
            <HelpTooltip
              title="送文字"
              usage="把文字塞進此 Edit/Combo/Document 控制項;優先 ValuePattern.SetValue() 瞬時、不模擬鍵盤、不影響 focus、背景 work"
              scenario="在 Excel cell 寫公式、填表單欄位、貼貼上想要的內容"
              example={'text="=SUM(D2:D{{row_count}})"\n→ 公式自動算對範圍'}
            >
              <button
                onClick={() => {
                  if (!textInput.trim()) { toast.error('請填文字'); return }
                  const bad = _singleBraceProblem(textInput)
                  if (bad) {
                    toast.error(`變數要用雙大括號 {{${bad}}} —— 單層 {${bad}} 不會被替換，會把字面字元填進欄位`)
                    return
                  }
                  onAdd('uia_send_keys', { text: textInput })
                  setTextInput('')
                }}
                className="px-2 py-1 bg-emerald-600 text-white rounded text-xs flex items-center gap-1 hover:bg-emerald-700 shrink-0 whitespace-nowrap"
              >
                <Type className="w-3 h-3" /> 送文字
              </button>
            </HelpTooltip>
          </div>
        </div>
      )}

      {/* 送鍵盤(任何元素都可、focus 該元素再按 keys) */}
      <div className="bg-blue-50/50 border border-blue-200 rounded p-2 space-y-1">
        <div className="text-[11px] font-semibold text-blue-700">送鍵盤到此控制項</div>
        <div className="flex gap-1">
          <input
            value={keysInput}
            onChange={e => setKeysInput(e.target.value)}
            placeholder="按鍵組合、例:enter / ctrl+s / tab / f5"
            className="flex-1 min-w-0 border border-gray-200 rounded px-2 py-1 text-xs font-mono"
          />
          <HelpTooltip
            title="送鍵"
            usage="送鍵盤事件(快捷鍵 / 特殊鍵)到此控制項;模擬鍵盤、需該元素 focus(focus 不在可能搶前景)"
            scenario="enter 確認 / Ctrl+S 存檔 / Ctrl+V 貼上剪貼簿 / F5 重整 / Tab 切下個焦點"
            example={'enter → 送 Enter\nctrl+s → 送 Ctrl+S 存檔\nctrl+shift+s → 多修飾鍵組合'}
          >
            <button
              onClick={() => {
                const keys = keysInput.trim().toLowerCase().split('+').map(s => s.trim()).filter(Boolean)
                if (keys.length === 0) { toast.error('請填按鍵、例 enter 或 ctrl+s'); return }
                onAdd('uia_send_keys', { keys })
                setKeysInput('')
              }}
              className="shrink-0 whitespace-nowrap px-2 py-1 bg-blue-600 text-white rounded text-xs flex items-center gap-1 hover:bg-blue-700 shrink-0"
            >
              ⌨️ 送鍵
            </button>
          </HelpTooltip>
        </div>
        <div className="text-[10px] text-blue-700/70">用於 enter 確認 / Ctrl+S 存檔 / F5 重整 / Tab 切焦點</div>
      </div>

      {/* 寫剪貼簿(任何元素都可、用於把上一步 save 的變數塞進剪貼簿、後續 Ctrl+V 貼用) */}
      <div className="bg-cyan-50/50 border border-cyan-200 rounded p-2 space-y-1">
        <div className="text-[11px] font-semibold text-cyan-700">📋 寫剪貼簿(同節點內)</div>
        <div className="flex gap-1 items-center">
          <input
            value={clipboardInput}
            onChange={e => setClipboardInput(e.target.value)}
            placeholder="例:{{order_id}}、{{ steps.X.output.Y }}、固定文字"
            className="flex-1 min-w-0 border border-gray-200 rounded px-2 py-1 text-xs font-mono"
          />
          <VariableButton
            workflowId={workflowId}
            onPick={(p) => {
              const local = _localizeVarPath(p, stepName)
              setClipboardInput(local === p
                ? `${clipboardInput}{{ ${p} }}`
                : `${clipboardInput}{{${local}}}`)
            }}
          />
          <HelpTooltip
            title="寫剪貼簿"
            usage="把文字 / 變數值塞進 Windows 剪貼簿、後續用 Ctrl+V 貼出來"
            scenario="跨應用 / 跨節點傳值;同節點:讀文字 → 寫剪貼簿 → 切到目標 → Ctrl+V 貼;跨節點:剪貼簿是 OS 全域、下個節點 Ctrl+V 仍取得到"
            example={'text="{{logged_user}}"\n→ 把上一步抓到的使用者名寫進剪貼簿'}
          >
            <button
              onClick={() => {
                if (!clipboardInput.trim()) { toast.error('請填內容'); return }
                const bad = _singleBraceProblem(clipboardInput)
                if (bad) {
                  toast.error(`變數要用雙大括號 {{${bad}}} —— 單層 {${bad}} 不會被替換`)
                  return
                }
                onAdd('uia_set_clipboard', { text: clipboardInput })
                setClipboardInput('')
              }}
              className="shrink-0 whitespace-nowrap px-2 py-1 bg-cyan-600 text-white rounded text-xs flex items-center gap-1 hover:bg-cyan-700 shrink-0"
            >
              📋 寫入
            </button>
          </HelpTooltip>
        </div>
        <div className="text-[10px] text-cyan-700/70">
          配合「讀文字」用:讀取 → 寫剪貼簿 → 後續 Ctrl+V 貼到目標應用。反向(讀剪貼簿存變數)在動作列表用 YAML/助手加 uia_get_clipboard,或用下方讀取鈕。{`{{變數}}`} 會被當下 step 變數值替換。
        </div>
      </div>

      {/* 表格動作(讀列數 + 點 cell):只有 DataGrid/List/Tree 才出 */}
      {isGrid && (
        <div className="bg-amber-50/50 border border-amber-200 rounded p-2 space-y-1.5">
          <div className="text-[11px] font-semibold text-amber-700">表格 / 列表動作</div>
          <div className="flex gap-1">
            <input
              value={saveAsInput}
              onChange={e => setSaveAsInput(e.target.value)}
              placeholder="變數名(例 row_count)"
              className="flex-1 min-w-0 border border-gray-200 rounded px-2 py-1 text-xs font-mono"
            />
            <HelpTooltip
              title="讀列數"
              usage="讀此 DataGrid / List / Tree 目前共幾列、存進變數;後續可用 {{變數}} 動態算 row"
              scenario="每天行數不同的訂單表;想點最後一筆下方空白格寫公式"
              example={'save_as="rows"\n→ 之後 row="{{rows + 1}}" 點下一個空白格'}
            >
              <button
                onClick={() => {
                  if (!saveAsInput.trim()) { toast.error('請填變數名'); return }
                  onAdd('uia_get_table_rowcount', { save_as: saveAsInput.trim() })
                }}
                className="px-2 py-1 bg-amber-600 text-white rounded text-xs flex items-center gap-1 hover:bg-amber-700 shrink-0"
              >
                <Hash className="w-3 h-3" /> 讀列數
              </button>
            </HelpTooltip>
          </div>
          <div className="text-[10px] text-amber-700/70">把表格目前列數存進變數、後續 row 用 {`{{變數}}`} 動態算</div>
          <div className="flex gap-1">
            <input
              value={rowInput}
              onChange={e => setRowInput(e.target.value)}
              placeholder="row(數字或 {{var + 1}})"
              className="flex-1 min-w-0 border border-gray-200 rounded px-2 py-1 text-xs font-mono"
            />
            <input
              value={colInput}
              onChange={e => setColInput(e.target.value)}
              placeholder="column"
              className="w-16 border border-gray-200 rounded px-2 py-1 text-xs font-mono"
            />
            <HelpTooltip
              title="點 cell"
              usage="點 DataGrid / List / Tree 第 N 列第 M 欄;優先 SelectionItemPattern.Select(背景 work)、退到 Click"
              scenario="點動態算出來的 cell(配合「讀列數」用);ERP 訂單表點某筆訂單看 detail"
              example={'row="{{rows + 1}}" column=4\n→ 動態定位到最後資料下方空白格、第 4 欄'}
            >
              <button
                onClick={() => {
                  if (!rowInput.trim() || !colInput.trim()) { toast.error('row / column 都要填'); return }
                  onAdd('uia_click_cell', {
                    row: /\D/.test(rowInput) ? rowInput : Number(rowInput),
                    column: /\D/.test(colInput) ? colInput : Number(colInput),
                  })
                }}
                className="px-2 py-1 bg-amber-600 text-white rounded text-xs flex items-center gap-1 hover:bg-amber-700 shrink-0"
              >
                <ListChecks className="w-3 h-3" /> 點 cell
              </button>
            </HelpTooltip>
          </div>
        </div>
      )}

      {/* 進階區:4 種斷言（讀文字已升級成上面的一級動作卡） */}
      <div className="rounded-lg border border-gray-200 bg-gray-50/50 overflow-hidden">
        <button
          type="button"
          onClick={() => setAdvancedOpen(v => !v)}
          className="w-full flex items-center gap-2 px-2 py-1.5 text-left hover:bg-gray-100 transition-colors text-[11px]"
        >
          {advancedOpen ? <ChevronDown className="w-3 h-3 text-gray-400" /> : <ChevronRight className="w-3 h-3 text-gray-400" />}
          <span className="font-semibold text-gray-600 flex-1">進階動作</span>
          <span className="text-gray-400 text-[10px]">斷言狀態</span>
        </button>
        {advancedOpen && (
          <div className="px-2 pb-2 space-y-1.5 border-t border-gray-200">
            {/* （讀文字已移到上面的一級動作卡） */}

            <div className="text-[10px] text-gray-600 font-semibold pt-1">斷言狀態(失敗 = 整步 fail):</div>
            <div className="grid grid-cols-2 gap-1">
              <SmallActionBtn
                label="存在"
                onClick={() => onAdd('uia_assert_state', { check: 'exists' })}
                help={{
                  usage: '驗元素必須存在、不存在 → 整步 fail',
                  scenario: '點完「儲存」後驗成功訊息有跳出、防止「按了但沒反應」一路錯下去',
                  example: '步驟 1: 點儲存\n步驟 2: 等就緒(成功訊息)\n步驟 3: 斷言 存在',
                }}
              />
              <SmallActionBtn
                label="enabled"
                onClick={() => onAdd('uia_assert_state', { check: 'enabled' })}
                help={{
                  usage: '驗元素必須是 enabled(可按)、disabled → 整步 fail',
                  scenario: '表單填完才該能按「送出」、用此驗證表單真的填完整、防 race / 防遺漏必填',
                  example: '填完所有欄位 → 斷言「送出」按鈕 enabled',
                }}
              />
              <SmallActionBtn
                label="focused"
                onClick={() => onAdd('uia_assert_state', { check: 'focused' })}
                help={{
                  usage: '驗鍵盤焦點目前在此元素、不在 → 整步 fail',
                  scenario: 'Tab 切下個欄位後驗焦點真的切到「電話」欄;UI 改版常見 tab order 偷偷換',
                  example: '送鍵 tab → 斷言「電話」欄位 focused',
                }}
              />
              <SmallActionBtn
                label="checked"
                onClick={() => onAdd('uia_assert_state', { check: 'checked' })}
                help={{
                  usage: '驗 checkbox / radio 已打勾、沒勾 → 整步 fail',
                  scenario: '註冊流程驗「同意條款」勾了;ERP 驗「啟用通知」開了',
                  example: '走過註冊流程 → 斷言「同意條款」checked',
                }}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

/** Hover 提示框 — 用 portal + fixed 位置、自動避開螢幕邊界、不被 panel 切掉 */
function HelpTooltip({
  children,
  title,
  usage,
  scenario,
  example,
}: {
  children: React.ReactNode
  title: string
  usage: string
  scenario?: string
  example?: string
}) {
  const [show, setShow] = useState(false)
  const [pos, setPos] = useState({ top: 0, left: 0 })
  const wrapperRef = useRef<HTMLDivElement>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const TIP_W = 280
  const ESTIMATED_H = 220   // 估值、實際 tooltip 撐多高用 max-content
  const MARGIN = 8

  const computePosition = () => {
    const el = wrapperRef.current
    if (!el) return null
    const rect = el.getBoundingClientRect()
    const vw = window.innerWidth
    const vh = window.innerHeight

    // 預設浮在按鈕上方、左邊對齊按鈕左
    let left = rect.left
    let top = rect.top - ESTIMATED_H - MARGIN

    // 如果上方空間不夠 → 改放下方
    if (top < MARGIN) {
      top = rect.bottom + MARGIN
    }
    // 如果右邊超出 viewport → 靠右對齊按鈕右
    if (left + TIP_W > vw - MARGIN) {
      left = rect.right - TIP_W
    }
    // 如果左邊超出 viewport → 貼齊左 8px
    if (left < MARGIN) {
      left = MARGIN
    }
    // 如果下方也不夠(超大 viewport 邊角)、強制 clamp 進畫面
    if (top + ESTIMATED_H > vh - MARGIN) {
      top = Math.max(MARGIN, vh - ESTIMATED_H - MARGIN)
    }
    return { top, left }
  }

  return (
    <div
      ref={wrapperRef}
      className="relative"
      onMouseEnter={() => {
        timerRef.current = setTimeout(() => {
          const p = computePosition()
          if (p) {
            setPos(p)
            setShow(true)
          }
        }, 400)
      }}
      onMouseLeave={() => {
        if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
        setShow(false)
      }}
    >
      {children}
      {show && typeof window !== 'undefined' && createPortal(
        <div
          className="fixed z-[100] bg-gray-900 text-white text-[11px] rounded-lg p-3 shadow-2xl pointer-events-none"
          style={{ top: pos.top, left: pos.left, width: TIP_W }}
        >
          <div className="font-bold text-emerald-300 mb-1.5 text-xs">{title}</div>
          <div className="mb-2 leading-relaxed">{usage}</div>
          {scenario && (
            <div className="mb-2">
              <div className="text-yellow-300 font-semibold text-[10px] mb-0.5">場景</div>
              <div className="text-gray-200 text-[10.5px] leading-relaxed">{scenario}</div>
            </div>
          )}
          {example && (
            <div>
              <div className="text-cyan-300 font-semibold text-[10px] mb-0.5">範例</div>
              <div className="bg-black/60 rounded px-1.5 py-1 font-mono text-[10px] leading-tight whitespace-pre-wrap text-gray-200">{example}</div>
            </div>
          )}
        </div>,
        document.body,
      )}
    </div>
  )
}

/** 大按鈕:標題 + 一行 desc + hover tooltip */
function BigActionBtn({
  icon: Icon, title, desc, onClick, help,
}: {
  icon: typeof MousePointerClick
  title: string
  desc: string
  onClick: () => void
  help?: { usage: string; scenario?: string; example?: string }
}) {
  const button = (
    <button
      onClick={onClick}
      className="text-left px-2.5 py-2 bg-white border border-purple-300 rounded hover:bg-purple-50 hover:border-purple-400 transition-colors w-full"
    >
      <div className="flex items-center gap-1.5 text-purple-700 font-semibold text-xs">
        <Icon className="w-3.5 h-3.5" /> {title}
      </div>
      <div className="text-[10px] text-gray-500 mt-0.5">{desc}</div>
    </button>
  )
  if (!help) return button
  return <HelpTooltip title={title} usage={help.usage} scenario={help.scenario} example={help.example}>{button}</HelpTooltip>
}

/** 小按鈕:斷言狀態用、純 label + hover tooltip */
function SmallActionBtn({
  label, onClick, help,
}: {
  label: string
  onClick: () => void
  help?: { usage: string; scenario?: string; example?: string }
}) {
  const button = (
    <button
      onClick={onClick}
      className="px-2 py-1 bg-white border border-gray-300 rounded text-[11px] hover:bg-gray-100 text-gray-700 w-full"
    >
      {label}
    </button>
  )
  if (!help) return button
  return <HelpTooltip title={`斷言:${label}`} usage={help.usage} scenario={help.scenario} example={help.example}>{button}</HelpTooltip>
}

