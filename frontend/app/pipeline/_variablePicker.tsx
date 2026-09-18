'use client'
/**
 * 變數選擇器 — Ticket 1e。
 *
 * 兩種使用方式:
 * 1. <VariableButton ... /> 按鈕點開 modal、列出所有可用變數、點選就插到 textarea / input 游標位置
 * 2. <VariableAutocomplete ... /> 在 textarea 偵測使用者敲 `{{` 時跳出下拉、鍵盤導航
 *
 * 兩個 component 都用同一份 useWorkflowVariables hook 拿可用變數 + 上次值。
 */
import { useState, useEffect, useRef, useCallback, useMemo, forwardRef, useImperativeHandle } from 'react'
import { createPortal } from 'react-dom'
import { Link as LinkIcon, X, Search } from 'lucide-react'
import {
  getWorkflowVariables,
  type WorkflowVariablesResult,
  type VariableField,
} from '@/lib/api'

// ── 共用 hook:拉變數清單 ─────────────────────────────────────────────────
function useWorkflowVariables(workflowId: string | undefined, enabled: boolean) {
  const [data, setData] = useState<WorkflowVariablesResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!enabled || !workflowId) return
    let cancelled = false
    setLoading(true)
    setError(null)
    getWorkflowVariables(workflowId)
      .then((r) => { if (!cancelled) setData(r) })
      .catch((e) => { if (!cancelled) setError(String(e?.message || e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [workflowId, enabled])

  return { data, loading, error }
}

// 把 dotted-path 攤平成「可選變數」list、給搜尋過濾用
interface PickerItem {
  path: string                 // 完整 dotted-path,例:steps.crawl.output.path
  category: 'steps' | 'input'
  label: string                // UI 顯示用,例:crawl.path
  detail: string               // 副標,例:Script · 上次:data/ptt.csv
  lastValue: string
  source?: string
}

function flattenVariables(data: WorkflowVariablesResult | null): PickerItem[] {
  if (!data) return []
  const out: PickerItem[] = []
  for (const step of data.available.steps) {
    for (const f of step.fields) {
      out.push({
        path: `steps.${step.name}.output.${f.key}`,
        category: 'steps',
        label: `${step.name}.${f.key}`,
        detail: `${step.node_type}${f.source ? ' · ' + f.source : ''}`,
        lastValue: String(f.last_value ?? ''),
        source: f.source,
      })
    }
  }
  for (const i of data.available.input) {
    out.push({
      path: `input.${i.key}`,
      category: 'input',
      label: `input.${i.key}`,
      detail: i.required ? '啟動參數' : '啟動參數(可選)',
      lastValue: String(i.last_value ?? ''),
    })
  }
  // 環境變數刻意不放進 picker / autocomplete:對 batch 編寫沒實際用途、徒增認知負擔。
  // runtime 仍會替換 {{ env.X }}、進階使用者要用就直接打字。
  return out
}

// ── Picker Modal ─────────────────────────────────────────────────────────
interface PickerModalProps {
  workflowId?: string
  onSelect: (path: string) => void
  onClose: () => void
}

function PickerModal({ workflowId, onSelect, onClose }: PickerModalProps) {
  const { data, loading, error } = useWorkflowVariables(workflowId, true)
  const [search, setSearch] = useState('')

  const all = useMemo(() => flattenVariables(data), [data])
  const filtered = useMemo(() => {
    if (!search.trim()) return all
    const s = search.toLowerCase()
    return all.filter(
      (it) =>
        it.label.toLowerCase().includes(s) ||
        it.path.toLowerCase().includes(s) ||
        it.lastValue.toLowerCase().includes(s),
    )
  }, [all, search])

  const grouped = useMemo(() => {
    const g: Record<string, PickerItem[]> = { steps: [], input: [] }
    for (const it of filtered) g[it.category].push(it)
    return g
  }, [filtered])

  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-2xl shadow-2xl w-[640px] max-w-[92vw] max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b">
          <div className="flex items-center gap-2">
            <LinkIcon className="w-4 h-4 text-indigo-500" />
            <span className="font-semibold text-sm text-gray-800">插入變數</span>
            {workflowId ? null : (
              <span className="text-xs text-amber-600 ml-2">
                (此工作流尚未存檔、暫無上次值參考)
              </span>
            )}
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Search */}
        <div className="px-5 py-2.5 border-b bg-gray-50">
          <div className="relative">
            <Search className="w-3.5 h-3.5 text-gray-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜尋變數名稱或值…"
              autoFocus
              className="w-full pl-8 pr-3 py-1.5 text-sm border border-gray-200 rounded-lg outline-none focus:border-indigo-400 bg-white"
            />
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-3 space-y-3">
          {loading && (
            <p className="text-center text-gray-400 py-4 text-sm">載入中…</p>
          )}
          {error && (
            <div className="text-sm text-red-600 bg-red-50 rounded-lg p-3">
              載入失敗:{error}
            </div>
          )}
          {!loading && !error && data && (
            <>
              {/* 上游節點 */}
              {grouped.steps.length > 0 && (
                <Section
                  title="🟦 上游節點輸出"
                  hint="點選會插入 {{ steps.X.output.Y }}"
                  items={grouped.steps}
                  onSelect={onSelect}
                />
              )}
              {/* 啟動參數 */}
              {grouped.input.length > 0 && (
                <Section
                  title="🟩 啟動參數 (input)"
                  hint="跑 workflow 時 user 傳入,例:/run X date=today"
                  items={grouped.input}
                  onSelect={onSelect}
                />
              )}
              {/* 環境變數區塊已移除 — 對 batch 編寫沒實際用途、徒增認知負擔。
                  如需 env 變數仍可在 batch 直接打 {{ env.X }}、runtime 會替換 */}
              {filtered.length === 0 && (
                <p className="text-center text-gray-400 py-6 text-sm">
                  沒有符合的變數
                </p>
              )}
            </>
          )}
        </div>

        {/* Footer hint */}
        <div className="px-5 py-2.5 border-t bg-gray-50 text-[11px] text-gray-500">
          💡 也可在輸入框直接敲 <code className="px-1 py-0.5 rounded bg-gray-200 font-mono">{`{{`}</code> 觸發自動完成
        </div>
      </div>
    </div>,
    document.body,
  )
}

