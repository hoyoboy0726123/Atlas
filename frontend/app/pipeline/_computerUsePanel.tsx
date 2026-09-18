'use client'
import { useEffect, useRef, useState } from 'react'
import { X, Circle, Square as StopIcon, Play, Trash2, ChevronUp, ChevronDown, Pencil, Plus, Eye, MousePointer2 } from 'lucide-react'
import { toast } from 'sonner'
import type { ComputerUseData, ComputerUseNode, ComputerUseAction } from './_helpers'
import OcrFieldInserter from './_ocrFieldInserter'
import WaitDownloadInserter from './_waitDownloadInserter'
import OcrWaitInserter from './_ocrWaitInserter'
import ForEachInserter from './_forEachInserter'
import ClipboardReadInserter from './_clipboardReadInserter'
import WakeWindowInserter from './_wakeWindowInserter'
import InsertHub from './_insertHub'
import AskAiButton from './_askAiButton'

// ── vlm_check 內建模板（6 個常見場景）─────────────────────────────
const VLM_CHECK_BUILTIN_TEMPLATES: { id: string; label: string; prompt: string }[] = [
  { id: 'login_success',    label: '登入成功訊息',  prompt: '畫面是否顯示「登入成功」、「歡迎回來」之類的成功提示？沒有任何錯誤訊息？' },
  { id: 'error_message',    label: '無錯誤訊息',     prompt: '畫面是否「沒有」紅色錯誤訊息或失敗提示？所有 input 欄位都正常無紅框？' },
  { id: 'dialog_appeared',  label: '對話框已出現',  prompt: '畫面是否有對話框（彈窗）出現，且標題或按鈕清楚可見？' },
  { id: 'page_loaded',      label: '頁面載入完成',  prompt: '頁面主要內容是否完整顯示？沒有 spinner、骨架屏或載入中狀態？' },
  { id: 'button_active',    label: '按鈕變可點擊',  prompt: '目標按鈕是否變為可點擊狀態（顏色變亮、不再 disabled）？' },
  { id: 'data_loaded',      label: '資料已載入',     prompt: '畫面是否顯示了實際資料（表格有列、清單有項目），不是空白或「無資料」訊息？' },
]

const VLM_CUSTOM_TEMPLATES_KEY = 'pipeline.vlm_check.custom_templates.v1'
type CustomTemplate = { id: string; label: string; prompt: string }

function loadCustomTemplates(): CustomTemplate[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(VLM_CUSTOM_TEMPLATES_KEY)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr.filter(x => x && x.label && x.prompt) : []
  } catch { return [] }
}

function saveCustomTemplates(items: CustomTemplate[]): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(VLM_CUSTOM_TEMPLATES_KEY, JSON.stringify(items))
  } catch (e) { console.warn('save custom templates failed:', e) }
}
import {
  startComputerUseRecording,
  stopComputerUseRecording,
  getComputerUseRecordingStatus,
  loadComputerUseRecording,
  analyzeAnchors,
  deleteComputerUseAssets,
  armComputerUseRecordingHotkey,
  disarmComputerUseRecordingHotkey,
  describeForGrounding,
  getGroundingStatus,
} from '@/lib/api'
import type { GroundingStatus } from '@/lib/api'
import AnchorEditorModal from './_anchorEditorModal'
import VlmAnchorPicker from './_vlmAnchorPicker'
import UiaInspectorPanel from './_uiaInspectorPanel'
import LlmRoleSelector from './_llmRoleSelector'
import { assetImageUrl } from '@/lib/api'

const NODE_COLOR = '#9333ea'

interface Props {
  node: ComputerUseNode
  pipelineName: string       // 用於推導預設 assets_dir
  onUpdate: (data: Partial<ComputerUseData>) => void
  onClose: () => void
  onDelete: () => void
  workflowId?: string
}

