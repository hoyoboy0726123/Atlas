'use client'
/**
 * 喚醒視窗插入器 —— 一鍵加入 activate_window + wait 兩個動作。
 * 場景:目標網頁閒置數小時被 Edge 睡眠分頁卸載,UIA 找得到視窗卻讀不到
 * 任何元素,背景喚醒叫不醒 —— 操作前要明確拉到前景讓分頁重載。
 */
import { useEffect, useState } from 'react'
import { AppWindow, RefreshCcw } from 'lucide-react'
import { toast } from 'sonner'
import type { ComputerUseAction } from './_helpers'
import { windowKeyword } from './_helpers'
import { uiaListWindows, uiaHighlight, type UiaWindowInfo } from '@/lib/api'

interface Props {
  index: number
  isOpen: boolean
  /** 節點的目標視窗 pattern(自動去掉 * 當預設關鍵字) */
  defaultTitle: string
  openMenu: () => void
  closeMenu: () => void
  onAdd: (index: number, action: ComputerUseAction) => void
  onAddMany: (index: number, actions: ComputerUseAction[]) => void
}

export default function WakeWindowInserter({ index, isOpen, defaultTitle, openMenu, closeMenu, onAdd, onAddMany }: Props) {
  const [title, setTitle] = useState('')
  const [waitSec, setWaitSec] = useState('1.5')
  const [wins, setWins] = useState<UiaWindowInfo[] | null>(null)
  const [loadingWins, setLoadingWins] = useState(false)
  // 每次打開都重帶節點「目標視窗」—— 使用者剛在 Inspector 選了別的視窗再來插喚醒,
  // 若沿用上次打的字就會喚醒錯視窗(實測使用者只好手打標題)
  useEffect(() => {
    if (isOpen) {
      setTitle(windowKeyword(defaultTitle))
      setWins(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen])

  const loadWins = async () => {
    setLoadingWins(true)
    try {
      const r = await uiaListWindows()
      setWins((r.windows || []).filter(w => (w.name || '').trim() && !w.is_offscreen))
    } catch (e) {
      toast.error(`列視窗失敗:${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setLoadingWins(false)
    }
  }

  const submit = () => {
    if (!title.trim()) { toast.error('請填視窗標題關鍵字'); return }
    const t = title.trim()
    const sec = Number(waitSec) || 1.5
    // 一定要一次插兩個 —— 連叫兩次 onAdd 會因 React 狀態競態互相蓋掉(實測)
    onAddMany(index, [
      {
        type: 'activate_window',
        title_contains: t,
        description: `喚醒「${t}」視窗(拉到前景、睡眠分頁重載)`,
      } as ComputerUseAction,
      { type: 'wait', seconds: sec, description: `等視窗載入 ${sec}s` },
    ])
    toast.success(`已插入喚醒「${t}」＋等待 ${sec}s`)
    closeMenu()
  }

  if (!isOpen) {
    return (
      <div className="flex justify-center -my-0.5">
        <button
          data-vlm-insert-trigger
          type="button"
          onClick={openMenu}
          title="喚醒視窗（activate_window + 等待）：閒置太久的網頁會被瀏覽器卸載，操作前要拉到前景重載"
          className="opacity-30 hover:opacity-100 transition-opacity flex items-center gap-0.5 px-2 py-0.5 rounded-full whitespace-nowrap text-[9px] text-cyan-600 border border-dashed border-cyan-300 hover:bg-cyan-50"
        >
          <AppWindow className="w-2.5 h-2.5" /> 喚醒
        </button>
      </div>
    )
  }

  return (
    // data-vlm-insert-popover:面板「點外面關閉」只認這個屬性
    <div data-vlm-insert-popover
         className="my-1 rounded-lg border border-cyan-200 bg-cyan-50/60 p-2 space-y-1.5">
      <div className="flex items-center gap-1 text-[11px] font-semibold text-cyan-800">
        <AppWindow className="w-3 h-3 shrink-0" />
        <span>喚醒視窗（拉到前景 + 等載入）</span>
      </div>
      <div className="block">
        <span className="text-[10px] text-gray-500">視窗標題關鍵字（包含比對）</span>
        <div className="flex gap-1">
          <input value={title} onChange={e => setTitle(e.target.value)}
            placeholder="例：E-Quote測試靶"
            className="flex-1 min-w-0 text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-cyan-500" />
          <button type="button" onClick={loadWins} disabled={loadingWins}
            title="列出目前開著的視窗,點一個自動填關鍵字"
            className="shrink-0 whitespace-nowrap flex items-center gap-0.5 text-[10px] px-1.5 py-1 rounded border border-cyan-300 text-cyan-700 bg-white hover:bg-cyan-50 disabled:opacity-50">
            {loadingWins ? <RefreshCcw className="w-3 h-3 animate-spin" /> : <AppWindow className="w-3 h-3" />}
            列視窗
          </button>
        </div>
      </div>
      {wins && (
        <div className="max-h-44 overflow-y-auto rounded border border-cyan-200 bg-white">
          {wins.length === 0 && <div className="px-2 py-2 text-[10px] text-gray-400 text-center">沒有可顯示的視窗</div>}
          {wins.map((w, i) => (
            <button key={i} type="button"
              onClick={() => { setTitle(windowKeyword(w.name)); setWins(null) }}
              onMouseEnter={() => {
                if (w.rect?.length === 4 && w.rect[2] > 0) {
                  uiaHighlight({ x: w.rect[0], y: w.rect[1], width: w.rect[2], height: w.rect[3], ttl_ms: 1500 }).catch(() => {})
                }
              }}
              className="w-full text-left px-2 py-1 border-b border-gray-100 last:border-b-0 hover:bg-cyan-50">
              <div className="text-[11px] text-gray-800 truncate" title={w.name}>{windowKeyword(w.name) || w.name}</div>
              <div className="text-[9px] text-gray-400 truncate">{w.name}</div>
            </button>
          ))}
        </div>
      )}
      <label className="flex items-center gap-1 text-[10px] text-gray-500 whitespace-nowrap">
        喚醒後等
        <input value={waitSec} onChange={e => setWaitSec(e.target.value)}
          className="w-14 text-[11px] px-1.5 py-1 rounded border border-gray-300 text-right" />
        秒（睡眠分頁重載需要時間）
      </label>
      <div className="flex gap-1 pt-0.5">
        <button type="button" onClick={submit}
          className="flex-1 text-[10px] bg-cyan-600 text-white px-2 py-1 rounded hover:bg-cyan-700">插入（共 2 個動作）</button>
        <button type="button" onClick={closeMenu}
          className="text-[10px] text-gray-500 hover:text-gray-800 px-2 py-1 rounded border border-gray-200">取消</button>
      </div>
      <p className="text-[9px] text-gray-500 leading-snug">
        需要的場景：排程半夜跑、頁面開著一整天不動。日常「人開著頁面按執行」不用加。
        會把該視窗拉到前景（搶焦點是喚醒的必要行為）。
      </p>
    </div>
  )
}
