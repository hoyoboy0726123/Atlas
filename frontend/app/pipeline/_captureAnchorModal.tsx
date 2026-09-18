'use client'
/**
 * 立即截圖 + 拖曳裁切 → 存進 assets_dir 變新錨點。
 *
 * 跟 AnchorEditorModal 不同(那個吃既有錄製的 full_image)、
 * 這個專給 VLM 錨點變體立即補:
 *   1. 倒數 3 秒(讓使用者把目標窗口擺好、或 hover 到變色狀態)
 *   2. /screen/snapshot 抓全螢幕 base64 PNG
 *   3. <img> 顯示、上層 overlay 讓使用者拖一個矩形
 *   4. 確認 → canvas 裁出該矩形 → toBlob → base64 → POST /computer-use/assets/save-png
 *   5. onApply(filename) 把新錨點檔名回給 picker、picker 自動加進 vlm_anchors
 *
 * 用途:Windows 關閉 X 滑鼠 hover 變色之類、CV 對單一錄製狀態容易 miss 的場景、
 * 多截幾張變體讓 VLM 從中挑當下狀態最像的那張。
 */
import { useEffect, useRef, useState } from 'react'
import { X, Camera, Check } from 'lucide-react'
import { toast } from 'sonner'
import { getScreenSnapshot, saveAnchorPng } from '@/lib/api'

interface Props {
  assetsDir: string
  onApply: (filename: string) => void
  onClose: () => void
  /** 預設檔名前綴(會接時間戳) */
  defaultNamePrefix?: string
}

type Phase = 'countdown' | 'capturing' | 'preview' | 'saving'

