'use client'
/**
 * 螢幕區域圖形拉選器（給視覺驗證節點 current_screen 模式用）
 *
 * 流程：
 *   開啟 → 後端 /screen/snapshot 抓「整個虛擬桌面」一張 PNG（含多螢幕負座標）
 *   → 等比縮放塞進 modal 畫布
 *   → 使用者拖曳拉出矩形（沒拖過 = 整個畫面）
 *   → 套用後回 [left, top, width, height]，皆為虛擬桌面絕對座標
 *
 * 跟 _anchorEditorModal.tsx 概念一樣，差別在：anchor editor 用既有錄製的全螢幕截圖；
 * 我們需要「即時」抓螢幕。
 */
import { useEffect, useRef, useState } from 'react'
import { X, RefreshCcw, MousePointerSquareDashed, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { getScreenSnapshot, type ScreenSnapshot } from '@/lib/api'

interface Props {
  // 初始矩形（虛擬桌面絕對座標 [l, t, w, h]）；空陣列 = 整個畫面
  initialRegion: number[]
  onApply: (region: number[]) => void  // 回 [l, t, w, h]，整個畫面時回 []
  onClose: () => void
}

export default function ScreenRegionPicker({ initialRegion, onApply, onClose }: Props) {
  const [snap, setSnap] = useState<ScreenSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>('')
  // box 是「畫布座標系」中的矩形 — 顯示用，套用時轉成虛擬桌面絕對座標
  const [box, setBox] = useState<{ x: number; y: number; w: number; h: number } | null>(null)
  // 拖曳狀態
  const [drag, setDrag] = useState<{ startX: number; startY: number } | null>(null)
  const canvasRef = useRef<HTMLDivElement>(null)
  const imgRef = useRef<HTMLImageElement>(null)

  // 第一次開啟先抓一張螢幕
  const fetchSnap = async () => {
    setLoading(true)
    setError('')
    try {
      const s = await getScreenSnapshot()
      setSnap(s)
      // 若有初始 region，把它換算成畫布座標顯示
      if (initialRegion && initialRegion.length === 4 && initialRegion[2] > 0 && initialRegion[3] > 0) {
        const [l, t, w, h] = initialRegion
        setBox({
          x: l - s.origin_x,
          y: t - s.origin_y,
          w,
          h,
        })
      } else {
        setBox(null)
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { fetchSnap() }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  // 把螢幕座標 (絕對虛擬桌面) 轉成 canvas 顯示座標
  // canvas 等比 scale 到容器，圖載入後存比例
  const [displayScale, setDisplayScale] = useState(1)
  const onImgLoad = () => {
    if (!imgRef.current || !canvasRef.current) return
    // canvas 容器最大尺寸：寬 90vw、高 70vh（CSS 設定），實際 displayed 尺寸來自 img
    const rect = imgRef.current.getBoundingClientRect()
    const naturalW = imgRef.current.naturalWidth
    setDisplayScale(naturalW > 0 ? rect.width / naturalW : 1)
  }
  useEffect(() => {
    const onResize = () => onImgLoad()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // ── 拖曳邏輯 ──
  const onMouseDown = (e: React.MouseEvent) => {
    if (!canvasRef.current || !snap) return
    const rect = canvasRef.current.getBoundingClientRect()
    const cx = (e.clientX - rect.left) / displayScale
    const cy = (e.clientY - rect.top) / displayScale
    setDrag({ startX: cx, startY: cy })
    setBox({ x: cx, y: cy, w: 0, h: 0 })
  }
  const onMouseMove = (e: React.MouseEvent) => {
    if (!drag || !canvasRef.current || !snap) return
    const rect = canvasRef.current.getBoundingClientRect()
    const cx = (e.clientX - rect.left) / displayScale
    const cy = (e.clientY - rect.top) / displayScale
    const x = Math.min(drag.startX, cx)
    const y = Math.min(drag.startY, cy)
    const w = Math.abs(cx - drag.startX)
    const h = Math.abs(cy - drag.startY)
    setBox({
      x: Math.max(0, x),
      y: Math.max(0, y),
      w: Math.min(snap.width - x, w),
      h: Math.min(snap.height - y, h),
    })
  }
  const onMouseUp = () => {
    setDrag(null)
    // 太小（< 20px）視為誤拖 → 清空
    if (box && (box.w < 20 || box.h < 20)) {
      setBox(null)
    }
  }

  // ── 套用 ──
  const apply = () => {
    if (!snap) { onClose(); return }
    if (!box || box.w < 20 || box.h < 20) {
      // 沒拉框 → 整個畫面（傳空陣列）
      onApply([])
      toast.success('已設定為整個螢幕（不裁切）')
    } else {
      const region = [
        Math.round(box.x + snap.origin_x),
        Math.round(box.y + snap.origin_y),
        Math.round(box.w),
        Math.round(box.h),
      ]
      onApply(region)
      toast.success(`區域：${region[0]},${region[1]} 起 ${region[2]}×${region[3]}`)
    }
    onClose()
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4"
         onMouseUp={onMouseUp}>
      <div className="bg-white rounded-xl shadow-2xl flex flex-col max-w-[95vw] max-h-[95vh] overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-3 border-b">
          <MousePointerSquareDashed className="w-5 h-5 text-indigo-600" />
          <div className="flex-1 min-w-0">
            <div className="font-semibold text-gray-800 text-sm">在螢幕上拉一塊區域</div>
            <div className="text-xs text-gray-500">
              拖曳滑鼠拉出矩形 → 點「套用」。沒拉就套用 = 看整個畫面。VLM 只看矩形內的內容（省 token、避開干擾）
            </div>
          </div>
          <button onClick={fetchSnap} title="重新抓螢幕（換另一個畫面再來）"
                  className="p-2 text-gray-500 hover:text-gray-800 hover:bg-gray-100 rounded">
            <RefreshCcw className="w-4 h-4" />
          </button>
          <button onClick={() => setBox(null)} title="清除選取"
                  className="p-2 text-gray-500 hover:text-red-500 hover:bg-gray-100 rounded">
            <Trash2 className="w-4 h-4" />
          </button>
          <button onClick={onClose} className="p-2 text-gray-500 hover:bg-gray-100 rounded">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Canvas */}
        <div className="flex-1 min-h-0 overflow-auto bg-gray-100 p-4 flex items-center justify-center"
             style={{ maxHeight: '75vh' }}>
          {loading && <div className="text-gray-500 text-sm">📸 抓取螢幕中…</div>}
          {error && (
            <div className="text-center">
              <div className="text-red-600 text-sm mb-2">⚠ {error}</div>
              <button onClick={fetchSnap}
                      className="px-3 py-1.5 bg-indigo-500 text-white rounded text-sm hover:bg-indigo-600">
                重試
              </button>
            </div>
          )}
          {snap && !loading && !error && (
            <div className="relative inline-block select-none cursor-crosshair"
                 ref={canvasRef}
                 onMouseDown={onMouseDown}
                 onMouseMove={onMouseMove}>
              <img
                ref={imgRef}
                src={`data:image/png;base64,${snap.image_b64}`}
                alt="screen snapshot"
                onLoad={onImgLoad}
                draggable={false}
                style={{ maxWidth: '90vw', maxHeight: '70vh', display: 'block', userSelect: 'none' }}
              />
              {/* 半透明遮罩 + 鏤空矩形（純視覺反饋） */}
              {box && (
                <div className="absolute pointer-events-none"
                     style={{
                       left: box.x * displayScale,
                       top: box.y * displayScale,
                       width: box.w * displayScale,
                       height: box.h * displayScale,
                       border: '2px solid #6366f1',
                       background: 'rgba(99, 102, 241, 0.15)',
                       boxShadow: '0 0 0 9999px rgba(0, 0, 0, 0.45)',
                     }}>
                  {/* 尺寸標籤 */}
                  <div className="absolute -bottom-7 left-0 px-1.5 py-0.5 bg-indigo-600 text-white text-[10px] font-mono rounded whitespace-nowrap">
                    {Math.round(box.w)} × {Math.round(box.h)}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-4 py-3 border-t bg-gray-50">
          <div className="text-xs text-gray-500 font-mono">
            {snap && box && box.w >= 20 && box.h >= 20 ? (
              <>
                絕對座標：({Math.round(box.x + snap.origin_x)}, {Math.round(box.y + snap.origin_y)}) 起 {Math.round(box.w)} × {Math.round(box.h)} px
              </>
            ) : (
              <span className="text-gray-400">尚未拉選 — 套用 = 看整個 {snap ? `${snap.width}×${snap.height}` : ''} 畫面</span>
            )}
          </div>
          <div className="flex gap-2">
            <button onClick={onClose}
                    className="px-3 py-1.5 border border-gray-200 rounded text-sm text-gray-600 hover:bg-gray-100">
              取消
            </button>
            <button onClick={apply}
                    className="px-4 py-1.5 bg-indigo-600 text-white rounded text-sm font-medium hover:bg-indigo-700">
              套用
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
