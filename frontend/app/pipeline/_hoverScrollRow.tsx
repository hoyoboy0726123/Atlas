'use client'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

/**
 * 一條水平 toolbar:子元素保持原寬度(w-max、不壓縮)。
 * 容器寬度動態量「從容器左邊到視窗右邊的可用空間」、超過就 overflow:hidden 裁掉。
 * 被裁的方向會浮出明顯的圓形 chevron 按鈕:
 *   - 滑鼠停在按鈕上 → 連續往那方向滾(rAF)
 *   - 點按鈕 → 跳一步(~180px、200ms 過渡)
 */
export default function HoverScrollRow({
  children,
  className = '',
  speed = 5,
}: {
  children: React.ReactNode
  className?: string
  speed?: number  // 每幀像素
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const trackRef = useRef<HTMLDivElement>(null)
  const scrollXRef = useRef(0)
  const maxOverflowRef = useRef(0)
  const hoverDirRef = useRef<'left' | 'right' | null>(null)
  const rafRef = useRef<number | null>(null)
  const [canLeft, setCanLeft] = useState(false)
  const [canRight, setCanRight] = useState(false)

  const measure = useCallback(() => {
    const c = containerRef.current
    const t = trackRef.current
    if (!c || !t) return
    // 動態算容器最大寬度:從容器自己的左邊到視窗右邊扣 1rem 邊距
    const rect = c.getBoundingClientRect()
    const available = Math.max(200, Math.floor(window.innerWidth - rect.left - 16))
    c.style.maxWidth = `${available}px`
    // 量 overflow
    const inner = t.scrollWidth
    const outer = c.clientWidth
    const overflow = Math.max(0, inner - outer)
    maxOverflowRef.current = overflow
    if (scrollXRef.current < -overflow) {
      scrollXRef.current = -overflow
      t.style.transform = `translateX(${-overflow}px)`
    }
    setCanLeft(scrollXRef.current < 0)
    setCanRight(scrollXRef.current > -overflow)
  }, [])

  useEffect(() => {
    measure()
    const ro = new ResizeObserver(measure)
    if (containerRef.current) ro.observe(containerRef.current)
    if (trackRef.current) ro.observe(trackRef.current)
    window.addEventListener('resize', measure)
    return () => { ro.disconnect(); window.removeEventListener('resize', measure) }
  }, [measure])

  const startHover = (dir: 'left' | 'right') => {
    hoverDirRef.current = dir
    if (rafRef.current != null) return
    const tick = () => {
      const d = hoverDirRef.current
      if (!d) { rafRef.current = null; return }
      const cur = scrollXRef.current
      const max = maxOverflowRef.current
      const next = d === 'right'
        ? Math.max(-max, cur - speed)
        : Math.min(0, cur + speed)
      if (next !== cur) {
        scrollXRef.current = next
        if (trackRef.current) trackRef.current.style.transform = `translateX(${next}px)`
        const newLeft = next < 0
        const newRight = next > -max
        setCanLeft(prev => prev === newLeft ? prev : newLeft)
        setCanRight(prev => prev === newRight ? prev : newRight)
      }
      rafRef.current = requestAnimationFrame(tick)
    }
    rafRef.current = requestAnimationFrame(tick)
  }
  const stopHover = () => { hoverDirRef.current = null }

  const jumpStep = (dir: 'left' | 'right') => {
    const STEP = 180
    const cur = scrollXRef.current
    const max = maxOverflowRef.current
    const next = dir === 'right' ? Math.max(-max, cur - STEP) : Math.min(0, cur + STEP)
    scrollXRef.current = next
    const t = trackRef.current
    if (t) {
      t.style.transition = 'transform 200ms ease'
      t.style.transform = `translateX(${next}px)`
      setTimeout(() => { if (t) t.style.transition = 'none' }, 220)
    }
    setCanLeft(next < 0)
    setCanRight(next > -max)
  }

  // 卸載清掉 rAF
  useEffect(() => () => { if (rafRef.current != null) cancelAnimationFrame(rafRef.current) }, [])

  return (
    <div ref={containerRef} className={`relative overflow-hidden ${className}`}>
      <div
        ref={trackRef}
        className="flex gap-2 w-max"
        style={{ transform: `translateX(${scrollXRef.current}px)`, willChange: 'transform' }}
      >
        {children}
      </div>

      {/* 左側裝飾漸層(pointer-events: none、不擋按鈕點擊)*/}
      {canLeft && (
        <div
          className="absolute left-0 top-0 bottom-0 pointer-events-none"
          style={{ width: 56, background: 'linear-gradient(to right, rgba(255,255,255,0.92) 35%, rgba(255,255,255,0))' }}
        />
      )}
      {/* 左側按鈕:停=連續滾、點=跳一步 */}
      {canLeft && (
        <button
          onMouseEnter={() => startHover('left')}
          onMouseLeave={stopHover}
          onClick={() => jumpStep('left')}
          title="拖回左邊(停在這裡會自動滾)"
          className="absolute left-1 top-1/2 -translate-y-1/2 w-7 h-7 rounded-full bg-white border border-gray-300 shadow-md flex items-center justify-center text-gray-600 hover:text-blue-600 hover:border-blue-300 transition-colors z-10"
        >
          <ChevronLeft className="w-4 h-4" />
        </button>
      )}

      {/* 右側裝飾漸層 */}
      {canRight && (
        <div
          className="absolute right-0 top-0 bottom-0 pointer-events-none"
          style={{ width: 56, background: 'linear-gradient(to left, rgba(255,255,255,0.92) 35%, rgba(255,255,255,0))' }}
        />
      )}
      {/* 右側按鈕 */}
      {canRight && (
        <button
          onMouseEnter={() => startHover('right')}
          onMouseLeave={stopHover}
          onClick={() => jumpStep('right')}
          title="拖向右邊(停在這裡會自動滾)"
          className="absolute right-1 top-1/2 -translate-y-1/2 w-7 h-7 rounded-full bg-white border border-gray-300 shadow-md flex items-center justify-center text-gray-600 hover:text-blue-600 hover:border-blue-300 transition-colors z-10"
        >
          <ChevronRight className="w-4 h-4" />
        </button>
      )}
    </div>
  )
}
