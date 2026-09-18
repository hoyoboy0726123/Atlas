'use client'
import { useState, useEffect, useMemo } from 'react'
import { X, Trash2, Plug, RefreshCw, ExternalLink } from 'lucide-react'
import Link from 'next/link'
import type { McpData, McpNode } from './_helpers'
import { getMcpServers, type McpServer, type McpTool } from '@/lib/api'
import { VariableButton } from './_variablePicker'

interface Props {
  node: McpNode
  onUpdate: (patch: Partial<McpData>) => void
  onClose: () => void
  onDelete: () => void
  workflowId?: string
}

/** 從 tool 的 input_schema 生一個參數模板(必填欄位、巢狀一層),給使用者一鍵帶入 */
function schemaTemplate(tool: McpTool): string {
  const schema = (tool.input_schema || {}) as Record<string, unknown>
  const props = (schema.properties || {}) as Record<string, Record<string, unknown>>
  const req = new Set((schema.required as string[]) || [])
  const obj: Record<string, unknown> = {}
  for (const [k, spec] of Object.entries(props)) {
    if (!req.has(k) && Object.keys(obj).length >= 4) continue
    const t = spec?.type
    if (t === 'string') obj[k] = ''
    else if (t === 'number' || t === 'integer') obj[k] = 0
    else if (t === 'boolean') obj[k] = false
    else if (t === 'array') {
      const items = (spec?.items || {}) as Record<string, unknown>
      if (items.type === 'object' && items.properties) {
        const inner: Record<string, unknown> = {}
        const ireq = new Set((items.required as string[]) || [])
        for (const [ik, ispec] of Object.entries(items.properties as Record<string, Record<string, unknown>>)) {
          if (!ireq.has(ik) && Object.keys(inner).length >= 4) continue
          inner[ik] = (ispec?.type === 'array') ? [] : (ispec?.type === 'number' || ispec?.type === 'integer') ? 0 : ''
        }
        obj[k] = [inner]
      } else obj[k] = []
    } else obj[k] = {}
  }
  return JSON.stringify(obj, null, 2)
}

export default function McpConfigPanel({ node, onUpdate, onClose, onDelete, workflowId }: Props) {
  const d = node.data
  const [servers, setServers] = useState<McpServer[]>([])
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try { setServers(await getMcpServers()) } catch { /* 後端未起 → 空 */ }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const selServer = useMemo(() => servers.find(s => s.name === d.server), [servers, d.server])
  const selTool = useMemo(() => selServer?.tools_cache.find(t => t.name === d.tool), [selServer, d.tool])

  return (
    <div className="absolute top-0 right-0 h-full w-[380px] bg-white shadow-2xl border-l border-gray-100 flex flex-col z-30 overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 flex items-center gap-2 border-b" style={{ background: '#0f766e' }}>
        <Plug className="w-4 h-4 text-white" />
        <span className="text-white font-semibold text-sm flex-1">MCP 節點</span>
        <button onClick={onDelete} className="text-white/70 hover:text-white" title="刪除節點">
          <Trash2 className="w-4 h-4" />
        </button>
        <button onClick={onClose} className="text-white/70 hover:text-white"><X className="w-4 h-4" /></button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* 名稱 */}
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">步驟名稱</label>
          <input value={d.name} onChange={e => onUpdate({ name: e.target.value })}
            className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-teal-500" />
        </div>

        {/* Server 選擇 */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="text-xs font-medium text-gray-600">MCP Server</label>
            <button onClick={load} className="text-gray-400 hover:text-teal-600" title="重新載入">
              <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
          {servers.length === 0 && !loading ? (
            <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-2.5">
              尚未安裝任何 MCP server。
              <Link href="/settings" className="inline-flex items-center gap-0.5 text-teal-700 underline ml-1">
                去設定頁安裝 <ExternalLink className="w-3 h-3" />
              </Link>
            </div>
          ) : (
            <select value={d.server} onChange={e => onUpdate({ server: e.target.value, tool: '', toolArgsText: '' })}
              className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-teal-500 bg-white">
              <option value="">— 選擇 server —</option>
              {servers.map(s => (
                <option key={s.name} value={s.name}>{s.name}({s.tools_cache.length} tools)</option>
              ))}
            </select>
          )}
        </div>

        {/* Tool 選擇 */}
        {selServer && (
          <div>
            <label className="text-xs font-medium text-gray-600 block mb-1">Tool</label>
            <select value={d.tool} onChange={e => {
              const toolName = e.target.value
              const t = selServer.tools_cache.find(x => x.name === toolName)
              // 換 tool 時:若參數區還是空的,自動帶入該 tool 的參數模板
              const patch: Partial<McpData> = { tool: toolName }
              if (t && !(d.toolArgsText || '').trim()) patch.toolArgsText = schemaTemplate(t)
              onUpdate(patch)
            }}
              className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-teal-500 bg-white font-mono">
              <option value="">— 選擇 tool —</option>
              {selServer.tools_cache.map(t => (
                <option key={t.name} value={t.name}>{t.name}</option>
              ))}
            </select>
            {selTool && (
              <p className="text-[11px] text-gray-400 mt-1 leading-snug">
                {(selTool.description || '').split('\n')[0].slice(0, 140)}
              </p>
            )}
          </div>
        )}

        {/* 參數 */}
        {d.tool && (
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-medium text-gray-600">Tool 參數(JSON 或 YAML,值可用變數)</label>
              <div className="flex items-center gap-1.5">
                {selTool && (
                  <button onClick={() => onUpdate({ toolArgsText: schemaTemplate(selTool) })}
                    className="text-[11px] text-teal-700 hover:underline">帶入模板</button>
                )}
                <VariableButton workflowId={workflowId}
                  onPick={(v: string) => onUpdate({ toolArgsText: (d.toolArgsText || '') + v })} />
              </div>
            </div>
            <textarea value={d.toolArgsText}
              onChange={e => onUpdate({ toolArgsText: e.target.value })}
              rows={8} spellCheck={false}
              placeholder={'{"path": "C:/data/x.txt"}\n值可用 {{ input.x }} / {{ steps.上一步.output.path }}'}
              className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-xs outline-none focus:border-teal-500 font-mono leading-relaxed" />
          </div>
        )}

        {/* 輸出路徑 */}
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">輸出檔(結果寫入,JSON 供下游引用)</label>
          <input value={d.outputPath} onChange={e => onUpdate({ outputPath: e.target.value })}
            placeholder="mcp_result.json"
            className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-teal-500 font-mono" />
        </div>

        {/* Timeout */}
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">逾時(秒)</label>
          <input type="number" value={d.timeout}
            onChange={e => onUpdate({ timeout: parseInt(e.target.value) || 60 })}
            className="w-24 border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-teal-500" />
        </div>

        <div className="text-[11px] text-gray-400 bg-gray-50 rounded-lg p-2.5 leading-relaxed">
          🔌 MCP 節點呼叫「設定頁 MCP 專區」安裝的外部工具。結構化結果會存成 JSON,
          下游可用 <code className="font-mono">{'{{ steps.'}{d.name || '此步'}{'​.output.<鍵> }}'}</code> 取值,
          或讓 skill 節點直接讀輸出檔。
        </div>
      </div>
    </div>
  )
}