// 變數用途提示 — 教使用者「該選哪個、別選哪個」
// 避免新手把 stdout / exit_code / status 當「上一步輸出檔」誤插
function getUsageHint(path: string): { tone: 'rec' | 'warn' | 'info'; text: string } | null {
  if (path.endsWith('.path')) {
    return { tone: 'rec', text: '✅ 推薦:檔案實際路徑(下游 LLM / 腳本要讀檔請選這個)' }
  }
  if (path.endsWith('.stdout')) {
    return { tone: 'warn', text: '⚠ 上一步 stdout 一行訊息、不是檔案內容,通常不要用' }
  }
  if (path.endsWith('.exit_code')) {
    return { tone: 'warn', text: '⚠ 只是 0 / 1 數字、沒語意,通常不要用' }
  }
  if (path.endsWith('.status')) {
    return { tone: 'warn', text: '⚠ 只是 ok / failed 字串(驗證結果)、沒實際資料,通常不要用' }
  }
  if (path.startsWith('input.')) {
    return { tone: 'info', text: '🟩 啟動參數:跑 workflow 時由 user 傳入(/run X arg=value)' }
  }
  return null
}

const _TONE_CLASS = {
  rec: 'text-emerald-600',
  warn: 'text-amber-600',
  info: 'text-gray-500',
} as const

