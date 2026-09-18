'use client'
import { memo } from 'react'
import { Handle, Position, useEdges, type NodeProps, type Node } from '@xyflow/react'
import { GitBranch, AlertTriangle } from 'lucide-react'
import type { ConditionData } from './_helpers'
import { useRunStatusStore } from './_runStatus'

const STATUS_ICON: Record<string, string> = { idle: '●', running: '⟳', success: '✓', failed: '✗' }
const STATUS_COLOR: Record<string, string> = {
  idle: 'text-white/60', running: 'text-yellow-200 animate-spin', success: 'text-green-200', failed: 'text-red-200',
}

const CONDITION_COLOR = '#f97316'  // 橘色

type ConditionNodeType = Node<ConditionData>

function ConditionNodeComponent({ id, data, selected }: NodeProps<ConditionNodeType>) {
  const runtime = useRunStatusStore(s => s.stepStatuses[data.name])
  const status = runtime?.status ?? 'idle'

  // 「還沒設定判斷條件」偵測:有出線、但 IF 的 expression / Switch 的 switch 是空的。
  // 這種節點後端 runner 會報錯、所以在畫布上提早用紅框 + 警告 icon 標記出來。
  const edges = useEdges()
  const hasOutgoing = edges.some(e => e.source === id)
  const conditionMissing = hasOutgoing && (
    data.mode === 'if' ? !data.expression?.trim() : !data.switch?.trim()
  )

  const color = conditionMissing ? '#ef4444'
    : status === 'failed' ? '#ef4444'
    : status === 'success' ? '#10b981'
    : status === 'running' ? '#f59e0b'
    : CONDITION_COLOR

  // 簡短描述 — 顯示在 body
  let summary = ''
  if (data.mode === 'if') {
    summary = data.expression
      ? `IF: ${data.expression.slice(0, 30)}${data.expression.length > 30 ? '…' : ''}`
      : '(未設條件)'
  } else {
    const cases = Object.keys(data.cases || {}).length
    summary = data.switch ? `Switch: ${cases} 個 case` : '(未設 switch)'
  }

  return (
    <div className="w-56 rounded-xl overflow-hidden shadow-md transition-shadow"
      style={{
        // 未設定條件 → 永遠紅框(即使沒選取)、讓使用者一眼看到要補設定
        border: (selected || conditionMissing) ? `2px solid ${color}` : '2px solid transparent',
        boxShadow: selected ? `0 0 0 3px ${color}33, 0 4px 16px rgba(0,0,0,0.12)` : '0 2px 8px rgba(0,0,0,0.10)',
      }}
    >
      <Handle type="target" position={Position.Left}
        className="!w-3 !h-3 !rounded-full !border-2 !border-white" style={{ background: color }} />

      {/* Header */}
      <div className="px-3 py-2.5 flex items-center gap-2" style={{ background: color }}>
        <GitBranch className="w-3.5 h-3.5 text-white shrink-0" strokeWidth={2.5} />
        <span className="text-white font-semibold text-sm flex-1 truncate leading-tight">{data.name}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/20 text-white font-medium shrink-0">
          {data.mode === 'if' ? 'IF' : 'Switch'}
        </span>
        <span className={`text-sm shrink-0 ${STATUS_COLOR[status]}`}>{STATUS_ICON[status]}</span>
      </div>

      {/* Body */}
      <div className="bg-white px-3 py-2.5 space-y-1">
        {conditionMissing && (
          <div className="flex items-center gap-1 text-[11px] text-red-600 bg-red-50 border border-red-200 rounded px-1.5 py-1 leading-tight">
            <AlertTriangle className="w-3 h-3 shrink-0" strokeWidth={2.5} />
            <span>還沒設定判斷條件,點開設定</span>
          </div>
        )}
        <p className="text-xs text-orange-600 font-mono truncate" title={summary}>
          {summary}
        </p>
        {data.mode === 'if' && (data.onTrue || data.onFalse) && (
          <div className="text-[10px] text-gray-500 leading-tight">
            {data.onTrue && <span>✓ → {data.onTrue}</span>}
            {data.onTrue && data.onFalse && <span className="mx-1">·</span>}
            {data.onFalse && <span>✗ → {data.onFalse}</span>}
          </div>
        )}
        {data.mode === 'switch' && data.default && (
          <div className="text-[10px] text-gray-500 leading-tight">
            default → {data.default}
          </div>
        )}
      </div>

      <Handle type="source" position={Position.Right}
        className="!w-3 !h-3 !rounded-full !border-2 !border-white" style={{ background: color }} />
    </div>
  )
}

export default memo(ConditionNodeComponent)
