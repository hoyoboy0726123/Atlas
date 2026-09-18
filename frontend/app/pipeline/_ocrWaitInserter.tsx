'use client'
/**
 * OCR 等待插入器 —— 在動作列表的插入點加一個 wait_text 動作。
 * 場景:等「資料處理中」遮罩消失、等「匯出完成」字樣出現。
 * OCR 會自動限縮在目標視窗範圍內辨識(後端行為),不用擔心掃到別的視窗。
 */
import { useState } from 'react'
import { Eye } from 'lucide-react'
import { toast } from 'sonner'
import type { ComputerUseAction } from './_helpers'

interface Props {
  index: number
  isOpen: boolean
  openMenu: () => void
  closeMenu: () => void
  onAdd: (index: number, action: ComputerUseAction) => void
}

export default function OcrWaitInserter({ index, isOpen, openMenu, closeMenu, onAdd }: Props) {
  const [text, setText] = useState('')
  const [until, setUntil] = useState('disappear')
  const [timeoutSec, setTimeoutSec] = useState('120')

  const submit = () => {
    if (!text.trim()) { toast.error('請填要等的文字'); return }
    onAdd(index, {
      type: 'wait_text',
      text: text.trim(),
      until: until as ComputerUseAction['until'],
      timeout_sec: Number(timeoutSec) || 120,
      description: `等文字「${text.trim()}」${until === 'disappear' ? '消失' : '出現'}(OCR)`,
    })
    toast.success('已插入 OCR 等待')
    closeMenu()
  }

  if (!isOpen) {
    return (
      <div className="flex justify-center -my-0.5">
        <button
          data-vlm-insert-trigger
          type="button"
          onClick={openMenu}
          title="在此位置插入「OCR 等待」（等畫面上某段文字出現或消失才繼續，適合 UIA 讀不到的畫面）"
          className="opacity-30 hover:opacity-100 transition-opacity flex items-center gap-0.5 px-2 py-0.5 rounded-full whitespace-nowrap text-[9px] text-orange-600 border border-dashed border-orange-300 hover:bg-orange-50"
        >
          <Eye className="w-2.5 h-2.5" /> OCR 等待
        </button>
      </div>
    )
  }

  return (
    // data-vlm-insert-popover:面板「點外面關閉」只認這個屬性
    <div data-vlm-insert-popover
         className="my-1 rounded-lg border border-orange-200 bg-orange-50/60 p-2 space-y-1.5">
      <div className="flex items-center gap-1 text-[11px] font-semibold text-orange-800">
        <Eye className="w-3 h-3 shrink-0" />
        <span>OCR 等待：等畫面文字出現 / 消失</span>
      </div>
      <label className="block">
        <span className="text-[10px] text-gray-500">要等的文字</span>
        <input value={text} onChange={e => setText(e.target.value)}
          placeholder="例：資料處理中"
          className="w-full text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-orange-500" />
      </label>
      <div className="flex items-center gap-2 flex-wrap">
        <select value={until} onChange={e => setUntil(e.target.value)}
          className="text-[11px] border border-gray-300 rounded px-1.5 py-1 bg-white">
          <option value="disappear">等它消失（查詢遮罩收掉）</option>
          <option value="appear">等它出現</option>
        </select>
        <label className="flex items-center gap-1 text-[10px] text-gray-500 whitespace-nowrap">
          最多等
          <input value={timeoutSec} onChange={e => setTimeoutSec(e.target.value)}
            className="w-14 text-[11px] px-1.5 py-1 rounded border border-gray-300 text-right" />
          秒
        </label>
      </div>
      <div className="flex gap-1 pt-0.5">
        <button type="button" onClick={submit}
          className="flex-1 text-[10px] bg-orange-600 text-white px-2 py-1 rounded hover:bg-orange-700">插入這個動作</button>
        <button type="button" onClick={closeMenu}
          className="text-[10px] text-gray-500 hover:text-gray-800 px-2 py-1 rounded border border-gray-200">取消</button>
      </div>
      <p className="text-[9px] text-gray-500 leading-snug">
        OCR 只會在目標視窗範圍內辨識，且視窗要實際顯示在螢幕上。
        UIA 讀得到的畫面優先用 Inspector 的「⏳ 等待這個元素」——更快更準、視窗被遮住也能跑。
      </p>
    </div>
  )
}
