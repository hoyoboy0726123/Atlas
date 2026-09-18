'use client'
import { memo } from 'react'
import { Handle, Position, type NodeProps, type Node } from '@xyflow/react'
import { Globe, Film } from 'lucide-react'
import type { WebCrawlerData } from './_helpers'
import { useRunStatusStore } from './_runStatus'

const STATUS_ICON: Record<string, string> = { idle: '●', running: '⟳', success: '✓', failed: '✗' }
const STATUS_COLOR: Record<string, string> = {
  idle: 'text-white/60', running: 'text-yellow-200 animate-spin', success: 'text-green-200', failed: 'text-red-200',
}

const WEB_COLOR = '#0d9488'    // teal-600
const VIDEO_COLOR = '#e11d48'  // rose-600 — 跟網頁模式視覺區隔

type WebCrawlerNodeType = Node<WebCrawlerData>

function WebCrawlerStepNode({ data, selected }: NodeProps<WebCrawlerNodeType>) {
  const runtime = useRunStatusStore(s => s.stepStatuses[data.name])
  const status = runtime?.status ?? 'idle'
  const errorMsg = runtime?.errorMsg ?? ''

  const isVideo = data.mode === 'video'
  const baseColor = isVideo ? VIDEO_COLOR : WEB_COLOR
  const color = status === 'failed' ? '#ef4444'
    : status === 'success' ? '#10b981'
    : status === 'running' ? '#3b82f6'
    : baseColor

  // 顯示 URL 的精簡版（host + 路徑前段，避免太長）
  // 網頁模式優先看 urls 陣列；多 URL 直接顯示「N 個 URL」、單一才秀第一個的精簡版
  const webUrlList = (data.urls || []).filter(u => u && u.trim() && !u.trim().startsWith('#'))
  const isMultiUrl = !isVideo && webUrlList.length > 1
  const activeUrl = isVideo ? data.videoUrl : (webUrlList[0] || data.url)
  let urlBrief = isVideo ? '尚未設定影片 URL' : '尚未設定 URL'
  if (isMultiUrl) {
    urlBrief = `📁 ${webUrlList.length} 個 URL`
  } else if (activeUrl) {
    try {
      const u = new URL(activeUrl)
      urlBrief = u.host + (u.pathname && u.pathname !== '/' ? u.pathname : '')
      if (u.search) urlBrief += u.search.slice(0, 16)
      if (urlBrief.length > 32) urlBrief = urlBrief.slice(0, 31) + '…'
    } catch {
      urlBrief = activeUrl.length > 32 ? activeUrl.slice(0, 31) + '…' : activeUrl
    }
  }

  // 額外 badge：因模式不同
  const badges: string[] = []
  if (data.cookies) badges.push('🔑 登入')
  if (isVideo) {
    badges.push(`📺 ${data.videoQuality || '720p'}`)
    if (data.videoSubs) badges.push('💬 字幕')
  } else {
    if (data.interactions && data.interactions.length > 0) badges.push(`▶ ${data.interactions.length}`)
    if (data.cloudflareFallback) badges.push('🛡 CF')
    if (data.downloadAssets) badges.push('📦 附件')
  }

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
        {isVideo
          ? <Film className="w-3.5 h-3.5 text-white shrink-0" strokeWidth={2.5} />
          : <Globe className="w-3.5 h-3.5 text-white shrink-0" strokeWidth={2.5} />}
        <span className="text-white font-semibold text-sm flex-1 truncate leading-tight">{data.name}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/20 text-white font-medium shrink-0">
          {isVideo ? '影片' : '爬蟲'}
        </span>
        <span className={`text-sm shrink-0 ${STATUS_COLOR[status]}`}>{STATUS_ICON[status]}</span>
      </div>

      {/* Body */}
      <div className="bg-white px-3 py-2.5 space-y-1">
        <p className={`text-xs truncate font-mono ${isVideo ? 'text-rose-700' : 'text-teal-700'}`} title={activeUrl}>{urlBrief}</p>
        {badges.length > 0 && (
          <p className="text-[10px] text-gray-500 flex gap-1.5 flex-wrap">{badges.map((b, i) => <span key={i}>{b}</span>)}</p>
        )}
        {data.outputPath ? (
          <p className="text-xs text-gray-400 truncate">→ {data.outputPath.replace(/^.*\/([^/]+)$/, '$1')}</p>
        ) : (
          <p className="text-xs text-gray-300 italic">→ 自動命名</p>
        )}
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

export default memo(WebCrawlerStepNode)
