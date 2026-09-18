'use client'
/**
 * Condition 節點面板。
 *
 * 兩個模式:
 * - IF:成立 / 不成立 兩條路
 * - Switch:依一個值的內容分多條路
 *
 * 零術語設計:使用者只用「簡易設定」的下拉就能完成;Jinja2 表達式收在
 * 「進階」摺疊區。打開既有節點時,簡易設定會反解析現有表達式、自動把
 * 下拉填好(使用者看到的是「已設定好的成品」)。
 */
import { useMemo, useState, useEffect, useRef } from 'react'
import { X, GitBranch, Plus, Trash2, Wand2 } from 'lucide-react'
import type { ConditionData, ConditionNode } from './_helpers'
import { VariableInput } from './_variablePicker'
import { getWorkflowVariables, type WorkflowVariablesResult } from '@/lib/api'

const CONDITION_COLOR = '#f97316'  // 橘色 — 跟其他節點區隔

interface Props {
  node: ConditionNode
  onUpdate: (data: Partial<ConditionData>) => void
  onClose: () => void
  onDelete: () => void
  workflowId?: string
  /** 此 workflow 內所有可跳轉的 step 名稱(供下拉選單) */
  availableStepNames?: string[]
}

export default function ConditionPanel({
  node, onUpdate, onClose, onDelete, workflowId, availableStepNames = [],
}: Props) {
  const data = node.data
  const inputCls = 'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-orange-400 focus:ring-1 focus:ring-orange-400/20 bg-white'

  // 排除自己,避免直接跳回自己造成無限迴圈
  const targetOptions = useMemo(
    () => availableStepNames.filter(n => n !== data.name),
    [availableStepNames, data.name],
  )

  return (
    <div className="absolute top-0 right-0 h-full w-[420px] bg-white shadow-2xl border-l border-gray-100 flex flex-col z-30 overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b" style={{ borderTopColor: CONDITION_COLOR, borderTopWidth: 3 }}>
        <span className="w-8 h-8 rounded-full flex items-center justify-center text-white shrink-0"
          style={{ background: CONDITION_COLOR }}><GitBranch className="w-4 h-4" strokeWidth={2.4} /></span>
        <div className="flex-1 min-w-0">
          <span className="font-semibold text-gray-800 text-sm block truncate">條件判斷</span>
          <span className="text-xs text-gray-400">依條件決定接下來走哪一步</span>
        </div>
        <button onClick={onDelete} title="刪除" className="text-gray-300 hover:text-red-400 transition-colors p-1">🗑</button>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors"><X className="w-4 h-4" /></button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* 名稱 */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">節點名稱</label>
          <input value={data.name} onChange={e => onUpdate({ name: e.target.value })} className={inputCls} placeholder="例:判斷資料量 / 依狀態分流" />
        </div>

        {/* 模式切換 */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">模式</label>
          <div className="flex gap-2">
            <button
              onClick={() => onUpdate({ mode: 'if' })}
              className={`flex-1 py-2 rounded-lg text-sm font-medium border transition-colors ${
                data.mode === 'if'
                  ? 'bg-orange-600 text-white border-orange-600'
                  : 'text-gray-600 border-gray-200 hover:border-orange-400'
              }`}
            >🔀 成立 / 不成立</button>
            <button
              onClick={() => onUpdate({ mode: 'switch' })}
              className={`flex-1 py-2 rounded-lg text-sm font-medium border transition-colors ${
                data.mode === 'switch'
                  ? 'bg-orange-600 text-white border-orange-600'
                  : 'text-gray-600 border-gray-200 hover:border-orange-400'
              }`}
            >🎯 分多種情況</button>
          </div>
        </div>

        {/* IF 模式 */}
        {data.mode === 'if' && (
          <>
            <ConditionBuilder
              workflowId={workflowId}
              expression={data.expression || ''}
              onChange={(expr) => onUpdate({ expression: expr })}
            />

            <div>
              <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">✅ 成立時 → 接著做</label>
              <StepSelect value={data.onTrue} options={targetOptions} onChange={v => onUpdate({ onTrue: v })} />
            </div>

            <div>
              <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">❌ 不成立時 → 接著做</label>
              <StepSelect value={data.onFalse} options={targetOptions} onChange={v => onUpdate({ onFalse: v })} />
            </div>

            {/* 進階:直接編輯表達式 */}
            <details>
              <summary className="text-[11px] text-gray-400 cursor-pointer select-none hover:text-gray-600">
                進階:直接編輯條件表達式
              </summary>
              <div className="mt-2">
                <VariableInput
                  value={data.expression || ''}
                  onChange={(v) => onUpdate({ expression: v })}
                  workflowId={workflowId}
                  multiline
                  rows={3}
                  placeholder={'例:{{ steps.統計.output.負評百分比 | int > 40 }}'}
                  showHint={false}
                />
                <p className="text-[11px] text-gray-400 mt-1">給進階使用者:Jinja2 布林表達式。一般情況用上方「簡易設定」即可,不用碰這裡。</p>
              </div>
            </details>
          </>
        )}

        {/* Switch 模式 */}
        {data.mode === 'switch' && (
          <>
            <SwitchBuilder
              workflowId={workflowId}
              switchValue={data.switch || ''}
              onChange={(expr) => onUpdate({ switch: expr })}
            />

            <div>
              <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">各種情況 → 接著做</label>
              <CasesEditor
                cases={data.cases}
                stepOptions={targetOptions}
                onChange={(c) => onUpdate({ cases: c })}
              />
            </div>

            <div>
              <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">⚙️ 其他情況(都不符合)→ 接著做</label>
              <StepSelect value={data.default} options={targetOptions} onChange={v => onUpdate({ default: v })} />
              <p className="text-[11px] text-gray-400 mt-1">留空 = 都不符合就結束流程</p>
            </div>

            {/* 進階:直接編輯表達式 */}
            <details>
              <summary className="text-[11px] text-gray-400 cursor-pointer select-none hover:text-gray-600">
                進階:直接編輯求值表達式
              </summary>
              <div className="mt-2">
                <VariableInput
                  value={data.switch || ''}
                  onChange={(v) => onUpdate({ switch: v })}
                  workflowId={workflowId}
                  multiline
                  rows={2}
                  placeholder={'例:{{ steps.判定等級.output.等級 }}'}
                  showHint={false}
                />
                <p className="text-[11px] text-gray-400 mt-1">給進階使用者:求值後跟上方各情況的值比對。一般情況用「簡易設定」即可。</p>
              </div>
            </details>
          </>
        )}

        {/* 提示 — 白話、不提術語 */}
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs text-amber-800 leading-relaxed">
          💡 每條分支跑完後,預設會接著往畫布上的下一個步驟走。若要某條分支跑完就直接結束,
          到那個步驟的設定裡把「跑完後」設成「結束流程」。
        </div>
      </div>
    </div>
  )
}

// ── 視覺化條件:運算子 ────────────────────────────────────────────────
type OperatorKey =
  | 'eq' | 'ne'
  | 'gt' | 'gte' | 'lt' | 'lte'
  | 'contains' | 'not_contains'
  | 'truthy' | 'falsy'

const OPERATORS: { key: OperatorKey; label: string; needsValue: boolean }[] = [
  { key: 'eq',           label: '等於',          needsValue: true },
  { key: 'ne',           label: '不等於',        needsValue: true },
  { key: 'gt',           label: '大於',          needsValue: true },
  { key: 'gte',          label: '大於等於',      needsValue: true },
  { key: 'lt',           label: '小於',          needsValue: true },
  { key: 'lte',          label: '小於等於',      needsValue: true },
  { key: 'contains',     label: '包含文字',      needsValue: true },
  { key: 'not_contains', label: '不包含文字',    needsValue: true },
  { key: 'truthy',       label: '有值 / 非空',   needsValue: false },
  { key: 'falsy',        label: '無值 / 空',     needsValue: false },
]

function buildExpression(varPath: string, op: OperatorKey, val: string): string {
  if (!varPath) return ''
  const v = `${varPath}`
  // 自動判斷:全數字 → 不加引號、轉 int 比;否則當字串
  const isNumeric = val.trim() !== '' && /^-?\d+(\.\d+)?$/.test(val.trim())
  const valQuoted = isNumeric ? val.trim() : `"${val.replace(/"/g, '\\"')}"`
  switch (op) {
    case 'eq':
      return isNumeric
        ? `{{ ${v} | int == ${val.trim()} }}`
        : `{{ ${v} == ${valQuoted} }}`
    case 'ne':
      return isNumeric
        ? `{{ ${v} | int != ${val.trim()} }}`
        : `{{ ${v} != ${valQuoted} }}`
    case 'gt':  return `{{ ${v} | int > ${val.trim() || '0'} }}`
    case 'gte': return `{{ ${v} | int >= ${val.trim() || '0'} }}`
    case 'lt':  return `{{ ${v} | int < ${val.trim() || '0'} }}`
    case 'lte': return `{{ ${v} | int <= ${val.trim() || '0'} }}`
    case 'contains':     return `{{ ${valQuoted} in ${v} }}`
    case 'not_contains': return `{{ ${valQuoted} not in ${v} }}`
    case 'truthy': return `{{ ${v} }}`
    case 'falsy':  return `{{ not ${v} }}`
  }
}

// 反解析:把既有的 Jinja2 表達式讀回「變數 / 運算子 / 值」,讓打開面板時
// 簡易設定的下拉能自動填好(使用者看到已設定好的成品、而不是空白)。
function parseExpression(expr: string): { varPath: string; op: OperatorKey; val: string } | null {
  if (!expr) return null
  const m = expr.trim().match(/^\{\{\s*([\s\S]+?)\s*\}\}$/)
  if (!m) return null
  const inner = m[1].trim()
  let mm: RegExpMatchArray | null
  // not <var>  → 無值/空
  mm = inner.match(/^not\s+(.+)$/)
  if (mm) return { varPath: mm[1].trim(), op: 'falsy', val: '' }
  // "文字" in <var>  /  "文字" not in <var>
  mm = inner.match(/^(["'][^"']*["'])\s+(not\s+in|in)\s+(.+)$/)
  if (mm) return {
    varPath: mm[3].trim(),
    op: /not/.test(mm[2]) ? 'not_contains' : 'contains',
    val: mm[1].replace(/^["']|["']$/g, ''),
  }
  // <var> | int <比較> <數字>
  mm = inner.match(/^(.+?)\s*\|\s*int\s*(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)$/)
  if (mm) {
    const map: Record<string, OperatorKey> = { '>': 'gt', '>=': 'gte', '<': 'lt', '<=': 'lte', '==': 'eq', '!=': 'ne' }
    return { varPath: mm[1].trim(), op: map[mm[2]], val: mm[3] }
  }
  // <var> == / != <文字>
  mm = inner.match(/^(.+?)\s*(==|!=)\s*(["'][^"']*["']|\S+)$/)
  if (mm) return {
    varPath: mm[1].trim(),
    op: mm[2] === '==' ? 'eq' : 'ne',
    val: mm[3].replace(/^["']|["']$/g, ''),
  }
  // 裸 <var>  → 有值/非空
  if (inner && !/[<>=|]/.test(inner)) return { varPath: inner.trim(), op: 'truthy', val: '' }
  return null
}

function parseSwitchValue(expr: string): string {
  const m = (expr || '').trim().match(/^\{\{\s*([\s\S]+?)\s*\}\}$/)
  return m ? m[1].trim() : ''
}

// 把變數路徑轉成好讀的中文標籤
function friendlyVarLabel(path: string): string {
  const m = path.match(/^steps\.(.+)\.output\.(.+)$/)
  if (m) return `${m[1]} ▸ ${m[2]}`
  const i = path.match(/^input\.(.+)$/)
  if (i) return `啟動參數:${i[1]}`
  return path
}

// ── 共用 hook:抓 workflow 可用變數、攤平成下拉選項 ──────────────────────
// group:'value' = 這步算出來的值(常用、排前面);'status' = 執行狀態(進階)
type VarOption = { path: string; label: string; detail: string; group: 'value' | 'status' }

// 執行狀態類欄位 → 換成白話標籤(原始 key 是 stdout / exit_code 這種術語)
const STATUS_FIELD_LABEL: Record<string, string> = {
  stdout: '畫面輸出的文字',
  exit_code: '是否執行成功',
  status: '驗證結果',
}

function useVarOptions(workflowId?: string): VarOption[] {
  const [vars, setVars] = useState<WorkflowVariablesResult | null>(null)
  useEffect(() => {
    if (!workflowId) return
    getWorkflowVariables(workflowId).then(setVars).catch(() => {})
  }, [workflowId])
  return useMemo(() => {
    if (!vars) return []
    const out: VarOption[] = []
    for (const s of vars.available.steps) {
      for (const f of s.fields) {
        // path / output.path 是檔案路徑字串,拿去做條件判斷沒意義 —— 不列入
        if (f.key === 'path') continue
        const isStatus = f.source === 'stdout' || f.source === 'exit_code' || f.source === 'validation'
        const fieldLabel = isStatus ? (STATUS_FIELD_LABEL[f.key] ?? f.key) : f.key
        out.push({
          path: `steps.${s.name}.output.${f.key}`,
          label: `${s.name} ▸ ${fieldLabel}`,
          detail: '',
          group: isStatus ? 'status' : 'value',
        })
      }
    }
    for (const i of vars.available.input) {
      out.push({
        path: `input.${i.key}`,
        label: friendlyVarLabel(`input.${i.key}`),
        detail: i.required ? '啟動參數' : '啟動參數(可選)',
        group: 'value',
      })
    }
    return out
  }, [vars])
}

// ── 變數下拉:分「這步算出的值 / 執行狀態(進階)」兩組,常用的排前面 ──────
function VarSelect({ value, options, onChange }: {
  value: string
  options: VarOption[]
  onChange: (v: string) => void
}) {
  const valueOpts = options.filter(o => o.group === 'value')
  const statusOpts = options.filter(o => o.group === 'status')
  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      className="w-full border border-gray-200 rounded-md px-2 py-1 text-xs outline-none focus:border-orange-400 bg-white"
    >
      <option value="">— 請選擇 —</option>
      {valueOpts.length > 0 && (
        <optgroup label="這個步驟算出的值">
          {valueOpts.map(o => (
            <option key={o.path} value={o.path}>{o.label}{o.detail ? ' · ' + o.detail : ''}</option>
          ))}
        </optgroup>
      )}
      {statusOpts.length > 0 && (
        <optgroup label="執行狀態(進階)">
          {statusOpts.map(o => (
            <option key={o.path} value={o.path}>{o.label}</option>
          ))}
        </optgroup>
      )}
    </select>
  )
}

function ConditionBuilder({
  workflowId, expression, onChange,
}: {
  workflowId?: string
  expression: string
  onChange: (expression: string) => void
}) {
  const apiOptions = useVarOptions(workflowId)
  const [varPath, setVarPath] = useState('')
  const [op, setOp] = useState<OperatorKey>('gt')
  const [val, setVal] = useState('')

  // 打開面板時:把現有表達式反解析回下拉(只跑一次)
  const parsedOnce = useRef(false)
  useEffect(() => {
    if (parsedOnce.current) return
    parsedOnce.current = true
    const p = parseExpression(expression)
    if (p) { setVarPath(p.varPath); setOp(p.op); setVal(p.val) }
  }, [expression])

  const currentOp = OPERATORS.find(o => o.key === op)!

  // 下拉清單:API 變數 + 確保「目前選用的變數」一定在清單裡
  // (例如範例的變數要跑過一次才會進 API 清單,但既有表達式已選了它)
  const options = useMemo(() => {
    if (varPath && !apiOptions.some(o => o.path === varPath)) {
      return [{ path: varPath, label: friendlyVarLabel(varPath), detail: '目前選用', group: 'value' as const }, ...apiOptions]
    }
    return apiOptions
  }, [apiOptions, varPath])

  const builtExpr = varPath ? buildExpression(varPath, op, val) : ''
  const canApply = !!varPath && (!currentOp.needsValue || val.trim() !== '')
  const dirty = builtExpr !== expression && canApply

  return (
    <div className="border border-orange-200 bg-orange-50/40 rounded-lg p-3 space-y-2">
      <div className="flex items-center gap-1.5 text-xs font-semibold text-orange-700">
        <Wand2 className="w-3.5 h-3.5" />
        <span>條件設定</span>
      </div>

      <div className="grid grid-cols-1 gap-2">
        {/* 變數 */}
        <div>
          <label className="text-[11px] text-gray-500 block mb-0.5">要判斷的值</label>
          <VarSelect value={varPath} options={options} onChange={setVarPath} />
          {options.length === 0 ? (
            <p className="text-[11px] text-amber-600 mt-1 leading-relaxed">
              ⚠ 目前沒有可選的值。請先把這個條件節點前面的步驟跑過一次,跑完後就能在這裡選到你要判斷的值。
            </p>
          ) : !varPath ? (
            <p className="text-[11px] text-amber-600 mt-1 leading-relaxed">
              💡 想判斷前面步驟算出的數值(百分比、數量…)?那些要先把工作流跑一次,才會出現在這個清單裡。
              可以先跑一次工作流,或用下方「進階」直接輸入。
            </p>
          ) : null}
        </div>

        {/* 比較 + 值 */}
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="text-[11px] text-gray-500 block mb-0.5">比較方式</label>
            <select
              value={op}
              onChange={e => setOp(e.target.value as OperatorKey)}
              className="w-full border border-gray-200 rounded-md px-2 py-1 text-xs outline-none focus:border-orange-400 bg-white"
            >
              {OPERATORS.map(o => (
                <option key={o.key} value={o.key}>{o.label}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="text-[11px] text-gray-500 block mb-0.5">
              比較對象{currentOp.needsValue ? '' : '(不需填)'}
            </label>
            <input
              value={val}
              onChange={e => setVal(e.target.value)}
              disabled={!currentOp.needsValue}
              placeholder={currentOp.needsValue ? '例:40 / 通過' : ''}
              className="w-full border border-gray-200 rounded-md px-2 py-1 text-xs outline-none focus:border-orange-400 bg-white disabled:bg-gray-100 disabled:text-gray-400"
            />
          </div>
        </div>

        {/* 套用 */}
        <div className="flex items-center justify-end gap-2">
          {dirty && <span className="text-[11px] text-orange-600">尚未套用變更</span>}
          <button
            onClick={() => onChange(buildExpression(varPath, op, val))}
            disabled={!canApply}
            className="px-3 py-1 text-xs font-medium rounded-md bg-orange-600 text-white hover:bg-orange-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
          >
            套用條件
          </button>
        </div>
      </div>
    </div>
  )
}

// ── SwitchBuilder — 選一個值當分流依據 ───────────────────────────────────
function SwitchBuilder({
  workflowId, switchValue, onChange,
}: {
  workflowId?: string
  switchValue: string
  onChange: (expression: string) => void
}) {
  const apiOptions = useVarOptions(workflowId)
  const [varPath, setVarPath] = useState('')

  const parsedOnce = useRef(false)
  useEffect(() => {
    if (parsedOnce.current) return
    parsedOnce.current = true
    const v = parseSwitchValue(switchValue)
    if (v) setVarPath(v)
  }, [switchValue])

  const options = useMemo(() => {
    if (varPath && !apiOptions.some(o => o.path === varPath)) {
      return [{ path: varPath, label: friendlyVarLabel(varPath), detail: '目前選用', group: 'value' as const }, ...apiOptions]
    }
    return apiOptions
  }, [apiOptions, varPath])

  const builtExpr = varPath ? `{{ ${varPath} }}` : ''
  const dirty = builtExpr !== switchValue && !!varPath

  return (
    <div className="border border-orange-200 bg-orange-50/40 rounded-lg p-3 space-y-2">
      <div className="flex items-center gap-1.5 text-xs font-semibold text-orange-700">
        <Wand2 className="w-3.5 h-3.5" />
        <span>分流依據</span>
      </div>
      <div>
        <label className="text-[11px] text-gray-500 block mb-0.5">依哪一個值來分流</label>
        <VarSelect value={varPath} options={options} onChange={setVarPath} />
        {options.length === 0 ? (
          <p className="text-[11px] text-amber-600 mt-1 leading-relaxed">
            ⚠ 目前沒有可選的值。請先把這個條件節點前面的步驟跑過一次,跑完後就能在這裡選到你要判斷的值。
          </p>
        ) : !varPath ? (
          <p className="text-[11px] text-amber-600 mt-1 leading-relaxed">
            💡 想依前面步驟算出的數值(分數、等級…)來分流?那些要先把工作流跑一次,才會出現在這個清單裡。
            可以先跑一次工作流,或用下方「進階」直接輸入。
          </p>
        ) : null}
      </div>
      <div className="flex items-center justify-end gap-2">
        {dirty && <span className="text-[11px] text-orange-600">尚未套用變更</span>}
        <button
          onClick={() => onChange(`{{ ${varPath} }}`)}
          disabled={!varPath}
          className="px-3 py-1 text-xs font-medium rounded-md bg-orange-600 text-white hover:bg-orange-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
        >
          套用
        </button>
      </div>
    </div>
  )
}

// ── step 名稱下拉(可選)+ 純文字輸入(打字)的 union ──
function StepSelect({ value, options, onChange }: {
  value: string
  options: string[]
  onChange: (v: string) => void
}) {
  return (
    <div className="flex gap-1.5">
      <input
        list="condition-step-list"
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder="留空 = 結束流程;或選一個步驟"
        className="flex-1 border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-orange-400 focus:ring-1 focus:ring-orange-400/20"
      />
      <datalist id="condition-step-list">
        {options.map(name => <option key={name} value={name} />)}
      </datalist>
    </div>
  )
}

// ── Switch 各情況編輯器 ─────────────────────────────────────────────────
function CasesEditor({ cases, stepOptions, onChange }: {
  cases: Record<string, string>
  stepOptions: string[]
  onChange: (c: Record<string, string>) => void
}) {
  const entries = Object.entries(cases || {})

  const updateKey = (oldKey: string, newKey: string) => {
    if (newKey === oldKey) return
    const next: Record<string, string> = {}
    for (const [k, v] of entries) {
      next[k === oldKey ? newKey : k] = v
    }
    onChange(next)
  }
  const updateVal = (key: string, val: string) => {
    onChange({ ...cases, [key]: val })
  }
  const addCase = () => {
    const nextKey = `情況${entries.length + 1}`
    onChange({ ...cases, [nextKey]: '' })
  }
  const removeCase = (key: string) => {
    const next = { ...cases }
    delete next[key]
    onChange(next)
  }

  return (
    <div className="space-y-1.5">
      {entries.map(([k, v]) => (
        <div key={k} className="flex gap-1.5 items-center">
          <input
            value={k}
            onChange={(e) => updateKey(k, e.target.value)}
            placeholder="值"
            className="w-24 shrink-0 border border-gray-200 rounded-md px-2 py-1 text-xs outline-none focus:border-orange-400"
          />
          <span className="text-xs text-gray-400">→</span>
          <input
            list="condition-step-list"
            value={v}
            onChange={(e) => updateVal(k, e.target.value)}
            placeholder="接著做哪一步"
            className="flex-1 border border-gray-200 rounded-md px-2 py-1 text-xs outline-none focus:border-orange-400"
          />
          <button
            onClick={() => removeCase(k)}
            title="刪除這個情況"
            className="shrink-0 p-1 text-gray-300 hover:text-red-400"
          ><Trash2 className="w-3.5 h-3.5" /></button>
        </div>
      ))}
      <button
        onClick={addCase}
        className="w-full py-1 text-xs text-orange-600 border border-dashed border-orange-300 rounded-md hover:bg-orange-50 transition-colors flex items-center justify-center gap-1"
      ><Plus className="w-3 h-3" /> 加一種情況</button>
    </div>
  )
}
