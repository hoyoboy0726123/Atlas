'use client'
import { useEffect, useRef, useState } from 'react'
import { X, AlertTriangle, Loader2, Check, Mail } from 'lucide-react'
import { toast } from 'sonner'
import { testOutlookConnection } from '@/lib/api'
import type { OutlookData, OutlookNode } from './_helpers'
import { VariableInput } from './_variablePicker'
import LlmRoleSelector from './_llmRoleSelector'

// 後端 outlook_templates._to_dt 認得的相對日期關鍵字。
// 這些值執行當下才換算，是「每天撈當天信」的正解（勝過 {{ input.date }}：
// 後者沒帶啟動參數時會變空字串、日期過濾整個失效）。
const REL_DATE_KEYWORDS = [
  'today', '今天', '本日', '當日', '今日',
  'yesterday', '昨天', '昨日',
  'tomorrow', '明天', '明日',
]
const isRelDate = (v: string) =>
  REL_DATE_KEYWORDS.includes((v || '').trim()) ||
  REL_DATE_KEYWORDS.includes((v || '').trim().toLowerCase())

// 帶「確定」按鈕的日期/時間欄位：onChange 只寫 draft，按確定才 commit 到 params
// 避免使用者在 picker 裡選一半就被當前值覆蓋（用戶反映需要明確確認）
//
// ⚠️ 為什麼要特別處理相對日期：<input type="datetime-local"> 只吃
// `YYYY-MM-DDTHH:mm`，塞 "today" 進去是非法值 → 瀏覽器直接渲染成空白。
// 結果 YAML 明明寫了 since:"today"、面板卻一片空白，使用者以為沒設定，
// 一旦手動補一個日期就把 "today" 覆蓋掉、每日排程就此失效。
// 所以關鍵字要用文字模式顯示，並提供雙向切換。
function DateTimeField({ value, type, onCommit }: {
  value: string
  type: 'date' | 'datetime-local'
  onCommit: (v: string) => void
}) {
  const [draft, setDraft] = useState(value || '')
  // 外部值變動（譬如切模板後重置）時同步 draft
  useEffect(() => { setDraft(value || '') }, [value])
  const dirty = draft !== (value || '')

  // ── 關鍵字模式：picker 顯示不了，改用唯讀文字 + 切回日期的按鈕 ──
  if (isRelDate(value)) {
    return (
      <div className="flex gap-1.5 items-center">
        <div className="flex-1 border border-sky-300 bg-sky-50 rounded-lg px-2.5 py-1.5 text-sm flex items-center gap-2 min-w-0">
          <span className="font-mono text-sky-700 shrink-0">{value}</span>
          <span className="text-[11px] text-sky-600/80 truncate">相對日期 · 每次執行換算成當下日期</span>
        </div>
        <button
          type="button"
          onClick={() => onCommit('')}
          title="清掉關鍵字、改用日期選擇器指定固定日期"
          className="shrink-0 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-gray-100 hover:bg-gray-200 text-gray-600 transition-colors"
        >
          改用日期
        </button>
      </div>
    )
  }

  // ── 日期模式：原本的 picker，另加常用關鍵字快捷鍵 ──
  return (
    <div className="space-y-1">
      <div className="flex gap-1.5 items-center">
        <input
          className="flex-1 border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-sky-400 focus:ring-1 focus:ring-sky-400/20 bg-white"
          type={type}
          value={draft}
          onChange={e => setDraft(e.target.value)}
        />
        <button
          type="button"
          onClick={() => onCommit(draft)}
          disabled={!dirty}
          title={dirty ? '套用此日期' : '已套用'}
          className={`shrink-0 px-2.5 py-1.5 rounded-lg text-xs font-medium flex items-center gap-1 transition-colors ${
            dirty
              ? 'bg-sky-500 hover:bg-sky-600 text-white'
              : 'bg-gray-100 text-gray-400 cursor-not-allowed'
          }`}
        >
          <Check className="w-3.5 h-3.5" />
          {dirty ? '確定' : '已套用'}
        </button>
      </div>
      <div className="flex gap-1 items-center">
        <span className="text-[11px] text-gray-400">每天自動跑選這個：</span>
        {['today', 'yesterday'].map(kw => (
          <button
            key={kw}
            type="button"
            onClick={() => onCommit(kw)}
            title={`填入 ${kw} —— 執行當下才換算，排程每天都會是當天`}
            className="px-1.5 py-0.5 rounded text-[11px] font-mono border border-sky-200 text-sky-600 hover:bg-sky-50 transition-colors"
          >
            {kw}
          </button>
        ))}
      </div>
    </div>
  )
}

