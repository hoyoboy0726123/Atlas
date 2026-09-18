'use client'
/**
 * 讀剪貼簿插入器 —— 在動作列表的插入點加一個 uia_get_clipboard 動作。
 * 場景:Tk 等 UIA 讀不到內容的工具,先按它的「複製」鈕,再用這個把
 * 剪貼簿內容存成變數(接 for_each 逐筆跑)。
 */
import { useState } from 'react'
import { ClipboardPaste } from 'lucide-react'
import { toast } from 'sonner'
import type { ComputerUseAction } from './_helpers'

interface Props {
  index: number
  isOpen: boolean
  openMenu: () => void
  closeMenu: () => void
  onAdd: (index: number, action: ComputerUseAction) => void
}

export default function ClipboardReadInserter({ index, isOpen, openMenu, closeMenu, onAdd }: Props) {
  const [saveAs, setSaveAs] = useState('清單原文')

  const submit = () => {
    if (!saveAs.trim()) { toast.error('請填變數名稱'); return }
    onAdd(index, {
      type: 'uia_get_clipboard',
      save_as: saveAs.trim(),
      description: `讀剪貼簿 → {{${saveAs.trim()}}}`,
    })
    toast.success(`已插入讀剪貼簿 → {{${saveAs.trim()}}}`)
    closeMenu()
  }

  if (!isOpen) {
    return (
      <div className="flex justify-center -my-0.5">
        <button
          data-vlm-insert-trigger
          type="button"
          onClick={openMenu}
          title="讀剪貼簿存成變數（Tk 等 UIA 讀不到的工具：先按它的「複製」鈕、再用這個接住內容）"
          className="opacity-30 hover:opacity-100 transition-opacity flex items-center gap-0.5 px-2 py-0.5 rounded-full whitespace-nowrap text-[9px] text-emerald-600 border border-dashed border-emerald-300 hover:bg-emerald-50"
        >
          <ClipboardPaste className="w-2.5 h-2.5" /> 讀剪貼簿
        </button>
      </div>
    )
  }

  return (
    // data-vlm-insert-popover:面板「點外面關閉」只認這個屬性
    <div data-vlm-insert-popover
         className="my-1 rounded-lg border border-emerald-200 bg-emerald-50/60 p-2 space-y-1.5">
      <div className="flex items-center gap-1 text-[11px] font-semibold text-emerald-800">
        <ClipboardPaste className="w-3 h-3 shrink-0" />
        <span>讀剪貼簿 → 存成變數</span>
      </div>
      <label className="flex items-center gap-1 text-[10px] text-gray-500 whitespace-nowrap">
        存到變數
        <input value={saveAs} onChange={e => setSaveAs(e.target.value)}
          placeholder="例：清單原文"
          className="flex-1 min-w-0 text-[11px] px-1.5 py-1 rounded border border-gray-300 font-mono" />
      </label>
      <div className="flex gap-1 pt-0.5">
        <button type="button" onClick={submit}
          className="flex-1 text-[10px] bg-emerald-600 text-white px-2 py-1 rounded hover:bg-emerald-700">插入這個動作</button>
        <button type="button" onClick={closeMenu}
          className="text-[10px] text-gray-500 hover:text-gray-800 px-2 py-1 rounded border border-gray-200">取消</button>
      </div>
      <p className="text-[9px] text-gray-500 leading-snug">
        {'放在「按下來源工具的複製鈕」動作之後。剪貼簿是空的會誠實報錯。接 🔁 逐筆時 items 填 {{變數名}}。'}
      </p>
    </div>
  )
}