function Section({
  title, hint, items, onSelect,
}: {
  title: string
  hint: string
  items: PickerItem[]
  onSelect: (path: string) => void
}) {
  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <div className="px-3 py-2 bg-gray-50 border-b">
        <div className="text-xs font-semibold text-gray-700">{title}</div>
        <div className="text-[11px] text-gray-500 mt-0.5">{hint}</div>
      </div>
      <div className="divide-y divide-gray-100">
        {items.map((it) => {
          const usage = getUsageHint(it.path)
          return (
            <button
              key={it.path}
              onClick={() => onSelect(it.path)}
              className="w-full flex items-center justify-between gap-3 px-3 py-2 hover:bg-indigo-50 text-left transition-colors"
            >
              <div className="flex-1 min-w-0">
                <div className="font-mono text-xs text-gray-800 truncate">{it.label}</div>
                <div className="text-[11px] text-gray-400 truncate mt-0.5">{it.detail}</div>
                {usage && (
                  <div className={`text-[11px] mt-0.5 truncate ${_TONE_CLASS[usage.tone]}`}>
                    {usage.text}
                  </div>
                )}
              </div>
              {it.lastValue && (
                <div
                  className="font-mono text-[11px] text-emerald-700 bg-emerald-50 px-2 py-1 rounded max-w-[200px] truncate shrink-0"
                  title={it.lastValue}
                >
                  {it.lastValue}
                </div>
              )}
              <span className="text-[11px] text-indigo-500 font-medium shrink-0">插入</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── 公開 component:VariableButton ─────────────────────────────────────────
/**
 * 「🔗 插入變數」按鈕 — 點開 modal、選擇變數後 callback 會收到完整 dotted-path 字串
 * (例:"steps.crawl.output.path")。Caller 需自己負責把字串包成 `{{ X }}` 並插到欄位。
 *
 * 通常搭配 <VariableInput /> 一起用,會幫你處理插入邏輯。
 */
export function VariableButton({
  workflowId,
  onPick,
  className = '',
  label = '插入變數',
}: {
  workflowId?: string
  onPick: (path: string) => void
  className?: string
  label?: string
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={
          // shrink-0 + nowrap:這顆常被塞進 flex 列（旁邊是 flex-1 的輸入框），
          // 不鎖的話面板一窄文字就被壓成直排（實測「插入變數」四個字疊兩行）
          'inline-flex shrink-0 items-center gap-1 px-2 py-1 text-[11px] whitespace-nowrap rounded-md border ' +
          'border-indigo-200 bg-indigo-50 text-indigo-700 hover:bg-indigo-100 transition-colors ' +
          className
        }
        title="開啟變數選擇器(列出所有可引用變數)"
      >
        <LinkIcon className="w-3 h-3" />
        {label}
      </button>
      {open && (
        <PickerModal
          workflowId={workflowId}
          onSelect={(path) => {
            onPick(path)
            // modal 不關 — 讓使用者連點插多個
          }}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  )
}

// ── ChipTextarea — 把 {{ ... }} 渲染成不可分割的標籤 ─────────────────────
// 使用 contenteditable + 不可編輯 span,讓 {{ steps.X.output.Y }} 變成一顆 chip:
//  - 點到 chip 內部不會把游標跑進去(contenteditable=false)
//  - 按 Backspace 整顆刪除(瀏覽器原生行為)
//  - 跟普通文字混排、可放句首/句尾/任意位置
// 序列化:DOM → string 時把 chip 還原成 {{ <path> }} 文字、其餘照舊
//
// 為什麼自己刻、不用 tiptap / slate?單一需求、不值得多 200KB bundle。

// 變數名稱可含中文(steps.<中文 step name>.output.path)、所以不能用 \w(JS \w 只認 ASCII)
// 改成「不是空白、不是大括號」就接受、跟後端 Jinja 解析寬鬆度一致
const _CHIP_RE = /\{\{\s*([^\s{}]+)\s*\}\}/g
const _CHIP_CLS =
  'var-chip inline-flex items-center px-1.5 py-px mx-0.5 rounded bg-indigo-100 text-indigo-700 ' +
  'text-[11px] font-semibold select-none cursor-default whitespace-nowrap align-baseline'

interface ChipTextareaHandle {
  insertChip: (path: string) => void
  insertText: (text: string) => void
  focus: () => void
  getCaretCoords: () => { left: number; top: number; height: number } | null
  getTextBeforeCaret: () => string  // 用於偵測未閉合的 {{
  replaceRange: (start: number, end: number, replacement: string, asChip: boolean) => void
}

const ChipTextarea = forwardRef<ChipTextareaHandle, {
  value: string
  onChange: (v: string) => void
  onKeyDown?: (e: React.KeyboardEvent<HTMLDivElement>) => void
  onInput?: () => void
  placeholder?: string
  rows?: number
  className?: string
}>(function ChipTextarea({ value, onChange, onKeyDown, onInput, placeholder, rows = 3, className = '' }, ref) {
  const elRef = useRef<HTMLDivElement | null>(null)
  // 用 ref 記「上次 set 進去的 value」,避免外部 onChange 回流時又 setInnerHTML 害游標跳
  const lastSetValueRef = useRef<string>('')
  // 記最後一次游標位置 — 點外部按鈕(插入變數)時 focus 會跑掉、用這個還原
  const lastRangeRef = useRef<Range | null>(null)
  const [isEmpty, setIsEmpty] = useState(!value)

  // 任何 selection 變動都存一份 Range(只接受在自己 elRef 內的)
  const saveRange = useCallback(() => {
    const el = elRef.current
    if (!el) return
    const sel = window.getSelection()
    if (!sel || sel.rangeCount === 0) return
    const r = sel.getRangeAt(0)
    if (el.contains(r.commonAncestorContainer)) {
      lastRangeRef.current = r.cloneRange()
    }
  }, [])

  // 還原游標(focus 從外部跑回來時)— focus 後 selection 不一定恢復、強制把存的 range 套回去
  const restoreRange = useCallback(() => {
    const el = elRef.current
    if (!el) return false
    el.focus()
    const saved = lastRangeRef.current
    if (!saved) return false
    const sel = window.getSelection()
    if (!sel) return false
    sel.removeAllRanges()
    sel.addRange(saved)
    return true
  }, [])

  // value → 安全 HTML(逃 escape 後、把 {{ ... }} 換成 chip span)
  const valueToHtml = useCallback((v: string): string => {
    const esc = (s: string) => s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
    let out = ''
    let last = 0
    _CHIP_RE.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = _CHIP_RE.exec(v)) !== null) {
      out += esc(v.slice(last, m.index)).replace(/\n/g, '<br>')
      const path = m[1]
      out += `<span class="${_CHIP_CLS}" contenteditable="false" data-var="${esc(path)}">${esc(path)}</span>`
      last = m.index + m[0].length
    }
    out += esc(v.slice(last)).replace(/\n/g, '<br>')
    return out
  }, [])

  // DOM → string(chip 還原成 {{ path }}、<br> 換成 \n)
  const serializeDom = useCallback((root: HTMLElement): string => {
    let s = ''
    const walk = (node: Node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        s += node.textContent || ''
        return
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return
      const el = node as HTMLElement
      if (el.tagName === 'BR') { s += '\n'; return }
      // chip:用 data-var 識別(class 名可能因 Tailwind 改變、用屬性穩)
      if (el.hasAttribute('data-var')) {
        const path = el.getAttribute('data-var') || el.textContent || ''
        s += `{{ ${path} }}`
        return
      }
      // div / p 等 block 邊界當換行
      const isBlock = el.tagName === 'DIV' || el.tagName === 'P'
      if (isBlock && s.length > 0 && !s.endsWith('\n')) s += '\n'
      el.childNodes.forEach(walk)
    }
    root.childNodes.forEach(walk)
    return s
  }, [])

  // 初始 + 外部 value 變動時同步進 DOM
  useEffect(() => {
    const el = elRef.current
    if (!el) return
    if (value === lastSetValueRef.current) return  // 是我們自己 onChange 出去的,不要再灌
    el.innerHTML = valueToHtml(value)
    lastSetValueRef.current = value
    setIsEmpty(!value)
  }, [value, valueToHtml])

  // 使用者打字 / 改動 → 序列化回 string、通知外部
  const handleInput = useCallback(() => {
    const el = elRef.current
    if (!el) return
    const s = serializeDom(el)
    lastSetValueRef.current = s
    onChange(s)
    setIsEmpty(!s)
    onInput?.()
  }, [onChange, onInput, serializeDom])

  // 貼上時剝掉 rich text、只留純文字(防 chip 被破壞、防 HTML 注入)
  const handlePaste = useCallback((e: React.ClipboardEvent<HTMLDivElement>) => {
    e.preventDefault()
    const text = e.clipboardData.getData('text/plain')
    document.execCommand('insertText', false, text)
  }, [])

  // Backspace / Delete 顯式處理:游標在 chip 邊界時整顆刪掉
  // 原因:不同瀏覽器對 contenteditable=false inline 元素的刪除行為不一致,
  // 直接靠瀏覽器預設常常按 Backspace 沒反應。
  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Backspace' || e.key === 'Delete') {
      const sel = window.getSelection()
      if (sel && sel.isCollapsed && sel.rangeCount > 0) {
        const range = sel.getRangeAt(0)
        const node = range.startContainer
        const offset = range.startOffset
        const isBackspace = e.key === 'Backspace'
        let target: Element | null = null
        const checkSibling = (n: Node | null): Element | null => {
          if (!n) return null
          if (n.nodeType === Node.ELEMENT_NODE && (n as Element).hasAttribute('data-var')) {
            return n as Element
          }
          // 跨 zero-width-space 文字節點:若是純空白/ZWSP、再往前/後看一格
          if (n.nodeType === Node.TEXT_NODE) {
            const t = (n.textContent || '').replace(/[​\s]/g, '')
            if (t === '') return checkSibling(isBackspace ? n.previousSibling : n.nextSibling)
          }
          return null
        }
        if (isBackspace) {
          // 游標前方是不是 chip
          if (node.nodeType === Node.TEXT_NODE && offset === 0) {
            target = checkSibling(node.previousSibling)
          } else if (node.nodeType === Node.ELEMENT_NODE && offset > 0) {
            target = checkSibling(node.childNodes[offset - 1])
          }
        } else {
          // Delete:游標後方是不是 chip
          if (node.nodeType === Node.TEXT_NODE && offset === (node.textContent?.length ?? 0)) {
            target = checkSibling(node.nextSibling)
          } else if (node.nodeType === Node.ELEMENT_NODE && offset < node.childNodes.length) {
            target = checkSibling(node.childNodes[offset])
          }
        }
        if (target) {
          e.preventDefault()
          target.remove()
          handleInput()
          return
        }
      }
    }
    onKeyDown?.(e)
  }, [handleInput, onKeyDown])

  // 暴露 imperative API 給外部 autocomplete / 插入變數按鈕用
  useImperativeHandle(ref, () => ({
    focus: () => elRef.current?.focus(),
    insertChip: (path: string) => {
      const el = elRef.current
      if (!el) return
      // 還原進入 modal 前的游標位置(否則 selection 不在 el 內、會插在預設位置 = 開頭)
      restoreRange()
      const span = document.createElement('span')
      span.className = _CHIP_CLS
      span.contentEditable = 'false'
      span.setAttribute('data-var', path)
      span.textContent = path
      const sel = window.getSelection()
      if (sel && sel.rangeCount > 0) {
        const range = sel.getRangeAt(0)
        range.deleteContents()
        range.insertNode(span)
        // 補一個 zero-width space 後綴讓游標可以放在 chip 後面
        const tail = document.createTextNode('​')
        span.parentNode?.insertBefore(tail, span.nextSibling)
        range.setStartAfter(tail)
        range.collapse(true)
        sel.removeAllRanges()
        sel.addRange(range)
      } else {
        el.appendChild(span)
      }
      handleInput()
    },
    insertText: (text: string) => {
      const el = elRef.current
      if (!el) return
      el.focus()
      document.execCommand('insertText', false, text)
      handleInput()
    },
    getCaretCoords: () => {
      const sel = window.getSelection()
      if (!sel || sel.rangeCount === 0) return null
      const range = sel.getRangeAt(0).cloneRange()
      range.collapse(false)
      const rect = range.getBoundingClientRect()
      // 如果是空的 range(剛 focus 時),fallback 用容器
      if (rect.left === 0 && rect.top === 0) {
        const eRect = elRef.current?.getBoundingClientRect()
        if (eRect) return { left: eRect.left, top: eRect.bottom + 4, height: eRect.height }
      }
      return { left: rect.left, top: rect.bottom + 4, height: rect.height || 20 }
    },
    getTextBeforeCaret: () => {
      const el = elRef.current
      if (!el) return ''
      const sel = window.getSelection()
      if (!sel || sel.rangeCount === 0) return ''
      const range = sel.getRangeAt(0).cloneRange()
      range.setStart(el, 0)
      // 用一個 Range fragment + 我們的 serializeDom 算游標前的字串
      const frag = range.cloneContents()
      const tmp = document.createElement('div')
      tmp.appendChild(frag)
      return serializeDom(tmp)
    },
    replaceRange: (start: number, end: number, replacement: string, asChip: boolean) => {
      // 用 value-string 座標來定位、再重新渲染
      const before = value.slice(0, start)
      const after = value.slice(end)
      const next = asChip ? `${before}{{ ${replacement} }}${after}` : `${before}${replacement}${after}`
      // 強制 DOM 重渲染(因為 lastSetValueRef 會擋外部 useEffect)
      lastSetValueRef.current = '__force__'
      onChange(next)
    },
  }), [handleInput, onChange, serializeDom, value, restoreRange])

  const baseCls =
    'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none ' +
    'focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400/20 bg-white font-mono ' +
    'whitespace-pre-wrap break-words overflow-y-auto'
  const minH = `${rows * 1.5 + 0.5}rem`

  return (
    <div className={`relative ${className}`}>
      <div
        ref={elRef}
        contentEditable
        suppressContentEditableWarning
        onInput={() => { handleInput(); saveRange() }}
        onKeyDown={handleKeyDown}
        onKeyUp={saveRange}
        onMouseUp={saveRange}
        onBlur={saveRange}
        onPaste={handlePaste}
        className={baseCls}
        style={{ minHeight: minH, overflowWrap: 'anywhere', wordBreak: 'break-word' }}
        spellCheck={false}
      />
      {isEmpty && placeholder && (
        <div
          className="absolute top-1.5 left-2.5 right-2.5 text-sm text-gray-400 pointer-events-none font-mono overflow-hidden"
          style={{
            whiteSpace: rows === 1 ? 'nowrap' : 'pre-wrap',
            textOverflow: 'ellipsis',
          }}
        >
          {placeholder}
        </div>
      )}
    </div>
  )
})

// ── 公開 component:VariableInput(textarea / input wrapper)──────────────
/**
 * 包裝 <textarea> 或 <input>:
 * - 上方右側放 [🔗 插入變數] 按鈕 + 提示字
 * - 偵測使用者敲 `{{` 觸發自動完成下拉
 * - 點選變數時自動把游標位置插入 `{{ <dotted_path> }}`
 *
 * 用法:
 *   <VariableInput
 *     value={data.batch}
 *     onChange={(v) => upd({ batch: v })}
 *     workflowId={workflowId}
 *     multiline
 *     placeholder="..."
 *   />
 */
export function VariableInput({
  value,
  onChange,
  workflowId,
  multiline = false,
  rows = 2,
  placeholder,
  className = '',
  buttonLabel = '插入變數',
  showHint = true,
}: {
  value: string
  onChange: (v: string) => void
  workflowId?: string
  multiline?: boolean
  rows?: number
  placeholder?: string
  className?: string
  buttonLabel?: string
  showHint?: boolean
}) {
  const inputRef = useRef<HTMLTextAreaElement | HTMLInputElement | null>(null)
  // multiline 走 chip-textarea(contenteditable + chip span)
  const chipRef = useRef<ChipTextareaHandle | null>(null)
  const [autocompleteOpen, setAutocompleteOpen] = useState(false)
  const [autocompleteQuery, setAutocompleteQuery] = useState('')
  // {{ 觸發位置:autocomplete popup 在這個游標位置展開,選中後從這裡開始覆蓋
  const [autocompleteAnchor, setAutocompleteAnchor] = useState<{
    start: number   // {{ 起始位置(含 `{{`)
    end: number     // 目前游標位置
    coords: { left: number; top: number; height: number }
  } | null>(null)
  const [highlightIdx, setHighlightIdx] = useState(0)

  const { data } = useWorkflowVariables(workflowId, autocompleteOpen)
  const allItems = useMemo(() => flattenVariables(data), [data])
  const filteredItems = useMemo(() => {
    if (!autocompleteQuery.trim()) return allItems.slice(0, 10)
    const s = autocompleteQuery.toLowerCase()
    return allItems
      .filter((it) => it.path.toLowerCase().includes(s) || it.label.toLowerCase().includes(s))
      .slice(0, 10)
  }, [allItems, autocompleteQuery])

  // 用 element 在 viewport 的位置近似計算 popup 座標(夠用)
  const computeAnchorCoords = (el: HTMLElement) => {
    const rect = el.getBoundingClientRect()
    return { left: rect.left, top: rect.bottom + 4, height: rect.height }
  }

  // ── multiline 模式:chip-textarea 的 input/keydown 觸發 ─────────────────
  // ChipTextarea 內部已把 value 回傳;我們這邊只負責「偵測 {{ 觸發 autocomplete」
  const handleChipInput = useCallback(() => {
    if (!multiline) return
    const before = chipRef.current?.getTextBeforeCaret() ?? ''
    const lastOpen = before.lastIndexOf('{{')
    const lastClose = before.lastIndexOf('}}')
    if (lastOpen >= 0 && lastOpen > lastClose) {
      const inside = before.slice(lastOpen + 2)
      if (!inside.includes('\n')) {
        const coords = chipRef.current?.getCaretCoords() || null
        setAutocompleteQuery(inside.trim())
        setAutocompleteAnchor({
          start: lastOpen,
          end: before.length,
          coords: coords || { left: 0, top: 0, height: 20 },
        })
        setAutocompleteOpen(true)
        setHighlightIdx(0)
        return
      }
    }
    setAutocompleteOpen(false)
  }, [multiline])

  const handleChipKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!autocompleteOpen || filteredItems.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlightIdx((i) => (i + 1) % filteredItems.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlightIdx((i) => (i - 1 + filteredItems.length) % filteredItems.length)
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      insertSelected(filteredItems[highlightIdx].path)
    } else if (e.key === 'Escape') {
      setAutocompleteOpen(false)
    }
  }, [autocompleteOpen, filteredItems, highlightIdx])  // eslint-disable-line react-hooks/exhaustive-deps

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement | HTMLInputElement>) => {
    const next = e.target.value
    onChange(next)
    const pos = e.target.selectionStart ?? next.length
    // 找前面最近的 `{{`、且尚未閉合
    const before = next.slice(0, pos)
    const lastOpen = before.lastIndexOf('{{')
    const lastClose = before.lastIndexOf('}}')
    if (lastOpen >= 0 && lastOpen > lastClose) {
      // 在 `{{` 後、未閉合 → 觸發自動完成
      const inside = next.slice(lastOpen + 2, pos)
      // 含換行就退出(別人在多行情境敲 `{{` 不是想觸發)
      if (!inside.includes('\n')) {
        setAutocompleteQuery(inside.trim())
        setAutocompleteAnchor({
          start: lastOpen,
          end: pos,
          coords: computeAnchorCoords(e.target),
        })
        setAutocompleteOpen(true)
        setHighlightIdx(0)
        return
      }
    }
    setAutocompleteOpen(false)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement | HTMLInputElement>) => {
    if (!autocompleteOpen || filteredItems.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlightIdx((i) => (i + 1) % filteredItems.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlightIdx((i) => (i - 1 + filteredItems.length) % filteredItems.length)
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      insertSelected(filteredItems[highlightIdx].path)
    } else if (e.key === 'Escape') {
      setAutocompleteOpen(false)
    }
  }

  const insertSelected = useCallback(
    (path: string) => {
      if (!autocompleteAnchor) return
      if (multiline && chipRef.current) {
        // chip 模式:replaceRange 把 `{{ inside` 換成 chip
        chipRef.current.replaceRange(autocompleteAnchor.start, autocompleteAnchor.end, path, true)
        setAutocompleteOpen(false)
        return
      }
      // textarea / input 模式
      const before = value.slice(0, autocompleteAnchor.start)
      const after = value.slice(autocompleteAnchor.end)
      const insertion = `{{ ${path} }}`
      const next = before + insertion + after
      onChange(next)
      setAutocompleteOpen(false)
      requestAnimationFrame(() => {
        const el = inputRef.current
        if (el) {
          const pos = before.length + insertion.length
          el.focus()
          el.setSelectionRange(pos, pos)
        }
      })
    },
    [autocompleteAnchor, value, onChange, multiline],
  )

  // VariableButton 的插入(沒在 textarea 自動完成情境):游標位置插
  const insertAtCursor = (path: string) => {
    if (multiline && chipRef.current) {
      chipRef.current.insertChip(path)
      return
    }
    const el = inputRef.current
    const insertion = `{{ ${path} }}`
    if (el) {
      const start = el.selectionStart ?? value.length
      const end = el.selectionEnd ?? value.length
      const next = value.slice(0, start) + insertion + value.slice(end)
      onChange(next)
      requestAnimationFrame(() => {
        const pos = start + insertion.length
        el.focus()
        el.setSelectionRange(pos, pos)
      })
    } else {
      onChange(value + insertion)
    }
  }

  const baseInputCls =
    'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none ' +
    'focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400/20 bg-white font-mono'

  return (
    <div className={`relative ${className}`}>
      <div className="flex items-center justify-end gap-2 mb-1">
        <VariableButton workflowId={workflowId} onPick={insertAtCursor} label={buttonLabel} />
      </div>

      {multiline ? (
        <ChipTextarea
          ref={chipRef}
          value={value}
          onChange={onChange}
          onInput={handleChipInput}
          onKeyDown={handleChipKeyDown}
          placeholder={placeholder}
          rows={rows}
        />
      ) : (
        <input
          ref={inputRef as React.RefObject<HTMLInputElement>}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          className={baseInputCls}
        />
      )}

      {showHint && (
        <p className="text-[11px] text-gray-400 mt-1">
          💡 輸入 <code className="px-1 rounded bg-gray-100 font-mono">{`{{`}</code> 或按
          <span className="font-semibold text-indigo-600"> 🔗 插入變數 </span>
          引用上游 step / input / env
        </p>
      )}

      {/* Autocomplete dropdown */}
      {autocompleteOpen && autocompleteAnchor && filteredItems.length > 0 &&
        createPortal(
          <div
            className="fixed z-[70] bg-white border border-gray-200 rounded-lg shadow-xl w-[420px] max-h-[280px] overflow-y-auto"
            style={{
              left: Math.min(autocompleteAnchor.coords.left, window.innerWidth - 440),
              top: Math.min(autocompleteAnchor.coords.top, window.innerHeight - 300),
            }}
          >
            <div className="px-3 py-1.5 bg-gray-50 border-b text-[11px] text-gray-500">
              ↑↓ 選擇 · Enter/Tab 插入 · Esc 關閉
            </div>
            <div className="divide-y divide-gray-100">
              {filteredItems.map((it, i) => (
                <button
                  type="button"
                  key={it.path}
                  onMouseEnter={() => setHighlightIdx(i)}
                  onMouseDown={(e) => {
                    e.preventDefault() // 防止 textarea blur
                    insertSelected(it.path)
                  }}
                  className={`w-full flex items-center justify-between gap-3 px-3 py-1.5 text-left transition-colors ${
                    i === highlightIdx ? 'bg-indigo-100' : 'hover:bg-indigo-50'
                  }`}
                >
                  <div className="flex-1 min-w-0">
                    <div className="font-mono text-[11px] text-gray-800 truncate">{it.path}</div>
                    {it.detail && (
                      <div className="text-[10px] text-gray-400 truncate mt-0.5">{it.detail}</div>
                    )}
                  </div>
                  {it.lastValue && (
                    <div className="font-mono text-[10px] text-emerald-700 bg-emerald-50 px-1.5 py-0.5 rounded max-w-[150px] truncate shrink-0">
                      {it.lastValue}
                    </div>
                  )}
                </button>
              ))}
            </div>
          </div>,
          document.body,
        )}
    </div>
  )
}
