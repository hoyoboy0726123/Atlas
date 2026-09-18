'use client'
import { memo } from 'react'
import { Handle, Position, type NodeProps, type Node } from '@xyflow/react'
import { Brain } from 'lucide-react'
import type { SubagentData } from './_helpers'
import { useRunStatusStore } from './_runStatus'

const STATUS_ICON: Record<string, string> = { idle: '●', running: '⟳', success: '✓', failed: '✗' }
const STATUS_COLOR: Record<string, string> = {
  idle: 'text-white/60', running: 'text-yellow-200 animate-spin', success: 'text-green-200', failed: 'text-red-200',
}

const SUBAGENT_COLOR = '#6366f1'  // indigo — 跟 skill 的紫 (#8b5cf6) 區分

const ROLE_LABEL: Record<string, string> = {
  data_analyst: '資料分析師',
  coder: '程式工程師',
  researcher: '研究員',
  critic: '審稿人',
  planner: '規劃師',
}

type SubagentNodeType = Node<SubagentData>

function SubagentStepNode({ data, selected }: NodeProps<SubagentNodeType>) {
  const runtime = useRunStatusStore(s => s.stepStatuses[data.name])
  const status = runtime?.status ?? 'idle'
  const errorMsg = runtime?.errorMsg ?? ''

  const color = status === 'failed' ? '#ef4444'
    : status === 'success' ? '#10b981'
    : status === 'running' ? '#3b82f6'
    : SUBAGENT_COLOR

  const roleLabel = ROLE_LABEL[data.role] || data.role

  return (
    <div className="w-60 rounded-xl overflow-hidden shadow-md transition-shadow"
      style={{
        border: selected ? `2px solid ${color}` : '2px solid transparent',
        boxShadow: selected ? `0 0 0 3px ${color}33, 0 4px 16px rgba(0,0,0,0.12)` : '0 2px 8px rgba(0,0,0,0.10)',
      }}
    >
      <Handle type="target" position={Position.Left}
        className="!w-3 !h-3 !rounded-full !border-2 !border-white" style={{ background: color }} />

      {/* Header */}
      <div className="px-3 py-2.5 flex items-center gap-2" style={{ background: color }}>
        <Brain className="w-3.5 h-3.5 text-white shrink-0" strokeWidth={2.5} />
        <span className="text-white font-semibold text-sm flex-1 truncate leading-tight">{data.name}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/20 text-white font-medium shrink-0">
          🤖 {roleLabel}
        </span>
        <span className={`text-sm shrink-0 ${STATUS_COLOR[status]}`}>{STATUS_ICON[status]}</span>
      </div>

      {/* Body */}
      <div className="bg-white px-3 py-2.5 space-y-1">
        <p className="text-xs text-indigo-600 truncate">
          {data.taskDescription || '尚未設定任務描述'}
        </p>
        <p className="text-[11px] text-gray-400">
          🧠 多輪推理 · 上限 {data.maxIter} 輪
        </p>
        {data.outputPath ? (
          <p className="text-xs text-gray-400 truncate">→ {data.outputPath.replace(/^.*\/([^/]+)$/, '$1')}</p>
        ) : null}
        {(data.expectText || data.jsonSchemaText) ? (
          <p className="text-[11px] text-purple-500 truncate" title={String(data.expectText || '')}>
            🛡 {data.jsonSchemaText ? 'Schema 合約' : ''}{data.jsonSchemaText && data.expectText ? ' + ' : ''}{data.expectText ? 'AI 驗證' : ''}
          </p>
        ) : null}
        {status === 'failed' && errorMsg && <p className="text-xs text-red-500 truncate">{errorMsg}</p>}
      </div>

      <Handle type="source" position={Position.Right}
        className="!w-3 !h-3 !rounded-full !border-2 !border-white" style={{ background: color }} />
    </div>
  )
}

export default memo(SubagentStepNode)
