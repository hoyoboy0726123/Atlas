'use client'
import { useState, useEffect } from 'react'
import { X, FolderOpen, ChevronDown, ChevronUp, Brain } from 'lucide-react'
import type { SubagentData, SubagentNode } from './_helpers'
import { fsBrowse, listSubagentRoles, type SubagentRole } from '@/lib/api'
import { toast } from 'sonner'
import { VariableButton } from './_variablePicker'
import LlmRoleSelector from './_llmRoleSelector'

// ── File Browser Modal（沿用 skillPanel 的設計）────────────────────────────────
interface BrowseItem { name: string; is_dir: boolean; path: string }

function FileBrowser({ onSelect, onClose }: { onSelect: (p: string) => void; onClose: () => void }) {
  const [currentPath, setCurrentPath] = useState('~')
  const [items, setItems] = useState<BrowseItem[]>([])
  const [loading, setLoading] = useState(false)
  const [manualPath, setManualPath] = useState('')

  const browse = async (p: string) => {
    setLoading(true)
    try {
      const data = await fsBrowse(p)
      setItems(data.items ?? [])
      setCurrentPath(data.path ?? p)
    } catch { toast.error('瀏覽失敗') }
    finally { setLoading(false) }
  }

  useEffect(() => { browse('~') }, [])

  const crumbs = currentPath.replace(/^\/Users\/[^/]+/, '~').split('/').filter(Boolean)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-2xl w-[480px] max-h-[70vh] flex flex-col">
        <div className="flex items-center justify-between px-4 py-3 border-b">
          <span className="font-semibold text-sm text-gray-700">選擇檔案</span>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X className="w-4 h-4" /></button>
        </div>
        <div className="flex items-center gap-1 px-4 py-2 text-xs text-gray-500 flex-wrap border-b bg-gray-50">
          {crumbs.map((c, i) => (
            <span key={i} className="flex items-center gap-1">
              {i > 0 && <span className="text-gray-300">/</span>}
              <button onClick={() => browse('/' + crumbs.slice(0, i + 1).join('/').replace(/^~/, '~'))}
                className="hover:text-indigo-600 transition-colors">{c}</button>
            </span>
          ))}
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
          {loading && <p className="text-center text-gray-400 py-4 text-sm">載入中…</p>}
          {!loading && items.length === 0 && <p className="text-center text-gray-400 py-4 text-sm">（空目錄）</p>}
          {!loading && items.map(item => (
            <button key={item.path} onClick={() => item.is_dir ? browse(item.path) : onSelect(item.path)}
              className="w-full flex items-center gap-2 px-3 py-1.5 rounded-lg hover:bg-indigo-50 text-left transition-colors">
              <span className="text-base">{item.is_dir ? '📁' : '📄'}</span>
              <span className="text-sm text-gray-700 truncate flex-1">{item.name}</span>
              {item.is_dir && <span className="text-xs text-gray-400 shrink-0">›</span>}
            </button>
          ))}
        </div>
        <div className="border-t p-3 space-y-2">
          <div className="flex gap-2">
            <input value={manualPath} onChange={e => setManualPath(e.target.value)} placeholder="手動輸入路徑…"
              className="flex-1 border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-indigo-400 font-mono" />
            <button onClick={() => manualPath && onSelect(manualPath)}
              className="px-3 py-1.5 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700 transition-colors">確認</button>
          </div>
          <button onClick={() => onSelect(currentPath)}
            className="w-full py-1.5 border border-gray-200 rounded-lg text-sm text-gray-600 hover:bg-gray-50 transition-colors"
          >使用目前目錄：{currentPath}</button>
        </div>
      </div>
    </div>
  )
}