const COMPATIBILITY_DISMISS_KEY = 'outlook-compat-warning-dismissed-v1'

const NODE_COLOR = '#0078d4'

// ── 模板定義（顯示給使用者選的選單） ─────────────────────────────────
// 後端 agent 會看 template ID + params 組 prompt；前端只負責收參數。
type ParamSpec = {
  key: string
  label: string
  type: 'text' | 'textarea' | 'date' | 'datetime-local' | 'number' | 'select' | 'bool'
  placeholder?: string
  options?: { value: string; label: string }[]
  hint?: string
}

type Template = {
  id: string
  label: string
  category: 'inbox' | 'send' | 'attach' | 'manage'
  description: string
  params: ParamSpec[]
  // execMode: 'direct' = 後端直接 call wrapper、不進 LLM（快、零 token、可預測）
  //          'llm'    = 進 LLM agent loop（需要摘要 / 分析時才用）
  execMode: 'direct' | 'llm'
}

const TEMPLATES: Template[] = [
  // A. 每日整理 / 摘要
  {
    id: 'daily_todo',
    label: '🗒 整理符合條件信件 → 待辦清單',
    category: 'inbox',
    execMode: 'direct',
    description: '掃指定資料夾的信，按條件過濾，結果整理成 markdown / xlsx 待辦清單',
    params: [
      { key: 'folder', label: '資料夾', type: 'text', placeholder: 'inbox / 收件匣 / 帳號@公司.com/收件匣', hint: '預設 inbox(第一個信箱)。多帳號:填「信箱名/收件匣」,例 ABC@company.com/收件匣' },
      { key: 'subject', label: '主旨關鍵字（多個用逗號）', type: 'text', placeholder: '報告, urgent' },
      { key: 'sender', label: '寄件人（多個用逗號）', type: 'text', placeholder: 'boss@x.com' },
      { key: 'exact_match', label: '完全相等比對（不勾 = 模糊比對）', type: 'bool' },
      { key: 'since', label: '從', type: 'datetime-local' },
      { key: 'until', label: '到', type: 'datetime-local' },
      { key: 'unread_only', label: '只取未讀', type: 'bool' },
      { key: 'include_body', label: '包含信件內文（下游要分析內文時必勾）', type: 'bool' },
      { key: 'output_format', label: '輸出格式', type: 'select',
        options: [{ value: 'md', label: 'Markdown' }, { value: 'xlsx', label: 'Excel' }, { value: 'txt', label: '純文字' }, { value: 'json', label: 'JSON（下游程式解析用）' }] },
    ],
  },
  {
    id: 'search_summary',
    label: '🔍 指定關鍵字撈相關信件 → 摘要報告',
    category: 'inbox',
    execMode: 'llm',
    description: '用 LLM 摘要符合條件的信件群、產出報告',
    params: [
      { key: 'keywords', label: '關鍵字（多個用逗號 = OR 邏輯）', type: 'text', placeholder: '客戶投訴, 退費' },
      { key: 'search_in', label: '搜尋範圍', type: 'select',
        options: [{ value: 'subject', label: '主旨' }, { value: 'body', label: '本文' }, { value: 'both', label: '主旨+本文' }] },
      { key: 'folder', label: '資料夾', type: 'text', placeholder: 'inbox / 收件匣 / 帳號@公司.com/收件匣', hint: '預設 inbox(第一個信箱)。多帳號:填「信箱名/收件匣」,例 ABC@company.com/收件匣' },
      { key: 'since', label: '從', type: 'datetime-local' },
      { key: 'until', label: '到', type: 'datetime-local' },
      { key: 'detail_level', label: '報告詳細度', type: 'select',
        options: [{ value: 'brief', label: '簡' }, { value: 'medium', label: '中' }, { value: 'detail', label: '詳' }] },
      { key: 'output_format', label: '輸出格式', type: 'select',
        options: [{ value: 'md', label: 'Markdown' }, { value: 'docx', label: 'Word' }, { value: 'pdf', label: 'PDF' }] },
    ],
  },
  {
    id: 'unanswered',
    label: '❓ 未回覆超過 N 天的信',
    category: 'inbox',
    execMode: 'llm',
    description: '找出收件匣中我還沒回過、且收件超過指定天數的信',
    params: [
      { key: 'days', label: '超過幾天未回', type: 'number', placeholder: '3' },
      { key: 'sender_filter', label: '寄件人過濾（可選）', type: 'text', placeholder: '只看特定人' },
    ],
  },
  // B. 寄信
  {
    id: 'send_mail',
    label: '✉ 寄信給指定收件人',
    category: 'send',
    execMode: 'direct',
    description: '直接寄一封信',
    params: [
      { key: 'to', label: 'To（多個用逗號）', type: 'text', placeholder: 'a@x.com, b@x.com' },
      { key: 'cc', label: 'CC（可選）', type: 'text' },
      { key: 'bcc', label: 'BCC（可選）', type: 'text' },
      { key: 'subject', label: '主旨', type: 'text' },
      { key: 'body', label: '本文（HTML / 純文字）', type: 'textarea' },
      { key: 'body_format', label: '本文格式', type: 'select',
        options: [{ value: 'html', label: 'HTML' }, { value: 'text', label: '純文字' }] },
      { key: 'attachments', label: '附件路徑（多個用換行）', type: 'textarea',
        hint: '可填 {prev_output} 接前一步驟輸出檔' },
      { key: 'save_to_drafts', label: '只存草稿不送出', type: 'bool' },
    ],
  },
  {
    id: 'send_with_attachment',
    label: '📤 把上一步輸出（或指定檔案）當附件寄出',
    category: 'send',
    execMode: 'direct',
    description: '常用情境：前一步整理產出 xlsx → 直接寄給主管。也可以填路徑寄任何檔案',
    params: [
      { key: 'to', label: 'To', type: 'text' },
      { key: 'subject', label: '主旨', type: 'text' },
      { key: 'body', label: '本文（簡短說明）', type: 'textarea' },
      { key: 'attachment_path', label: '附件路徑（留空 = 上一步輸出）', type: 'text',
        placeholder: '留空自動用上一步；或填路徑（支援 {prev_output}）',
        hint: '相對路徑以專案根為基準；可填 {prev_output} 接前一步輸出檔' },
    ],
  },
  {
    id: 'bulk_send',
    label: '📨 從 csv/xlsx 收件清單群發',
    category: 'send',
    execMode: 'direct',
    description: '收件清單一筆一封，主旨/本文可帶 {欄位名} 變數',
    params: [
      { key: 'recipient_file', label: '收件清單檔', type: 'text', placeholder: 'recipients.csv' },
      { key: 'subject_template', label: '主旨範本', type: 'text', placeholder: 'Hi {name}, 請查收' },
      { key: 'body_template', label: '本文範本', type: 'textarea' },
    ],
  },
  // C. 附件
  {
    id: 'download_attachments',
    label: '📎 批次下載符合條件信件的附件',
    category: 'attach',
    execMode: 'direct',
    description: '把搜到的信件附件全部存到資料夾，可自訂檔名規則',
    params: [
      { key: 'subject', label: '主旨關鍵字', type: 'text' },
      { key: 'sender', label: '寄件人', type: 'text' },
      { key: 'since', label: '從', type: 'datetime-local' },
      { key: 'until', label: '到', type: 'datetime-local' },
      { key: 'out_dir', label: '目標資料夾', type: 'text', placeholder: 'D:/downloads/...' },
      { key: 'extensions', label: '檔案類型（留空=全部）', type: 'text',
        placeholder: 'pdf, xlsx, zip',
        hint: '逗號分隔副檔名（可帶或不帶 .，不分大小寫）；留空抓所有附件' },
      { key: 'name_template', label: '檔名範本', type: 'text',
        placeholder: '{date}_{sender}_{filename}',
        hint: '可用變數：{date} {sender} {subject} {filename}' },
    ],
  },
  // E. 信件管理（批次 move / mark / flag）
  {
    id: 'bulk_move',
    label: '📂 批次搬信到指定資料夾',
    category: 'manage',
    execMode: 'direct',
    description: '搜出符合條件的信件、批次搬到目標資料夾',
    params: [
      { key: 'folder', label: '來源資料夾', type: 'text', placeholder: 'inbox / 收件匣 / 帳號@公司.com/收件匣', hint: '預設 inbox(第一個信箱)。多帳號:填「信箱名/收件匣」,例 ABC@company.com/收件匣' },
      { key: 'subject', label: '主旨關鍵字（可選）', type: 'text' },
      { key: 'sender', label: '寄件人（可選）', type: 'text' },
      { key: 'since', label: '從（可選）', type: 'datetime-local' },
      { key: 'until', label: '到（可選）', type: 'datetime-local' },
      { key: 'target_folder', label: '目標資料夾', type: 'text',
        placeholder: 'Inbox/Projects/2026',
        hint: '可寫別名（inbox / 收件匣）或路徑（Inbox/Projects/2026）' },
      { key: 'limit', label: '最多處理幾封', type: 'number', placeholder: '500' },
    ],
  },
  {
    id: 'bulk_mark_read',
    label: '✅ 批次標已讀／未讀',
    category: 'manage',
    execMode: 'direct',
    description: '搜出符合條件的信件、批次設為已讀或未讀',
    params: [
      { key: 'folder', label: '資料夾', type: 'text', placeholder: 'inbox / 收件匣 / 帳號@公司.com/收件匣', hint: '預設 inbox(第一個信箱)。多帳號:填「信箱名/收件匣」,例 ABC@company.com/收件匣' },
      { key: 'subject', label: '主旨關鍵字（可選）', type: 'text' },
      { key: 'sender', label: '寄件人（可選）', type: 'text' },
      { key: 'since', label: '從（可選）', type: 'datetime-local' },
      { key: 'until', label: '到（可選）', type: 'datetime-local' },
      { key: 'state', label: '目標狀態', type: 'select',
        options: [{ value: 'read', label: '標已讀' }, { value: 'unread', label: '標未讀' }] },
      { key: 'limit', label: '最多處理幾封', type: 'number', placeholder: '500' },
    ],
  },
  {
    id: 'bulk_set_flag',
    label: '🚩 批次設旗標 / 標完成 / 清除',
    category: 'manage',
    execMode: 'direct',
    description: '搜出符合條件的信件、批次加追蹤旗標、標完成或清除',
    params: [
      { key: 'folder', label: '資料夾', type: 'text', placeholder: 'inbox / 收件匣 / 帳號@公司.com/收件匣', hint: '預設 inbox(第一個信箱)。多帳號:填「信箱名/收件匣」,例 ABC@company.com/收件匣' },
      { key: 'subject', label: '主旨關鍵字（可選）', type: 'text' },
      { key: 'sender', label: '寄件人（可選）', type: 'text' },
      { key: 'since', label: '從（可選）', type: 'datetime-local' },
      { key: 'until', label: '到（可選）', type: 'datetime-local' },
      { key: 'flag', label: '旗標', type: 'select',
        options: [
          { value: 'follow_up', label: '🚩 追蹤（紅旗）' },
          { value: 'complete', label: '✓ 已完成' },
          { value: 'clear', label: '清除旗標' },
        ] },
      { key: 'limit', label: '最多處理幾封', type: 'number', placeholder: '500' },
    ],
  },
]