export default function ComputerUsePanel({ node, pipelineName, onUpdate, onClose, onDelete, workflowId }: Props) {
  const data = node.data
  const inputCls = 'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-purple-400 focus:ring-1 focus:ring-purple-400/20 bg-white'

  // 錄製狀態
  const [recording, setRecording] = useState(false)
  const [statusText, setStatusText] = useState('')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  // F7 待命模式:arm 後最小化瀏覽器、把焦點留在目標 app、按 F7 啟動錄製
  const [armed, setArmed] = useState(false)

  // CV 比對設定摺疊（預設收折，避免佔太多空間）
  const [cvOpen, setCvOpen] = useState(false)
  // OCR 比對設定摺疊（預設收折）
  const [ocrOpen, setOcrOpen] = useState(false)
  // VLM 把關 Phase 1 摺疊（預設收折、進階功能）
  const [vlmOpen, setVlmOpen] = useState(false)
  // 4 種 VLM 功能決策樹摺疊（預設收折、給混淆的人查）
  // 進階選項顯示開關 — 預設關、用 localStorage 記住使用者偏好
  // 關閉時:藏「Pixel/UIA 模式切換」按鈕(強制 Pixel 模式、錄製會自動抓 UIA + CV 三層 fallback)
  // 開啟時:顯示模式切換、使用者可手動切到 UIA Inspector 進階功能
  const [showAdvanced, setShowAdvanced] = useState<boolean>(() => {
    if (typeof window === 'undefined') return false
    return window.localStorage.getItem('computer_use_show_advanced') === '1'
  })
  useEffect(() => {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem('computer_use_show_advanced', showAdvanced ? '1' : '0')
    }
  }, [showAdvanced])

  // 預設錄製輸出目錄
  const defaultAssetsDir = data.assetsDir ||
    `ai_output/${pipelineName || 'pipeline'}/${data.name}_assets`

  // 錄製過程輪詢狀態
  useEffect(() => {
    if (!recording && !armed) {
      if (pollRef.current) clearInterval(pollRef.current)
      pollRef.current = null
      return
    }
    const poll = async () => {
      try {
        const s = await getComputerUseRecordingStatus()
        if (s.recording) {
          // 不論 arm or 直接按按鈕、recording true 都要進錄製狀態
          if (!recording) setRecording(true)
          if (armed) setArmed(false)
          setStatusText(`錄製中… ${s.action_count ?? 0} 個動作`)
        } else if (recording) {
          // 錄製已被 F9 或後端自行停止
          setRecording(false)
          setStatusText('')
          await handleLoadRecording()
        }
        // armed but not recording: 還在等 F7、保持 armed 狀態
      } catch {/* ignore transient errors */}
    }
    pollRef.current = setInterval(poll, 1000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [recording, armed])

  // panel 關閉時、清掉 arm(避免熱鍵繼續綁著)
  useEffect(() => {
    return () => {
      if (armed) {
        disarmComputerUseRecordingHotkey().catch(() => {})
      }
    }
  }, [armed])

  // 攔掉 F7 / F9 的瀏覽器預設行為:
  //   F7 = Chrome/Edge「鍵盤瀏覽 Caret Browsing」確認框
  //   F9 = 部分瀏覽器的閱讀模式 / reader view
  // 這兩個都是我們的錄製熱鍵(F7 待命開錄、F9 結束),後端 OS 層級全域熱鍵在收、跟瀏覽器無關,
  // 所以這裡 preventDefault 不影響錄製、只是不讓瀏覽器搶這兩個鍵。panel 開著就生效。
  useEffect(() => {
    const blockFnKeys = (e: KeyboardEvent) => {
      if (e.key === 'F7' || e.key === 'F9' || e.keyCode === 118 || e.keyCode === 120) {
        e.preventDefault()
        e.stopPropagation()
      }
    }
    window.addEventListener('keydown', blockFnKeys, true)
    return () => window.removeEventListener('keydown', blockFnKeys, true)
  }, [])

  const handleArmHotkey = async () => {
    if (armed || recording) return
    try {
      const sessionId = `${data.name}-${Date.now()}`
      await armComputerUseRecordingHotkey(sessionId, defaultAssetsDir)
      onUpdate({ assetsDir: defaultAssetsDir })
      setArmed(true)
      toast.success('🔫 F7 已待命。最小化瀏覽器、把焦點放到目標 app、按 F7 開始錄製')
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  const handleDisarmHotkey = async () => {
    try {
      await disarmComputerUseRecordingHotkey()
    } finally {
      setArmed(false)
    }
  }

  const handleStart = async () => {
    if (recording) return
    try {
      const sessionId = `${data.name}-${Date.now()}`
      await startComputerUseRecording(sessionId, defaultAssetsDir)
      onUpdate({ assetsDir: defaultAssetsDir })
      setRecording(true)
      setStatusText('錄製中…（按 F9 或這個按鈕結束）')
      toast.success('🔴 開始錄製。請操作螢幕，F9 停止。')
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  const handleStop = async () => {
    try {
      await stopComputerUseRecording()
      setRecording(false)
      setStatusText('')
      await handleLoadRecording()
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  // 錨點獨特性：index → 這張錨點回放時可能被哪些地方搶走。
  // 只提醒、不改執行行為（試過依它自動鎖搜尋範圍，實測不可行 ——
  // 錄製當下的替身分佈預測不了回放當下的）。
  const [anchorRisk, setAnchorRisk] = useState<Record<number, {
    rivals: number; nearest: number; scanned: number
    phases?: { box: number; near: number; fullscreen: number }
    flat?: boolean; variance?: number
    targetScore?: number; rivalScore?: number
  }>>({})

  // silent = 設定變動時的自動重算，不跳 toast（拖框時會洗版）
  const runAnchorCheck = async (acts: ComputerUseAction[], dir: string, silent = false) => {
    if (!acts?.length || !dir) return
    try {
      const res = await analyzeAnchors(dir, acts as unknown as Record<string, unknown>[], {
        cv_search_radius: data.cvSearchRadius ?? 400,
        cv_threshold: data.cvThreshold ?? 0.5,
        cv_search_only_near: data.cvSearchOnlyNear === true,
      })
      const map: typeof anchorRisk = {}
      for (const r of res.results) {
        // flat（純色錨點）比有替身嚴重 —— CV 對它完全無效，一定要報
        if (r.checked && (r.rivals > 0 || r.flat)) {
          map[r.index] = {
            rivals: r.rivals, nearest: r.nearest_rival_px,
            scanned: r.scanned ?? r.rivals, phases: r.phases,
            flat: r.flat, variance: r.variance,
            targetScore: r.target_score, rivalScore: r.best_rival_score,
          }
        }
      }
      setAnchorRisk(map)
      if (silent) return
      const nFlat = Object.values(map).filter(m => m.flat).length
      const nRival = Object.keys(map).length - nFlat
      if (nFlat > 0) {
        toast.error(`${nFlat} 個錨點幾乎沒有特徵（純色），CV 會亂命中 —— 請重圈`, { duration: 10000 })
      }
      if (nRival > 0) {
        toast.warning(`${nRival} 個錨點有分數逼近的替身，回放時可能被搶走（見動作列表的 ⚠）`, { duration: 7000 })
      }
      if (nFlat === 0 && nRival === 0) {
        toast.success('錨點檢查通過：搜尋範圍內沒有分數搶得走真目標的地方')
      }
    } catch (e) {
      console.warn('anchor check:', e)   // 分析失敗不影響錄製結果
    }
  }

  const handleLoadRecording = async () => {
    try {
      const res = await loadComputerUseRecording(defaultAssetsDir)
      const rec = res.actions || []
      const existing = actionsRef.current
      const at = uiaInsertAtRef.current
      // ⚠ 原本是整份取代 —— 已有動作的節點再錄製，原有動作被靜默清掉。
      //   改成合併：沒有既有動作照舊全載；有 → 插入點優先，否則接在最後。
      let next: ComputerUseAction[]
      if (existing.length === 0) {
        next = rec
      } else if (at !== null && at <= existing.length) {
        next = [...existing.slice(0, at), ...rec, ...existing.slice(at)]
        setUiaInsertAt(at + rec.length)
      } else {
        next = [...existing, ...rec]
      }
      onUpdate({ actions: next, assetsDir: defaultAssetsDir })
      toast.success(existing.length === 0
        ? `已載入 ${rec.length} 個動作`
        : `已${at !== null ? `插入到 #${at + 1}` : '接在最後'}：${rec.length} 個錄製動作（原有 ${existing.length} 個保留）`)
    } catch (e) {
      // 錄製尚未停好或目錄不存在是正常狀況
      console.warn('Load recording:', e)
    }
  }

  // ── 設定一改就重算警告 ────────────────────────────────────────────
  // 警告是「依目前設定判斷風險」，設定變了卻不重算就會說謊：縮小橘框把風險
  // 解掉了紅字還掛著；半徑調大引入新風險卻一片安靜 —— 後者更危險。
  const riskSignature = JSON.stringify({
    dir: data.assetsDir,
    radius: data.cvSearchRadius ?? 400,
    threshold: data.cvThreshold ?? 0.5,
    onlyNear: data.cvSearchOnlyNear === true,
    acts: (data.actions || []).map((a: any) => [
      a.type, a.image, a.x, a.y, a.search_region, a.cv_strict_region, a.confidence,
    ]),
  })
  useEffect(() => {
    const acts = data.actions || []
    if (!acts.some((a: any) => a.type === 'click_image' && a.image)) {
      setAnchorRisk({})
      return
    }
    const t = setTimeout(() => {
      runAnchorCheck(acts, data.assetsDir || '', true)
    }, 500)
    return () => clearTimeout(t)
    // riskSignature 已涵蓋所有會影響結果的輸入
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [riskSignature])


  // 動作操作
  const moveAction = (i: number, dir: -1 | 1) => {
    const next = [...(data.actions || [])]
    const j = i + dir
    if (j < 0 || j >= next.length) return
    ;[next[i], next[j]] = [next[j], next[i]]
    onUpdate({ actions: next })
  }
  const deleteAction = (i: number) => {
    const next = [...(data.actions || [])]
    next.splice(i, 1)
    onUpdate({ actions: next })
  }
  const [editingAnchor, setEditingAnchor] = useState<number | null>(null)
  // VLM 挑錨點 file picker：用 actionIndex 表示對哪一個動作開
  const [pickingVlmAnchorsAt, setPickingVlmAnchorsAt] = useState<number | null>(null)
  // grounding 描述產生中的動作索引（null = 沒在跑）
  const [describingIdx, setDescribingIdx] = useState<number | null>(null)
  // 每個動作的描述自我驗證結果：null/undefined = 通過或沒驗過，字串 = 沒過的原因
  const [descVerify, setDescVerify] = useState<Record<number, string | null>>({})
  // 這台機器能不能用「直接定位」（需要沙盒容器 + NVIDIA GPU + 已下載模型）
  const [gStatus, setGStatus] = useState<GroundingStatus | null>(null)
  useEffect(() => {
    let alive = true
    getGroundingStatus().then(s => { if (alive) setGStatus(s) }).catch(() => {})
    return () => { alive = false }
  }, [])

  // 用主模型看錄製時的截圖，產生「要點什麼」的描述草稿。
  // 手動觸發而非錄製時自動跑 —— 每次都是一次雲端視覺呼叫，多數步驟用不到。
  const genGroundingDesc = async (i: number, a: ComputerUseAction) => {
    const dir = data.assetsDir || defaultAssetsDir
    if (!dir) { toast.error('找不到 assets 目錄'); return }
    setDescribingIdx(i)
    try {
      const j = await describeForGrounding(dir, a as unknown as Record<string, unknown>)
      applyAnchorPatch(i, { vlm_prompt: j.description })
      // 系統已經自己把描述餵回定位模型驗過一次 —— 驗不過就要講清楚，
      // 不能只丟一句「請確認」了事（使用者不會逐字比對畫面）。
      // 三種狀態要分開講：驗過且通過 / 驗過但沒過 / 根本沒驗到。
      // 把「沒驗到」講成「通過」會給假的安全感。
      if (j.verified && j.verify_px != null) {
        toast.success(`已產生描述（自我驗證通過，誤差 ${j.verify_px}px）`)
        setDescVerify(v => ({ ...v, [i]: null }))
      } else if (j.verified) {
        toast.info(`已產生描述，但${j.verify_msg}`, { duration: 6000 })
        setDescVerify(v => ({ ...v, [i]: `SKIP:${j.verify_msg}` }))
      } else {
        toast.warning(`描述已填入，但自我驗證沒過：${j.verify_msg}`, { duration: 8000 })
        setDescVerify(v => ({ ...v, [i]: j.verify_msg }))
      }
    } catch (e) {
      toast.error(`產生描述失敗：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setDescribingIdx(null)
    }
  }
  // 行內編輯：點 ✎ 展開該動作的可改欄位
  const [editingAction, setEditingAction] = useState<number | null>(null)
  // 選擇器/錄製新動作的插入位置。null = 附加在最後（預設）。
  const [uiaInsertAt, setUiaInsertAt] = useState<number | null>(null)
  // 錄製停止是輪詢 effect 的舊 closure 呼叫進來的，直接讀 state 會過期 —— 用 ref
  const actionsRef = useRef<ComputerUseAction[]>(data.actions || [])
  useEffect(() => { actionsRef.current = data.actions || [] }, [data.actions])
  const uiaInsertAtRef = useRef<number | null>(null)
  useEffect(() => { uiaInsertAtRef.current = uiaInsertAt }, [uiaInsertAt])

  const applyAnchorPatch = (i: number, patch: Partial<ComputerUseAction>) => {
    const next = [...(data.actions || [])]
    next[i] = { ...next[i], ...patch }
    onUpdate({ actions: next })
  }

  // 在指定位置插入 vlm_check 動作（template 帶 prompt 進去；無 template 時 prompt 留空，使用者自填）
  const insertVlmCheckAt = (index: number, prompt: string, label?: string) => {
    const next = [...(data.actions || [])]
    const newAction: ComputerUseAction = {
      type: 'vlm_check',
      description: label ? `視覺判斷：${label}` : '視覺判斷',
      vlm_prompt: prompt,
    } as ComputerUseAction
    next.splice(index, 0, newAction)
    onUpdate({ actions: next })
  }

  /** 通用:在 index 位置插入一個動作(OCR 取值等)。 */
  const insertActionAt = (index: number, action: ComputerUseAction) => {
    const next = [...(data.actions || [])]
    next.splice(index, 0, action)
    onUpdate({ actions: next })
  }

  /** 一次插入多個動作。⚠ 連續呼叫兩次 insertActionAt 會因 React 狀態
   *  還沒更新而互相蓋掉(實測:喚醒插入器的 activate_window 被 wait 蓋掉、
   *  只剩一個動作),要插多個必須用這個一次做完。 */
  const insertActionsAt = (index: number, acts: ComputerUseAction[]) => {
    const next = [...(data.actions || [])]
    next.splice(index, 0, ...acts)
    onUpdate({ actions: next })
  }

  /** 把目前序列的全部動作包進一個 for_each(先調通單筆、再一鍵套迴圈)。 */
  const wrapAllIntoForEach = (fe: ComputerUseAction) => {
    onUpdate({ actions: [{ ...fe, do: [...(data.actions || [])] }] })
  }

  /** 把「index 之後」的動作包進迴圈,前面的(錄製/讀清單等前置)保留在外。 */
  const wrapAfterIntoForEach = (index: number, fe: ComputerUseAction) => {
    const acts = data.actions || []
    onUpdate({ actions: [...acts.slice(0, index), { ...fe, do: acts.slice(index) }] })
  }

  // ➕ popover 開關：用 actionIndex 表示要在哪一個 index 插入（actions.length = 在最後）
  // insertKind 區分同一個位置的兩種插入器(視覺判斷 / OCR 取值),否則會同時展開
  const [insertOpenAt, setInsertOpenAt] = useState<number | null>(null)
  // 插入選單收折:一次只展開一個間隙(7 顆全常駐太擠,使用者反饋)
  const [gapOpenAt, setGapOpenAt] = useState<number | null>(null)
  const [insertKind, setInsertKind] = useState<'vlm' | 'ocr' | 'dl' | 'ocrwait' | 'foreach' | 'clip' | 'wake'>('vlm')
  const [customTemplates, setCustomTemplates] = useState<CustomTemplate[]>(() => loadCustomTemplates())
  // 點外面關閉 popover
  useEffect(() => {
    if (insertOpenAt === null) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement
      if (!t.closest('[data-vlm-insert-popover]') && !t.closest('[data-vlm-insert-trigger]')) {
        setInsertOpenAt(null)
      }
    }
    window.addEventListener('mousedown', onDown)
    return () => window.removeEventListener('mousedown', onDown)
  }, [insertOpenAt])

  const handlePickTemplate = (index: number, prompt: string, label: string) => {
    insertVlmCheckAt(index, prompt, label)
    setInsertOpenAt(null)
    toast.success(`已插入 vlm_check：${label}`)
  }

  const handleSaveCustomFromCurrent = (label: string, prompt: string) => {
    const trimmed = label.trim()
    const trimmedPrompt = prompt.trim()
    if (!trimmed || !trimmedPrompt) { toast.error('名稱和 prompt 都不能空白'); return }
    const next = [...customTemplates, { id: `custom_${Date.now()}`, label: trimmed, prompt: trimmedPrompt }]
    setCustomTemplates(next)
    saveCustomTemplates(next)
    toast.success(`已存自訂模板：${trimmed}`)
  }

  const handleDeleteCustom = (id: string) => {
    const next = customTemplates.filter(t => t.id !== id)
    setCustomTemplates(next)
    saveCustomTemplates(next)
  }

  // 三層 fallback (UIA / CV / 座標) 各自獨立 toggle, 預設全 True
  // 對應 backend ComputerUseAction.{use_uia, use_cv, use_coord}
  // 全勾 = UIA → CV → 強制座標 (預設); 取消某層改變鏈, 全關 = 該 action 失敗
  const toggleLayer = (i: number, field: 'use_uia' | 'use_cv' | 'use_coord') => {
    const next = [...(data.actions || [])]
    const cur: any = { ...next[i] }
    // 預設視為 True (所有欄位都是預設 true)
    const currentlyOn = cur[field] !== false
    cur[field] = !currentlyOn
    next[i] = cur
    onUpdate({ actions: next })
  }

  // Preset 一鍵設好 3 個 toggle (對應 4 個常見模式:全 / 純UIA / 純CV / 純座標)
  // 舊「圖像比對」單鍵的等價回歸:點「🔍 純 CV」一鍵切到純 CV 模式
  const applyLayerPreset = (i: number, uia: boolean, cv: boolean, coord: boolean) => {
    const next = [...(data.actions || [])]
    next[i] = { ...next[i], use_uia: uia, use_cv: cv, use_coord: coord } as any
    onUpdate({ actions: next })
  }

  return (
    <div className="absolute top-0 right-0 h-full w-[420px] bg-white shadow-2xl border-l border-gray-100 flex flex-col z-30 overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b" style={{ borderTopColor: NODE_COLOR, borderTopWidth: 3 }}>
        <span className="w-8 h-8 rounded-full flex items-center justify-center text-white shrink-0"
          style={{ background: NODE_COLOR }}><MousePointer2 className="w-4 h-4" strokeWidth={2.4} /></span>
        <div className="flex-1 min-w-0">
          <span className="font-semibold text-gray-800 text-sm block truncate">桌面自動化節點</span>
          <span className="text-xs text-gray-400">
            {(data.cuMode || 'pixel') === 'uia'
              ? 'UIA 控制(讀 GUI 結構、不靠座標)'
              : '錄製滑鼠/鍵盤操作，以圖像錨點穩定回放'}
          </span>
        </div>
        {/* ⚠ 這顆一定要放在標題區塊「外面」。塞進上面那個 block truncate 的 span 裡,
            truncate 的 overflow:hidden + white-space:nowrap 會把按鈕文字裁掉/壓扁,
            面板固定 420px 寬更明顯。 */}
        <AskAiButton
          className="shrink-0"
          nodeLabel={`桌面自動化節點（${(data.cuMode || 'pixel') === 'uia' ? 'UIA 控制' : '錄製回放'}）`}
          stepName={data.name}
          uiaWindow={data.uiaWindow || ''}
          actions={data.actions}
        />
        <button onClick={onDelete} title="刪除" className="text-gray-300 hover:text-red-400 transition-colors p-1 shrink-0">🗑</button>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors"><X className="w-4 h-4" /></button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Name */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">節點名稱</label>
          <input value={data.name} onChange={e => onUpdate({ name: e.target.value })} className={`${inputCls} font-mono`} />
        </div>

        {/* 模式切換:Pixel(錄製座標) vs UIA(讀 GUI 結構) — 進階選項、預設藏起來
            預設只顯示 Pixel 模式(自動三層 fallback:UIA→CV→座標、無腦用)。
            想用獨立 UIA Inspector 才需要打開「顯示進階選項」。 */}
        {showAdvanced ? (
          <>
            <div className="rounded-xl border border-gray-200 overflow-hidden flex">
              <button
                type="button"
                onClick={() => onUpdate({ cuMode: 'pixel' })}
                className={`flex-1 px-3 py-2 text-sm font-medium transition-colors ${
                  (data.cuMode || 'pixel') === 'pixel'
                    ? 'bg-purple-600 text-white'
                    : 'bg-white text-gray-600 hover:bg-purple-50'
                }`}
              >
                🎯 Pixel 模式<span className="text-[10px] block mt-0.5 opacity-80">錄製座標 + CV/OCR/VLM</span>
              </button>
              <button
                type="button"
                onClick={() => onUpdate({ cuMode: 'uia' })}
                className={`flex-1 px-3 py-2 text-sm font-medium transition-colors ${
                  data.cuMode === 'uia'
                    ? 'bg-purple-600 text-white'
                    : 'bg-white text-gray-600 hover:bg-purple-50'
                }`}
              >
                🪟 UIA 模式<span className="text-[10px] block mt-0.5 opacity-80">讀 GUI 結構、座標漂免疫</span>
              </button>
            </div>

            {/* UIA 模式:走 inspector 抓元素、選元素、加動作 */}
            {data.cuMode === 'uia' && (
              <UiaInspectorPanel
                stepName={data.name}
                uiaWindow={data.uiaWindow || ''}
                onUpdateWindow={(w) => onUpdate({ uiaWindow: w })}
                onAddAction={(action) => {
                  const next = [...(data.actions || [])]
                  if (uiaInsertAt !== null && uiaInsertAt <= next.length) {
                    next.splice(uiaInsertAt, 0, action)
                    setUiaInsertAt(uiaInsertAt + 1)   // 連加幾個都保持順序
                  } else {
                    next.push(action)
                  }
                  onUpdate({ actions: next })
                }}
                workflowId={workflowId}
              />
            )}
          </>
        ) : (
          // 隱藏進階模式時、確保 cuMode 是 pixel(避免進階關閉但 cuMode 還停在 uia 導致面板亂)
          (() => {
            if (data.cuMode === 'uia') onUpdate({ cuMode: 'pixel' })
            return null
          })()
        )}

        {/* 錄製按鈕 — 只 Pixel 模式才顯示 */}
        {(data.cuMode || 'pixel') === 'pixel' && (
        <div className="p-3 rounded-lg border border-purple-200 bg-purple-50/50 space-y-2">
          <div className="flex items-center gap-2">
            {!recording ? (
              <button onClick={handleStart}
                className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-red-500 hover:bg-red-600 text-white rounded-lg text-sm font-medium transition-colors">
                <Circle className="w-3.5 h-3.5 fill-current" />
                開始錄製
              </button>
            ) : (
              <div className="flex-1 flex flex-col gap-1">
                <div className="text-center text-xs font-semibold text-red-600 animate-pulse">
                  🎯 推薦:按 <kbd className="px-1.5 py-0.5 rounded bg-red-100 text-red-700 font-mono text-[11px]">F9</kbd> 停止(免回來點按鈕害目標 app 失焦)
                </div>
                <button onClick={handleStop}
                  className="flex items-center justify-center gap-2 px-3 py-2 bg-gray-700 hover:bg-gray-800 text-white rounded-lg text-sm font-medium transition-colors">
                  <StopIcon className="w-3.5 h-3.5" />
                  或點此停止錄製
                </button>
              </div>
            )}
          </div>
          {/* F7 待命模式 — 用熱鍵開啟錄製、不必回來點按鈕害目標 app 失焦 */}
          {!recording && (
            <div className="flex items-center gap-2">
              {!armed ? (
                <button onClick={handleArmHotkey}
                  className="flex-1 flex items-center justify-center gap-2 px-3 py-1.5 border border-purple-300 text-purple-700 hover:bg-purple-100 rounded-lg text-xs font-medium transition-colors">
                  📡 啟用 F7 待命(免點按鈕、按 F7 直接錄)
                </button>
              ) : (
                <button onClick={handleDisarmHotkey}
                  className="flex-1 flex items-center justify-center gap-2 px-3 py-1.5 border border-orange-400 bg-orange-50 text-orange-700 hover:bg-orange-100 rounded-lg text-xs font-medium transition-colors animate-pulse">
                  🔫 F7 已待命、按 F7 開始(點此取消)
                </button>
              )}
            </div>
          )}
          {recording && (
            <p className="text-xs text-red-600 flex items-center gap-1.5">
              <span className="inline-block w-2 h-2 rounded-full bg-red-500 animate-pulse" />
              {statusText}
            </p>
          )}
        </div>
        )}

        {/* 動作列表 */}
        <div>
          {/* 顯示進階選項 toggle — 從上面挪過來、放動作序列上方,跟列表視覺上一組 */}
          <div className="flex items-center justify-end mb-1.5">
            <label className="flex items-center gap-1.5 text-[11px] text-gray-400 hover:text-gray-600 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={showAdvanced}
                onChange={e => setShowAdvanced(e.target.checked)}
                className="w-3 h-3 accent-purple-500"
              />
              顯示進階選項(UIA Inspector、模式切換)
            </label>
          </div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
              動作序列（{data.actions?.length ?? 0}）
              {uiaInsertAt !== null && (
                <button type="button" onClick={() => setUiaInsertAt(null)}
                  title="取消插入點、回到加在最後"
                  className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-50 border border-indigo-200 text-indigo-700 whitespace-nowrap">
                  新動作插在 #{uiaInsertAt + 1} 前 ✕
                </button>
              )}
            </label>
            {data.actions && data.actions.length > 0 && (
              <button onClick={async () => {
                const dir = data.assetsDir || defaultAssetsDir
                const alsoDelete = confirm(
                  '清除所有動作？\n\n按「確定」會同時刪除磁碟上的錨點圖資料夾（建議，避免殘留檔）。\n按「取消」則只清空節點動作、保留磁碟檔（通常不需要）。'
                )
                onUpdate({ actions: [] })
                if (alsoDelete && dir) {
                  try {
                    const r = await deleteComputerUseAssets(dir)
                    if (r.deleted) toast.success(`已刪除錨點資料夾：${r.path}`)
                    else toast.info(r.reason || '資料夾不存在')
                  } catch (e) {
                    toast.error((e as Error).message)
                  }
                }
              }}
                className="text-[11px] text-red-500 hover:text-red-700">清除全部</button>
            )}
          </div>
          {/* 錄製中提示：F8 插入視覺判斷標記、F9 停止 */}
          {recording && (
            <p className="text-[11px] text-purple-700 bg-purple-50 border border-purple-200 rounded px-2 py-1 mb-2">
              <Eye className="inline w-3 h-3 mr-1" />
              錄製中：按 <span className="font-mono font-bold">F8</span> 在當下位置插入視覺判斷（vlm_check）標記
              ／按 <span className="font-mono font-bold">F9</span> 停止錄製
            </p>
          )}
          {(!data.actions || data.actions.length === 0) ? (
            <>
              <p className="text-xs text-gray-400 text-center py-6 border border-dashed border-gray-200 rounded-lg">
                尚未錄製任何動作
              </p>
              {/* 沒動作時也可以手動加 vlm_check */}
              <InsertHub expanded={gapOpenAt === 0} onExpand={() => setGapOpenAt(0)} onCollapse={() => setGapOpenAt(null)}>
              <OcrFieldInserter
                index={0}
                isOpen={insertOpenAt === 0 && insertKind === 'ocr'}
                openMenu={() => { setInsertOpenAt(0); setInsertKind('ocr') }}
                closeMenu={() => setInsertOpenAt(null)}
                onAdd={insertActionAt}
              />
              <WaitDownloadInserter
                index={0}
                isOpen={insertOpenAt === 0 && insertKind === 'dl'}
                openMenu={() => { setInsertOpenAt(0); setInsertKind('dl') }}
                closeMenu={() => setInsertOpenAt(null)}
                onAdd={insertActionAt}
              />
              <OcrWaitInserter
                index={0}
                isOpen={insertOpenAt === 0 && insertKind === 'ocrwait'}
                openMenu={() => { setInsertOpenAt(0); setInsertKind('ocrwait') }}
                closeMenu={() => setInsertOpenAt(null)}
                onAdd={insertActionAt}
              />
              <ForEachInserter
                index={0}
                isOpen={insertOpenAt === 0 && insertKind === 'foreach'}
                hasActions={(data.actions || []).length > 0}
                tailCount={Math.max(0, (data.actions || []).length - (0))}
                openMenu={() => { setInsertOpenAt(0); setInsertKind('foreach') }}
                closeMenu={() => setInsertOpenAt(null)}
                onInsert={insertActionAt}
                onWrapAll={wrapAllIntoForEach}
                onWrapAfter={wrapAfterIntoForEach}
              />
              <ClipboardReadInserter
                index={0}
                isOpen={insertOpenAt === 0 && insertKind === 'clip'}
                openMenu={() => { setInsertOpenAt(0); setInsertKind('clip') }}
                closeMenu={() => setInsertOpenAt(null)}
                onAdd={insertActionAt}
              />
              <WakeWindowInserter
                index={0}
                isOpen={insertOpenAt === 0 && insertKind === 'wake'}
                defaultTitle={data.uiaWindow || ''}
                openMenu={() => { setInsertOpenAt(0); setInsertKind('wake') }}
                closeMenu={() => setInsertOpenAt(null)}
                onAdd={insertActionAt}
  onAddMany={insertActionsAt}
              />
              </InsertHub>
              <VlmCheckInserter
                index={0}
                isOpen={insertOpenAt === 0 && insertKind === 'vlm'}
                openMenu={() => { setInsertOpenAt(0); setInsertKind('vlm') }}
                onPick={handlePickTemplate}
                onSaveCustom={handleSaveCustomFromCurrent}
                onDeleteCustom={handleDeleteCustom}
                customTemplates={customTemplates}
              />
            </>
          ) : (
            <div className="space-y-1.5">
              {data.actions.map((a: ComputerUseAction, i: number) => (
                <div key={i}>
                {/* 動作前的 ➕ 插入點 */}
                <div className="flex items-center justify-center gap-2 flex-wrap">
                  <InsertHub expanded={gapOpenAt === i} onExpand={() => setGapOpenAt(i)} onCollapse={() => setGapOpenAt(null)}>
                  <OcrFieldInserter
                    index={i}
                    isOpen={insertOpenAt === i && insertKind === 'ocr'}
                    openMenu={() => { setInsertOpenAt(i); setInsertKind('ocr') }}
                    closeMenu={() => setInsertOpenAt(null)}
                    onAdd={insertActionAt}
                  />
                  <WaitDownloadInserter
                    index={i}
                    isOpen={insertOpenAt === i && insertKind === 'dl'}
                    openMenu={() => { setInsertOpenAt(i); setInsertKind('dl') }}
                    closeMenu={() => setInsertOpenAt(null)}
                    onAdd={insertActionAt}
                  />
                  <OcrWaitInserter
                    index={i}
                    isOpen={insertOpenAt === i && insertKind === 'ocrwait'}
                    openMenu={() => { setInsertOpenAt(i); setInsertKind('ocrwait') }}
                    closeMenu={() => setInsertOpenAt(null)}
                    onAdd={insertActionAt}
                  />
                  <ForEachInserter
                    index={i}
                    isOpen={insertOpenAt === i && insertKind === 'foreach'}
                    hasActions={(data.actions || []).length > 0}
                    tailCount={Math.max(0, (data.actions || []).length - (i))}
                    openMenu={() => { setInsertOpenAt(i); setInsertKind('foreach') }}
                    closeMenu={() => setInsertOpenAt(null)}
                    onInsert={insertActionAt}
                    onWrapAll={wrapAllIntoForEach}
                    onWrapAfter={wrapAfterIntoForEach}
                  />
                  <ClipboardReadInserter
                    index={i}
                    isOpen={insertOpenAt === i && insertKind === 'clip'}
                    openMenu={() => { setInsertOpenAt(i); setInsertKind('clip') }}
                    closeMenu={() => setInsertOpenAt(null)}
                    onAdd={insertActionAt}
                  />
                  <WakeWindowInserter
                    index={i}
                    isOpen={insertOpenAt === i && insertKind === 'wake'}
                    defaultTitle={data.uiaWindow || ''}
                    openMenu={() => { setInsertOpenAt(i); setInsertKind('wake') }}
                    closeMenu={() => setInsertOpenAt(null)}
                    onAdd={insertActionAt}
  onAddMany={insertActionsAt}
                  />
                  </InsertHub>
                  <VlmCheckInserter
                    index={i}
                    isOpen={insertOpenAt === i && insertKind === 'vlm'}
                    openMenu={() => { setInsertOpenAt(i); setInsertKind('vlm') }}
                    onPick={handlePickTemplate}
                    onSaveCustom={handleSaveCustomFromCurrent}
                    onDeleteCustom={handleDeleteCustom}
                    customTemplates={customTemplates}
                  />
                  <button type="button"
                    onClick={() => setUiaInsertAt(uiaInsertAt === i ? null : i)}
                    title="把「選擇器 / 讀文字 / 填入文字 / 錄製」新增的動作插到這個位置（再點一次取消）"
                    className={`text-[10px] px-1.5 py-0.5 rounded-full border whitespace-nowrap shrink-0 transition-colors ${
                      uiaInsertAt === i
                        ? 'bg-indigo-600 border-indigo-600 text-white'
                        : 'border-dashed border-gray-300 text-gray-400 hover:text-indigo-600 hover:border-indigo-300'}`}>
                    {uiaInsertAt === i ? '⊕ 新動作插入這裡' : '⊕ 插入點'}
                  </button>
                </div>
                <div className="flex items-start gap-2 p-2 bg-gray-50 border border-gray-200 rounded-lg">
                  <span className="text-[10px] font-mono text-gray-400 pt-0.5">#{i + 1}</span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-[11px] px-1.5 py-0.5 rounded font-mono bg-purple-100 text-purple-700">
                        {a.type}
                      </span>
                      {a.image && <span className="text-[11px] text-gray-500 truncate">{a.image}</span>}
                      {/* 三層 fallback toggle (UIA / CV / 強制座標)
                          預設全勾 = UIA → CV → 強制座標(使用者零學習成本、最高命中率)
                          OCR 或 VLM 啟用時整組 disabled——那兩個自帶 primary 邏輯、三層不適用
                          只勾單一 = 嚴格模式(沒中就 fail)、組合 = 自定義 fallback 鏈 */}
                      {(a.type === 'click_image' || a.type === 'click_at') && (() => {
                        const ocrActive = a.use_ocr === true
                        const vlmActive = (a.vlm_mode || 'off') !== 'off'
                        const explicitPrimary = ocrActive || vlmActive
                        const useUia = (a as any).use_uia !== false
                        const useCv = (a as any).use_cv !== false
                        const useCoord = (a as any).use_coord !== false
                        const layerBtn = (label: string, field: 'use_uia' | 'use_cv' | 'use_coord', on: boolean, hint: string) => (
                          <button
                            key={field}
                            type="button"
                            onClick={() => toggleLayer(i, field)}
                            disabled={explicitPrimary}
                            title={explicitPrimary
                              ? `${ocrActive ? 'OCR' : 'VLM'} 啟用中、三層 fallback 不適用`
                              : hint}
                            className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
                              explicitPrimary
                                ? 'bg-gray-50 border-gray-200 text-gray-300 cursor-not-allowed'
                                : on
                                  ? 'bg-emerald-50 border-emerald-300 text-emerald-700'
                                  : 'bg-white border-gray-200 text-gray-400 hover:text-gray-700 hover:border-gray-400'
                            }`}
                          >
                            <span className="font-mono mr-0.5">{on ? '☑' : '☐'}</span>{label}
                          </button>
                        )
                        // Preset chip:常見模式一鍵切到對應的 3-toggle 組合
                        // 純 CV preset 設 use_coord=T (action 層級開), 真正座標 fallback 開關走 step-level cvCoordFallback
                        // 純 UIA / 純 座標 preset 嚴格、其他層 toggle 設 F → 沒中立即 fail
                        const isAll = useUia && useCv && useCoord
                        const isUiaOnly = useUia && !useCv && !useCoord
                        const isCvOnly = !useUia && useCv && useCoord
                        const isCoordOnly = !useUia && !useCv && useCoord
                        const currentMode: 'all' | 'uia-only' | 'cv-only' | 'coord-only' | 'custom' =
                          isAll ? 'all'
                            : isUiaOnly ? 'uia-only'
                            : isCvOnly ? 'cv-only'
                            : isCoordOnly ? 'coord-only'
                            : 'custom'
                        // step-level cvCoordFallback (預設 False) → 純 CV 模式下『座標 fallback 是否啟用』的真正 gate
                        // 純 CV 模式下 座標 checkbox 顯示 = cvCoordFallback、點擊 → toggle cvCoordFallback (而不是 action use_coord)
                        const cvCoordFallback = data.cvCoordFallback === true
                        const actCoordFallback = (a as any).coord_fallback === true
                        const presetBtn = (label: string, active: boolean, onClick: () => void, hint: string) => (
                          <button
                            type="button"
                            onClick={onClick}
                            disabled={explicitPrimary}
                            title={explicitPrimary
                              ? `${ocrActive ? 'OCR' : 'VLM'} 啟用中、模式 preset 不適用`
                              : hint}
                            className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
                              explicitPrimary
                                ? 'bg-gray-50 border-gray-200 text-gray-300 cursor-not-allowed'
                                : active
                                  ? 'bg-purple-500 text-white border-purple-500'
                                  : 'bg-white text-gray-500 border-gray-200 hover:border-purple-300 hover:text-purple-600'
                            }`}
                          >{label}</button>
                        )
                        // 純 CV 模式下、座標 checkbox 的特製版:狀態 = 這個動作的 coord_fallback 或節點全開;
                        // 點擊只切這個動作(節點全開時由節點設定決定、這裡鎖住)
                        const coordFbOn = actCoordFallback || cvCoordFallback
                        const coordBoxCvOnly = (
                          <button
                            key="coord-cv-only"
                            type="button"
                            onClick={() => {
                              const next = [...(data.actions || [])]
                              next[i] = { ...next[i], coord_fallback: !actCoordFallback } as any
                              onUpdate({ actions: next })
                            }}
                            disabled={explicitPrimary || cvCoordFallback}
                            title={cvCoordFallback
                              ? '節點的『CV 詳細設定 → CV 失敗退回錄製座標』已全開,要逐個關請先關掉那個設定'
                              : `CV 找不到時是否退到錄製座標(目前 ${actCoordFallback ? '啟用' : '關閉'})`}
                            className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
                              explicitPrimary
                                ? 'bg-gray-50 border-gray-200 text-gray-300 cursor-not-allowed'
                                : coordFbOn
                                  ? 'bg-emerald-50 border-emerald-300 text-emerald-700'
                                  : 'bg-white border-gray-200 text-gray-400 hover:text-gray-700 hover:border-gray-400'
                            }`}
                          >
                            <span className="font-mono mr-0.5">{coordFbOn ? '☑' : '☐'}</span>📍 座標 (fallback)
                          </button>
                        )
                        // 顯示哪些 checkbox 依 currentMode(避免「純 UIA / 純 CV / 純 座標」preset 還顯示無關 layer 視覺重疊)
                        const showUiaBox = currentMode === 'all' || currentMode === 'uia-only' || currentMode === 'custom'
                        const showCvBox = (currentMode === 'all' || currentMode === 'cv-only' || currentMode === 'custom') && a.type === 'click_image'
                        const showCoordBox = currentMode === 'all' || currentMode === 'cv-only' || currentMode === 'coord-only' || currentMode === 'custom'
                        return (
                          <>
                            {/* 模式快速 preset(一鍵切常見組合) */}
                            {presetBtn('全 (三層)', isAll, () => applyLayerPreset(i, true, true, true),
                              '預設三層 fallback:UIA → CV → 強制座標,命中率最高。下方 3 個 checkbox 顯示全勾、可手動取消任一變組合模式')}
                            {presetBtn('🪟 純 UIA', isUiaOnly, () => applyLayerPreset(i, true, false, false),
                              '純 UIA 嚴格模式:只用 UI 結構定位、找不到立即 fail(適合自家程式 + 有 AutomationId)')}
                            {a.type === 'click_image' && presetBtn('🔍 純 CV', isCvOnly, () => applyLayerPreset(i, false, true, true),
                              '純圖像比對:UIA 跳過, CV 找不到時要不要退座標看下方「📍 座標 (fallback)」(錄製預設已勾)')}
                            {presetBtn('📍 純 座標', isCoordOnly, () => applyLayerPreset(i, false, false, true),
                              '純座標模式:直接點錄製的 x/y、不嘗試任何識別(最快、視窗位置固定才安全)')}
                            <span className="text-[10px] text-gray-300 select-none">|</span>
                            {/* 細項 checkbox(依當前 preset 動態決定顯示哪幾個、避免跟 preset 視覺重疊) */}
                            {showUiaBox && layerBtn('🪟 UIA', 'use_uia', useUia,
                              '啟用 UIA element 結構定位(視窗位置變化最穩、自家程式有 AutomationId 命中率最高)。取消 = 跳過 UIA 直接走下一層')}
                            {showCvBox && layerBtn('🔍 CV', 'use_cv', useCv,
                              '啟用 CV 圖像比對(用錄製的錨點圖找)。取消 = 跳過 CV、UIA 沒中直接退強制座標')}
                            {showCoordBox && (currentMode === 'cv-only' ? coordBoxCvOnly : layerBtn('📍 座標', 'use_coord', useCoord,
                              '啟用強制座標(最終 fallback、直接點錄製的 x/y)。取消 = 前面層失敗就立即 fail、不退座標'))}
                          </>
                        )
                      })()}
                      {/* 手動編輯錨點（click_image/drag 有 full_image 時才顯示） */}
                      {(a.type === 'click_image' || a.type === 'drag') && a.full_image && (
                        <button onClick={() => setEditingAnchor(i)}
                          title="手動圈選錨點（用全螢幕截圖重新定義這個動作要比對的區域）"
                          className="text-[10px] px-1.5 py-0.5 rounded border bg-white border-purple-200 text-purple-600 hover:bg-purple-50">
                          <Pencil className="w-2.5 h-2.5 inline" /> 編輯錨點
                        </button>
                      )}
                    </div>
                    {/* uia 取值/填值的描述動態組 —— description 是加入當下冷凍的字串，
                        改了 save_as/text 不會跟著變 */}
                    {(() => {
                      const dyn = a.type === 'uia_get_clipboard'
                        ? `讀剪貼簿 → {{${a.save_as || '?'}}}`
                        : a.type === 'uia_get_text' && a.save_as
                        ? `讀「${a.control?.name || a.control?.auto_id || '控制項'}」→ {{${a.save_as}}}`
                        : a.type === 'uia_select' && a.text
                        ? `選「${a.control?.name || a.control?.auto_id || '下拉'}」→ ${a.text}`
                        : a.type === 'if_element_found'
                        ? `若「${a.control?.name || a.control?.auto_id || '元素'}」出現 → ${(a.then || []).length} 個子動作，否則 ${(a.else || []).length} 個`
                        : a.type === 'wait_download'
                        ? `等下載完成：${a.pattern || '*'}${a.save_as ? ` → {{${a.save_as}}}` : ''}`
                        : a.type === 'for_each'
                        ? `逐筆迴圈 ${Array.isArray(a.items) ? a.items.length + ' 筆' : `「${String(a.items || '').slice(0, 30)}」`} → {{${a.save_as || '?'}}}，每筆 ${(a.do || []).length} 個子動作`
                        : a.type === 'wait_text'
                        ? `等文字「${a.text || ''}」${a.until === 'disappear' ? '消失' : '出現'}(OCR)`
                        : a.type === 'if_text_found'
                        ? `若讀到「${a.text || ''}」→ ${(a.then || []).length} 個子動作，否則 ${(a.else || []).length} 個(OCR)`
                        : a.type === 'uia_wait'
                        ? `等「${a.control?.name || a.control?.auto_id || '目標'}」${a.until === 'disappear' ? '消失' : a.until === 'text_contains' ? `文字含「${a.text || ''}」` : a.until === 'text_equals' ? `文字=「${a.text || ''}」` : '出現'}`
                        : a.type === 'uia_send_keys' && a.text
                          ? `填入「${a.control?.name || a.control?.auto_id || '控制項'}」← ${a.text}`
                          : a.description
                      return dyn ? <p className="text-xs text-gray-600 mt-0.5 truncate">{dyn}</p> : null
                    })()}
                    {/* 純 UIA + 匿名元素 = 必定找不到 */}
                    {(() => {
                      const ui = (a as any).ui
                      const uiaOnly = a.use_uia !== false && a.use_cv === false && a.use_coord === false
                      if (!uiaOnly || !ui || ui.name || ui.auto_id) return null
                      return (
                        <p className="text-[10px] text-red-700 bg-red-50 border border-red-200 rounded px-1.5 py-1 mt-1 leading-relaxed">
                          ⛔ 錄到的 UIA 元素是<strong>匿名的</strong>（沒有 name 也沒有 auto_id，
                          type={ui.type || '?'}）——純 UIA 模式<strong>必定找不到</strong>。
                          請保留 CV 層（全三層或純 CV），或回 Inspector 找可指名的替代元素。
                        </p>
                      )
                    })()}
                    {/* 錄到注音按鍵的提示 */}
                    {(a.type === 'type_text' || (a.type === 'uia_send_keys' && a.text)) &&
                      looksLikeImeKeys(a.text || '') && (
                      (a as any).type_method === 'keys' ? (
                        <p className="text-[10px] text-sky-700 bg-sky-50 border border-sky-200 rounded px-1.5 py-1 mt-1 leading-relaxed">
                          ℹ 這是<strong>注音按鍵序列</strong>，回放會逐鍵敲、由輸入法重新組字成中文。
                          前提：回放時輸入法的中英狀態要跟錄製時一致。想更穩就按 ✎ 直接改成中文。
                        </p>
                      ) : (
                        <p className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-1 mt-1 leading-relaxed">
                          ⚠ 這串看起來是<strong>注音鍵盤的按鍵序列</strong>——錄到的是原始按鍵，
                          回放會原樣貼進欄位。請按 ✎ 改成實際要輸入的中文。
                        </p>
                      )
                    )}
                    {/* 純色錨點：比「有替身」嚴重 —— CV 對它完全無效 */}
                    {anchorRisk[i]?.flat && (
                      <p className="text-[10px] text-red-700 bg-red-50 border border-red-200 rounded px-1.5 py-1 mt-1 leading-snug">
                        ⛔ 這張錨點<strong>幾乎沒有特徵</strong>（灰階變異數 {anchorRisk[i].variance}）。
                        它跟畫面上<strong>任何一塊平坦區域</strong>比對都會是滿分，
                        所以 CV 可能命中完全無關的位置。
                        <br />
                        請按「編輯錨點」重圈一個<strong>含文字或邊框</strong>的範圍。
                      </p>
                    )}
                    {/* 有替身：只在「執行時搆得到而且分數搶得走」時才報，
                        而且建議要對症下藥 —— 替身在搜尋半徑內時勾「只搜附近」沒用 */}
                    {anchorRisk[i] && !anchorRisk[i].flat && (() => {
                      const r = anchorRisk[i]
                      const nearRisk = (r.phases?.near || 0) > 0 || (r.phases?.box || 0) > 0
                      const onlyFullscreen = !nearRisk && (r.phases?.fullscreen || 0) > 0
                      return (
                        <p className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-1 mt-1 leading-snug">
                          ⚠ 回放時搜尋範圍內有 {r.rivals} 個地方<strong>相似度逼近真目標</strong>
                          （目標 {r.targetScore?.toFixed(2)} vs 它 {r.rivalScore?.toFixed(2)}，
                          最近的在 {r.nearest}px 外）。
                          CV 取範圍內分數最高的，所以真目標只要掉一點分就可能被搶走。
                          {r.scanned > r.rivals && (
                            <span className="text-amber-600">
                              （另有 {r.scanned - r.rivals} 個較像的地方，但分數差得夠遠、搶不走，沒列入）
                            </span>
                          )}
                          <br />
                          {onlyFullscreen
                            ? '只有在「橘框和附近都找不到、退回整個桌面」時才會撞到。可勾步驟設定的「只搜錄製座標附近」（找不到就停，不退回全桌面）。'
                            : '它就在搜尋範圍內，所以勾「只搜附近」沒有用。建議：把 CV 搜尋半徑縮小／拖一個橘框把範圍鎖小／改用 UIA 定位／把錨點框大一點含周邊文字讓它變獨特。'}
                        </p>
                      )
                    })()}

                    {a.text && <p className="text-xs text-gray-500 mt-0.5 truncate font-mono">"{a.text}"</p>}
                    {a.keys && a.keys.length > 0 && (
                      <p className="text-xs text-gray-500 mt-0.5 font-mono">{a.keys.join('+')}</p>
                    )}
                    {typeof a.seconds === 'number' && a.seconds > 0 && (
                      <p className="text-xs text-gray-500 mt-0.5">{a.seconds}s</p>
                    )}
                    {/* vlm_check 動作：直接內嵌 vlm_prompt 編輯 */}
                    {a.type === 'vlm_check' && (
                      <div className="mt-1 space-y-1">
                        <textarea
                          value={a.vlm_prompt || ''}
                          onChange={e => applyAnchorPatch(i, { vlm_prompt: e.target.value } as Partial<ComputerUseAction>)}
                          placeholder="判斷條件（例：畫面是否出現綠色「登入成功」訊息？）"
                          rows={2}
                          className="w-full text-[11px] px-1.5 py-1 rounded border border-purple-300 bg-white outline-none focus:border-purple-500 focus:ring-1 focus:ring-purple-400/20 font-mono resize-y"
                        />
                        {!a.vlm_prompt && (
                          <p className="text-[10px] text-amber-600">⚠ vlm_prompt 為空 — 步驟執行時會直接報錯</p>
                        )}
                      </div>
                    )}
                    {/* VLM 輔助模式（click_image 專用）
                        - off          → 走原本 OCR / 座標 / CV 三模式
                        - description  → VLM 看圖 + vlm_prompt → 回目標文字 → OCR 找該文字 → 點中心
                        - anchor_pick  → VLM 從多張錨點變體挑一張 → 用挑出的圖走 CV 比對
                        - grounding    → 地端 GUI 定位模型直接回座標（唯一讓 VLM 決定座標的模式；
                                         用的是專門訓練過 GUI 定位的地端模型，不是雲端通用模型。
                                         失敗會自動退回 CV，不會讓整步掛掉）
                        VLM mode 開了會吃掉 OCR/座標短路（每次回放多一次 VLM 呼叫；準確度↑、速度↓） */}
                    {a.type === 'click_image' && (() => {
                      const vlmMode = (a.vlm_mode || 'off') as 'off' | 'description' | 'anchor_pick' | 'grounding'
                      const vlmActive = vlmMode !== 'off'
                      return (
                        <div className="mt-1 space-y-1">
                          <div className="flex items-center gap-1 flex-wrap">
                            <span className="text-[10px] text-gray-500 mr-0.5">VLM 輔助：</span>
                            {([
                              { v: 'off',         label: '關',         hint: '不啟用 VLM，走原本 OCR / 座標 / CV' },
                              { v: 'description', label: '描述→OCR',   hint: 'VLM 看圖回目標文字→OCR 找文字→點中心。VLM 不給座標' },
                              { v: 'anchor_pick', label: '挑錨點',     hint: 'VLM 從多張變體錨點挑最像的→用該張走 CV 比對' },
                              { v: 'grounding',   label: '直接定位',   hint: '地端 GUI 定位模型直接給座標（連 CV 都點不準時用）。第一次呼叫要載入模型約 30 秒，之後每次約 2-7 秒。失敗自動退回 CV' },
                            ] as const).map(opt => {
                              // 「直接定位」需要沙盒容器 + NVIDIA GPU + 已下載模型。
                              // 條件不足時停用而不是藏起來 —— 藏起來使用者永遠不知道有這功能。
                              const locked = opt.v === 'grounding' && gStatus !== null && !gStatus.available
                              return (
                              <button
                                key={opt.v}
                                type="button"
                                disabled={locked}
                                onClick={() => applyAnchorPatch(i, { vlm_mode: opt.v })}
                                title={locked
                                  ? `無法使用：${gStatus?.reason}\n${gStatus?.install_hint}`
                                  : opt.hint}
                                className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
                                  locked
                                    ? 'bg-gray-50 text-gray-300 border-gray-200 cursor-not-allowed'
                                    : vlmMode === opt.v
                                      ? 'bg-indigo-500 text-white border-indigo-500'
                                      : 'bg-white text-gray-500 border-gray-200 hover:border-indigo-300 hover:text-indigo-600'
                                }`}
                              >{locked ? `${opt.label} 🔒` : opt.label}</button>
                              )
                            })}
                          </div>
                          {gStatus && !gStatus.available && (
                            <p className="text-[10px] text-gray-500 bg-gray-50 border border-gray-200 rounded px-1.5 py-1 leading-snug">
                              🔒「直接定位」無法使用：{gStatus.reason}
                              <br />
                              {gStatus.install_hint}
                            </p>
                          )}
                          {vlmMode === 'description' && (
                            <textarea
                              value={a.vlm_prompt || ''}
                              onChange={e => applyAnchorPatch(i, { vlm_prompt: e.target.value })}
                              placeholder="描述要點什麼（例：紅色「送出」按鈕，不是藍色取消鈕）"
                              rows={2}
                              className="w-full text-[11px] px-1.5 py-1 rounded border border-indigo-300 bg-white outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-400/20 font-mono resize-y"
                            />
                          )}
                          {vlmMode === 'grounding' && (
                            <>
                              <textarea
                                value={a.vlm_prompt || ''}
                                onChange={e => applyAnchorPatch(i, { vlm_prompt: e.target.value })}
                                placeholder="描述要點什麼（例：工具列上的排序按鈕 / 左側導覽的「下載」）"
                                rows={2}
                                className="w-full text-[11px] px-1.5 py-1 rounded border border-indigo-300 bg-white outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-400/20 font-mono resize-y"
                              />
                              <div className="flex items-center gap-1.5">
                                <button
                                  type="button"
                                  disabled={describingIdx === i || !a.image}
                                  onClick={() => genGroundingDesc(i, a)}
                                  title={a.image
                                    ? '用主模型看錄製時的截圖，產生描述草稿（會呼叫一次雲端視覺 API）'
                                    : '這個步驟沒有錨點圖，無法自動產生'}
                                  className="text-[10px] px-1.5 py-0.5 rounded border border-indigo-300 text-indigo-600 bg-white hover:bg-indigo-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                                >{describingIdx === i ? '產生中…' : '🪄 幫我產生'}</button>
                                <span className="text-[10px] text-gray-400">產生後會自動驗一次</span>
                              </div>
                              {descVerify[i]?.startsWith('SKIP:') && (
                                <p className="text-[10px] text-gray-500 bg-gray-50 border border-gray-200 rounded px-1.5 py-1 leading-snug">
                                  ℹ {descVerify[i]!.slice(5)} —— 這段描述<strong>沒有被驗證過</strong>，請自己確認。
                                </p>
                              )}
                              {descVerify[i] && !descVerify[i]!.startsWith('SKIP:') && (
                                <p className="text-[10px] text-red-600 bg-red-50 border border-red-200 rounded px-1.5 py-1 leading-snug">
                                  ⚠ 自我驗證沒過：{descVerify[i]}
                                  <br />
                                  這段描述很可能會點到別的東西，請自己改寫（說出元件上的文字 + 所在區域）。
                                </p>
                              )}
                              <p className="text-[10px] text-gray-500 leading-snug">
                                地端模型直接算座標，不需要錨點圖。適合錨點常失效的元素（主題色會變、視窗會縮放）。
                                <span className="text-amber-600">失敗會自動退回 CV 比對</span>，所以錨點圖建議保留。
                                <br />
                                <span className="text-amber-600">描述不要用否定句</span>
                                （「不是⋯⋯的那個」會被忽略而點錯），改用正面且獨特的特徵。
                              </p>
                            </>
                          )}
                          {vlmMode === 'anchor_pick' && (
                            <>
                              <textarea
                                value={a.vlm_prompt || ''}
                                onChange={e => applyAnchorPatch(i, { vlm_prompt: e.target.value })}
                                placeholder="描述要點什麼（給 VLM 判斷哪張錨點符合當下螢幕）"
                                rows={1}
                                className="w-full text-[11px] px-1.5 py-1 rounded border border-indigo-300 bg-white outline-none focus:border-indigo-500 font-mono resize-y"
                              />
                              {/* 已選錨點 chips（縮圖 + 檔名 + 移除）*/}
                              {(a.vlm_anchors && a.vlm_anchors.length > 0) ? (
                                <div className="flex flex-wrap gap-1.5">
                                  {a.vlm_anchors.map((name, ai) => (
                                    <div key={ai}
                                      className="inline-flex items-center gap-1 px-1.5 py-1 bg-white border border-indigo-300 rounded">
                                      <img
                                        src={assetImageUrl(data.assetsDir || defaultAssetsDir, name)}
                                        alt={name}
                                        className="w-8 h-6 object-contain rounded bg-gray-100"
                                        onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
                                      />
                                      <span className="text-[11px] font-mono text-gray-700 max-w-[120px] truncate" title={name}>
                                        {name}
                                      </span>
                                      <button
                                        type="button"
                                        onClick={() => applyAnchorPatch(i, {
                                          vlm_anchors: (a.vlm_anchors || []).filter(x => x !== name)
                                        })}
                                        title="從候選中移除"
                                        className="text-gray-300 hover:text-red-500 p-0.5"
                                      >
                                        <X className="w-3 h-3" />
                                      </button>
                                    </div>
                                  ))}
                                </div>
                              ) : null}
                              <button
                                type="button"
                                onClick={() => setPickingVlmAnchorsAt(i)}
                                className="w-full text-[11px] px-2 py-1.5 rounded border border-dashed border-indigo-300 bg-white text-indigo-700 hover:bg-indigo-50 hover:border-indigo-500 transition-colors"
                              >
                                {(a.vlm_anchors && a.vlm_anchors.length > 0) ? '+ 從錨點資料夾再選 / 修改' : '+ 從錨點資料夾選圖（不用打檔名）'}
                              </button>
                              <p className="text-[10px] text-gray-500 leading-relaxed">
                                共 {(a.vlm_anchors || []).length} 張候選。
                                <strong className="text-indigo-700">1 張</strong> = VLM 守門員 + 強制 CV（避開盲點錄製座標的 fast-path）；
                                <strong className="text-indigo-700">2+ 張不同變體</strong> = VLM 看畫面當下挑最像的那張，再走 CV
                              </p>
                            </>
                          )}
                          {vlmActive && (
                            <p className="text-[10px] text-amber-600 leading-relaxed">
                              ⚠ VLM 模式啟用中，下方 OCR / 圖像比對切換會被忽略（VLM 永遠優先）。
                              每次回放多一次 VLM 呼叫（耗 token + ~1-3 秒）
                            </p>
                          )}
                        </div>
                      )
                    })()}
                    {/* OCR 文字比對（只對 click_image action 顯示）
                        規則：
                          - checkbox 勾選 = use_ocr=true，input enable；OCR 變為 primary 方法
                          - 取消勾選 = use_ocr=false，但 ocr_text 保留（下次再勾就不用重打）
                          - 勾選 OCR 不改動 use_coord（primary mode 互相獨立；use_coord 只控制
                            OCR 關閉時用什麼）；失敗 fallback 行為由步驟層級 ocr_cv_fallback 控制 */}
                    {a.type === 'click_image' && (() => {
                      const ocrEnabled = a.use_ocr === true
                      const inputId = `ocr-input-${i}`
                      return (
                        <div className="mt-1 flex items-center gap-1.5">
                          <label className="flex items-center gap-1 shrink-0 cursor-pointer select-none"
                            title={ocrEnabled
                              ? '已啟用 OCR 文字比對；OCR 為主要方法（取代 CV）。預設失敗直接 FAIL（不退 CV），需在下方「OCR 比對設定」手動開啟 ocr_cv_fallback 才會退回 CV'
                              : '勾選啟用 Windows OCR 文字比對。需搭配右側輸入目標文字；取消時保留文字供下次使用'}>
                            <input
                              type="checkbox"
                              checked={ocrEnabled}
                              onChange={e => {
                                if (e.target.checked) {
                                  // 啟用 OCR。不動 use_coord、不動 ocr_text（可能有舊值，直接重用）
                                  applyAnchorPatch(i, { use_ocr: true })
                                  // 若沒文字就 focus input 提示使用者填
                                  if (!a.ocr_text) {
                                    setTimeout(() => {
                                      const el = document.getElementById(inputId) as HTMLInputElement | null
                                      el?.focus()
                                    }, 50)
                                  }
                                } else {
                                  // 只翻 use_ocr，保留 ocr_text（下次勾選可直接重用）
                                  applyAnchorPatch(i, { use_ocr: false })
                                }
                              }}
                              className="w-3 h-3 rounded accent-purple-600"
                            />
                            <span className={`text-[10px] ${ocrEnabled ? 'text-purple-700 font-medium' : 'text-gray-500'}`}>
                              🔤 OCR
                            </span>
                          </label>
                          <input
                            id={inputId}
                            type="text"
                            value={a.ocr_text || ''}
                            onChange={e => applyAnchorPatch(i, { ocr_text: e.target.value })}
                            disabled={!ocrEnabled}
                            placeholder={ocrEnabled ? '要找的文字（例：關閉、下載）' : '勾選 OCR 才能填寫（會保留上次輸入）'}
                            className={`flex-1 min-w-0 text-[11px] px-1.5 py-0.5 rounded border outline-none ${
                              ocrEnabled
                                ? 'border-purple-300 bg-white focus:border-purple-500 focus:ring-1 focus:ring-purple-400/20'
                                : 'border-gray-200 bg-gray-50 text-gray-400 cursor-not-allowed'
                            }`}
                          />
                        </div>
                      )
                    })()}
                    {/* VLM 把關 Phase 1：每動作後驗證(expected outcome)─────── */}
                    {/* 只在節點層級啟用 VLM 時(strategy != off)才顯示這欄、避免干擾沒用的人 */}
                    {data.cuVlmCheckStrategy && data.cuVlmCheckStrategy !== 'off' && (
                      <div className="mt-1.5 pt-1.5 border-t border-dashed border-emerald-200">
                        <div className="flex items-center gap-1.5 mb-1">
                          <span className="text-[10px] font-semibold text-emerald-700">🛡 VLM 預期</span>
                          {data.cuVlmCheckStrategy === 'critical_only' && (
                            <label className="flex items-center gap-1 text-[10px] cursor-pointer">
                              <input type="checkbox" checked={a.verify_critical === true}
                                onChange={e => applyAnchorPatch(i, { verify_critical: e.target.checked } as Partial<ComputerUseAction>)}
                                className="w-3 h-3 accent-emerald-600" />
                              <span className="text-emerald-700">標為 critical</span>
                            </label>
                          )}
                          {a.expected && (
                            <span className="text-[9px] text-emerald-600 font-mono">(會驗)</span>
                          )}
                        </div>
                        <textarea
                          value={a.expected || ''}
                          onChange={e => applyAnchorPatch(i, { expected: e.target.value } as Partial<ComputerUseAction>)}
                          placeholder={data.cuVlmCheckStrategy === 'critical_only' && !a.verify_critical
                            ? '(critical_only 模式下、勾上方 critical 才會驗;此欄留空 = 不驗)'
                            : '動作後應看到的狀態（例：另存新檔對話框已開啟、含 File name 輸入框）'}
                          rows={1}
                          className="w-full text-[10px] px-1.5 py-1 rounded border border-emerald-200 bg-emerald-50/30 outline-none focus:border-emerald-400 focus:ring-1 focus:ring-emerald-300/20 resize-y"
                        />
                      </div>
                    )}
                    {editingAction === i && (
                      <InlineActionEditor
                        action={a}
                        workflowId={workflowId}
                        stepName={data.name}
                        onPatch={(patch) => applyAnchorPatch(i, patch)}
                        onClose={() => setEditingAction(null)}
                      />
                    )}
                  </div>
                  <div className="flex flex-col shrink-0">
                    <button onClick={() => moveAction(i, -1)} className="p-0.5 text-gray-400 hover:text-gray-700 disabled:opacity-30" disabled={i === 0}>
                      <ChevronUp className="w-3 h-3" />
                    </button>
                    <button onClick={() => moveAction(i, 1)} className="p-0.5 text-gray-400 hover:text-gray-700 disabled:opacity-30" disabled={i === (data.actions!.length - 1)}>
                      <ChevronDown className="w-3 h-3" />
                    </button>
                  </div>
                  <button onClick={() => setEditingAction(editingAction === i ? null : i)}
                    title="編輯這個動作的欄位（文字 / 變數名 / 視窗…）"
                    className={`shrink-0 p-0.5 rounded ${editingAction === i ? 'text-indigo-600 bg-indigo-50' : 'text-gray-300 hover:text-indigo-500'}`}>
                    <Pencil className="w-3 h-3" />
                  </button>
                  <button onClick={() => deleteAction(i)} className="text-gray-300 hover:text-red-500 shrink-0">
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
                </div>
              ))}
              {/* 列表最後的 ➕ 插入點 */}
              <InsertHub expanded={gapOpenAt === data.actions.length} onExpand={() => setGapOpenAt(data.actions.length)} onCollapse={() => setGapOpenAt(null)}>
              <OcrFieldInserter
                index={data.actions.length}
                isOpen={insertOpenAt === data.actions.length && insertKind === 'ocr'}
                openMenu={() => { setInsertOpenAt(data.actions.length); setInsertKind('ocr') }}
                closeMenu={() => setInsertOpenAt(null)}
                onAdd={insertActionAt}
              />
              <WaitDownloadInserter
                index={data.actions.length}
                isOpen={insertOpenAt === data.actions.length && insertKind === 'dl'}
                openMenu={() => { setInsertOpenAt(data.actions.length); setInsertKind('dl') }}
                closeMenu={() => setInsertOpenAt(null)}
                onAdd={insertActionAt}
              />
              <OcrWaitInserter
                index={data.actions.length}
                isOpen={insertOpenAt === data.actions.length && insertKind === 'ocrwait'}
                openMenu={() => { setInsertOpenAt(data.actions.length); setInsertKind('ocrwait') }}
                closeMenu={() => setInsertOpenAt(null)}
                onAdd={insertActionAt}
              />
              <ForEachInserter
                index={data.actions.length}
                isOpen={insertOpenAt === data.actions.length && insertKind === 'foreach'}
                hasActions={(data.actions || []).length > 0}
                tailCount={Math.max(0, (data.actions || []).length - (data.actions.length))}
                openMenu={() => { setInsertOpenAt(data.actions.length); setInsertKind('foreach') }}
                closeMenu={() => setInsertOpenAt(null)}
                onInsert={insertActionAt}
                onWrapAll={wrapAllIntoForEach}
                onWrapAfter={wrapAfterIntoForEach}
              />
              <ClipboardReadInserter
                index={data.actions.length}
                isOpen={insertOpenAt === data.actions.length && insertKind === 'clip'}
                openMenu={() => { setInsertOpenAt(data.actions.length); setInsertKind('clip') }}
                closeMenu={() => setInsertOpenAt(null)}
                onAdd={insertActionAt}
              />
              <WakeWindowInserter
                index={data.actions.length}
                isOpen={insertOpenAt === data.actions.length && insertKind === 'wake'}
                defaultTitle={data.uiaWindow || ''}
                openMenu={() => { setInsertOpenAt(data.actions.length); setInsertKind('wake') }}
                closeMenu={() => setInsertOpenAt(null)}
                onAdd={insertActionAt}
  onAddMany={insertActionsAt}
              />
              </InsertHub>
              <VlmCheckInserter
                index={data.actions.length}
                isOpen={insertOpenAt === data.actions.length && insertKind === 'vlm'}
                openMenu={() => { setInsertOpenAt(data.actions.length); setInsertKind('vlm') }}
                onPick={handlePickTemplate}
                onSaveCustom={handleSaveCustomFromCurrent}
                onDeleteCustom={handleDeleteCustom}
                customTemplates={customTemplates}
              />
            </div>
          )}
        </div>

        {/* Assets 目錄 */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
            錨點圖片資料夾（相對專案根或絕對路徑）
          </label>
          <input value={data.assetsDir} onChange={e => onUpdate({ assetsDir: e.target.value })}
            placeholder={defaultAssetsDir}
            className={`${inputCls} font-mono text-xs`} />
        </div>

        {/* 選項 */}
        <div className="space-y-2">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={data.failFast}
              onChange={e => onUpdate({ failFast: e.target.checked })} className="w-4 h-4 accent-purple-600" />
            <span className="text-gray-700">遇錯立即中止（fail_fast）</span>
          </label>
        </div>

        {/* CV 比對設定（可摺疊，預設收折） */}
        <div className="rounded-xl border border-gray-200 bg-gray-50/50 overflow-hidden">
          <button
            type="button"
            onClick={() => setCvOpen(v => !v)}
            className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-100/80 transition-colors"
          >
            {cvOpen ? <ChevronUp className="w-3.5 h-3.5 text-gray-400" />
                    : <ChevronDown className="w-3.5 h-3.5 text-gray-400" />}
            <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide flex-1">CV 比對設定</span>
            <span className="text-[11px] text-gray-400 font-mono">
              {(data.cvThreshold ?? 0.5)}{data.cvSearchOnlyNear ? ' · 只搜附近' : ''}{(data.cvTriggerHover ?? true) ? ` · hover ${data.cvHoverWaitMs ?? 200}ms` : ''}
            </span>
          </button>
          {cvOpen && (
            <div className="px-3 pb-3 space-y-3 border-t border-gray-200">
              <div className="pt-3" />
              {/* 比對門檻 3 段 */}
              <div>
                <label className="text-xs text-gray-600 block mb-1.5">比對門檻</label>
                <div className="grid grid-cols-3 gap-1">
                  {[
                    { v: 0.50, label: '寬鬆', hint: '容錯高，DPI / 主題色 / hover 差異容忍' },
                    { v: 0.80, label: '標準', hint: '預設 sweet spot' },
                    { v: 0.90, label: '嚴格', hint: '幾乎不誤判' },
                  ].map(opt => (
                    <button
                      key={opt.v}
                      type="button"
                      onClick={() => onUpdate({ cvThreshold: opt.v })}
                      title={opt.hint}
                      className={`px-2 py-1.5 rounded-lg text-xs font-medium transition-colors border ${
                        (data.cvThreshold ?? 0.5) === opt.v
                          ? 'bg-purple-500 text-white border-purple-500'
                          : 'bg-white text-gray-600 border-gray-200 hover:border-purple-300'
                      }`}
                    >
                      {opt.label} {opt.v}
                    </button>
                  ))}
                </div>
              </div>

              {/* 只搜附近 toggle */}
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input type="checkbox" checked={data.cvSearchOnlyNear}
                  onChange={e => onUpdate({ cvSearchOnlyNear: e.target.checked })}
                  className="w-4 h-4 accent-purple-600" />
                <span className="text-gray-700">只搜錄製座標附近</span>
              </label>
              <p className="text-[11px] text-gray-400 leading-relaxed pl-6 -mt-1">
                {data.cvSearchOnlyNear
                  ? '開啟：只在附近搜尋，不擴大到全螢幕（避免跨螢幕找錯位置）'
                  : '關閉：附近找不到 → 擴大到全螢幕 CV 搜尋'}
              </p>

              {/* CV 失敗退回座標 toggle（預設 false：失敗就停、不亂點）*/}
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input type="checkbox" checked={data.cvCoordFallback === true}
                  onChange={e => onUpdate({ cvCoordFallback: e.target.checked })}
                  className="w-4 h-4 accent-purple-600" />
                <span className="text-gray-700">CV 失敗時退回錄製座標</span>
              </label>
              <p className="text-[11px] text-gray-400 leading-relaxed pl-6 -mt-1">
                {data.cvCoordFallback === true
                  ? '開啟：CV 完全找不到 → 退回原錄製座標硬點下去（對畫面穩定的場景多一層保險）'
                  : '關閉（預設）：CV 失敗就直接 FAIL、不亂點。選擇 CV 就代表位置可能有偏差，盲點座標反而更危險'}
              </p>

              {/* 觸發 hover toggle */}
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input type="checkbox" checked={data.cvTriggerHover ?? true}
                  onChange={e => onUpdate({ cvTriggerHover: e.target.checked })}
                  className="w-4 h-4 accent-purple-600" />
                <span className="text-gray-700">比對前觸發 hover 效果</span>
              </label>
              <p className="text-[11px] text-gray-400 leading-relaxed pl-6 -mt-1">
                {(data.cvTriggerHover ?? true)
                  ? '開啟（建議）：先把游標移到錄製座標 + 等待，讓 Windows hover highlight 出現後再比對。'
                  : '關閉：跳過 hover 觸發、每次 click_image 會快一點。若錨點不含 hover 變色區域可關掉'}
              </p>

              {/* hover 等待 2 段 */}
              {(data.cvTriggerHover ?? true) && (
                <div>
                  <label className="text-xs text-gray-600 block mb-1.5">Hover 等待時間</label>
                  <div className="grid grid-cols-2 gap-1">
                    {[
                      { v: 200, label: '快', hint: '200ms，夠大多數 Windows UI' },
                      { v: 400, label: '保險', hint: '400ms，應付 fade-in 較慢的動畫或遠端桌面' },
                    ].map(opt => (
                      <button
                        key={opt.v}
                        type="button"
                        onClick={() => onUpdate({ cvHoverWaitMs: opt.v })}
                        title={opt.hint}
                        className={`px-2 py-1.5 rounded-lg text-xs font-medium transition-colors border ${
                          (data.cvHoverWaitMs ?? 200) === opt.v
                            ? 'bg-purple-500 text-white border-purple-500'
                            : 'bg-white text-gray-600 border-gray-200 hover:border-purple-300'
                        }`}
                      >
                        {opt.label} {opt.v}ms
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* 搜尋半徑 */}
              <div>
                <label className="text-xs text-gray-600 block mb-1.5">
                  附近搜尋半徑
                  <span className="text-gray-400 font-normal">
                    （實際搜尋 {(data.cvSearchRadius ?? 400) * 2}×{(data.cvSearchRadius ?? 400) * 2} px）
                  </span>
                </label>
                <input
                  type="number"
                  min={50}
                  max={2000}
                  step={50}
                  value={data.cvSearchRadius ?? 400}
                  onChange={e => {
                    const v = parseInt(e.target.value) || 400
                    onUpdate({ cvSearchRadius: Math.max(50, Math.min(2000, v)) })
                  }}
                  className={inputCls}
                />
                <p className="text-[11px] text-gray-400 mt-1">
                  視窗很少移動 → 可調小（150-200）更快更準；常跨螢幕 → 調大（600-800）
                </p>
              </div>
            </div>
          )}
        </div>

        {/* OCR 比對設定（摺疊，預設收折）*/}
        <div className="rounded-xl border border-gray-200 bg-gray-50/50 overflow-hidden">
          <button
            type="button"
            onClick={() => setOcrOpen(v => !v)}
            className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-100/80 transition-colors"
          >
            {ocrOpen ? <ChevronUp className="w-3.5 h-3.5 text-gray-400" />
                    : <ChevronDown className="w-3.5 h-3.5 text-gray-400" />}
            <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide flex-1">🔤 OCR 比對設定</span>
            <span className="text-[11px] text-gray-400 font-mono">
              門檻 {(data.ocrThreshold ?? 0.6).toFixed(2)}{data.ocrCvFallback ? ' · fallback→CV' : ''}
            </span>
          </button>
          {ocrOpen && (
            <div className="px-3 pb-3 space-y-3 border-t border-gray-200">
              <div className="pt-3" />
              {/* OCR 最小 conf 門檻 */}
              <div>
                <label className="text-xs text-gray-600 block mb-1.5">最小匹配信心</label>
                <div className="grid grid-cols-4 gap-1">
                  {[
                    { v: 0.6, label: '模糊', hint: '包含大小寫+去空白的模糊匹配（最寬）' },
                    { v: 0.8, label: '跨詞', hint: '允許 CJK 被 OCR 拆字後行層級拼接匹配' },
                    { v: 0.9, label: '詞包含', hint: '目標必須是某個 OCR word 的子字串' },
                    { v: 1.0, label: '精確', hint: 'OCR word 必須完全等於目標文字' },
                  ].map(opt => (
                    <button
                      key={opt.v}
                      type="button"
                      onClick={() => onUpdate({ ocrThreshold: opt.v })}
                      title={opt.hint}
                      className={`px-2 py-1.5 rounded-lg text-[11px] font-medium transition-colors border ${
                        (data.ocrThreshold ?? 0.6) === opt.v
                          ? 'bg-purple-500 text-white border-purple-500'
                          : 'bg-white text-gray-600 border-gray-200 hover:border-purple-300'
                      }`}
                    >
                      {opt.label} {opt.v.toFixed(1)}
                    </button>
                  ))}
                </div>
                <p className="text-[11px] text-gray-400 mt-1.5 leading-relaxed">
                  低於此 conf 視為沒找到。繁中被 OCR 拆字時，"跨詞 0.8" 才能從分字結果拼回原目標。
                </p>
              </div>

              {/* OCR 失敗時的 fallback 行為 */}
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input type="checkbox" checked={data.ocrCvFallback === true}
                  onChange={e => onUpdate({ ocrCvFallback: e.target.checked })}
                  className="w-4 h-4 accent-purple-600" />
                <span className="text-gray-700">OCR 失敗時退回 CV 比對</span>
              </label>
              <p className="text-[11px] text-gray-400 leading-relaxed pl-6 -mt-1">
                {data.ocrCvFallback === true
                  ? '開啟：OCR 找不到 → 接著跑 CV 圖像比對鏈（gray→edge），CV 再失敗時是否退座標看上方 CV 設定'
                  : '關閉（預設）：OCR 失敗就直接 FAIL，不退到 CV 或座標。選擇 OCR 代表目標位置/樣式會變、CV 不適用'}
              </p>
            </div>
          )}
        </div>

        {/* 🛡 VLM 把關設定（Phase 1 — 動作後驗 expected outcome）─────────── */}
        <div className="rounded-xl border border-emerald-200 bg-emerald-50/30 overflow-hidden">
          <button
            type="button"
            onClick={() => setVlmOpen(v => !v)}
            className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-emerald-100/50 transition-colors"
          >
            {vlmOpen ? <ChevronUp className="w-3.5 h-3.5 text-emerald-600" />
                     : <ChevronDown className="w-3.5 h-3.5 text-emerald-600" />}
            <span className="text-xs font-semibold text-emerald-700 uppercase tracking-wide flex-1">🛡 VLM 把關（每動作後驗證）</span>
            <span className="text-[11px] text-emerald-600 font-mono">
              {(data.cuVlmCheckStrategy || 'off') === 'off' ? '已關閉' :
               (data.cuVlmCheckStrategy === 'after_each' ? '每步都驗' : '只驗 critical')}
              {data.cuOnMismatch && data.cuOnMismatch !== 'stop_notify' ? ` · ${data.cuOnMismatch === 'retry_once' ? '重試' : '略過'}` : ''}
            </span>
          </button>
          {vlmOpen && (
            <div className="px-3 pb-3 space-y-3 border-t border-emerald-200">
              <div className="pt-3" />
              <p className="text-[11px] text-emerald-700 leading-relaxed">
                錄製座標準確主路徑、VLM 額外當「驗證者」。每動作後比對「動作前 / 動作後截圖」+ 你寫的 expected 描述、
                偏離立刻停 + push TG 通知。99% 失敗模式從「整套悶著錯」變「立刻發現+人介入」。
                <br />
                <span className="text-[10px] text-emerald-600">每次驗證 ~$0.005-0.015、用設定頁的主模型(必須支援 vision)。</span>
              </p>

              {/* strategy 3 選 */}
              <div>
                <label className="text-xs text-gray-600 block mb-1.5">驗證策略</label>
                <div className="grid grid-cols-3 gap-1">
                  {[
                    { v: 'off', label: '關閉', hint: '完全不驗(預設、向後相容)' },
                    { v: 'after_each', label: '每步都驗', hint: '所有有 expected 的動作都送 VLM' },
                    { v: 'critical_only', label: '只驗 critical', hint: '只驗有勾「critical」的動作' },
                  ].map(opt => (
                    <button
                      key={opt.v}
                      type="button"
                      onClick={() => onUpdate({ cuVlmCheckStrategy: opt.v as 'off' | 'after_each' | 'critical_only' })}
                      title={opt.hint}
                      className={`px-2 py-1.5 rounded-lg text-xs font-medium transition-colors border ${
                        (data.cuVlmCheckStrategy || 'off') === opt.v
                          ? 'bg-emerald-500 text-white border-emerald-500'
                          : 'bg-white text-gray-600 border-gray-200 hover:border-emerald-300'
                      }`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* on_mismatch 3 選 */}
              {(data.cuVlmCheckStrategy || 'off') !== 'off' && (
                <>
                  <div>
                    <label className="text-xs text-gray-600 block mb-1.5">偏離時行為</label>
                    <div className="grid grid-cols-3 gap-1">
                      {[
                        { v: 'stop_notify', label: '停 + 通知', hint: '立即停 pipeline、push TG 截圖、等人介入(預設、最安全)' },
                        { v: 'retry_once', label: '重試', hint: '重執行同動作 N 次、仍失敗才停' },
                        { v: 'skip_and_continue', label: '略過繼續', hint: '警告但繼續、用於非關鍵步' },
                      ].map(opt => (
                        <button
                          key={opt.v}
                          type="button"
                          onClick={() => onUpdate({ cuOnMismatch: opt.v as 'stop_notify' | 'retry_once' | 'skip_and_continue' })}
                          title={opt.hint}
                          className={`px-2 py-1.5 rounded-lg text-xs font-medium transition-colors border ${
                            (data.cuOnMismatch || 'stop_notify') === opt.v
                              ? 'bg-emerald-500 text-white border-emerald-500'
                              : 'bg-white text-gray-600 border-gray-200 hover:border-emerald-300'
                          }`}
                        >
                          {opt.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* retry_once 模式才顯示 max_retries */}
                  {data.cuOnMismatch === 'retry_once' && (
                    <div>
                      <label className="text-xs text-gray-600 block mb-1.5">重試上限</label>
                      <input type="number" min={1} max={5}
                        value={data.cuVlmMaxRetries || 1}
                        onChange={e => onUpdate({ cuVlmMaxRetries: Math.max(1, Math.min(5, parseInt(e.target.value) || 1)) })}
                        className={`${inputCls} w-24`} />
                      <p className="text-[10px] text-emerald-600 mt-1">每動作最多重試 N 次、仍失敗才走 stop_notify</p>
                    </div>
                  )}

                  <p className="text-[11px] text-emerald-700 leading-relaxed bg-emerald-100/40 px-2 py-1.5 rounded">
                    💡 在每個動作上面填「expected 預期狀態」(例「另存對話框已開」)、VLM 驗證才有用。
                    {data.cuVlmCheckStrategy === 'critical_only' && ' 此模式下還要勾上 critical 那格。'}
                  </p>
                </>
              )}
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">超時（秒）</label>
            <input type="number" value={data.timeout}
              onChange={e => onUpdate({ timeout: parseInt(e.target.value) || 300 })} className={inputCls} />
          </div>
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">重試次數</label>
            <input type="number" value={data.retry}
              onChange={e => onUpdate({ retry: parseInt(e.target.value) || 0 })} className={inputCls} />
            <p className="text-[10px] text-gray-500 mt-1 leading-relaxed">
              預設 0:桌面自動化重試會從動作 #1 重頭跑一遍、可能重複點擊、造成副作用(例如重複送單)。建議 0;確定所有動作 idempotent 才調大
            </p>
          </div>
        </div>

        <LlmRoleSelector
          value={data.llmRole || 'primary'}
          onChange={(v) => onUpdate({ llmRole: v } as any)}
        />

        <div className="p-2.5 bg-yellow-50 border border-yellow-200 rounded-lg text-[11px] text-yellow-800 leading-relaxed">
          <strong>⚠ 安全提醒</strong>:執行時滑鼠會實際操作系統。失控時<strong>連按兩次 <kbd className="px-1 py-0.5 bg-white border border-yellow-300 rounded font-mono text-[10px]">Esc</kbd>(500ms 內)</strong> 立即中止;備援機制是滑鼠甩到螢幕左上角 (0,0)。動作數上限 500。
        </div>
      </div>

      {/* 手動圈選錨點 Modal */}
      {editingAnchor !== null && data.actions && data.actions[editingAnchor] && (
        <AnchorEditorModal
          action={data.actions[editingAnchor]}
          actionIndex={editingAnchor}
          assetsDir={data.assetsDir || defaultAssetsDir}
          defaultSearchRadius={data.cvSearchRadius || 400}
          onApply={(patch) => applyAnchorPatch(editingAnchor, patch)}
          onClose={() => setEditingAnchor(null)}
        />
      )}

      {/* VLM 挑錨點 file picker Modal */}
      {pickingVlmAnchorsAt !== null && data.actions && data.actions[pickingVlmAnchorsAt] && (
        <VlmAnchorPicker
          assetsDir={data.assetsDir || defaultAssetsDir}
          initialSelected={data.actions[pickingVlmAnchorsAt].vlm_anchors || []}
          onApply={(anchors) => applyAnchorPatch(pickingVlmAnchorsAt, { vlm_anchors: anchors } as Partial<ComputerUseAction>)}
          onClose={() => setPickingVlmAnchorsAt(null)}
        />
      )}
    </div>
  )
}


// ── VLM check 插入點：➕ 按鈕 + 模板選單 ─────────────────────────────
// 6 個內建模板 + 自訂模板（localStorage）+ 「自訂…」可即時新增
interface VlmCheckInserterProps {
  index: number
  isOpen: boolean
  openMenu: () => void
  onPick: (index: number, prompt: string, label: string) => void
  onSaveCustom: (label: string, prompt: string) => void
  onDeleteCustom: (id: string) => void
  customTemplates: CustomTemplate[]
}

function VlmCheckInserter({
  index, isOpen, openMenu, onPick, onSaveCustom, onDeleteCustom, customTemplates
}: VlmCheckInserterProps) {
  const [showCustomForm, setShowCustomForm] = useState(false)
  const [customLabel, setCustomLabel] = useState('')
  const [customPrompt, setCustomPrompt] = useState('')

  const submitCustom = () => {
    onSaveCustom(customLabel, customPrompt)
    if (customLabel.trim() && customPrompt.trim()) {
      // 立即用該模板插入
      onPick(index, customPrompt.trim(), customLabel.trim())
      setShowCustomForm(false)
      setCustomLabel('')
      setCustomPrompt('')
    }
  }

  if (!isOpen) {
    return (
      <div className="flex justify-center -my-0.5">
        <button
          data-vlm-insert-trigger
          type="button"
          onClick={openMenu}
          title="在此位置插入 vlm_check 視覺判斷"
          className="opacity-30 hover:opacity-100 transition-opacity flex items-center gap-0.5 px-2 py-0.5 rounded-full text-[10px] text-purple-600 hover:bg-purple-100 hover:text-purple-700 border border-transparent hover:border-purple-300"
        >
          <Plus className="w-2.5 h-2.5" /> vlm_check
        </button>
      </div>
    )
  }

  return (
    <div data-vlm-insert-popover
      className="border border-purple-300 bg-white rounded-lg shadow-lg p-2 my-1 space-y-1.5">
      <div className="flex items-center justify-between mb-0.5">
        <span className="text-[11px] font-semibold text-purple-700 flex items-center gap-1">
          <Eye className="w-3 h-3" /> 插入視覺判斷（vlm_check）
        </span>
        <span className="text-[10px] text-gray-400">在 #{index + 1} 之前</span>
      </div>
      {/* 內建 6 個模板 */}
      <div className="space-y-0.5">
        {VLM_CHECK_BUILTIN_TEMPLATES.map(t => (
          <button
            key={t.id}
            type="button"
            onClick={() => onPick(index, t.prompt, t.label)}
            className="w-full text-left px-2 py-1 rounded hover:bg-purple-50 border border-transparent hover:border-purple-200"
          >
            <div className="text-[11px] font-medium text-gray-700">{t.label}</div>
            <div className="text-[10px] text-gray-500 truncate">{t.prompt}</div>
          </button>
        ))}
      </div>
      {customTemplates.length > 0 && (
        <>
          <div className="text-[10px] text-gray-400 uppercase tracking-wide pt-1 border-t border-gray-100">自訂模板</div>
          <div className="space-y-0.5">
            {customTemplates.map(t => (
              <div key={t.id} className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => onPick(index, t.prompt, t.label)}
                  className="flex-1 text-left px-2 py-1 rounded hover:bg-purple-50 border border-transparent hover:border-purple-200"
                >
                  <div className="text-[11px] font-medium text-gray-700">{t.label}</div>
                  <div className="text-[10px] text-gray-500 truncate">{t.prompt}</div>
                </button>
                <button
                  type="button"
                  onClick={() => onDeleteCustom(t.id)}
                  title="刪除這個自訂模板"
                  className="text-gray-300 hover:text-red-500 px-1"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
            ))}
          </div>
        </>
      )}
      {!showCustomForm ? (
        <div className="flex items-center gap-2 pt-1 border-t border-gray-100">
          <button
            type="button"
            onClick={() => setShowCustomForm(true)}
            className="flex-1 text-[10px] text-purple-600 hover:text-purple-800 px-2 py-1 rounded border border-dashed border-purple-300 hover:bg-purple-50"
          >
            ➕ 新增自訂模板
          </button>
          <button
            type="button"
            onClick={() => onPick(index, '', '空白模板')}
            title="插入空白 vlm_check（在動作面板內手填判斷條件）"
            className="text-[10px] text-gray-500 hover:text-gray-800 px-2 py-1 rounded border border-gray-200 hover:bg-gray-50"
          >
            空白
          </button>
        </div>
      ) : (
        <div className="space-y-1 pt-1 border-t border-gray-100">
          <input
            value={customLabel}
            onChange={e => setCustomLabel(e.target.value)}
            placeholder="模板名稱（例：表單送出成功）"
            className="w-full text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-purple-500"
          />
          <textarea
            value={customPrompt}
            onChange={e => setCustomPrompt(e.target.value)}
            placeholder="判斷條件（給 VLM 看的提示）"
            rows={2}
            className="w-full text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-purple-500 resize-y font-mono"
          />
          <div className="flex gap-1">
            <button type="button" onClick={submitCustom}
              className="flex-1 text-[10px] bg-purple-500 text-white px-2 py-1 rounded hover:bg-purple-600">儲存並插入</button>
            <button type="button" onClick={() => { setShowCustomForm(false); setCustomLabel(''); setCustomPrompt('') }}
              className="text-[10px] text-gray-500 hover:text-gray-800 px-2 py-1 rounded border border-gray-200">取消</button>
          </div>
        </div>
      )}
    </div>
  )
}


/**
 * 注音鍵盤按鍵序列的特徵。錄製器掛鍵盤鉤子只看得到**原始按鍵**；
 * 使用者打中文時輸入法組字後才把字送進應用程式，錄到的是「ji3g4go6」
 * 這種按鍵串（= ㄨㄛˇ…的鍵位）。回放會把這串英數原樣貼進欄位 —— 必錯。
 * 特徵：全小寫英數標點、且聲調鍵(3/4/6/7)緊跟在字母後（如 i3、g4、o6）。
 * 可能誤判含數字的英文（如 test3）—— 警告是提示不是攔截，可接受。
 */
function looksLikeImeKeys(s: string): boolean {
  if (!s || !/^[a-z0-9;,./\- ]+$/.test(s)) return false
  return /[a-z;,./][3467]/.test(s)
}

// ── 行內動作編輯器 ──────────────────────────────────────────
// 使用者反饋：設定好的步驟原本只能刪掉重加 —— 改個變數名 / 文字 / 視窗
// 都得重選控制項重來。這裡讓常用欄位就地改；控制項本身（挑哪個元素）
// 還是回 Inspector 重選，那個沒辦法用打字改。
function InlineActionEditor({ action, workflowId, stepName, onPatch, onClose }: {
  action: ComputerUseAction
  workflowId?: string
  stepName?: string
  onPatch: (patch: Partial<ComputerUseAction>) => void
  onClose: () => void
}) {
  const t = action.type
  const rows: React.ReactNode[] = []
  const input = (label: string, key: keyof ComputerUseAction, placeholder = '', mono = true) => (
    <div key={String(key)} className="flex items-center gap-1.5">
      <span className="text-[10px] text-gray-500 w-14 shrink-0 text-right">{label}</span>
      <input
        value={String((action as any)[key] ?? '')}
        onChange={e => onPatch({ [key]: e.target.value } as any)}
        placeholder={placeholder}
        className={`flex-1 min-w-0 border border-indigo-200 rounded px-2 py-1 text-xs ${mono ? 'font-mono' : ''}`}
      />
    </div>
  )

  if (t === 'uia_get_text' || t === 'uia_get_table_rowcount') {
    rows.push(input('變數名', 'save_as', '例：總計金額'))
    rows.push(input('視窗', 'window', '例：*BK簽呈*（留空＝用節點視窗）'))
  } else if (t === 'activate_window') {
    rows.push(input('標題關鍵字', 'title_contains' as any, '例：E-Quote測試靶'))
  } else if (t === 'uia_get_clipboard') {
    rows.push(input('存到變數', 'save_as', '例：清單原文'))
  } else if (t === 'for_each') {
    rows.push(
      <div key="items" className="space-y-0.5">
        <span className="text-[10px] text-gray-500">{'清單（一行一筆；也可 {{變數}}）'}</span>
        <textarea
          value={Array.isArray(action.items) ? action.items.join('\n') : String(action.items ?? '')}
          onChange={e => onPatch({ items: e.target.value } as any)}
          rows={5}
          placeholder={'UX3407%\nRC71L%\nGU605%'}
          className="w-full border border-indigo-200 rounded px-2 py-1 text-xs font-mono resize-y"
        />
      </div>,
    )
    rows.push(input('每輪存到變數', 'save_as', '例：品規'))
    rows.push(input('拆成多變數(|分隔)', 'split_as' as any, '例：查詢年|查詢月(選填)'))
    rows.push(input('欄位分隔符', 'split_sep' as any, '-'))
  } else if (t === 'wait_text') {
    rows.push(input('文字', 'text', '例：資料處理中'))
    rows.push(input('條件(until)', 'until', 'appear / disappear'))
    rows.push(input('逾時秒數', 'timeout_sec', '60'))
  } else if (t === 'if_text_found') {
    rows.push(input('文字', 'text', '例：查無資料'))
    rows.push(input('探測秒數', 'timeout_sec', '3'))
  } else if (t === 'if_element_found') {
    rows.push(input('探測秒數', 'timeout_sec', '3'))
    rows.push(input('視窗', 'window', '例：*SCM Portal*'))
  } else if (t === 'wait_download') {
    rows.push(input('檔名樣式', 'pattern', '例：PP_Component*.xlsx'))
    rows.push(input('資料夾(空=Downloads)', 'dir', ''))
    rows.push(input('逾時秒數', 'timeout_sec', '300'))
    rows.push(input('存路徑到變數', 'save_as', '例：下載檔'))
  } else if (t === 'uia_click') {
    rows.push(input('點擊方式(留空=pattern優先)', 'click_method' as any, "mouse = 強制滑鼠點(Tk 匿名鈕用)"))
  } else if (t === 'uia_wait') {
    rows.push(input('條件(until)', 'until', 'appear / disappear / text_contains / text_equals'))
    rows.push(input('關鍵字(text_*用)', 'text', '例：已匯出'))
    rows.push(input('逾時秒數', 'timeout_sec', '60'))
  } else if (t === 'uia_select') {
    rows.push(input('選項文字', 'text', '例：08 或 {{ now.month }}'))
    rows.push(input('視窗', 'window', '例：*E-Quote*'))
  } else if (t === 'uia_send_keys') {
    rows.push(input('填入文字', 'text', '例：{{總計金額}}'))
    rows.push(input('視窗', 'window', '例：*OCR取值測試靶*'))
  } else if (t === 'uia_set_clipboard' || t === 'type_text') {
    rows.push(input('文字', 'text', '可含 {{變數}}'))
  } else if (t === 'wait') {
    rows.push(input('秒數', 'seconds', '例：2'))
  } else if (t === 'ocr_get_text') {
    rows.push(input('標籤', 'label', '例：總計金額'))
    rows.push(input('變數名', 'save_as', '例：金額'))
    rows.push(
      <div key="dir" className="flex items-center gap-1.5">
        <span className="text-[10px] text-gray-500 w-14 shrink-0 text-right">方向</span>
        <select value={action.direction || 'right'}
          onChange={e => onPatch({ direction: e.target.value as any })}
          className="border border-indigo-200 rounded px-1.5 py-1 text-xs">
          <option value="right">右側</option>
          <option value="below">下方</option>
        </select>
        <span className="text-[10px] text-gray-500 shrink-0">格式</span>
        <select value={action.kind || 'amount'}
          onChange={e => onPatch({ kind: e.target.value as any })}
          className="border border-indigo-200 rounded px-1.5 py-1 text-xs">
          <option value="amount">金額</option>
          <option value="ident">單號</option>
          <option value="taxid">統編</option>
          <option value="any">任意</option>
        </select>
      </div>,
    )
  } else if (t === 'assert_text' || t === 'wait_image') {
    if ('ocr_text' in action) rows.push(input('目標文字', 'ocr_text' as any, ''))
    if (t === 'wait_image') rows.push(input('條件(until)', 'until' as any, 'appear / disappear(等消失)'))
  }

  // uia 動作的目標元素(control)欄位 —— 之前只能「刪掉重選」,萬用字元
  // name(資料處理中*)這種微調逼人重抓一次元素,直接開放改。
  if ((t.startsWith('uia_') || t === 'if_element_found') && action.control && t !== 'uia_get_clipboard') {
    const oldName = String((action.control as Record<string, unknown>)?.name ?? '')
    // 分歧的子動作是從同一個元素生出來的(點它 / 等它消失),改名要一起跟,
    // 否則探測用 資料處理中* 命中、子動作卻還在等舊的精確名稱。
    const renameBranch = (list: unknown, name: string) => Array.isArray(list)
      ? list.map((s: any) => (s?.control && String(s.control.name ?? '') === oldName)
          ? { ...s, control: { ...s.control, name } } : s)
      : list
    rows.push(
      <div key="ctl-name" className="flex items-center gap-1.5">
        <span className="text-[10px] text-gray-500 w-14 shrink-0 text-right">目標 name</span>
        <input
          value={oldName}
          onChange={e => onPatch({
            control: { ...(action.control || {}), name: e.target.value },
            ...(t === 'if_element_found'
              ? { then: renameBranch(action.then, e.target.value), else: renameBranch((action as any).else, e.target.value) }
              : {}),
          } as any)}
          placeholder="支援 * 萬用字元，例：資料處理中*"
          className="flex-1 min-w-0 border border-indigo-200 rounded px-2 py-1 text-xs font-mono"
        />
      </div>,
      <div key="ctl-aid" className="flex items-center gap-1.5">
        <span className="text-[10px] text-gray-500 w-14 shrink-0 text-right">目標 id</span>
        <input
          value={String((action.control as Record<string, unknown>)?.auto_id ?? '')}
          onChange={e => onPatch({ control: { ...(action.control || {}), auto_id: e.target.value } } as any)}
          placeholder="auto_id（沒有就留空）"
          className="flex-1 min-w-0 border border-indigo-200 rounded px-2 py-1 text-xs font-mono"
        />
      </div>,
    )
  }

  return (
    <div className="mt-1.5 p-2 rounded-lg border border-indigo-200 bg-indigo-50/40 space-y-1.5">
      {rows.length > 0 ? rows : (
        <p className="text-[10px] text-gray-500">
          這個動作沒有可打字修改的欄位 —— 要換目標控制項請刪掉後回 Inspector 重選。
        </p>
      )}
      <div className="flex justify-end">
        <button onClick={onClose}
          className="px-2 py-0.5 rounded text-[11px] bg-indigo-600 text-white hover:bg-indigo-700 whitespace-nowrap">
          完成
        </button>
      </div>
    </div>
  )
}
