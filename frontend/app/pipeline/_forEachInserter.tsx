'use client'
/**
 * 逐筆迴圈插入器 —— 建立 for_each 動作。
 * 三種範圍:包「此位置之後」的動作(前面有錄製/讀剪貼簿等前置時用)、
 * 包全部、空迴圈。清單用多行 textarea 貼(一行一筆,Excel 直行直接貼)。
 */
import { useEffect, useState } from 'react'
import { Repeat } from 'lucide-react'
import { toast } from 'sonner'
import type { ComputerUseAction } from './_helpers'

interface Props {
  index: number
  isOpen: boolean
  hasActions: boolean
  /** 此插入點之後有幾個動作(wrapAfter 的範圍) */
  tailCount: number
  openMenu: () => void
  closeMenu: () => void
  onInsert: (index: number, action: ComputerUseAction) => void
  onWrapAll: (action: ComputerUseAction) => void
  onWrapAfter: (index: number, action: ComputerUseAction) => void
}

export default function ForEachInserter({ index, isOpen, hasActions, tailCount, openMenu, closeMenu, onInsert, onWrapAll, onWrapAfter }: Props) {
  const [items, setItems] = useState('')
  const [saveAs, setSaveAs] = useState('品規')
  const [cont, setCont] = useState(true)
  const [splitAs, setSplitAs] = useState('')
  const [splitSep, setSplitSep] = useState('-')
  const [mode, setMode] = useState<'wrapAfter' | 'wrap' | 'empty'>('wrap')
  // 開啟時依位置挑預設:中間的插入點 → 包之後;最前面 → 包全部;沒動作 → 空迴圈
  useEffect(() => {
    if (isOpen) {
      setMode(tailCount > 0 && index > 0 ? 'wrapAfter' : hasActions ? 'wrap' : 'empty')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen])

  const submit = () => {
    if (!items.trim()) { toast.error('請貼清單（或填 {{變數}}）'); return }
    if (!saveAs.trim()) { toast.error('請填變數名稱'); return }
    const fe: ComputerUseAction = {
      type: 'for_each',
      items: items.trim(),
      save_as: saveAs.trim(),
      continue_on_error: cont,
      ...(splitAs.trim() ? { split_as: splitAs.trim(), split_sep: splitSep || '-' } : {}),
      do: [],
      description: `逐筆迴圈 → {{${saveAs.trim()}}}`,
    }
    if (mode === 'wrapAfter' && tailCount > 0) {
      onWrapAfter(index, fe)
      toast.success(`已把之後的 ${tailCount} 個動作包進迴圈 — 記得把「填值」動作的文字改成 {{${saveAs.trim()}}}`, { duration: 8000 })
    } else if (mode === 'wrap' && hasActions) {
      onWrapAll(fe)
      toast.success(`已把全部動作包進迴圈 — 記得把「填值」動作的文字改成 {{${saveAs.trim()}}}`, { duration: 8000 })
    } else {
      onInsert(index, fe)
      toast.success('已插入空迴圈 — 子動作用 YAML 或 AI 助手放進 do:')
    }
    closeMenu()
  }

  if (!isOpen) {
    return (
      <div className="flex justify-center -my-0.5">
        <button
          data-vlm-insert-trigger
          type="button"
          onClick={openMenu}
          title="逐筆迴圈：清單裡每一筆各跑一遍子動作（例：5 個品規逐一查詢匯出）"
          className="opacity-30 hover:opacity-100 transition-opacity flex items-center gap-0.5 px-2 py-0.5 rounded-full whitespace-nowrap text-[9px] text-fuchsia-600 border border-dashed border-fuchsia-300 hover:bg-fuchsia-50"
        >
          <Repeat className="w-2.5 h-2.5" /> 逐筆
        </button>
      </div>
    )
  }

  return (
    // data-vlm-insert-popover:面板「點外面關閉」只認這個屬性
    <div data-vlm-insert-popover
         className="my-1 rounded-lg border border-fuchsia-200 bg-fuchsia-50/60 p-2 space-y-1.5">
      <div className="flex items-center gap-1 text-[11px] font-semibold text-fuchsia-800">
        <Repeat className="w-3 h-3 shrink-0" />
        <span>逐筆迴圈：每一筆各跑一遍</span>
      </div>
      <label className="block">
        <span className="text-[10px] text-gray-500">
          {'清單（一行一筆，可從 Excel 直行貼上；清單在別的視窗時填 {{變數}}，先用「讀文字」/「讀剪貼簿」抓下來）'}
        </span>
        <textarea
          value={items}
          onChange={e => setItems(e.target.value)}
          rows={5}
          placeholder={'UX3407%\nRC71L%\nGU605%\n或 {{清單原文}} / {{ input.品規清單 }}'}
          className="w-full text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-fuchsia-500 font-mono resize-y"
        />
      </label>
      <div className="flex items-center gap-2 flex-wrap">
        <label className="flex items-center gap-1 text-[10px] text-gray-500 whitespace-nowrap">
          每輪存到變數
          <input value={saveAs} onChange={e => setSaveAs(e.target.value)}
            className="w-20 text-[11px] px-1.5 py-1 rounded border border-gray-300 font-mono" />
        </label>
        <label className="flex items-center gap-1 text-[10px] text-gray-600 whitespace-nowrap cursor-pointer">
          <input type="checkbox" checked={cont} onChange={e => setCont(e.target.checked)} />
          某筆失敗跳下一筆繼續
        </label>
      </div>
      <div className="flex items-center gap-1.5 flex-wrap text-[10px] text-gray-500">
        <span className="whitespace-nowrap">每筆拆成多個變數(選填)</span>
        <input value={splitAs} onChange={e => setSplitAs(e.target.value)}
          placeholder="例：查詢年|查詢月"
          className="flex-1 min-w-[110px] text-[11px] px-1.5 py-1 rounded border border-gray-300 font-mono" />
        <span className="whitespace-nowrap">分隔符</span>
        <input value={splitSep} onChange={e => setSplitSep(e.target.value)}
          className="w-9 text-[11px] px-1.5 py-1 rounded border border-gray-300 font-mono text-center" />
      </div>
      {splitAs.trim() && (
        <p className="text-[9px] text-gray-500 leading-snug -mt-1">
          {'每筆會按分隔符拆開、依序存進這些變數。例：清單一筆「2026-08」+ 拆成「查詢年|查詢月」→ {{查詢年}}=2026、{{查詢月}}=08。月份+品規的雙層查詢：外層迴圈拆年月、內層迴圈跑品規。'}
        </p>
      )}
      <div className="space-y-0.5 text-[10px] text-gray-600">
        <label className="flex items-center gap-1 cursor-pointer">
          <input type="radio" checked={mode === 'wrapAfter'} onChange={() => setMode('wrapAfter')} disabled={tailCount === 0} />
          <span className={tailCount > 0 ? '' : 'opacity-40'}>
            把這個位置<b>之後的 {tailCount} 個動作</b>包進迴圈（前面有錄製/讀清單等前置時用）
          </span>
        </label>
        <label className="flex items-center gap-1 cursor-pointer">
          <input type="radio" checked={mode === 'wrap'} onChange={() => setMode('wrap')} disabled={!hasActions} />
          <span className={hasActions ? '' : 'opacity-40'}>
            把目前序列的<b>全部動作</b>包進迴圈
          </span>
        </label>
        <label className="flex items-center gap-1 cursor-pointer">
          <input type="radio" checked={mode === 'empty'} onChange={() => setMode('empty')} />
          建立空迴圈，插入在此位置（子動作之後用 YAML / AI 助手放進去）
        </label>
      </div>
      <div className="flex gap-1 pt-0.5">
        <button type="button" onClick={submit}
          className="flex-1 text-[10px] bg-fuchsia-600 text-white px-2 py-1 rounded hover:bg-fuchsia-700">建立迴圈</button>
        <button type="button" onClick={closeMenu}
          className="text-[10px] text-gray-500 hover:text-gray-800 px-2 py-1 rounded border border-gray-200">取消</button>
      </div>
      <p className="text-[9px] text-gray-500 leading-snug">
        {'包進迴圈後，把「填值」動作的文字改成 {{變數名}}（✎ 編輯），每輪就會帶入當前那一筆；{{變數名_序號}} 是第幾筆。'}
      </p>
    </div>
  )
}