const CATEGORY_LABEL: Record<Template['category'], string> = {
  inbox: '📥 收信整理',
  send: '📤 寄信',
  attach: '📎 附件',
  manage: '🗂 信件管理',
}

interface Props {
  node: OutlookNode
  pipelineName: string
  onUpdate: (data: Partial<OutlookData>) => void
  onClose: () => void
  onDelete: () => void
  workflowId?: string
}

export default function OutlookPanel({ node, onUpdate, onClose, onDelete, workflowId }: Props) {
  const data = node.data
  const inputCls = 'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 bg-white'

  // ── UX：點選模板後自動把該模板按鈕捲到面板頂端，下方完整呈現參數欄位 ────
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const templateBtnRefs = useRef<Map<string, HTMLButtonElement>>(new Map())
  useEffect(() => {
    if (!data.template) return
    const btn = templateBtnRefs.current.get(data.template)
    const container = scrollContainerRef.current
    if (!btn || !container) return
    // 等 expand 區塊渲染完成再算位移，避免捲完又被新 layout 推開
    const id = window.setTimeout(() => {
      const containerTop = container.getBoundingClientRect().top
      const btnTop = btn.getBoundingClientRect().top
      const delta = btnTop - containerTop
      container.scrollBy({ top: delta, behavior: 'smooth' })
    }, 60)
    return () => window.clearTimeout(id)
  }, [data.template])

  // ── UX：跨模板殘留偵測 ─────────────────────────────────────────────
  // 切到新模板時不立即清舊 params/freeText，等使用者點到新模板輸入欄位才彈確認
  // 使用者已經辛苦填過內容、不該被默默清掉；但若選新模板後完全沒互動就走人，
  // 下次重開 panel 也是乾淨狀態（component 重建、staleFrom 自然 reset）
  const [staleFrom, setStaleFrom] = useState<{ id: string; label: string } | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const handleParamInteract = () => {
    if (staleFrom) setConfirmOpen(true)
  }
  const onConfirmClear = () => {
    onUpdate({ params: {}, freeText: '' })
    setStaleFrom(null)
    setConfirmOpen(false)
  }
  const onConfirmKeep = () => {
    setStaleFrom(null)  // 不再彈
    setConfirmOpen(false)
  }

  // 相容性警告：只支援傳統 Outlook（New Outlook for Windows / Web 不支援 COM）
  // 使用者讀過、按「我知道了」後存到 localStorage 不再顯示，但摘要列永遠保留
  const [warnDismissed, setWarnDismissed] = useState(() => {
    if (typeof window === 'undefined') return false
    return localStorage.getItem(COMPATIBILITY_DISMISS_KEY) === '1'
  })
  const dismissWarn = () => {
    try { localStorage.setItem(COMPATIBILITY_DISMISS_KEY, '1') } catch {/* ignore */}
    setWarnDismissed(true)
  }

  // 連線測試 — 直接打 backend /outlook/test-connection 看 inbox 有幾封
  const [testing, setTesting] = useState(false)
  const runConnectionTest = async () => {
    if (testing) return
    setTesting(true)
    try {
      const res = await testOutlookConnection()
      if (res.ok && res.inbox_count && res.inbox_count > 0) {
        toast.success(res.diagnosis, { duration: 8000 })
      } else if (res.ok) {
        // COM 通了但 inbox = 0
        toast.warning(res.diagnosis, { duration: 12000 })
      } else {
        toast.error(res.diagnosis + (res.error ? `\n\n錯誤：${res.error}` : ''), { duration: 12000 })
      }
    } catch (e) {
      toast.error(`測試失敗：${(e as Error).message}`, { duration: 8000 })
    } finally {
      setTesting(false)
    }
  }

  const setParam = (key: string, value: unknown) => {
    onUpdate({ params: { ...data.params, [key]: value } })
  }

  // 點選模板：保留舊 params/freeText，等使用者點到輸入欄位才彈確認是否清除
  // （直接清會誤刪辛苦填的內容；改成延後確認讓使用者有反悔機會）
  const selectTemplate = (newId: string) => {
    const oldId = data.template
    if (oldId && oldId !== newId) {
      const oldTpl = TEMPLATES.find(t => t.id === oldId)
      const hasResidual = !!data.freeText || Object.values(data.params || {}).some(
        v => v !== '' && v !== false && v !== null && v !== undefined
            && !(Array.isArray(v) && v.length === 0)
      )
      if (oldTpl && hasResidual) {
        setStaleFrom({ id: oldId, label: oldTpl.label })
      } else {
        setStaleFrom(null)
      }
    } else if (!newId) {
      setStaleFrom(null)
    }
    onUpdate({ template: newId })
  }

  const clearTemplate = () => {
    onUpdate({ template: '', params: {}, freeText: '' })
    setStaleFrom(null)
  }

  const renderParam = (p: ParamSpec) => {
    const v = data.params?.[p.key]
    // textarea / text 走 VariableInput(內建 chip + 變數按鈕)
    // date / select / bool / number 維持原生控制項
    if (p.type === 'textarea') {
      return (
        <VariableInput
          value={(v as string) || ''}
          onChange={(val) => setParam(p.key, val)}
          workflowId={workflowId}
          multiline
          rows={6}
          placeholder={p.placeholder}
          showHint={false}
        />
      )
    }
    if (p.type === 'select' && p.options) {
      return (
        <select className={inputCls} value={(v as string) || ''} onChange={e => setParam(p.key, e.target.value)}>
          <option value="">（未選）</option>
          {p.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      )
    }
    if (p.type === 'bool') {
      return (
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={!!v} onChange={e => setParam(p.key, e.target.checked)} />
          <span>{p.label}</span>
        </label>
      )
    }
    if (p.type === 'date' || p.type === 'datetime-local') {
      return (
        <DateTimeField
          value={(v as string) || ''}
          type={p.type}
          onCommit={(val) => setParam(p.key, val)}
        />
      )
    }
    // text 類:走 chip(可插變數);number 維持原生 input
    if (p.type === 'number') {
      return (
        <input
          className={inputCls}
          type="number"
          placeholder={p.placeholder}
          value={(v as number) ?? ''}
          onChange={e => {
            const raw = e.target.value
            setParam(p.key, raw === '' ? '' : Number(raw))
          }}
        />
      )
    }
    return (
      <VariableInput
        value={(v as string) || ''}
        onChange={(val) => setParam(p.key, val)}
        workflowId={workflowId}
        multiline
        rows={1}
        placeholder={p.placeholder}
        showHint={false}
      />
    )
  }

  return (
    <div className="absolute top-0 right-0 h-full w-[420px] bg-white shadow-2xl border-l border-gray-100 flex flex-col z-30 overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b" style={{ borderTopColor: NODE_COLOR, borderTopWidth: 3 }}>
        <span className="w-8 h-8 rounded-full flex items-center justify-center text-white shrink-0"
          style={{ background: NODE_COLOR }}><Mail className="w-4 h-4" strokeWidth={2.4} /></span>
        <div className="flex-1 min-w-0">
          <span className="font-semibold text-gray-800 text-sm block truncate">Outlook 自動化節點</span>
          <span className="text-xs text-gray-400">透過 pywin32 + Outlook COM；只在 Windows host 跑</span>
        </div>
        <button onClick={onDelete} title="刪除" className="text-gray-300 hover:text-red-400 transition-colors p-1">🗑</button>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors"><X className="w-4 h-4" /></button>
      </div>

      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* ── 相容性警告：只支援 Classic Outlook ────────────────────── */}
        {!warnDismissed ? (
          <div className="p-3 rounded-lg border border-amber-300 bg-amber-50 space-y-2">
            <div className="flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <p className="text-xs font-semibold text-amber-900">⚠ 開始前請先確認 Outlook 版本</p>
                <p className="text-[11px] text-amber-800 mt-1 leading-relaxed">
                  本節點透過 <code className="font-mono bg-amber-100 px-1 rounded">pywin32 + Outlook COM</code> 操作，
                  <b>只支援傳統 Outlook（Classic Outlook）</b>，不支援新版 Outlook for Windows、Outlook 網頁版、Outlook 行動版。
                </p>
                <p className="text-[11px] text-amber-900 mt-1.5 leading-relaxed bg-amber-100/60 px-2 py-1 rounded">
                  💡 若你用的是新版 Outlook：點上方「<b>說明</b>」分頁 → 右邊最後一個按鈕「<b>前往傳統 Outlook</b>」即可切回（保留同個帳號）。
                </p>
                <p className="text-[11px] text-amber-800 mt-1.5 leading-relaxed">按下方按鈕測試你的環境是否可用。</p>
              </div>
            </div>
            <div className="flex items-center gap-2 pt-1">
              <button
                onClick={runConnectionTest}
                disabled={testing}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-white rounded-lg text-xs font-medium transition-colors"
              >
                {testing ? <Loader2 className="w-3 h-3 animate-spin" /> : <span>🧪</span>}
                {testing ? '測試中…' : '測試 Outlook 連線'}
              </button>
              <button
                onClick={dismissWarn}
                className="text-[11px] text-amber-700 hover:bg-amber-200 px-2 py-0.5 rounded transition-colors"
              >
                我知道了，不再顯示
              </button>
            </div>
          </div>
        ) : (
          <div className="px-2 py-1.5 rounded-lg bg-gray-50 border border-gray-200 flex items-center gap-2">
            <AlertTriangle className="w-3 h-3 text-amber-500 shrink-0" />
            <span className="text-[11px] text-gray-500 flex-1">只支援 Classic Outlook</span>
            <button
              onClick={runConnectionTest}
              disabled={testing}
              className="text-[11px] text-blue-600 hover:underline disabled:opacity-50 shrink-0"
            >
              {testing ? '測試中…' : '🧪 測試連線'}
            </button>
            <button
              onClick={() => setWarnDismissed(false)}
              className="text-[11px] text-gray-400 hover:underline shrink-0"
            >
              詳情
            </button>
          </div>
        )}

        {/* Name */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">節點名稱</label>
          <input value={data.name} onChange={e => onUpdate({ name: e.target.value })} className={`${inputCls} font-mono`} />
        </div>

        {/* 模式說明 */}
        <div className="p-3 rounded-lg border border-blue-200 bg-blue-50/50 space-y-1.5">
          <p className="text-xs text-blue-900 font-medium">執行路徑分兩種，各有適用情境：</p>
          <p className="text-[11px] text-blue-800/90 leading-relaxed">
            <span className="inline-block w-2 h-2 rounded-full bg-emerald-500 mr-1.5 align-middle" />
            <b>🚀 直接執行</b>：不進 LLM、後端直接呼叫 Outlook API，快、零 token、結果可預測
          </p>
          <p className="text-[11px] text-blue-800/90 leading-relaxed">
            <span className="inline-block w-2 h-2 rounded-full bg-purple-500 mr-1.5 align-middle" />
            <b>🤖 AI 處理</b>：進 LLM agent loop，會花 token、可能多輪 retry，適合需要摘要 / 分析的情境
          </p>
        </div>

        {/* ── 🚀 直接執行區（無需 LLM）──────────────────────────── */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-semibold text-emerald-700 uppercase tracking-wide flex items-center gap-1">
              🚀 直接執行（無需 LLM）
            </label>
            {data.template && TEMPLATES.find(t => t.id === data.template)?.execMode === 'direct' && (
              <button onClick={clearTemplate} className="text-xs text-blue-600 hover:underline">清除選擇</button>
            )}
          </div>
          {(['inbox', 'send', 'attach', 'manage'] as const).map(cat => {
            const directInCat = TEMPLATES.filter(t => t.category === cat && t.execMode === 'direct')
            if (directInCat.length === 0) return null
            return (
              <div key={cat} className="mb-3">
                <p className="text-[11px] font-semibold text-gray-500 mb-1">{CATEGORY_LABEL[cat]}</p>
                <div className="space-y-1">
                  {directInCat.map(t => {
                    const isSelected = data.template === t.id
                    return (
                      <div key={t.id} className="space-y-0">
                        <button
                          ref={el => { if (el) templateBtnRefs.current.set(t.id, el) }}
                          onClick={() => selectTemplate(t.id)}
                          className={`w-full text-left px-2.5 py-1.5 text-xs border transition-colors ${
                            isSelected
                              ? 'bg-emerald-500 text-white border-emerald-500 rounded-t-lg'
                              : 'bg-white text-gray-700 border-gray-200 hover:border-emerald-300 rounded-lg'
                          }`}
                          title={t.description}
                        >
                          {t.label}
                        </button>
                        {isSelected && (
                          <div
                            onClickCapture={handleParamInteract}
                            className="px-3 py-3 rounded-b-lg border border-t-0 border-emerald-500 bg-emerald-50/30 space-y-3"
                          >
                            <p className="text-[11px] text-gray-600 leading-relaxed">{t.description}</p>
                            {t.params.map(p => (
                              <div key={p.key}>
                                {p.type !== 'bool' && (
                                  <label className="text-xs text-gray-600 block mb-1">{p.label}</label>
                                )}
                                {renderParam(p)}
                                {p.hint && <p className="text-[10px] text-gray-400 mt-0.5">{p.hint}</p>}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>

        {/* ── 🤖 AI 處理區（LLM 模板 + 自由輸入）─────────────────── */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-semibold text-purple-700 uppercase tracking-wide flex items-center gap-1">
              🤖 AI 處理（需要 LLM、會花 token）
            </label>
            {data.template && TEMPLATES.find(t => t.id === data.template)?.execMode === 'llm' && (
              <button onClick={clearTemplate} className="text-xs text-blue-600 hover:underline">清除選擇</button>
            )}
          </div>
          {(['inbox', 'send', 'attach', 'manage'] as const).map(cat => {
            const llmInCat = TEMPLATES.filter(t => t.category === cat && t.execMode === 'llm')
            if (llmInCat.length === 0) return null
            return (
              <div key={cat} className="mb-3">
                <p className="text-[11px] font-semibold text-gray-500 mb-1">{CATEGORY_LABEL[cat]}</p>
                <div className="space-y-1">
                  {llmInCat.map(t => {
                    const isSelected = data.template === t.id
                    return (
                      <div key={t.id} className="space-y-0">
                        <button
                          ref={el => { if (el) templateBtnRefs.current.set(t.id, el) }}
                          onClick={() => selectTemplate(t.id)}
                          className={`w-full text-left px-2.5 py-1.5 text-xs border transition-colors ${
                            isSelected
                              ? 'bg-purple-500 text-white border-purple-500 rounded-t-lg'
                              : 'bg-white text-gray-700 border-gray-200 hover:border-purple-300 rounded-lg'
                          }`}
                          title={t.description}
                        >
                          {t.label}
                        </button>
                        {isSelected && (
                          <div
                            onClickCapture={handleParamInteract}
                            className="px-3 py-3 rounded-b-lg border border-t-0 border-purple-500 bg-purple-50/30 space-y-3"
                          >
                            <p className="text-[11px] text-gray-600 leading-relaxed">{t.description}</p>
                            {t.params.map(p => (
                              <div key={p.key}>
                                {p.type !== 'bool' && (
                                  <label className="text-xs text-gray-600 block mb-1">{p.label}</label>
                                )}
                                {renderParam(p)}
                                {p.hint && <p className="text-[10px] text-gray-400 mt-0.5">{p.hint}</p>}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}

          {/* 自由輸入：屬於 AI 處理區的最後一塊 */}
          <div className="mb-3">
            <p className="text-[11px] font-semibold text-gray-500 mb-1">✏️ 自由輸入需求</p>
            <p className="text-[10px] text-gray-500 mb-1.5 leading-relaxed">
              不在上面選單裡的需求，直接打字描述。agent 限定使用 pywin32 + Outlook COM；做不到會回報無法執行、不會 fallback 到其他工具。
            </p>
            <textarea
              className={`${inputCls} font-mono resize-y min-h-[110px] ${data.template ? 'opacity-50' : ''}`}
              rows={6}
              placeholder="範例：把昨天到今天主旨含『發票』的信件附件全部存到 D:/invoices，並寄一封摘要給 a@x.com"
              value={data.freeText}
              disabled={!!data.template}
              onWheel={(e) => {
                const el = e.currentTarget
                const atTop = el.scrollTop === 0
                const atBot = el.scrollTop + el.clientHeight >= el.scrollHeight - 1
                if ((!atTop && e.deltaY < 0) || (!atBot && e.deltaY > 0)) e.stopPropagation()
              }}
              onChange={e => onUpdate({ freeText: e.target.value })}
            />
            {data.template && (
              <p className="text-[10px] text-gray-400 mt-1">已選模板「{TEMPLATES.find(t => t.id === data.template)?.label}」，自由輸入暫時不啟用。</p>
            )}
          </div>
        </div>

        {/* 輸出檔（可選） */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">輸出檔路徑（可選）</label>
          <input
            className={`${inputCls} font-mono`}
            placeholder="ai_output/{name}/result.xlsx"
            value={data.outputPath}
            onChange={e => onUpdate({ outputPath: e.target.value })}
          />
          <p className="text-[10px] text-gray-400 mt-0.5">整理 / 摘要結果寫到這裡，下個節點可以讀</p>
        </div>

        {/* Retry */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">失敗重試次數</label>
          <input type="number" min={0} max={5}
            className={inputCls}
            value={data.retry}
            onChange={e => onUpdate({ retry: Number(e.target.value) || 0 })}
          />
        </div>

        <LlmRoleSelector
          value={data.llmRole || 'primary'}
          onChange={(v) => onUpdate({ llmRole: v } as any)}
        />

        {/* Timeout（秒）*/}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">執行上限（秒）</label>
          <input type="number" min={60} max={7200} step={60}
            className={inputCls}
            value={typeof data.timeout === 'number' ? data.timeout : 600}
            onChange={e => onUpdate({ timeout: Number(e.target.value) || 600 })}
          />
          <p className="text-[10px] text-gray-400 mt-0.5">
            Outlook COM 對巨型收信夾 search_mail 可能跑 4-5 分鐘；30k+ 信箱建議 1800-3600
          </p>
        </div>
      </div>

      {/* ── 跨模板殘留確認 modal ─────────────────────────────────────
        切到新模板時不直接清舊內容；使用者點到任一輸入欄位才彈這個 modal 問。
        無論清或保留，都把 staleFrom 設 null（同一次切換只彈一次、不再煩）。
      */}
      {confirmOpen && staleFrom && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-[380px] bg-white rounded-xl shadow-2xl p-5 space-y-3" role="dialog" aria-modal="true">
            <div className="flex items-start gap-2.5">
              <AlertTriangle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <h3 className="text-sm font-semibold text-gray-800">是否清除之前的設定？</h3>
                <p className="text-xs text-gray-600 leading-relaxed mt-1.5">
                  你之前選過「<b className="text-gray-800">{staleFrom.label}</b>」並填了內容。
                  繼續編輯前要把舊資料清掉嗎？
                </p>
              </div>
            </div>
            <div className="flex gap-2 justify-end pt-1">
              <button
                onClick={onConfirmKeep}
                className="px-3 py-1.5 text-xs rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 font-medium"
              >
                保留繼續編輯
              </button>
              <button
                onClick={onConfirmClear}
                className="px-3 py-1.5 text-xs rounded-lg bg-rose-500 hover:bg-rose-600 text-white font-medium"
              >
                清除舊資料
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
