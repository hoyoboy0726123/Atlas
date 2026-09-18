'use client'
import { useState, useEffect } from 'react'
import { X, GitCompareArrows, RefreshCw } from 'lucide-react'
import { getRunDiff, type RunDiffResult, type RunDiffStep } from '@/lib/api'

interface Props {
  workflowId: string
  workflowName: string
  onClose: () => void
}

function StepDiff({ s }: { s: RunDiffStep }) {
  const o = s.output
  const changed = !o.identical && o.kind !== 'none'
  return (
    <div className={`rounded-xl border p-3 ${changed ? 'border-amber-300 bg-amber-50/40' : 'border-gray-200'}`}>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-sm font-medium text-gray-800">{s.step_name}</span>
        <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${changed ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-500'}`}>
          {changed ? '有變化' : '相同'}
        </span>
        <span className="text-[11px] text-gray-400 ml-auto">{o.summary}</span>
      </div>
      {/* JSON 鍵值差異 */}
      {o.kind === 'json' && !o.identical && (
        <div className="space-y-1 mt-2 text-xs font-mono">
          {(o.changed || []).map((c, i) => (
            <div key={`c${i}`} className="flex gap-2 items-baseline">
              <span className="text-amber-700 shrink-0">~ {c.key}</span>
              <span className="text-gray-400 line-through truncate">{JSON.stringify(c.before)}</span>
              <span className="text-gray-800 truncate">→ {JSON.stringify(c.after)}</span>
              {c.delta !== undefined && (
                <span className={`shrink-0 font-semibold ${c.delta > 0 ? 'text-emerald-600' : 'text-red-500'}`}>
                  {c.delta > 0 ? `+${c.delta}` : c.delta}
                </span>
              )}
            </div>
          ))}
          {(o.added || []).map((a, i) => (
            <div key={`a${i}`} className="text-emerald-700 truncate">+ {a.key} = {JSON.stringify(a.value)}</div>
          ))}
          {(o.removed || []).map((r, i) => (
            <div key={`r${i}`} className="text-red-500 truncate">- {r.key}(原:{JSON.stringify(r.value)})</div>
          ))}
          {o.truncated && <div className="text-gray-400">…(差異過多已截斷)</div>}
        </div>
      )}
      {/* 文字 unified diff */}
      {o.kind === 'text' && !o.identical && o.diff && (
        <pre className="mt-2 text-[11px] leading-relaxed bg-gray-900 text-gray-100 rounded-lg p-2.5 overflow-x-auto max-h-64 overflow-y-auto">
          {o.diff.split('\n').map((l, i) => (
            <div key={i} className={l.startsWith('+') ? 'text-emerald-400' : l.startsWith('-') ? 'text-red-400' : 'text-gray-400'}>{l || ' '}</div>
          ))}
        </pre>
      )}
    </div>
  )
}

export default function RunDiffModal({ workflowId, workflowName, onClose }: Props) {
  const [diff, setDiff] = useState<RunDiffResult | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true); setError('')
    try { setDiff(await getRunDiff(workflowId)) }
    catch (e) { setError((e as Error).message) }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [workflowId])

  const fmt = (iso?: string) => iso ? new Date(iso).toLocaleString() : '—'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-2xl w-[640px] max-h-[86vh] overflow-hidden flex flex-col"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 px-5 py-4 border-b">
          <GitCompareArrows className="w-4 h-4 text-indigo-600" />
          <div className="flex-1">
            <span className="font-semibold text-gray-800">Run Diff — 這次 vs 上次</span>
            <span className="text-xs text-gray-400 ml-2">{workflowName}</span>
          </div>
          <button onClick={load} className="text-gray-400 hover:text-indigo-600 p-1" title="重新比較">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X className="w-4 h-4" /></button>
        </div>

        <div className="p-5 space-y-3 overflow-y-auto">
          {loading && <p className="text-sm text-gray-400 text-center py-6">比較中…</p>}
          {error && <p className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3">{error}</p>}
          {diff && !loading && (
            <>
              <div className="flex items-center gap-2 text-xs text-gray-500">
                <span className="px-2 py-1 rounded-lg bg-gray-100 font-mono">{diff.run_a.run_id}</span>
                <span className="text-gray-400">({fmt(diff.run_a.started_at)})</span>
                <span className="text-gray-300">→</span>
                <span className="px-2 py-1 rounded-lg bg-indigo-50 text-indigo-700 font-mono">{diff.run_b.run_id}</span>
                <span className="text-gray-400">({fmt(diff.run_b.started_at)})</span>
                <span className="ml-auto font-medium text-gray-700">{diff.summary}</span>
              </div>
              {diff.steps.map(s => <StepDiff key={s.step_name} s={s} />)}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
