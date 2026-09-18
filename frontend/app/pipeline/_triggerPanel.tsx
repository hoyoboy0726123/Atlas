'use client'
import { useState, useEffect } from 'react'
import { X, Zap, FolderSearch, Copy, Trash2, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import {
  getWebhook, createWebhook, disableWebhook,
  getFolderWatch, createFolderWatch, disableFolderWatch,
  type WebhookInfo, type FolderWatchInfo,
} from '@/lib/api'

interface Props {
  workflowId: string
  workflowName: string
  onClose: () => void
}

/** 觸發器面板:Webhook + 檔案夾監看(cron 排程另有「排程」按鈕) */
export default function TriggerPanel({ workflowId, workflowName, onClose }: Props) {
  const [hook, setHook] = useState<WebhookInfo | null>(null)
  const [watch, setWatch] = useState<FolderWatchInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  // 監看建立表單
  const [folderPath, setFolderPath] = useState('')
  const [pattern, setPattern] = useState('*')

  const load = async () => {
    setLoading(true)
    try {
      const [h, w] = await Promise.all([
        getWebhook(workflowId).catch(() => null),
        getFolderWatch(workflowId).catch(() => null),
      ])
      setHook(h && h.enabled ? h : null)
      setWatch(w && w.enabled ? w : null)
      if (w && w.enabled) { setFolderPath(w.folder_path); setPattern(w.pattern) }
    } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [workflowId])

  const copyUrl = (url: string) => {
    navigator.clipboard?.writeText(url)
      .then(() => toast.success('已複製觸發網址'))
      .catch(() => toast.error('複製失敗,請手動選取'))
  }

  const fmtTime = (t: number) => t ? new Date(t * 1000).toLocaleString() : '—'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-2xl w-[520px] max-h-[86vh] overflow-hidden flex flex-col"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 px-5 py-4 border-b">
          <Zap className="w-4 h-4 text-teal-600" />
          <div className="flex-1">
            <span className="font-semibold text-gray-800">觸發器</span>
            <span className="text-xs text-gray-400 ml-2">{workflowName}</span>
          </div>
          <button onClick={load} className="text-gray-400 hover:text-teal-600 p-1" title="重新整理">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X className="w-4 h-4" /></button>
        </div>

        <div className="p-5 space-y-5 overflow-y-auto">
          {/* ── Webhook ── */}
          <div className="rounded-xl border border-gray-200 p-4">
            <div className="flex items-center gap-2 mb-1.5">
              <Zap className="w-4 h-4 text-teal-600" />
              <span className="font-semibold text-sm text-gray-800">Webhook 觸發</span>
              {hook && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-teal-50 text-teal-700">啟用中 · 已觸發 {hook.fire_count} 次</span>}
            </div>
            <p className="text-xs text-gray-400 mb-3 leading-relaxed">
              外部系統 POST 這個網址就會啟動本工作流;body(JSON)會變成 <code className="font-mono bg-gray-100 px-1 rounded">{'{{ input.<鍵> }}'}</code> 可取用的啟動參數。
            </p>
            {hook ? (
              <div className="space-y-2">
                <div className="flex gap-2">
                  <input readOnly value={hook.url || ''} onFocus={e => e.target.select()}
                    className="flex-1 border border-gray-200 rounded-lg px-2.5 py-1.5 text-xs font-mono bg-gray-50 text-gray-700" />
                  <button onClick={() => hook.url && copyUrl(hook.url)}
                    className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-teal-600 text-white text-xs hover:bg-teal-700">
                    <Copy className="w-3 h-3" /> 複製
                  </button>
                </div>
                <div className="flex items-center justify-between text-[11px] text-gray-400">
                  <span>最後觸發:{fmtTime(hook.last_fired_at)}</span>
                  <div className="flex gap-3">
                    <button onClick={async () => {
                      if (!confirm('重新產生會讓舊網址立刻失效,確定?')) return
                      setBusy('hook')
                      try { await createWebhook(workflowId); setHook(await getWebhook(workflowId)); toast.success('已重新產生(舊網址失效)') }
                      catch (e) { toast.error((e as Error).message) } finally { setBusy('') }
                    }} className="text-teal-600 hover:underline" disabled={busy === 'hook'}>重新產生</button>
                    <button onClick={async () => {
                      if (!confirm('停用後外部將無法再觸發本工作流,確定?')) return
                      setBusy('hook')
                      try { await disableWebhook(workflowId); setHook(null); toast.success('已停用 webhook') }
                      catch (e) { toast.error((e as Error).message) } finally { setBusy('') }
                    }} className="text-red-500 hover:underline flex items-center gap-0.5"><Trash2 className="w-3 h-3" />停用</button>
                  </div>
                </div>
              </div>
            ) : (
              <button onClick={async () => {
                setBusy('hook')
                try {
                  await createWebhook(workflowId)
                  // create 回傳不含統計欄位 → 重新 GET 拿完整資料(fire_count/last_fired)
                  const h = await getWebhook(workflowId)
                  setHook(h)
                  toast.success('Webhook 已建立,網址已就緒')
                }
                catch (e) { toast.error((e as Error).message) } finally { setBusy('') }
              }} disabled={busy === 'hook'}
                className="w-full py-2 rounded-lg bg-teal-600 text-white text-sm font-medium hover:bg-teal-700 disabled:opacity-50">
                {busy === 'hook' ? '建立中…' : '⚡ 產生觸發網址'}
              </button>
            )}
          </div>

          {/* ── 檔案夾監看 ── */}
          <div className="rounded-xl border border-gray-200 p-4">
            <div className="flex items-center gap-2 mb-1.5">
              <FolderSearch className="w-4 h-4 text-amber-600" />
              <span className="font-semibold text-sm text-gray-800">檔案夾監看觸發</span>
              {watch && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-50 text-amber-700">監看中 · 已觸發 {watch.trigger_count} 次</span>}
            </div>
            <p className="text-xs text-gray-400 mb-3 leading-relaxed">
              指定資料夾出現符合的<strong>新檔</strong>(設定之後才落地的)就自動啟動;工作流用
              <code className="font-mono bg-gray-100 px-1 rounded">{'{{ input.file_path }}'}</code> /
              <code className="font-mono bg-gray-100 px-1 rounded">{'{{ input.file_name }}'}</code> 取得該檔。約 8 秒偵測一次。
            </p>
            <div className="space-y-2">
              <div className="flex gap-2">
                <input value={folderPath} onChange={e => setFolderPath(e.target.value)}
                  placeholder="C:\Users\me\掃描發票" disabled={!!watch}
                  className="flex-1 border border-gray-200 rounded-lg px-2.5 py-1.5 text-xs font-mono disabled:bg-gray-50 disabled:text-gray-500" />
                <input value={pattern} onChange={e => setPattern(e.target.value)}
                  placeholder="*.pdf" disabled={!!watch}
                  className="w-24 border border-gray-200 rounded-lg px-2.5 py-1.5 text-xs font-mono disabled:bg-gray-50 disabled:text-gray-500" />
              </div>
              {watch ? (
                <div className="flex items-center justify-between text-[11px] text-gray-400">
                  <span>自 {fmtTime(watch.created_at)} 起監看</span>
                  <button onClick={async () => {
                    if (!confirm('停止監看此資料夾?')) return
                    setBusy('watch')
                    try { await disableFolderWatch(workflowId); setWatch(null); toast.success('已停止監看') }
                    catch (e) { toast.error((e as Error).message) } finally { setBusy('') }
                  }} className="text-red-500 hover:underline flex items-center gap-0.5"><Trash2 className="w-3 h-3" />停止監看</button>
                </div>
              ) : (
                <button onClick={async () => {
                  if (!folderPath.trim()) { toast.error('請填資料夾路徑'); return }
                  setBusy('watch')
                  try {
                    const w = await createFolderWatch(workflowId, folderPath.trim(), pattern.trim() || '*')
                    setWatch(w); toast.success('已開始監看(只觸發之後的新檔)')
                  } catch (e) { toast.error((e as Error).message) } finally { setBusy('') }
                }} disabled={busy === 'watch'}
                  className="w-full py-2 rounded-lg bg-amber-600 text-white text-sm font-medium hover:bg-amber-700 disabled:opacity-50">
                  {busy === 'watch' ? '建立中…' : '📁 開始監看'}
                </button>
              )}
            </div>
          </div>

          <p className="text-[11px] text-gray-400">
            ⏰ 定時觸發請用工具列的「排程」;三種觸發可並存。觸發都走快速模式(命中 Recipe)+ 無人值守。
          </p>
        </div>
      </div>
    </div>
  )
}
