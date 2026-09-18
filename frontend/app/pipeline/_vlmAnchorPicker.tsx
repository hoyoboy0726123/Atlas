'use client'
/**
 * VLM 挑錨點 — 檔案選擇器（取代手打檔名 textarea）
 *
 * 開啟後：
 *   1. 後端 /computer-use/assets/list?dir=... 列出該節點 assets_dir 內所有 PNG
 *      （自動排除 full_*.png 全螢幕截圖）
 *   2. 顯示縮圖 grid，目前已選的會打勾
 *   3. 點圖切換 add/remove
 *   4. 套用 → 把新陣列回傳給 panel
 *
 * 比手打檔名穩多了：使用者直接「看」哪張像、勾起來就好；不會打錯字、不會
 * 漏副檔名、不會搞混 _manual 後綴。
 */
import { useEffect, useState } from 'react'
import { X, RefreshCcw, Check, Camera } from 'lucide-react'
import { toast } from 'sonner'
import { listAssetFiles, assetImageUrl, type AssetFileEntry } from '@/lib/api'
import CaptureAnchorModal from './_captureAnchorModal'

interface Props {
  assetsDir: string                // 該動作所在節點的 assets_dir（相對或絕對）
  initialSelected: string[]        // 目前 vlm_anchors 已選的檔名
  onApply: (anchors: string[]) => void
  onClose: () => void
}

export default function VlmAnchorPicker({ assetsDir, initialSelected, onApply, onClose }: Props) {
  const [files, setFiles] = useState<AssetFileEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string[]>(initialSelected || [])
  // 立即截圖 modal 開關(允許使用者不用先把圖放好就能加新錨點變體)
  const [captureOpen, setCaptureOpen] = useState(false)

  const fetchList = async () => {
    setLoading(true)
    setError('')
    try {
      const r = await listAssetFiles(assetsDir)
      setFiles(r.files)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { fetchList() }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (name: string) => {
    setSelected(s => s.includes(name) ? s.filter(x => x !== name) : [...s, name])
  }
  const apply = () => {
    onApply(selected)
    toast.success(`已選 ${selected.length} 張錨點`)
    onClose()
  }

  // 區分：使用者已選 + 還沒選，已選的排前面便於檢查
  const selSet = new Set(selected)
  const sortedFiles = [...files].sort((a, b) => {
    const aSel = selSet.has(a.name) ? 0 : 1
    const bSel = selSet.has(b.name) ? 0 : 1
    if (aSel !== bSel) return aSel - bSel
    return a.name.localeCompare(b.name)
  })

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4">
      <div className="bg-white rounded-xl shadow-2xl flex flex-col max-w-[90vw] max-h-[90vh] overflow-hidden w-[820px]">
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-3 border-b">
          <Check className="w-5 h-5 text-indigo-600" />
          <div className="flex-1 min-w-0">
            <div className="font-semibold text-gray-800 text-sm">挑錨點變體</div>
            <div className="text-xs text-gray-500 truncate">
              從 <span className="font-mono">{assetsDir}</span> 選；點圖加入/移除
            </div>
          </div>
          <span className="text-[11px] px-2 py-0.5 rounded-full bg-indigo-50 border border-indigo-200 text-indigo-700">
            已選 {selected.length}
          </span>
          <button onClick={() => setCaptureOpen(true)}
                  title="立即截圖加新變體錨點(例 hover 變色狀態)"
                  className="px-2.5 py-1.5 text-xs font-medium bg-emerald-600 text-white rounded hover:bg-emerald-700 flex items-center gap-1">
            <Camera className="w-3.5 h-3.5" /> 立即截圖
          </button>
          <button onClick={fetchList} title="重新讀取資料夾"
                  className="p-2 text-gray-500 hover:text-gray-800 hover:bg-gray-100 rounded">
            <RefreshCcw className="w-4 h-4" />
          </button>
          <button onClick={onClose} className="p-2 text-gray-500 hover:bg-gray-100 rounded">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 min-h-0 overflow-auto p-4 bg-gray-50">
          {loading && <div className="text-center py-12 text-gray-500 text-sm">📂 讀取中…</div>}
          {error && (
            <div className="text-center py-12">
              <div className="text-red-600 text-sm mb-2">⚠ {error}</div>
              <button onClick={fetchList}
                      className="px-3 py-1.5 bg-indigo-500 text-white rounded text-sm hover:bg-indigo-600">
                重試
              </button>
            </div>
          )}
          {!loading && !error && files.length === 0 && (
            <div className="text-center py-12 text-gray-500 text-sm">
              這個資料夾沒有錨點 PNG。可以按右上角「📷 立即截圖」直接擷取一張、或先在動作上點「✏️ 編輯錨點」存幾張不同變體再來這選。
            </div>
          )}
          {!loading && !error && files.length > 0 && (
            <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-3">
              {sortedFiles.map(f => {
                const checked = selSet.has(f.name)
                return (
                  <button
                    key={f.name}
                    type="button"
                    onClick={() => toggle(f.name)}
                    className={`relative rounded-lg border-2 overflow-hidden transition-all bg-white text-left ${
                      checked
                        ? 'border-indigo-500 ring-2 ring-indigo-300 shadow-md'
                        : 'border-gray-200 hover:border-indigo-300 hover:shadow'
                    }`}
                  >
                    <div className="aspect-video bg-gray-100 flex items-center justify-center overflow-hidden">
                      <img
                        src={assetImageUrl(assetsDir, f.name)}
                        alt={f.name}
                        className="max-w-full max-h-full object-contain"
                        loading="lazy"
                        draggable={false}
                      />
                    </div>
                    <div className="px-2 py-1.5 text-[11px] font-mono truncate text-gray-700">
                      {f.name}
                    </div>
                    {checked && (
                      <div className="absolute top-1.5 right-1.5 w-5 h-5 rounded-full bg-indigo-600 text-white flex items-center justify-center">
                        <Check className="w-3 h-3" strokeWidth={3} />
                      </div>
                    )}
                  </button>
                )
              })}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-4 py-3 border-t bg-white">
          <div className="text-[11px] text-gray-500 leading-relaxed">
            <strong>1 張</strong> = VLM 守門員 + 強制 CV（不走錄製座標 fast-path）<br />
            <strong>2+ 張不同變體</strong> = VLM 看畫面當下狀態挑最像的那張，再走 CV
          </div>
          <div className="flex gap-2">
            <button onClick={onClose}
                    className="px-3 py-1.5 border border-gray-200 rounded text-sm text-gray-600 hover:bg-gray-100">
              取消
            </button>
            <button onClick={apply}
                    className="px-4 py-1.5 bg-indigo-600 text-white rounded text-sm font-medium hover:bg-indigo-700">
              套用 ({selected.length})
            </button>
          </div>
        </div>
      </div>

      {/* 立即截圖 modal、儲存後自動加進 selected + 重抓檔案清單 */}
      {captureOpen && (
        <CaptureAnchorModal
          assetsDir={assetsDir}
          onApply={(filename) => {
            setSelected(s => s.includes(filename) ? s : [...s, filename])
            fetchList()
          }}
          onClose={() => setCaptureOpen(false)}
        />
      )}
    </div>
  )
}