export default function CaptureAnchorModal({
  assetsDir, onApply, onClose, defaultNamePrefix = 'vlm_variant',
}: Props) {
  const [phase, setPhase] = useState<Phase>('countdown')
  const [countdown, setCountdown] = useState(3)
  const [imgB64, setImgB64] = useState<string>('')   // data:image/png;base64,...
  const [imgSize, setImgSize] = useState<{w: number; h: number}>({ w: 0, h: 0 })

  // crop box(畫面 px、相對顯示尺寸、不是原始解析度)
  const [crop, setCrop] = useState<{x: number; y: number; w: number; h: number} | null>(null)
  const dragStateRef = useRef<{startX: number; startY: number} | null>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const overlayRef = useRef<HTMLDivElement | null>(null)
  const [saveName, setSaveName] = useState<string>('')

  // 倒數 → 截圖
  useEffect(() => {
    if (phase !== 'countdown') return
    if (countdown <= 0) {
      setPhase('capturing')
      ;(async () => {
        try {
          const snap = await getScreenSnapshot()
          // /screen/snapshot 回 image_b64(純 base64、不含 data: prefix)
          setImgB64(`data:image/png;base64,${snap.image_b64}`)
          setImgSize({ w: snap.width, h: snap.height })
          // 預設檔名:prefix + 時間戳
          const ts = Date.now()
          setSaveName(`${defaultNamePrefix}_${ts}`)
          setPhase('preview')
        } catch (e) {
          toast.error(`截圖失敗:${(e as Error).message}`)
          onClose()
        }
      })()
      return
    }
    const t = setTimeout(() => setCountdown(c => c - 1), 1000)
    return () => clearTimeout(t)
  }, [phase, countdown, defaultNamePrefix, onClose])

  // 拖曳開始
  const onMouseDown = (e: React.MouseEvent) => {
    if (phase !== 'preview' || !overlayRef.current) return
    const rect = overlayRef.current.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    dragStateRef.current = { startX: x, startY: y }
    setCrop({ x, y, w: 0, h: 0 })
  }
  const onMouseMove = (e: React.MouseEvent) => {
    if (!dragStateRef.current || !overlayRef.current) return
    const rect = overlayRef.current.getBoundingClientRect()
    const cx = Math.max(0, Math.min(e.clientX - rect.left, rect.width))
    const cy = Math.max(0, Math.min(e.clientY - rect.top, rect.height))
    const { startX, startY } = dragStateRef.current
    setCrop({
      x: Math.min(startX, cx),
      y: Math.min(startY, cy),
      w: Math.abs(cx - startX),
      h: Math.abs(cy - startY),
    })
  }
  const onMouseUp = () => { dragStateRef.current = null }

  // 確認 → canvas 裁切 → toBlob → base64 → save
  const handleSave = async () => {
    if (!crop || crop.w < 10 || crop.h < 10) {
      toast.error('請拖一個至少 10×10 的範圍')
      return
    }
    if (!imgRef.current || !overlayRef.current) return
    if (!saveName.trim()) {
      toast.error('檔名不能為空')
      return
    }

    setPhase('saving')
    try {
      // 顯示尺寸 → 原始尺寸座標換算
      const dispRect = imgRef.current.getBoundingClientRect()
      const scaleX = imgSize.w / dispRect.width
      const scaleY = imgSize.h / dispRect.height
      const sx = Math.round(crop.x * scaleX)
      const sy = Math.round(crop.y * scaleY)
      const sw = Math.round(crop.w * scaleX)
      const sh = Math.round(crop.h * scaleY)

      // canvas 裁
      const canvas = document.createElement('canvas')
      canvas.width = sw
      canvas.height = sh
      const ctx = canvas.getContext('2d')
      if (!ctx) throw new Error('canvas getContext 失敗')

      // 把 base64 image 重新載成 Image element 好 drawImage
      const tmp = new Image()
      tmp.src = imgB64
      await new Promise<void>((resolve, reject) => {
        tmp.onload = () => resolve()
        tmp.onerror = () => reject(new Error('讀取截圖失敗'))
      })
      ctx.drawImage(tmp, sx, sy, sw, sh, 0, 0, sw, sh)
      const dataUrl = canvas.toDataURL('image/png')
      const b64 = dataUrl.split(',')[1]

      const r = await saveAnchorPng({
        dir: assetsDir,
        name: saveName,
        png_b64: b64,
      })
      toast.success(`已存 ${r.image} (${r.width}×${r.height}, variance=${r.variance})`)
      onApply(r.image)
      onClose()
    } catch (e) {
      toast.error(`存錨點失敗:${(e as Error).message}`)
      setPhase('preview')
    }
  }

  return (
    <div className="fixed inset-0 z-[60] bg-black/70 flex items-center justify-center p-4"
         onMouseUp={onMouseUp}>
      <div className="bg-white rounded-xl shadow-2xl flex flex-col max-w-[95vw] max-h-[95vh] overflow-hidden w-[1100px]">
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-3 border-b">
          <Camera className="w-5 h-5 text-emerald-600" />
          <div className="flex-1 min-w-0">
            <div className="font-semibold text-gray-800 text-sm">立即截圖 → 加錨點變體</div>
            <div className="text-xs text-gray-500 truncate">
              拖曳出要當錨點的範圍(例 hover 變色狀態的關閉 X);儲存後自動加入 VLM 候選
            </div>
          </div>
          <button onClick={onClose}
                  className="p-2 text-gray-500 hover:bg-gray-100 rounded">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 min-h-0 overflow-auto bg-gray-900 flex items-center justify-center relative">
          {phase === 'countdown' && (
            <div className="text-center text-white py-20">
              <div className="text-7xl font-bold mb-4 text-emerald-400">{countdown || '📸'}</div>
              <div className="text-sm text-gray-300">
                {countdown > 0
                  ? `準備中… ${countdown} 秒後截圖。`
                  : '截圖中…'}
              </div>
              <div className="text-xs text-gray-500 mt-3 max-w-md mx-auto leading-relaxed">
                {countdown > 0 && '把要截的視窗擺好、若要錄製 hover / 焦點變色狀態、現在把滑鼠移過去等變色'}
              </div>
            </div>
          )}

          {phase === 'capturing' && (
            <div className="text-center text-white py-20">
              <div className="text-3xl mb-4">📸</div>
              <div className="text-sm text-gray-300">正在抓取螢幕…</div>
            </div>
          )}

          {(phase === 'preview' || phase === 'saving') && imgB64 && (
            <div className="relative inline-block max-w-full max-h-full">
              {/* 畫面預覽 */}
              <img
                ref={imgRef}
                src={imgB64}
                alt="screen"
                className="max-w-full max-h-[75vh] block select-none pointer-events-none"
                draggable={false}
              />
              {/* 拖曳 overlay(覆在圖上、捕捉滑鼠事件) */}
              <div
                ref={overlayRef}
                className="absolute inset-0 cursor-crosshair"
                onMouseDown={onMouseDown}
                onMouseMove={onMouseMove}
              />
              {/* 已選矩形 */}
              {crop && crop.w > 0 && crop.h > 0 && (
                <div
                  className="absolute border-2 border-emerald-400 bg-emerald-300/20 pointer-events-none"
                  style={{ left: crop.x, top: crop.y, width: crop.w, height: crop.h }}
                />
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center gap-3 px-4 py-3 border-t bg-white">
          {phase === 'preview' && (
            <>
              <div className="flex-1 flex items-center gap-2 min-w-0">
                <label className="text-[11px] text-gray-500 shrink-0">檔名</label>
                <input
                  value={saveName}
                  onChange={e => setSaveName(e.target.value)}
                  className="flex-1 min-w-0 px-2 py-1 border border-gray-200 rounded text-xs font-mono focus:border-emerald-400 focus:ring-1 focus:ring-emerald-300/30 outline-none"
                  placeholder="檔名(不必含 .png)"
                />
                <span className="text-[11px] text-gray-400">
                  {crop && crop.w > 0 ? `${Math.round(crop.w)}×${Math.round(crop.h)}` : '拖曳框選'}
                </span>
              </div>
              <button onClick={onClose}
                      className="px-3 py-1.5 border border-gray-200 rounded text-sm text-gray-600 hover:bg-gray-100">
                取消
              </button>
              <button onClick={handleSave}
                      disabled={!crop || crop.w < 10 || crop.h < 10}
                      className="px-4 py-1.5 bg-emerald-600 text-white rounded text-sm font-medium hover:bg-emerald-700 disabled:bg-gray-300 disabled:cursor-not-allowed flex items-center gap-1.5">
                <Check className="w-3.5 h-3.5" /> 儲存錨點
              </button>
            </>
          )}
          {phase === 'saving' && (
            <div className="flex-1 text-center text-sm text-gray-500">儲存中…</div>
          )}
          {phase === 'countdown' && (
            <div className="flex-1 text-center text-xs text-gray-400">
              倒數中、準備好就等截圖
            </div>
          )}
          {phase === 'capturing' && (
            <div className="flex-1 text-center text-xs text-gray-400">截圖中…</div>
          )}
        </div>
      </div>
    </div>
  )
}
