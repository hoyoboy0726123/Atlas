'use client'
import { useState, useEffect } from 'react'
import { X, FolderOpen, ChevronDown, ChevronUp, Code2 } from 'lucide-react'
import type { StepData, ScriptNode } from './_helpers'
import { fsBrowse, fsCheckVenv, fsNativePick } from '@/lib/api'
import { toast } from 'sonner'
import { VariableInput } from './_variablePicker'

// ── 執行前綴 ─────────────────────────────────────────────────────────────────
// 下拉顯示用的選項（命名以 Windows 慣例的 `venv/` 為主；勾選虛擬環境時會依
// 後端實際找到的資料夾名動態設定，支援 venv/ 與 .venv/ 兩種慣例）
const EXEC_PREFIXES = [
  { label: 'python',                           value: 'python',                platform: 'cross' },
  { label: 'python3',                          value: 'python3',               platform: 'cross' },
  { label: 'node',                             value: 'node',                  platform: 'cross' },
  { label: 'npx',                              value: 'npx',                   platform: 'cross' },
  { label: '直接執行（不加前綴）',               value: '',                      platform: 'cross' },
  { label: 'venv/bin/python (venv)',           value: 'venv/bin/python',        platform: 'unix' },
  { label: 'bash',                             value: 'bash',                  platform: 'unix' },
  { label: 'sh',                               value: 'sh',                   platform: 'unix' },
  { label: 'py (Windows Launcher)',            value: 'py',                    platform: 'win' },
  { label: 'py -3 (Windows Py3)',              value: 'py -3',                 platform: 'win' },
  { label: 'venv\\Scripts\\python (Win venv)',  value: 'venv\\Scripts\\python',  platform: 'win' },
  { label: 'cmd /c',                           value: 'cmd /c',               platform: 'win' },
  { label: 'powershell -File',                 value: 'powershell -File',      platform: 'win' },
]

// 解析 batch 字串用的額外前綴（向下相容舊存檔裡的 `.venv/...`）
// 不顯示在下拉選單，只用於 splitBatch 還原前綴
const LEGACY_PREFIXES = [
  { value: '.venv/bin/python' },
  { value: '.venv\\Scripts\\python' },
]

function splitBatch(batch: string): { prefix: string; filePath: string } {
  // 包含 legacy 的 `.venv/...` 讓舊存檔還是能解析出前綴
  const all = [...EXEC_PREFIXES, ...LEGACY_PREFIXES]
  const sorted = all.sort((a, b) => b.value.length - a.value.length)
  for (const p of sorted) {
    if (p.value && batch.startsWith(p.value + ' '))
      return { prefix: p.value, filePath: batch.slice(p.value.length + 1).trim() }
  }
  return { prefix: '', filePath: batch }
}

// ── File Browser Modal ────────────────────────────────────────────────────────
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
              <button onClick={() => browse('/' + crumbs.slice(0, i + 1).join('/').replace(/^~/, `~`))}
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

// ── ScriptConfigPanel ─────────────────────────────────────────────────────────
interface Props {
  node: ScriptNode
  onUpdate: (data: Partial<StepData>) => void
  onClose: () => void
  onDelete: () => void
  aiExpectText?: string
  workflowId?: string
}