// ── 角色資訊（前端硬編、跟 backend/subagent_roles.yaml 對齊）─────────────────
const ROLE_INFO: Record<string, { label: string; tools: string[]; hint: string }> = {
  data_analyst: {
    label: '資料分析師',
    tools: ['run_python', 'read_file', 'web_search', 'done'],
    hint: '處理 csv/xlsx、產 markdown/xlsx/png、可查網路找 API 用法',
  },
  coder: {
    label: '程式工程師',
    tools: ['run_python', 'run_shell', 'read_file', 'web_search', 'done'],
    hint: '寫 Python script、跑驗證、debug 到通；查 API / error 解法',
  },
  researcher: {
    label: '研究員',
    tools: ['web_search', 'read_file', 'run_python', 'done'],
    hint: '純收料 + 產 markdown 摘要、不下決策、列 trade-off',
  },
  critic: {
    label: '審稿人',
    tools: ['read_file', 'done'],
    hint: '純唯讀、挑 3 個最重要的問題、不建議怎麼改',
  },
  planner: {
    label: '規劃師',
    tools: ['done'],
    hint: '純推理、把模糊大任務拆成可執行步驟、產計畫 markdown',
  },
}

const SUBAGENT_COLOR = '#6366f1'

interface Props {
  node: SubagentNode
  onUpdate: (data: Partial<SubagentData>) => void
  onClose: () => void
  onDelete: () => void
  workflowId?: string
}

