'use client'
import { memo } from 'react'
import { Handle, Position, type NodeProps, type Node } from '@xyflow/react'
import { ScanEye } from 'lucide-react'
import type { VisualValidationData } from './_helpers'

type VisualValidationNode = Node<VisualValidationData>

const SOURCE_LABEL: Record<VisualValidationData['source'], string> = {
  prev_output: '上一步輸出檔',
  current_screen: '目前螢幕畫面',
}

function VisualValidationNodeComponent({ data, selected }: NodeProps<VisualValidationNode>) {
  const color = '#6366f1'  // indigo — 跟 AI 驗證的 amber 區分，避免使用者混淆

  return (
    <div
      className="w-56 rounded-xl overflow-hidden shadow-md transition-shadow"
      style={{
        border: selected ? `2px solid ${color}` : '2px solid transparent',
        boxShadow: selected
          ? `0 0 0 3px ${color}33, 0 4px 16px rgba(0,0,0,0.12)`
          : '0 2px 8px rgba(0,0,0,0.10)',
      }}
    >
      <Handle type="target" position={Position.Left}
        className="!w-3 !h-3 !rounded-full !border-2 !border-white" style={{ background: color }} />

      <div className="px-3 py-2 flex items-center gap-2" style={{ background: color }}>
        <ScanEye className="w-3.5 h-3.5 text-white shrink-0" strokeWidth={2.5} />
        <span className="text-white font-semibold text-sm flex-1 truncate">{data.name || '視覺驗證'}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/20 text-white font-medium">VLM</span>
      </div>

      <div className="bg-white px-3 py-2.5 space-y-1">
        <div className="flex items-center gap-1">
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 font-medium">
            {SOURCE_LABEL[data.source]}
          </span>
          {data.source === 'current_screen' && data.searchRegion && data.searchRegion.length === 4 && (
            <span className="text-[10px] text-gray-500" title={`只看 ${data.searchRegion.join(',')} 範圍`}>
              📐 區域
            </span>
          )}
        </div>
        <p className="text-xs text-gray-500 leading-relaxed" style={{
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
        }}>
          {data.prompt || '點擊設定判斷條件…'}
        </p>
      </div>

      <Handle type="source" position={Position.Right}
        className="!w-3 !h-3 !rounded-full !border-2 !border-white" style={{ background: color }} />
    </div>
  )
}

export default memo(VisualValidationNodeComponent)