export default function ScriptConfigPanel({ node, onUpdate, onClose, onDelete, aiExpectText, workflowId }: Props) {
  const data = node.data
  const color = '#3b82f6'
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [browserTarget, setBrowserTarget] = useState<'batch' | 'output' | null>(null)
  const [venvChecking, setVenvChecking] = useState(false)

  const { prefix: initPrefix } = splitBatch(data.batch)
  const [selectedPrefix, setSelectedPrefix] = useState(initPrefix || 'python')

  useEffect(() => {
    const { prefix } = splitBatch(data.batch)
    setSelectedPrefix(prefix || 'python')
  }, [node.id])

  const upd = (patch: Partial<StepData>) => onUpdate(patch)
  const { filePath } = splitBatch(data.batch)
  // 同時認 venv 與 .venv 兩種慣例
  const isUsingVenv = /(?:^|[\\\/])\.?venv[\\\/]/.test(data.batch) && data.batch.includes('python')
  const pyPathMatch = data.batch.match(/(?:python\S*|\.?venv[/\\]\S*python\S*)\s+(\S+\.py)/)
  const pyPath = pyPathMatch?.[1] ?? null

  const handleVenvToggle = async (checked: boolean) => {
    if (!pyPath) return
    const sep = pyPath.includes('\\') ? '\\' : '/'
    const scriptDir = pyPath.substring(0, pyPath.lastIndexOf(sep))
    if (!checked) {
      const fallback = /venv/.test(selectedPrefix) ? 'python' : selectedPrefix || 'python'
      upd({ batch: `${fallback} ${pyPath}` }); setSelectedPrefix(fallback); return
    }
    setVenvChecking(true)
    try {
      const res = await fsCheckVenv(scriptDir)
      if (res.has_venv && res.python_path) {
        upd({ batch: `${res.python_path} ${pyPath}` })
        // 後端回傳的 venv_dir_name 可能是 "venv" 或 ".venv" → 用實際的當前綴顯示
        const isWin = res.python_path.includes('Scripts')
        const dirName = res.venv_dir_name || 'venv'
        setSelectedPrefix(isWin ? `${dirName}\\Scripts\\python` : `${dirName}/bin/python`)
        toast.success(`已切換為虛擬環境 Python（${dirName}/）`)
      } else {
        toast.error('找不到 venv 或 .venv，請先在專案目錄建立虛擬環境', { duration: 8000 })
      }
    } catch { toast.error('檢查虛擬環境失敗') }
    finally { setVenvChecking(false) }
  }

  const inputCls = 'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400/20 bg-white font-mono'

  // 套用選到的路徑(原生對話框與內建 modal 共用)
  const applyPickedPath = (path: string, target: 'batch' | 'output') => {
    if (target === 'batch') {
      let prefix = selectedPrefix
      if (path.endsWith('.sh'))                                { prefix = 'bash';             setSelectedPrefix('bash') }
      else if (path.endsWith('.bat') || path.endsWith('.cmd')) { prefix = 'cmd /c';           setSelectedPrefix('cmd /c') }
      else if (path.endsWith('.ps1'))                          { prefix = 'powershell -File'; setSelectedPrefix('powershell -File') }
      else if (path.endsWith('.js') || path.endsWith('.mjs'))  { prefix = 'node';             setSelectedPrefix('node') }
      else if (path.endsWith('.py') && !prefix)                { prefix = 'python';           setSelectedPrefix('python') }
      upd({ batch: prefix ? `${prefix} ${path}` : path })
    } else {
      upd({ outputPath: path })
    }
  }

  // 點資料夾鈕:先開 OS 原生對話框(Windows 檔案總管);原生不可用才 fallback 內建瀏覽 modal
  const openPicker = async (target: 'batch' | 'output') => {
    try {
      const r = await fsNativePick(
        target === 'batch'
          ? { mode: 'open', py_only: true }
          : { mode: 'save', default_name: 'output.json' }
      )
      if (r.path) { applyPickedPath(r.path, target); return }
      if (r.error) { setBrowserTarget(target); return }   // 原生不可用 → fallback
      // path=null 且無 error = 使用者按取消 → 不動作
    } catch {
      setBrowserTarget(target)
    }
  }

  return (
    <>
      {browserTarget && (
        <FileBrowser
          onSelect={path => {
            applyPickedPath(path, browserTarget)
            setBrowserTarget(null)
          }}
          onClose={() => setBrowserTarget(null)}
        />
      )}

      <div className="absolute top-0 right-0 h-full w-[380px] bg-white shadow-2xl border-l border-gray-100 flex flex-col z-30 overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-3.5 border-b" style={{ borderTopColor: color, borderTopWidth: 3 }}>
          <span className="w-8 h-8 rounded-full flex items-center justify-center text-white shrink-0"
            style={{ background: color }}><Code2 className="w-4 h-4" strokeWidth={2.4} /></span>
          <div className="flex-1 min-w-0">
            <span className="font-semibold text-gray-800 text-sm block truncate">Python腳本節點</span>
            <span className="text-xs text-gray-400">執行你寫好的腳本或 Shell 指令</span>
          </div>
          <button onClick={onDelete} title="刪除" className="text-gray-300 hover:text-red-400 transition-colors p-1">🗑</button>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors"><X className="w-4 h-4" /></button>
        </div>

        {/* Fields */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {/* Name */}
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">步驟名稱</label>
            <input value={data.name} onChange={e => upd({ name: e.target.value })} className={inputCls} placeholder="描述這個步驟的功能" />
          </div>

          {/* Command */}
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-2">執行指令</label>
            <select value={selectedPrefix}
              onChange={e => { const p = e.target.value; setSelectedPrefix(p); if (filePath) upd({ batch: p ? `${p} ${filePath}` : filePath }) }}
              className="w-full border border-gray-200 rounded-lg px-2 py-1.5 text-xs bg-white text-gray-700 outline-none focus:border-indigo-400 cursor-pointer mb-1.5">
              <optgroup label="跨平台">
                {EXEC_PREFIXES.filter(p => p.platform === 'cross').map((p, i) => <option key={`c-${i}`} value={p.value}>{p.label}</option>)}
              </optgroup>
              <optgroup label="macOS / Linux">
                {EXEC_PREFIXES.filter(p => p.platform === 'unix').map((p, i) => <option key={`u-${i}`} value={p.value}>{p.label}</option>)}
              </optgroup>
              <optgroup label="Windows">
                {EXEC_PREFIXES.filter(p => p.platform === 'win').map((p, i) => <option key={`w-${i}`} value={p.value}>{p.label}</option>)}
              </optgroup>
            </select>
            <div className="flex gap-1.5 mb-1.5">
              <div className="flex-1">
                <VariableInput
                  value={splitBatch(data.batch).filePath}
                  onChange={(v) => upd({ batch: selectedPrefix ? `${selectedPrefix} ${v}` : v })}
                  workflowId={workflowId}
                  multiline
                  rows={1}
                  placeholder="選擇或輸入腳本路徑、可含 {{ input.X }} 等變數"
                  showHint={false}
                />
              </div>
              <button onClick={() => openPicker('batch')}
                className="shrink-0 w-8 h-8 flex items-center justify-center border border-gray-200 rounded-lg hover:bg-indigo-50 text-gray-400 hover:text-indigo-600 transition-colors mt-7">
                <FolderOpen className="w-3.5 h-3.5" /></button>
            </div>
            {data.batch && (
              <div className="text-xs text-gray-400 font-mono bg-gray-50 rounded-lg px-2.5 py-1.5 break-all">▶ {data.batch}</div>
            )}
            {pyPath && (
              <>
                <label className="flex items-center gap-2 mt-2 cursor-pointer select-none">
                  <input type="checkbox" checked={isUsingVenv} onChange={e => handleVenvToggle(e.target.checked)}
                    disabled={venvChecking} className="w-3.5 h-3.5 rounded accent-indigo-500" />
                  <span className="text-xs text-gray-500">{venvChecking ? '偵測中…' : '使用虛擬環境（自動偵測 venv/ 或 .venv/）'}</span>
                </label>
                <p className="text-[11px] text-gray-400 mt-1 pl-5.5 leading-snug">
                  {isUsingVenv
                    ? '已用專案自帶的虛擬環境執行,依賴最齊全。'
                    : '未勾 = 用系統全域 Python 執行(不會用本工具的環境)。若腳本有特殊依賴,建議勾選專案自帶的 venv,否則缺套件會直接失敗。'}
                </p>
              </>
            )}
          </div>

          {/* Output path */}
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">輸出路徑</label>
            <div className="flex gap-1.5">
              <div className="flex-1">
                <VariableInput
                  value={data.outputPath || ''}
                  onChange={(v) => upd({ outputPath: v })}
                  workflowId={workflowId}
                  multiline
                  rows={1}
                  placeholder="~/ai_output/...、可含 {{ input.date }}"
                  showHint={false}
                />
              </div>
              <button onClick={() => openPicker('output')}
                className="shrink-0 w-8 h-8 flex items-center justify-center border border-gray-200 rounded-lg hover:bg-indigo-50 text-gray-400 hover:text-indigo-600 transition-colors mt-7">
                <FolderOpen className="w-3.5 h-3.5" /></button>
            </div>
            <p className="text-xs text-gray-400 mt-1">Pipeline 用此路徑確認步驟是否成功執行</p>
          </div>

          {/* Advanced */}
          <div>
            <button onClick={() => setShowAdvanced(!showAdvanced)}
              className="flex items-center gap-1.5 text-xs font-semibold text-gray-500 uppercase tracking-wide hover:text-indigo-600 transition-colors">
              {showAdvanced ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
              進階設定
            </button>
            {showAdvanced && (
              <div className="mt-3 space-y-3 pl-4 border-l-2 border-gray-100">
                <div className="flex gap-3">
                  <div className="flex-1">
                    <label className="text-xs text-gray-500 block mb-1">逾時（秒）</label>
                    <input type="number" min={10} max={3600} value={data.timeout}
                      onChange={e => upd({ timeout: parseInt(e.target.value) || 300 })} className={inputCls} />
                  </div>
                  <div className="flex-1">
                    <label className="text-xs text-gray-500 block mb-1">自動重試次數</label>
                    <input type="number" min={0} max={5} value={data.retry}
                      onChange={e => upd({ retry: parseInt(e.target.value) || 0 })} className={inputCls} />
                  </div>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">期望輸出描述</label>
                  {aiExpectText ? (
                    <div className="w-full border border-amber-200 bg-amber-50 rounded-lg px-2.5 py-1.5 text-xs font-mono text-amber-700 leading-relaxed">
                      <span className="text-amber-400 mr-1">✓</span>{aiExpectText}
                    </div>
                  ) : (
                    <textarea rows={2} value={data.expect} onChange={e => upd({ expect: e.target.value })}
                      placeholder="描述輸出應包含什麼內容…" className={`${inputCls} resize-none`} />
                  )}
                  <p className="text-[11px] text-gray-400 mt-1">
                    <b>填了</b> → AI 用此描述驗證（+5-15s）。<b>留空</b> → 只看 exit code + 檔案存在（你寫的腳本你負責）。
                  </p>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">JSON Schema 合約（選填、輸出是 .json 時）</label>
                  <textarea rows={2} value={(data as any).jsonSchemaText || ''} onChange={e => upd({ jsonSchemaText: e.target.value } as any)}
                    placeholder={'標準 JSON Schema,例:{"type":"object","required":["total"]}'} className={`${inputCls} resize-none font-mono text-xs`} />
                  <p className="text-[11px] text-gray-400 mt-1">
                    0-token 確定性驗證,結構不對直接 fail、先於 AI 驗證執行。
                  </p>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">工作目錄</label>
                  <input value={data.workingDir} onChange={e => upd({ workingDir: e.target.value })}
                    placeholder="（留空 = 使用預設目錄）" className={inputCls} />
                </div>
                {/* 背景模式(GUI / daemon)*/}
                <div className="rounded-lg border border-orange-200 bg-orange-50/50 p-2.5">
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={!!(data as { background?: boolean }).background}
                      onChange={e => upd({ background: e.target.checked } as Partial<typeof data>)}
                      className="mt-0.5 w-3.5 h-3.5 rounded accent-orange-500"
                    />
                    <div className="flex-1">
                      <div className="text-xs font-semibold text-orange-700">🚀 背景模式(這支腳本不會結束)</div>
                      <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">
                        勾起來:啟動後**不等**腳本結束、立刻下一步、subprocess 留著。
                        適合開 GUI / server / daemon 等永遠不退出的進程,讓後續節點可以對它做自動化。
                        Workflow 跑完預設會自動 kill(下方可改為保留)。
                      </p>
                    </div>
                  </label>
                  {(data as { background?: boolean }).background && (
                    <div className="mt-2 pl-5 space-y-2">
                      <label className="flex items-start gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={(data as { background_keep?: boolean }).background_keep !== false}
                          onChange={e => upd({ background_keep: e.target.checked } as Partial<typeof data>)}
                          className="mt-0.5 w-3.5 h-3.5 rounded accent-orange-500"
                        />
                        <div className="flex-1">
                          <div className="text-[11px] font-semibold text-orange-700">🛡️ 保留進程、結束不自動 kill(預設開)</div>
                          <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">
                            預設勾選:Workflow 跑完後**不**關閉這個 GUI / 進程,留在桌面讓你手動操作。
                            取消勾選 → 結束時自動 kill。(手動中止 abort 一律強制關閉;保留時進程需自行關閉以釋放資源)
                          </p>
                        </div>
                      </label>
                      <div>
                        <label className="text-[11px] text-gray-500 block mb-0.5">啟動後等幾秒讓它 ready 再下一步(秒,0=不等)</label>
                        <input
                          type="number"
                          min={0}
                          max={120}
                          value={(data as { readyAfterSeconds?: number }).readyAfterSeconds ?? 0}
                          onChange={e => upd({ readyAfterSeconds: parseInt(e.target.value) || 0 } as Partial<typeof data>)}
                          className={`${inputCls} font-mono`}
                        />
                      </div>
                    </div>
                  )}
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">跑完跳到(next)</label>
                  <input value={data.next || ''} onChange={e => upd({ next: e.target.value })}
                    placeholder="留空=線性下一個 / end=結束 / step name=指定跳"
                    className={`${inputCls} font-mono`} />
                  <p className="text-[11px] text-gray-400 mt-1">
                    用於 condition 分支:branch 跑完寫 <code className="bg-gray-100 px-1 rounded font-mono">end</code> 結束流程、避免線性掉到下個 branch
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="p-4 border-t bg-gray-50">
          <div className="flex items-center justify-between text-xs text-gray-400">
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full" style={{ background: color }} />
              Python腳本節點
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
