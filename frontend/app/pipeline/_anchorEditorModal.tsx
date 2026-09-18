'use client'
import { useEffect, useMemo, useRef, useState } from 'react'
import { X, Check, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'
import type { ComputerUseAction } from './_helpers'
import { computerUseAssetImageUrl, cropAnchorFromFull, getComputerUseMonitors, type MonitorRect } from '@/lib/api'

interface Props {
  action: ComputerUseAction
  actionIndex: number
  assetsDir: string
  // step.cvSearchRadius — 同時當 CV 搜尋半徑視覺化的基準、以及 OCR 藍框的預設尺寸
  defaultSearchRadius?: number
  onApply: (patch: Partial<ComputerUseAction>) => void
  onClose: () => void
}

/**
 * 手動圈選錨點 Modal。
 * 顯示錄製當下的全螢幕截圖、點擊位置的紅十字、可拖曳的綠色裁切框。
 * 使用者按確認 → 呼叫後端裁出新錨點並更新 action。
 */
export default function AnchorEditorModal({ action, actionIndex, assetsDir, defaultSearchRadius, onApply, onClose }: Props) {
  const searchRadius = Math.max(80, defaultSearchRadius ?? 400)
  // 目前僅支援有 full_image 的 action（新錄製才有）
  const fullImg = action.full_image || ''
  const fullLeft = action.full_left || 0
  const fullTop = action.full_top || 0

  // 編輯模式：anchor = CV 錨點分頁（同時編輯綠框錨點 + 橘框搜尋範圍）/ ocr = 藍框 OCR
  const [editMode, setEditMode] = useState<'anchor' | 'ocr'>('anchor')
  // anchor 分頁裡目前在拖哪個框：綠框（錨點圖樣板）還是橘框（CV 搜尋範圍）
  const [anchorTarget, setAnchorTarget] = useState<'green' | 'orange'>('green')

  // 點擊位置（虛擬桌面絕對座標；可拖曳紅十字調整）
  const [clickPos, setClickPos] = useState(() => ({
    x: action.x || 0,
    y: action.y || 0,
  }))

  // 綠框：裁切範圍（虛擬桌面絕對座標）— 預設 240×80，之後會由已存的錨點圖尺寸反推取代
  const [box, setBox] = useState(() => ({
    left: (action.x || 0) - 120,
    top: (action.y || 0) - 40,
    width: 240,
    height: 80,
  }))

  // 藍框：OCR 搜尋範圍（虛擬桌面絕對座標）
  // 有存 ocr_box_* 就用存的；沒存就以點擊位置為中心、半徑用 searchRadius（預設 400）
  // 嚴格鎖定範圍：true = 框內找不到立即 fail，跳過附近/全螢幕 fallback
  const [ocrStrictRegion, setOcrStrictRegion] = useState<boolean>(action.ocr_strict_region === true)
  const [ocrBox, setOcrBox] = useState(() => {
    const hasSaved = (action.ocr_box_width || 0) > 0 && (action.ocr_box_height || 0) > 0
    if (hasSaved) {
      return {
        left: action.ocr_box_left || 0,
        top: action.ocr_box_top || 0,
        width: action.ocr_box_width || 0,
        height: action.ocr_box_height || 0,
      }
    }
    const r = searchRadius
    return {
      left: (action.x || 0) - r,
      top: (action.y || 0) - r,
      width: r * 2,
      height: r * 2,
    }
  })

  // 橘框：CV 搜尋範圍（虛擬桌面絕對座標，存在 action.search_region = [l,t,w,h]）
  // - 有存就用存的（cvCustomized = true → 套用時會寫進 search_region）
  // - 沒存就用 clickPos ± searchRadius 預設方框（cvCustomized = false → 套用時不寫，沿用步驟預設）
  // 一旦使用者拖過或改大小，cvCustomized 變 true、套用時就把 search_region 存進 action
  const [cvStrictRegion, setCvStrictRegion] = useState<boolean>(action.cv_strict_region === true)
  const [cvCustomized, setCvCustomized] = useState<boolean>(() => {
    const sr = action.search_region
    return Array.isArray(sr) && sr.length === 4 && sr[2] > 0 && sr[3] > 0
  })
  const [cvBox, setCvBox] = useState(() => {
    const sr = action.search_region
    if (Array.isArray(sr) && sr.length === 4 && sr[2] > 0 && sr[3] > 0) {
      return { left: sr[0], top: sr[1], width: sr[2], height: sr[3] }
    }
    const r = searchRadius
    return {
      left: (action.x || 0) - r,
      top: (action.y || 0) - r,
      width: r * 2,
      height: r * 2,
    }
  })
  // 開 Modal 時嘗試載入目前的錨點圖，從尺寸 + anchor_off_x/y 反推上次裁切框的位置
  // 這樣第二次開啟時框位置/大小 = 上次儲存的，不會退回預設 240×80
  useEffect(() => {
    if (!action.image) return
    const img = new Image()
    img.onload = () => {
      const W = img.naturalWidth
      const H = img.naturalHeight
      const ax = action.anchor_off_x || 0
      const ay = action.anchor_off_y || 0
      // click 在影像中的相對位置：click_dx = ax + W/2
      // 影像左上（虛擬桌面絕對座標） = click - click_dx
      const clickX = action.x || 0
      const clickY = action.y || 0
      const imgLeft = clickX - (ax + W / 2)
      const imgTop = clickY - (ay + H / 2)
      setBox({ left: Math.round(imgLeft), top: Math.round(imgTop), width: W, height: H })
    }
    img.onerror = () => {/* ignore — 用預設框 */}
    img.src = computerUseAssetImageUrl(assetsDir, action.image)
  }, [action.image, action.anchor_off_x, action.anchor_off_y, action.x, action.y, assetsDir])

  // Canvas / 圖片
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const [imgLoaded, setImgLoaded] = useState(false)
  const [displayScale, setDisplayScale] = useState(1)  // 縮放比（fit to viewport）
  const [preview, setPreview] = useState<string>('')

  // 多螢幕切換：
  //   monitors[0] = 虛擬桌面全景（mss 慣例），monitors[1..N] = 實體螢幕
  //   viewMode='all' → 顯示整張 full 圖（舊行為）；viewMode=k → 只裁出該螢幕範圍
  //   為什麼要切：6400×2160 虛擬桌面 fit 到 ~800px 視窗後元素變超小看不清
  const [monitors, setMonitors] = useState<MonitorRect[]>([])
  const [viewMode, setViewMode] = useState<'all' | number>('all')
  useEffect(() => {
    getComputerUseMonitors()
      .then(r => setMonitors(r.monitors || []))
      .catch(() => {/* 沒 monitors → 切換鈕不顯示，退回全圖模式 */})
  }, [])
  // 有 monitors 後，自動預設 viewMode 為「點擊所在的螢幕」省一次點擊
  // （只在 mount 執行一次，之後使用者切就照使用者的選）
  const _didAutoViewRef = useRef(false)
  useEffect(() => {
    if (_didAutoViewRef.current || monitors.length <= 2) return
    const ax = action.x || 0
    const ay = action.y || 0
    for (let i = 1; i < monitors.length; i++) {
      const m = monitors[i]
      if (ax >= m.left && ax < m.left + m.width && ay >= m.top && ay < m.top + m.height) {
        setViewMode(i)
        _didAutoViewRef.current = true
        return
      }
    }
    _didAutoViewRef.current = true
  }, [monitors, action.x, action.y])

  // viewRect：目前可見區域（in full-image 相對座標，以 full_*.png 左上角為原點）
  // 全部模式 = 整張圖；單螢幕模式 = 該螢幕在虛擬桌面上的矩形 − full_left/top，clamp 到圖內
  const [imgW, imgW_set] = useState(0)
  const [imgH, imgH_set] = useState(0)
  const viewRect = useMemo(() => {
    if (imgW === 0 || imgH === 0) return { x: 0, y: 0, w: imgW, h: imgH }
    if (viewMode === 'all') return { x: 0, y: 0, w: imgW, h: imgH }
    const mon = monitors[viewMode]
    if (!mon) return { x: 0, y: 0, w: imgW, h: imgH }
    const x = Math.max(0, mon.left - fullLeft)
    const y = Math.max(0, mon.top - fullTop)
    const w = Math.max(1, Math.min(imgW - x, mon.width))
    const h = Math.max(1, Math.min(imgH - y, mon.height))
    return { x, y, w, h }
  }, [viewMode, monitors, fullLeft, fullTop, imgW, imgH])

  // 世界座標（虛擬桌面絕對座標）⇄ Canvas CSS pixel 轉換
  // 加上 viewRect 後所有繪圖要扣掉 viewRect 的位移
  const worldToCanvas = (wx: number, wy: number) => ({
    cx: (wx - fullLeft - viewRect.x) * displayScale,
    cy: (wy - fullTop - viewRect.y) * displayScale,
  })
  const canvasToWorld = (cx: number, cy: number) => ({
    x: cx / displayScale + fullLeft + viewRect.x,
    y: cy / displayScale + fullTop + viewRect.y,
  })

  // 拖曳狀態（新增 move-click 拖紅十字）
  const [dragMode, setDragMode] = useState<'none' | 'move' | 'resize-br' | 'resize-tl' | 'move-click'>('none')
  const dragRef = useRef({ startX: 0, startY: 0, boxLeft: 0, boxTop: 0, boxW: 0, boxH: 0, clickX: 0, clickY: 0 })

  // 載入 full image
  useEffect(() => {
    if (!fullImg) return
    const url = computerUseAssetImageUrl(assetsDir, fullImg)
    const img = new Image()
    img.crossOrigin = 'anonymous'
    img.onload = () => {
      imgRef.current = img
      imgW_set(img.width)
      imgH_set(img.height)
      setImgLoaded(true)
    }
    img.onerror = () => toast.error('無法載入全螢幕截圖（full_*.png）')
    img.src = url
  }, [fullImg, assetsDir])

  // 計算 fit-to-viewport 縮放：fit 的是 viewRect（不是整張圖）
  // 切到單螢幕 → viewRect 變小 → scale 變大 → 畫面放大到看得清楚
  useEffect(() => {
    if (!imgLoaded || !imgRef.current || !containerRef.current) return
    const cont = containerRef.current
    const recalc = () => {
      const viewportW = cont.clientWidth - 40
      const viewportH = cont.clientHeight - 40
      const scale = Math.min(viewportW / viewRect.w, viewportH / viewRect.h, 1)
      setDisplayScale(scale || 1)
    }
    recalc()
    const ro = new ResizeObserver(recalc)
    ro.observe(cont)
    return () => ro.disconnect()
  }, [imgLoaded, viewRect.w, viewRect.h])

  // 重繪 Canvas（HiDPI-safe）
  useEffect(() => {
    const canvas = canvasRef.current
    const img = imgRef.current
    if (!canvas || !img || !imgLoaded) return
    // CSS 尺寸 = viewRect fit 到 viewport 後的大小
    const dispW = viewRect.w * displayScale
    const dispH = viewRect.h * displayScale
    const dpr = Math.max(1, Math.min(4, window.devicePixelRatio || 1))
    canvas.width = Math.round(dispW * dpr)
    canvas.height = Math.round(dispH * dpr)
    canvas.style.width = `${dispW}px`
    canvas.style.height = `${dispH}px`
    const ctx = canvas.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    // drawImage 的 source 從 viewRect 取；destination 填整個 canvas
    ctx.drawImage(img, viewRect.x, viewRect.y, viewRect.w, viewRect.h, 0, 0, dispW, dispH)

    // CV 錨點模式：畫出橘色 CV 搜尋範圍（cvBox）讓使用者直觀看到
    //   - 預設 = 錄製座標 ±cv_search_radius（沿用步驟預設）
    //   - 使用者拖過 = 自訂搜尋範圍，套用時會寫進 action.search_region
    //   - cvCustomized 區別兩者：自訂時實心 + 顯眼顏色，預設時半透明 dashed
    if (editMode === 'anchor') {
      const { cx: rx, cy: ry } = worldToCanvas(cvBox.left, cvBox.top)
      const rw = cvBox.width * displayScale
      const rh = cvBox.height * displayScale
      // 顏色強度：customized + 在拖橘框 → 飽和；customized 但拖綠框 → 中等；預設值 → 半透明
      const isActive = anchorTarget === 'orange'
      const alpha = isActive ? 0.18 : (cvCustomized ? 0.12 : 0.06)
      const strokeAlpha = isActive ? 1 : (cvCustomized ? 0.8 : 0.55)
      ctx.fillStyle = `rgba(249, 115, 22, ${alpha})`
      ctx.fillRect(rx, ry, rw, rh)
      ctx.strokeStyle = `rgba(234, 88, 12, ${strokeAlpha})`
      ctx.lineWidth = isActive ? 2 : 1.5
      ctx.setLineDash([4, 3])
      ctx.strokeRect(rx, ry, rw, rh)
      ctx.setLineDash([])
      // 拖橘框模式 → 畫四角 handle 提示可拉
      if (isActive) {
        ctx.fillStyle = '#ea580c'
        const hs = 8
        ctx.fillRect(rx - hs / 2, ry - hs / 2, hs, hs)
        ctx.fillRect(rx + rw - hs / 2, ry + rh - hs / 2, hs, hs)
      }
    }

    // 紅十字標點擊位置（世界座標 → viewRect 下的 canvas 座標）
    const { cx, cy } = worldToCanvas(clickPos.x, clickPos.y)
    // 外層白色描邊讓紅十字在各種背景下都看得清楚
    ctx.strokeStyle = 'white'
    ctx.lineWidth = 5
    ctx.beginPath()
    ctx.moveTo(cx - 18, cy); ctx.lineTo(cx + 18, cy)
    ctx.moveTo(cx, cy - 18); ctx.lineTo(cx, cy + 18)
    ctx.stroke()
    ctx.strokeStyle = 'red'
    ctx.lineWidth = 2.5
    ctx.beginPath()
    ctx.moveTo(cx - 18, cy); ctx.lineTo(cx + 18, cy)
    ctx.moveTo(cx, cy - 18); ctx.lineTo(cx, cy + 18)
    ctx.stroke()
    // 中心小圓點
    ctx.fillStyle = 'red'
    ctx.beginPath(); ctx.arc(cx, cy, 4, 0, 2 * Math.PI); ctx.fill()
    ctx.strokeStyle = 'white'
    ctx.lineWidth = 1.5
    ctx.stroke()

    // anchor 分頁畫綠框（錨點），ocr 分頁畫藍框
    // anchor 分頁的綠框可能不是當下被拖的（橘框被拖時也要顯示綠框，只是低調些）
    if (editMode === 'anchor') {
      const isActive = anchorTarget === 'green'
      const { cx: bx, cy: by } = worldToCanvas(box.left, box.top)
      const bw = box.width * displayScale
      const bh = box.height * displayScale
      const strokeAlpha = isActive ? 1 : 0.6
      ctx.strokeStyle = `rgba(16, 185, 129, ${strokeAlpha})`
      ctx.lineWidth = isActive ? 2 : 1.5
      ctx.setLineDash([6, 4])
      ctx.strokeRect(bx, by, bw, bh)
      ctx.setLineDash([])
      if (isActive) {
        ctx.fillStyle = '#10b981'
        const hs = 8
        ctx.fillRect(bx - hs / 2, by - hs / 2, hs, hs)
        ctx.fillRect(bx + bw - hs / 2, by + bh - hs / 2, hs, hs)
      }
    } else {  // 'ocr' 分頁畫藍框
      const { cx: bx, cy: by } = worldToCanvas(ocrBox.left, ocrBox.top)
      const bw = ocrBox.width * displayScale
      const bh = ocrBox.height * displayScale
      ctx.fillStyle = 'rgba(59, 130, 246, 0.12)'
      ctx.fillRect(bx, by, bw, bh)
      ctx.strokeStyle = '#3b82f6'
      ctx.lineWidth = 2
      ctx.setLineDash([6, 4])
      ctx.strokeRect(bx, by, bw, bh)
      ctx.setLineDash([])
      ctx.fillStyle = '#3b82f6'
      const hs = 8
      ctx.fillRect(bx - hs / 2, by - hs / 2, hs, hs)
      ctx.fillRect(bx + bw - hs / 2, by + bh - hs / 2, hs, hs)
    }
  }, [imgLoaded, displayScale, box, ocrBox, cvBox, editMode, anchorTarget, cvCustomized, clickPos.x, clickPos.y, fullLeft, fullTop, viewRect.x, viewRect.y, viewRect.w, viewRect.h, searchRadius])

  // 更新右側預覽（錨點：原尺寸裁切；OCR：寬度壓到 ≤320px 節省記憶體，僅用於預覽）
  const [ocrPreview, setOcrPreview] = useState<string>('')
  useEffect(() => {
    const img = imgRef.current
    if (!img || !imgLoaded) return
    // 錨點預覽：原尺寸（用 pixelated 放大顯示能看清楚）
    {
      const pCanvas = document.createElement('canvas')
      pCanvas.width = box.width
      pCanvas.height = box.height
      const ctx = pCanvas.getContext('2d')!
      ctx.drawImage(img, box.left - fullLeft, box.top - fullTop, box.width, box.height, 0, 0, box.width, box.height)
      setPreview(pCanvas.toDataURL('image/png'))
    }
    // OCR 預覽：藍框範圍可能很大（500×500 ~ 2000×2000），裁原尺寸會吃記憶體，縮到 ≤320px 寬
    {
      const W = Math.max(1, ocrBox.width)
      const H = Math.max(1, ocrBox.height)
      const scale = Math.min(320 / W, 200 / H, 1)
      const pW = Math.max(1, Math.round(W * scale))
      const pH = Math.max(1, Math.round(H * scale))
      const pCanvas = document.createElement('canvas')
      pCanvas.width = pW
      pCanvas.height = pH
      const ctx = pCanvas.getContext('2d')!
      ctx.imageSmoothingEnabled = true
      ctx.imageSmoothingQuality = 'high'
      ctx.drawImage(img, ocrBox.left - fullLeft, ocrBox.top - fullTop, W, H, 0, 0, pW, pH)
      setOcrPreview(pCanvas.toDataURL('image/jpeg', 0.85))
    }
  }, [imgLoaded, box, ocrBox, fullLeft, fullTop])

  // 計算 variance（簡單版：RGB 標準差）
  const [variance, setVariance] = useState(0)
  useEffect(() => {
    const img = imgRef.current
    if (!img || !imgLoaded) return
    const tCanvas = document.createElement('canvas')
    tCanvas.width = Math.min(box.width, 100)
    tCanvas.height = Math.min(box.height, 100)
    const ctx = tCanvas.getContext('2d')!
    const sx = box.left - fullLeft
    const sy = box.top - fullTop
    ctx.drawImage(img, sx, sy, box.width, box.height, 0, 0, tCanvas.width, tCanvas.height)
    const data = ctx.getImageData(0, 0, tCanvas.width, tCanvas.height).data
    let sum = 0, sumSq = 0, n = 0
    for (let i = 0; i < data.length; i += 4) {
      const gray = (data[i] + data[i + 1] + data[i + 2]) / 3
      sum += gray; sumSq += gray * gray; n++
    }
    const mean = sum / n
    const v = sumSq / n - mean * mean
    setVariance(Math.round(v))
  }, [imgLoaded, box, fullLeft, fullTop])

  // 滑鼠事件處理（Canvas 相對座標 → 世界座標；已透過 worldToCanvas/canvasToWorld 考慮 viewRect）
  // 多框並存時：以「點到哪個框就拖哪個」為原則，自動決定。priority 由小到大：
  //   1. 紅十字（最小、最具體）— 12px 範圍內
  //   2. 綠框 / 藍框（中等）— 內部或四角 handle
  //   3. 橘框（最大、整體背景）— 內部或四角 handle
  // anchor 分頁（綠+橘並存）會 auto-set anchorTarget，sub-toggle 只是指示狀態
  const onMouseDown = (e: React.MouseEvent) => {
    const rect = canvasRef.current!.getBoundingClientRect()
    const cx = e.clientX - rect.left
    const cy = e.clientY - rect.top

    // helper：檢查某個 box 是否被命中（near TL / near BR / inside）
    const hit = (b: { left: number; top: number; width: number; height: number }) => {
      const { cx: bx, cy: by } = worldToCanvas(b.left, b.top)
      const bw = b.width * displayScale
      const bh = b.height * displayScale
      const tl = Math.abs(cx - bx) < 10 && Math.abs(cy - by) < 10
      const br = Math.abs(cx - (bx + bw)) < 10 && Math.abs(cy - (by + bh)) < 10
      const inside = cx >= bx && cx <= bx + bw && cy >= by && cy <= by + bh
      if (tl) return 'resize-tl' as const
      if (br) return 'resize-br' as const
      if (inside) return 'move' as const
      return null
    }

    // 1. 紅十字（最高優先）
    const { cx: crossX, cy: crossY } = worldToCanvas(clickPos.x, clickPos.y)
    if (Math.abs(cx - crossX) < 12 && Math.abs(cy - crossY) < 12) {
      setDragMode('move-click')
      dragRef.current = {
        startX: cx, startY: cy,
        boxLeft: 0, boxTop: 0, boxW: 0, boxH: 0,
        clickX: clickPos.x, clickY: clickPos.y,
      }
      e.preventDefault()
      return
    }

    // 2. 內框（綠 / 藍）— 比外框優先
    let pickedBox: { left: number; top: number; width: number; height: number } | null = null
    let pickedSetter: typeof setBox | typeof setOcrBox | typeof setCvBox | null = null
    let pickedMode: 'move' | 'resize-tl' | 'resize-br' | null = null
    let switchTarget: 'green' | 'orange' | null = null

    if (editMode === 'ocr') {
      const m = hit(ocrBox)
      if (m) { pickedBox = ocrBox; pickedSetter = setOcrBox; pickedMode = m }
    } else {
      // anchor 分頁：先試綠框（內、小、優先）
      const mGreen = hit(box)
      if (mGreen) {
        pickedBox = box; pickedSetter = setBox; pickedMode = mGreen
        switchTarget = 'green'
      } else {
        // 綠框沒中 → 試橘框（外、大）
        const mOrange = hit(cvBox)
        if (mOrange) {
          pickedBox = cvBox; pickedSetter = setCvBox; pickedMode = mOrange
          switchTarget = 'orange'
        }
      }
    }

    if (!pickedBox || !pickedMode) return
    // 自動切 sub-toggle 顯示「目前在拖什麼」+ 拖橘框時 mark customized
    if (switchTarget && switchTarget !== anchorTarget) setAnchorTarget(switchTarget)
    if (switchTarget === 'orange' && !cvCustomized) setCvCustomized(true)

    setDragMode(pickedMode)
    // 把選中的 setter 暫存到 ref（onMouseMove 要用）
    pickedSetterRef.current = pickedSetter
    dragRef.current = {
      startX: cx, startY: cy,
      boxLeft: pickedBox.left, boxTop: pickedBox.top, boxW: pickedBox.width, boxH: pickedBox.height,
      clickX: clickPos.x, clickY: clickPos.y,
    }
    e.preventDefault()
  }
  // onMouseDown 選中的 setter 帶到 onMouseMove；不能用 useState 因為要立即生效
  const pickedSetterRef = useRef<typeof setBox | typeof setOcrBox | typeof setCvBox | null>(null)

  const onMouseMove = (e: React.MouseEvent) => {
    if (dragMode === 'none') return
    const rect = canvasRef.current!.getBoundingClientRect()
    const cx = e.clientX - rect.left
    const cy = e.clientY - rect.top
    const dx = (cx - dragRef.current.startX) / displayScale
    const dy = (cy - dragRef.current.startY) / displayScale
    if (dragMode === 'move-click') {
      setClickPos({
        x: Math.round(dragRef.current.clickX + dx),
        y: Math.round(dragRef.current.clickY + dy),
      })
      return
    }
    // 用 onMouseDown 選中的那個 setter（單一真相來源，不再依賴 anchorTarget 推導）
    const setter = pickedSetterRef.current
    if (!setter) return
    if (dragMode === 'move') {
      setter(b => ({ ...b, left: dragRef.current.boxLeft + Math.round(dx), top: dragRef.current.boxTop + Math.round(dy) }))
    } else if (dragMode === 'resize-br') {
      setter(b => ({
        ...b,
        width: Math.max(20, dragRef.current.boxW + Math.round(dx)),
        height: Math.max(20, dragRef.current.boxH + Math.round(dy)),
      }))
    } else if (dragMode === 'resize-tl') {
      setter(b => ({
        left: dragRef.current.boxLeft + Math.round(dx),
        top: dragRef.current.boxTop + Math.round(dy),
        width: Math.max(20, dragRef.current.boxW - Math.round(dx)),
        height: Math.max(20, dragRef.current.boxH - Math.round(dy)),
      }))
    }
  }

  const onMouseUp = () => setDragMode('none')

  // 確認：依 mode 不同走不同路徑
  //   anchor → 呼叫後端裁錨點 PNG、更新 image+offset；同時也把橘框 search_region
  //            （若 cvCustomized）+ cv_strict_region 一起寫進 action
  //   ocr    → 純前端存 ocr_box_* 座標、自動啟用 use_ocr
  const handleConfirm = async () => {
    if (editMode === 'ocr') {
      onApply({
        ocr_box_left: ocrBox.left,
        ocr_box_top: ocrBox.top,
        ocr_box_width: ocrBox.width,
        ocr_box_height: ocrBox.height,
        ocr_strict_region: ocrStrictRegion,
        // 套用藍框 = 明確想走 OCR；自動勾 use_ocr，避免使用者忘記勾 checkbox
        use_ocr: true,
      } as any)
      toast.success(`OCR 搜尋範圍已更新（${ocrBox.width}×${ocrBox.height}${ocrStrictRegion ? '・嚴格鎖定' : ''}）`)
      onClose()
      return
    }
    try {
      const saveAs = `img_${String(actionIndex + 1).padStart(3, '0')}_manual.png`
      const res = await cropAnchorFromFull({
        dir: assetsDir,
        full_image: fullImg,
        click_x: clickPos.x,
        click_y: clickPos.y,
        full_left: fullLeft,
        full_top: fullTop,
        crop_left: box.left,
        crop_top: box.top,
        crop_width: box.width,
        crop_height: box.height,
        save_as: saveAs,
      })
      // 同時也把 CV 搜尋範圍（橘框）一起 apply：
      //   cvCustomized = true 才寫 search_region（沒拖過就讓步驟層級 cv_search_radius 接管）
      //   cv_strict_region 永遠寫（即使是 false 也要明確寫，避免上一次 true 沒清掉）
      const patch: any = {
        image: res.image,
        anchor_off_x: res.anchor_off_x,
        anchor_off_y: res.anchor_off_y,
        x: clickPos.x,       // 點擊座標（可能被拖曳調整過）
        y: clickPos.y,
        // 編輯錨點的意義就是「要用圖像比對」→ 自動切到圖像模式（use_coord=false）
        // 不然系統還是走座標模式，永遠點原座標、錨點完全沒用到
        use_coord: false,
        cv_strict_region: cvStrictRegion,
      }
      if (cvCustomized) {
        patch.search_region = [cvBox.left, cvBox.top, cvBox.width, cvBox.height]
      } else {
        // 沒自訂 → 清空可能殘留的舊 search_region（沿用步驟預設）
        patch.search_region = []
      }
      onApply(patch)
      const cvNote = cvCustomized
        ? `；CV 搜尋範圍 ${cvBox.width}×${cvBox.height}${cvStrictRegion ? '・嚴格' : ''}`
        : (cvStrictRegion ? '；CV 嚴格鎖定（用步驟預設範圍）' : '')
      toast.success(`錨點已更新（${res.width}×${res.height}, variance=${res.variance}）${cvNote}`)
      onClose()
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  // 重置：依 mode 回到預設
  const handleReset = () => {
    if (editMode === 'ocr') {
      const r = searchRadius
      setOcrBox({ left: clickPos.x - r, top: clickPos.y - r, width: r * 2, height: r * 2 })
      return
    }
    // anchor 分頁：依 anchorTarget 決定重置哪個框
    if (anchorTarget === 'orange') {
      const r = searchRadius
      setCvBox({ left: clickPos.x - r, top: clickPos.y - r, width: r * 2, height: r * 2 })
      setCvCustomized(false)   // 重置 = 回到「沿用步驟預設」狀態
    } else {
      setBox({ left: clickPos.x - 120, top: clickPos.y - 40, width: 240, height: 80 })
    }
  }

  // OCR 範圍面積（用於效能提醒）
  const ocrArea = ocrBox.width * ocrBox.height
  // 經驗值：全 1080p (~2M px²) 大約 400-800ms，半屏 (~1M) 約 150-300ms
  const ocrPerfTier: 'fast' | 'medium' | 'slow' =
    ocrArea < 800_000 ? 'fast' : ocrArea < 2_000_000 ? 'medium' : 'slow'
  const ocrPerfNote = {
    fast:   { color: 'bg-blue-50 border-blue-200 text-blue-800',      text: '⚡ 範圍小 → OCR 速度快（約 <200ms）' },
    medium: { color: 'bg-amber-50 border-amber-200 text-amber-800',   text: '⚠️ 範圍中等 → OCR 約 200-500ms，可接受' },
    slow:   { color: 'bg-rose-50 border-rose-200 text-rose-800',      text: '🐢 範圍很大 → OCR 可能 >500ms，影響動作節奏。建議盡量縮到目標文字附近。' },
  }[ocrPerfTier]

  if (!fullImg) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div className="bg-white rounded-xl p-6 max-w-md">
          <h3 className="text-lg font-semibold mb-2">無法編輯錨點</h3>
          <p className="text-sm text-gray-600 mb-4">
            這個動作沒有錄製時的全螢幕截圖（可能是舊版錄製的）。請重新錄製這個動作才能手動圈選錨點。
          </p>
          <button onClick={onClose} className="px-4 py-1.5 bg-gray-200 rounded-lg text-sm">關閉</button>
        </div>
      </div>
    )
  }

  // 根據錨點框大小給不同場景建議（取代原本單純的 variance 警告）
  // 小錨點 → 追蹤會移動的元素；大錨點 → 用周圍結構定位特徵少的目標
  const boxArea = box.width * box.height
  const sizeTier: 'small' | 'medium' | 'large' =
    boxArea < 10000 ? 'small' : boxArea > 30000 ? 'large' : 'medium'

  const sizeGuidance = {
    small: {
      icon: '🎯',
      title: '小錨點',
      color: 'bg-blue-50 border-blue-200 text-blue-800',
      titleColor: 'text-blue-700',
      desc: '適合追蹤會「獨立移動」的元素，例如可被拖到不同位置的 icon、可重新排序的選單項目。目標本身就是唯一特徵，找到它就點它。',
    },
    medium: {
      icon: '⚖️',
      title: '中等錨點',
      color: 'bg-gray-50 border-gray-200 text-gray-700',
      titleColor: 'text-gray-700',
      desc: '預設尺寸，適合一般按鈕、文字標籤、圖示等「目標自帶特徵」的情境。',
    },
    large: {
      icon: '🌐',
      title: '大錨點',
      color: 'bg-emerald-50 border-emerald-200 text-emerald-800',
      titleColor: 'text-emerald-700',
      desc: '適合以周圍穩定 UI 結構定位特徵稀少的目標。例如 Excel 空白儲存格 → 納入列號/欄字母；空白對話框區域 → 納入周圍邊框或標題列。',
    },
  }[sizeTier]

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-2" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl flex flex-col"
        style={{ width: '96vw', height: '96vh' }}
        onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center gap-3 px-5 py-3 border-b border-gray-100">
          <h3 className="font-semibold text-gray-800">✏️ 編輯錨點 — 動作 #{actionIndex + 1}</h3>
          {/* 模式切換：CV 錨點分頁（綠框錨點 + 橘框搜尋範圍合在這） / 藍框 OCR */}
          <div className="flex items-center gap-0 ml-4 border border-gray-200 rounded-lg overflow-hidden text-xs">
            <button
              onClick={() => setEditMode('anchor')}
              className={`px-3 py-1.5 transition-colors ${
                editMode === 'anchor'
                  ? 'bg-emerald-500 text-white font-semibold'
                  : 'bg-white text-gray-600 hover:bg-gray-50'
              }`}
            >🟩 CV 錨點</button>
            <button
              onClick={() => setEditMode('ocr')}
              className={`px-3 py-1.5 transition-colors ${
                editMode === 'ocr'
                  ? 'bg-blue-500 text-white font-semibold'
                  : 'bg-white text-gray-600 hover:bg-gray-50'
              }`}
            >🟦 OCR 範圍</button>
          </div>
          {/* 多螢幕切換：只有兩張以上實體螢幕時顯示（monitors[1..N] 代表實體螢幕）*/}
          {/* 切單螢幕後畫面放大好幾倍，小按鈕、文字都看得清楚 */}
          {monitors.length > 2 && (
            <div className="flex items-center gap-0 ml-2 border border-gray-200 rounded-lg overflow-hidden text-xs">
              <button
                onClick={() => setViewMode('all')}
                className={`px-3 py-1.5 transition-colors ${
                  viewMode === 'all'
                    ? 'bg-gray-700 text-white font-semibold'
                    : 'bg-white text-gray-600 hover:bg-gray-50'
                }`}
                title="顯示整個虛擬桌面"
              >🖥️ 全部</button>
              {monitors.slice(1).map((_m, i) => {
                const idx = i + 1
                return (
                  <button
                    key={idx}
                    onClick={() => setViewMode(idx)}
                    className={`px-3 py-1.5 transition-colors ${
                      viewMode === idx
                        ? 'bg-gray-700 text-white font-semibold'
                        : 'bg-white text-gray-600 hover:bg-gray-50'
                    }`}
                    title={`${monitors[idx].width}×${monitors[idx].height} @ (${monitors[idx].left},${monitors[idx].top})`}
                  >🖥️ {idx}</button>
                )
              })}
            </div>
          )}
          <div className="flex-1" />
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X className="w-4 h-4" /></button>
        </div>

        <div className="flex flex-1 min-h-0 overflow-hidden">
          {/* 左側：Canvas */}
          <div ref={containerRef} className="flex-1 overflow-auto p-5 bg-gray-100">
            <canvas
              ref={canvasRef}
              onMouseDown={onMouseDown}
              onMouseMove={onMouseMove}
              onMouseUp={onMouseUp}
              onMouseLeave={onMouseUp}
              className="border border-gray-300 cursor-move"
              style={{ cursor: dragMode === 'move' ? 'grabbing' : 'default' }}
            />
          </div>

          {/* 右側：預覽 + 控制 */}
          <div className="w-72 border-l border-gray-200 flex flex-col p-4 space-y-3 overflow-y-auto">
            {editMode === 'anchor' ? (
              <>
                {/* 顯示「上次拖了哪個框」當狀態指示。點哪個框就拖哪個（不用先切這） */}
                <div className="flex items-center gap-0 border border-gray-200 rounded-lg overflow-hidden text-xs">
                  <div className={`flex-1 px-2 py-1.5 text-center ${
                    anchorTarget === 'green'
                      ? 'bg-emerald-500 text-white font-semibold'
                      : 'bg-white text-gray-500'
                  }`}>🟩 綠框（錨點）{anchorTarget === 'green' ? ' · 上次拖' : ''}</div>
                  <div className={`flex-1 px-2 py-1.5 text-center ${
                    anchorTarget === 'orange'
                      ? 'bg-orange-500 text-white font-semibold'
                      : 'bg-white text-gray-500'
                  }`}>🟧 橘框（搜尋範圍）{anchorTarget === 'orange' ? ' · 上次拖' : ''}</div>
                </div>
                <p className="text-[11px] text-gray-500 -mt-1">
                  💡 直接點哪個框就拖哪個。綠框優先（重疊時），點到綠框外的橘框區域就拖橘框
                </p>

                {anchorTarget === 'green' ? (
                  <>
                    <div>
                      <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">🟩 CV 錨點（綠框）</div>
                      {preview && (
                        <img src={preview} alt="anchor preview"
                          style={{ imageRendering: 'pixelated' }}
                          className="border border-gray-300 bg-checkered w-full" />
                      )}
                      <div className="text-xs text-gray-500 mt-1 font-mono">
                        {box.width} × {box.height} px
                      </div>
                    </div>

                    <div className={`p-3 border rounded-lg ${sizeGuidance.color}`}>
                      <div className={`text-sm font-bold mb-1 ${sizeGuidance.titleColor}`}>
                        {sizeGuidance.icon} {sizeGuidance.title}
                      </div>
                      <div className="text-xs leading-relaxed">{sizeGuidance.desc}</div>
                      <div className="text-[11px] text-gray-500 mt-2 pt-2 border-t border-current opacity-60 font-mono">
                        面積 {boxArea.toLocaleString()} px² · variance {variance}
                      </div>
                    </div>
                  </>
                ) : (
                  <>
                    <div>
                      <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">🟧 CV 搜尋範圍（橘框）</div>
                      <div className="text-xs text-gray-500 mt-1 font-mono">
                        {cvBox.width} × {cvBox.height} px · 面積 {(cvBox.width * cvBox.height).toLocaleString()} px²
                      </div>
                      <div className="text-[11px] text-gray-400 mt-0.5 font-mono">
                        left={cvBox.left} top={cvBox.top}
                      </div>
                      {cvCustomized ? (
                        <div className="text-[11px] text-orange-700 mt-1 font-medium">
                          已自訂此動作的搜尋範圍（套用後寫入 search_region，覆蓋步驟預設）
                        </div>
                      ) : (
                        <div className="text-[11px] text-gray-500 mt-1">
                          目前用步驟預設「錄製座標 ±{searchRadius}px」。拖一下這個橘框就會切換成自訂
                        </div>
                      )}
                    </div>
                  </>
                )}

                <div className="p-2 bg-gray-50 rounded-lg text-xs text-gray-600 leading-relaxed space-y-1.5">
                  <div className="font-semibold text-gray-700">🔎 CV 三階段搜尋</div>
                  {cvStrictRegion && cvCustomized ? (
                    <>
                      <div className="text-orange-700 font-medium">⛔ 嚴格鎖定：只跑 Phase 1，橘框內找不到立即 FAIL</div>
                      <div className="text-gray-400 line-through">Phase 2 錄製座標附近（已 skip）</div>
                      <div className="text-gray-400 line-through">Phase 3 整個螢幕（已 skip）</div>
                      <div className="text-gray-400 line-through">退回錄製座標硬點（已 skip）</div>
                    </>
                  ) : (
                    <>
                      <div>1️⃣ <b>橘框內</b> 找綠框錨點（速度快、避開跨螢幕誤判）</div>
                      <div>2️⃣ 橘框沒找到 → <b>錄製座標附近 ±{searchRadius}px</b> 再試</div>
                      <div>3️⃣ 還是沒找到 → <b>整個螢幕</b> 最後保險（除非勾「只搜附近」）</div>
                      <div>✅ 找到 → 點擊位置 = 錨點中心 + 紅十字偏移</div>
                      <div>❌ 全部失敗 → 看 cv_coord_fallback 決定 FAIL 或退錄製座標</div>
                    </>
                  )}
                </div>

                {/* 嚴格鎖定 toggle（CV 用）：只在自訂橘框時才有意義，沒自訂時 grey out */}
                <label className={`flex items-start gap-2 p-2 rounded-lg border transition-colors ${
                  !cvCustomized ? 'bg-gray-50 border-gray-200 cursor-not-allowed opacity-60'
                  : cvStrictRegion ? 'bg-orange-50 border-orange-300 cursor-pointer'
                  : 'bg-white border-gray-200 hover:border-orange-300 cursor-pointer'
                }`}>
                  <input
                    type="checkbox"
                    disabled={!cvCustomized}
                    checked={cvStrictRegion}
                    onChange={e => setCvStrictRegion(e.target.checked)}
                    className="mt-0.5 w-4 h-4 accent-orange-600"
                  />
                  <div className="flex-1">
                    <div className="text-xs font-medium text-gray-800">⛔ 嚴格鎖定橘框（找不到立即 fail）</div>
                    <div className="text-[11px] text-gray-600 leading-relaxed mt-0.5">
                      {!cvCustomized ? (
                        <>先拖橘框自訂搜尋範圍，這個 toggle 才生效</>
                      ) : cvStrictRegion ? (
                        <>
                          <strong className="text-orange-700">已啟用</strong>：只認橘框。
                          Phase 2 附近、Phase 3 全螢幕、cv_coord_fallback 退座標 全部 skip。
                          適合「目標必須在固定區域才合法」的場景
                        </>
                      ) : (
                        <>
                          預設關。橘框沒命中會擴大到附近 → 全螢幕。多數場景用這個。
                          要鎖死位置避免誤點才需要勾起來
                        </>
                      )}
                    </div>
                  </div>
                </label>

                <div className="p-2 bg-gray-50 rounded-lg text-xs text-gray-600 space-y-1">
                  <div>🎯 紅十字 = 點擊座標 <b className="text-red-600">（可拖曳調整）</b></div>
                  <div>🟩 綠框 = 錨點樣板（上面切「拖綠框」才能拖）</div>
                  <div>🟧 橘框 = CV 搜尋範圍（上面切「拖橘框」才能拖；沒拖過就用步驟預設）</div>
                  <div className="text-gray-500 pt-1 border-t border-gray-200 font-mono">
                    點擊座標：({clickPos.x}, {clickPos.y})
                  </div>
                </div>
                <div className="p-2 bg-purple-50 border border-purple-200 rounded-lg text-xs text-purple-800 leading-relaxed">
                  <strong>💡 小錨點 vs 大錨點</strong><br/>
                  小 = 追會移動的元素（icon、選單項）；大 = 以周圍 UI 結構定位特徵稀少的目標（空白儲存格、對話框）。
                </div>
              </>
            ) : (
              <>
                <div>
                  <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">🟦 OCR 搜尋範圍（藍框）</div>
                  {ocrPreview && (
                    <img src={ocrPreview} alt="ocr range preview"
                      className="border border-gray-300 w-full" />
                  )}
                  <div className="text-xs text-gray-500 mt-1 font-mono">
                    {ocrBox.width} × {ocrBox.height} px · 面積 {ocrArea.toLocaleString()} px²
                  </div>
                  <div className="text-[11px] text-gray-400 mt-0.5 font-mono">
                    left={ocrBox.left} top={ocrBox.top}
                  </div>
                </div>

                <div className={`p-3 border rounded-lg text-xs leading-relaxed ${ocrPerfNote.color}`}>
                  {ocrPerfNote.text}
                </div>

                <div className="p-2 bg-gray-50 rounded-lg text-xs text-gray-600 leading-relaxed space-y-1.5">
                  <div className="font-semibold text-gray-700">🔤 OCR 三階段搜尋</div>
                  {ocrStrictRegion ? (
                    <>
                      <div className="text-orange-700 font-medium">⛔ 嚴格鎖定模式：只跑 Phase 1，框內找不到立即 FAIL</div>
                      <div className="text-gray-400 line-through">2️⃣ 附近 ±400px（已 skip）</div>
                      <div className="text-gray-400 line-through">3️⃣ 整個螢幕（已 skip）</div>
                    </>
                  ) : (
                    <>
                      <div>1️⃣ <b>框內</b> 找 <code className="px-1 bg-white rounded font-mono">ocr_text</code>（速度快、避開跨螢幕誤判）</div>
                      <div>2️⃣ 框內沒找到 → <b>錄製座標附近 ±400px</b> 再試一次</div>
                      <div>3️⃣ 還是沒找到 → <b>整個螢幕</b> 最後保險再試一次</div>
                      <div>4️⃣ 找到 → 點文字中心（<b>忽略紅十字座標</b>）</div>
                      <div>5️⃣ 三階段都失敗 → 依 ocr_cv_fallback 決定 FAIL 或退回 CV 錨點</div>
                      <div className="text-gray-500 pt-1 mt-1 border-t border-gray-200">
                        👉 框是「優先位置」不是「強制位置」。畫面飄位（開始選單、動態 UI）也能找到
                      </div>
                    </>
                  )}
                </div>

                {/* 嚴格鎖定範圍 toggle：適合「目標必須在固定位置才合法」的場景 */}
                <label className={`flex items-start gap-2 p-2 rounded-lg cursor-pointer border transition-colors ${
                  ocrStrictRegion ? 'bg-orange-50 border-orange-300' : 'bg-white border-gray-200 hover:border-orange-300'
                }`}>
                  <input
                    type="checkbox"
                    checked={ocrStrictRegion}
                    onChange={e => setOcrStrictRegion(e.target.checked)}
                    className="mt-0.5 w-4 h-4 accent-orange-600"
                  />
                  <div className="flex-1">
                    <div className="text-xs font-medium text-gray-800">⛔ 嚴格鎖定範圍（找不到立即 fail）</div>
                    <div className="text-[11px] text-gray-600 leading-relaxed mt-0.5">
                      {ocrStrictRegion ? (
                        <>
                          <strong className="text-orange-700">已啟用</strong>：只認框內。Phase 2 附近、Phase 3 全螢幕、CV fallback 全部 skip。
                          適合「目標必須在固定位置才合法」（例：通知必須在右下角、不能誤點其他地方同名元素）
                        </>
                      ) : (
                        <>
                          預設關。框是優先位置；找不到會擴大到附近 → 全螢幕。多數場景用這個。
                          要鎖死位置避免誤點才需要勾起來
                        </>
                      )}
                    </div>
                  </div>
                </label>

                <div className="p-2 bg-gray-50 rounded-lg text-xs text-gray-600 space-y-1">
                  <div>🎯 紅十字 = 點擊座標 <span className="text-gray-400">(OCR 模式不使用)</span></div>
                  <div>🟦 藍框 = OCR 掃描區域（拖中間移動、拖左上/右下角改大小）</div>
                  <div className="text-gray-500 pt-1 border-t border-gray-200 font-mono">
                    點擊座標：({clickPos.x}, {clickPos.y})
                  </div>
                </div>
                <div className="p-2 bg-blue-50 border border-blue-200 rounded-lg text-xs text-blue-800 leading-relaxed">
                  <strong>💡 套用後會自動勾 use_ocr</strong><br/>
                  記得在動作列也填好 <code className="px-1 bg-white rounded font-mono">ocr_text</code> 才算完整設定。
                </div>
              </>
            )}

            <div className="flex-1" />

            <button onClick={handleReset}
              className="flex items-center justify-center gap-1.5 px-3 py-2 border border-gray-200 rounded-lg text-sm text-gray-600 hover:bg-gray-50">
              <RotateCcw className="w-3.5 h-3.5" />
              {editMode === 'ocr'
                ? `重置藍框（${searchRadius * 2}×${searchRadius * 2}）`
                : anchorTarget === 'orange'
                  ? `重置橘框（${searchRadius * 2}×${searchRadius * 2}，回用步驟預設）`
                  : '重置綠框（以點擊點為中心 240×80）'}
            </button>

            <button onClick={handleConfirm}
              className={`flex items-center justify-center gap-1.5 px-3 py-2 text-white rounded-lg text-sm font-medium ${
                editMode === 'ocr' ? 'bg-blue-600 hover:bg-blue-700'
                : 'bg-purple-600 hover:bg-purple-700'
              }`}>
              <Check className="w-4 h-4" /> 確認套用
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