export default function SubagentConfigPanel({ node, onUpdate, onClose, onDelete, workflowId }: Props) {
  const data = node.data
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [browserTarget, setBrowserTarget] = useState<'output' | 'workingDir' | null>(null)

  // 從後端動態抓所有 role(內建 + 自訂)、把 hardcoded ROLE_INFO 當 fallback
  const [apiRoles, setApiRoles] = useState<SubagentRole[]>([])
  useEffect(() => {
    listSubagentRoles().then(r => setApiRoles(r.roles)).catch(() => {})
  }, [])

  // role 顯示資訊:優先用後端拿到的、找不到才退到內建 hardcode、再不到才退 data_analyst
  const _apiRole = apiRoles.find(r => r.role_id === data.role)
  const roleInfo = _apiRole
    ? { label: _apiRole.label, tools: _apiRole.tools, hint: _apiRole.description }
    : (ROLE_INFO[data.role] || ROLE_INFO.data_analyst)

  const inputCls = 'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400/20 bg-white'

  return (
    <>
      {browserTarget && (
        <FileBrowser
          onSelect={path => {
            if (browserTarget === 'output') onUpdate({ outputPath: path })
            else onUpdate({ workingDir: path })
            setBrowserTarget(null)
          }}
          onClose={() => setBrowserTarget(null)}
        />
      )}

      <div className="absolute top-0 right-0 h-full w-[380px] bg-white shadow-2xl border-l border-gray-100 flex flex-col z-30 overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-3.5 border-b" style={{ borderTopColor: SUBAGENT_COLOR, borderTopWidth: 3 }}>
          <span className="w-8 h-8 rounded-full flex items-center justify-center text-white shrink-0"
            style={{ background: SUBAGENT_COLOR }}><Brain className="w-4 h-4" strokeWidth={2.4} /></span>
          <div className="flex-1 min-w-0">
            <span className="font-semibold text-gray-800 text-sm block truncate">AI 多輪代理節點</span>
            <span className="text-xs text-gray-400">指派 AI 角色多輪推理、自主選擇工具完成任務、不存 Recipe</span>
          </div>
          <button onClick={onDelete} title="刪除" className="text-gray-300 hover:text-red-400 transition-colors p-1">🗑</button>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors"><X className="w-4 h-4" /></button>
        </div>

        {/* Fields */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {/* Name */}
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">步驟名稱</label>
            <input value={data.name} onChange={e => onUpdate({ name: e.target.value })}
              className={`${inputCls} font-mono`} placeholder="描述這個步驟的功能" />
          </div>

          {/* Role */}
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">角色</label>
            <select
              value={data.role}
              onChange={e => onUpdate({ role: e.target.value })}
              className={`${inputCls} font-mono`}
            >
              {/* 後端有回 → 用後端清單(含自訂);沒回 → 退到 hardcode 5 個內建 */}
              {(apiRoles.length > 0 ? apiRoles : Object.entries(ROLE_INFO).map(([k, v]) => ({
                role_id: k, label: v.label, is_builtin: true,
              } as Pick<SubagentRole, 'role_id' | 'label' | 'is_builtin'>))).map(r => (
                <option key={r.role_id} value={r.role_id}>
                  {r.label}({r.role_id}){r.is_builtin ? '' : ' • 自訂'}
                </option>
              ))}
            </select>
            <div className="mt-2 p-2.5 rounded-lg bg-indigo-50 border border-indigo-100 text-xs leading-relaxed">
              <p className="text-indigo-700 mb-1.5">{roleInfo.hint}</p>
              <p className="text-[11px] text-indigo-500/80">
                可用工具：
                <span className="font-mono">{roleInfo.tools.join(' · ')}</span>
              </p>
            </div>
          </div>

          {/* Task Description */}
          <div>
            <div className="flex items-end justify-between mb-2">
              <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide">任務描述</label>
              <VariableButton
                workflowId={workflowId}
                onPick={(p) => onUpdate({ taskDescription: `${data.taskDescription || ''}{{ ${p} }}` })}
              />
            </div>
            <textarea
              rows={7}
              value={data.taskDescription}
              onChange={e => onUpdate({ taskDescription: e.target.value })}
              onWheel={(e) => {
                const el = e.currentTarget
                const atTop = el.scrollTop === 0
                const atBot = el.scrollTop + el.clientHeight >= el.scrollHeight - 1
                if ((!atTop && e.deltaY < 0) || (!atBot && e.deltaY > 0)) e.stopPropagation()
              }}
              placeholder={'用自然語言描述任務、subagent 會自主決定如何用工具完成…\n例如:讀 {{ steps.fetch.output.path }}、找出 Q1 環比下滑最嚴重的 3 個品類'}
              className={`${inputCls} resize-y font-mono text-xs leading-relaxed min-h-[120px]`}
            />
            <p className="text-xs text-gray-400 mt-1.5">
              Subagent 會多輪推理、按需使用配發的工具完成任務(支援 <code className="bg-gray-100 px-1 rounded font-mono">{`{{ }}`}</code> 變數)
            </p>
          </div>

          {/* Max Iterations */}
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">最多輪數 (max_iter)</label>
            <input
              type="number"
              min={1}
              max={10}
              value={data.maxIter}
              onChange={e => onUpdate({ maxIter: Math.max(1, Math.min(10, parseInt(e.target.value) || 5)) })}
              className={inputCls}
            />
            <p className="text-xs text-gray-400 mt-1">
              超過上限會視為失敗(沿用 runner retry 機制)。建議 3-5 輪、複雜任務 8-10 輪。
            </p>
          </div>

          <LlmRoleSelector
            value={data.llmRole || 'primary'}
            onChange={(v) => onUpdate({ llmRole: v } as any)}
          />

          {/* Output path */}
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">輸出路徑（選填）</label>
            <div className="flex gap-1.5">
              <input value={data.outputPath} onChange={e => onUpdate({ outputPath: e.target.value })}
                placeholder="ai_output/...（提示給 subagent 知道要寫到哪）" className={`${inputCls} font-mono flex-1`} />
              <button onClick={() => setBrowserTarget('output')}
                className="shrink-0 w-8 h-8 flex items-center justify-center border border-gray-200 rounded-lg hover:bg-indigo-50 text-gray-400 hover:text-indigo-600 transition-colors">
                <FolderOpen className="w-3.5 h-3.5" /></button>
            </div>
            <p className="text-xs text-gray-400 mt-1">
              填了會把路徑寫進 prompt 提示 subagent；留空 subagent 自己決定（可能在 sandbox 暫存）
            </p>
          </div>

          {/* 驗證閘：expect + JSON Schema（對應 YAML output.expect / output.json_schema） */}
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">預期輸出描述（AI 驗證、選填）</label>
            <textarea
              rows={3}
              value={data.expectText || ''}
              onChange={e => onUpdate({ expectText: e.target.value })}
              placeholder={'描述「怎樣才算做完做對」…\n例如：輸入幾筆輸出就幾筆、無遺漏'}
              className={`${inputCls} resize-y text-xs leading-relaxed min-h-[60px]`}
            />
            <p className="text-xs text-gray-400 mt-1">
              <b>填了</b> → 跑完會深度驗證產出檔（適合「全部/每一筆」可數任務）。<b>研究/探索型請留空</b>，避免誤殺正常輸出。
            </p>
          </div>
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">JSON Schema 合約（選填）</label>
            <textarea
              rows={3}
              value={data.jsonSchemaText || ''}
              onChange={e => onUpdate({ jsonSchemaText: e.target.value })}
              placeholder={'輸出是 .json 時填標準 JSON Schema…\n例：{"type":"array","items":{"type":"object","required":["name","price"]}}'}
              className={`${inputCls} resize-y font-mono text-xs leading-relaxed min-h-[60px]`}
            />
            <p className="text-xs text-gray-400 mt-1">
              0-token 確定性驗證：結構不對直接 fail 並回報具體欄位錯誤，比 AI 驗證更硬、更省。
            </p>
          </div>

          {/* 提醒：跟 AI 技能節點的差別 */}
          <div className="p-3 rounded-xl bg-amber-50 border border-amber-200 text-xs leading-relaxed">
            <div className="font-medium text-amber-800 mb-1">⚠️ AI 多輪代理 vs AI 技能節點</div>
            <ul className="text-amber-700 space-y-0.5 ml-4 list-disc">
              <li>每次都重新 LLM 推理、<b>沒有 Recipe 快取</b>（多輪結果非確定性）</li>
              <li>跳過外部 AI 驗證（loop 內已自我驗證）</li>
              <li>消耗 token 比 AI 技能多 2-5 倍</li>
              <li>適合：探索性、結構不固定、要邊想邊改的任務（研究、debug、寫稿循環）</li>
              <li>不適合：每天跑相同邏輯的固定任務 → 改用 AI 技能 + Recipe</li>
            </ul>
          </div>

          {/* Advanced */}
          <div>
            <button onClick={() => setShowAdvanced(!showAdvanced)}
              className="flex items-center gap-1.5 text-xs font-semibold text-gray-500 uppercase tracking-wide hover:text-indigo-600 transition-colors">
              {showAdvanced ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
              進階設定
            </button>
            {showAdvanced && (
              <div className="mt-3 space-y-3 pl-4 border-l-2 border-indigo-100">
                <div className="flex gap-3">
                  <div className="flex-1">
                    <label className="text-xs text-gray-500 block mb-1">逾時（秒）</label>
                    <input type="number" min={10} max={3600} value={data.timeout}
                      onChange={e => onUpdate({ timeout: parseInt(e.target.value) || 600 })} className={inputCls} />
                  </div>
                  <div className="flex-1">
                    <label className="text-xs text-gray-500 block mb-1">自動重試次數</label>
                    <input type="number" min={0} max={5} value={data.retry}
                      onChange={e => onUpdate({ retry: parseInt(e.target.value) || 0 })} className={inputCls} />
                  </div>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">工作目錄</label>
                  <div className="flex gap-1.5">
                    <input value={data.workingDir} onChange={e => onUpdate({ workingDir: e.target.value })}
                      placeholder="（留空 = 使用預設目錄）" className={`${inputCls} font-mono flex-1`} />
                    <button onClick={() => setBrowserTarget('workingDir')}
                      className="shrink-0 w-8 h-8 flex items-center justify-center border border-gray-200 rounded-lg hover:bg-indigo-50 text-gray-400 hover:text-indigo-600 transition-colors">
                      <FolderOpen className="w-3.5 h-3.5" /></button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="p-4 border-t bg-indigo-50">
          <div className="flex items-center justify-between text-xs text-gray-400">
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full" style={{ background: SUBAGENT_COLOR }} />
              AI 多輪代理節點
            </span>
            <span className={`px-2 py-0.5 rounded-full font-medium ${
              data.status === 'success' ? 'bg-green-100 text-green-700' :
              data.status === 'failed'  ? 'bg-red-100 text-red-700' :
              data.status === 'running' ? 'bg-blue-100 text-blue-700' :
              'bg-gray-100 text-gray-500'
            }`}>
              {data.status === 'idle' ? '等待中' : data.status === 'running' ? '執行中' : data.status === 'success' ? '成功' : '失敗'}
            </span>
          </div>
        </div>
      </div>
    </>
  )
}
