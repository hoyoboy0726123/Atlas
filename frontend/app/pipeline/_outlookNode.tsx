'use client'
import { memo } from 'react'
import { Handle, Position, type NodeProps, type Node } from '@xyflow/react'
import { Mail } from 'lucide-react'
import type { OutlookData } from './_helpers'
import { useRunStatusStore } from './_runStatus'

const STATUS_ICON: Record<string, string> = { idle: '●', running: '⟳', success: '✓', failed: '✗' }
const STATUS_COLOR: Record<string, string> = {
  idle: 'text-white/60', running: 'text-yellow-200 animate-spin', success: 'text-green-200', failed: 'text-red-200',
}

const OUTLOOK_COLOR = '#0078d4'  // Outlook 官方藍

// 模板 ID → 顯示用簡稱（節點上看一眼就知道幹嘛）
const TEMPLATE_LABEL: Record<string, string> = {
  daily_todo: '🗒 每日待辦整理',
  search_summary: '🔍 關鍵字摘要',
  unanswered: '❓ 未回覆信件',
  download_attachments: '📎 下載附件',
  send_mail: '✉ 寄信',
  send_with_attachment: '📤 寄信附前一步輸出',
  bulk_send: '📨 群發信件',
  reply_mail: '↩ 回覆',
  forward_mail: '↪ 轉寄',
  calendar_list: '📅 列出會議',
  create_meeting: '🆕 新增會議',
  find_free_slot: '⌚ 找空檔',
}

type OutlookNodeType = Node<OutlookData>

function OutlookStepNode({ data, selected }: NodeProps<OutlookNodeType>) {
  const runtime = useRunStatusStore(s => s.stepStatuses[data.name])
  const status = runtime?.status ?? 'idle'
  const errorMsg = runtime?.errorMsg ?? ''

  const color = status === 'failed' ? '#ef4444'
    : status === 'success' ? '#10b981'
    : status === 'running' ? '#3b82f6'
    : OUTLOOK_COLOR

  const subtitle = data.template
    ? (TEMPLATE_LABEL[data.template] || data.template)
    : (data.freeText ? '✏ 自由輸入' : '尚未設定模板')

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
        <Mail className="w-3.5 h-3.5 text-white shrink-0" strokeWidth={2.5} />
        <span className="text-white font-semibold text-sm flex-1 truncate leading-tight">{data.name}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/20 text-white font-medium shrink-0">
          Outlook
        </span>
        <span className={`text-sm shrink-0 ${STATUS_COLOR[status]}`}>{STATUS_ICON[status]}</span>
      </div>

      {/* Body */}
      <div className="bg-white px-3 py-2.5 space-y-1">
        <p className="text-xs text-blue-700 truncate">{subtitle}</p>
        {data.outputPath ? (
          <p className="text-xs text-gray-400 truncate">→ {data.outputPath.replace(/^.*\/([^/]+)$/, '$1')}</p>
        ) : (
          <p className="text-xs text-gray-300 italic">（無輸出檔）</p>
        )}
        {status === 'failed' && errorMsg && <p className="text-xs text-red-500 truncate">{errorMsg}</p>}
      </div>

      <Handle type="source" position={Position.Right}
        className="!w-3 !h-3 !rounded-full !border-2 !border-white" style={{ background: color }} />
    </div>
  )
}

export default memo(OutlookStepNode)
