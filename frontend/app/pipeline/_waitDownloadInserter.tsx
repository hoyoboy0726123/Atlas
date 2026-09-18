'use client'
/**
 * 等下載插入器 —— 在動作列表的插入點加一個 wait_download 動作。
 * 場景:按匯出後檔案開始下載,下一步(開檔/搬檔/下一筆)必須等檔案真的寫完。
 */
import { useState } from 'react'
import { Download } from 'lucide-react'
import { toast } from 'sonner'
import type { ComputerUseAction } from './_helpers'

interface Props {
  index: number
  isOpen: boolean
  openMenu: () => void
  closeMenu: () => void
  onAdd: (index: number, action: ComputerUseAction) => void
}

export default function WaitDownloadInserter({ index, isOpen, openMenu, closeMenu, onAdd }: Props) {
  const [pattern, setPattern] = useState('*.xlsx')
  const [dir, setDir] = useState('')
  const [timeoutSec, setTimeoutSec] = useState('300')
  const [saveAs, setSaveAs] = useState('下載檔')

  const submit = () => {
    onAdd(index, {
      type: 'wait_download',
      pattern: pattern.trim() || '*',
      ...(dir.trim() ? { dir: dir.trim() } : {}),
      timeout_sec: Number(timeoutSec) || 300,
      ...(saveAs.trim() ? { save_as: saveAs.trim() } : {}),
      description: `等下載完成：${pattern.trim() || '*'}`,
    })
    toast.success('已插入「等下載完成」')
    closeMenu()
  }

  if (!isOpen) {
    return (
      <div className="flex justify-center -my-0.5">
        <button
          data-vlm-insert-trigger
          type="button"
          onClick={openMenu}
          title="在此位置插入「等下載完成」（等下載資料夾出現寫完的新檔案才繼續）"
          className="opacity-30 hover:opacity-100 transition-opacity flex items-center gap-0.5 px-2 py-0.5 rounded-full whitespace-nowrap text-[9px] text-sky-600 border border-dashed border-sky-300 hover:bg-sky-50"
        >
          <Download className="w-2.5 h-2.5" /> 等下載
        </button>
      </div>
    )
  }

  return (
    // data-vlm-insert-popover:面板「點外面關閉」只認這個屬性(同 OCR 插入器的教訓)
    <div data-vlm-insert-popover
         className="my-1 rounded-lg border border-sky-200 bg-sky-50/60 p-2 space-y-1.5">
      <div className="flex items-center gap-1 text-[11px] font-semibold text-sky-800">
        <Download className="w-3 h-3 shrink-0" />
        <span>等下載完成（檔案寫完才繼續）</span>
      </div>
      <label className="block">
        <span className="text-[10px] text-gray-500">檔名樣式（* 萬用字元）</span>
        <input value={pattern} onChange={e => setPattern(e.target.value)}
          placeholder="例：PP_Component*.xlsx"
          className="w-full text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-sky-500 font-mono" />
      </label>
      <label className="block">
        <span className="text-[10px] text-gray-500">下載資料夾（留空 = Windows 的「下載」）</span>
        <input value={dir} onChange={e => setDir(e.target.value)}
          placeholder={'例：D:\\reports'}
          className="w-full text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-sky-500 font-mono" />
      </label>
      <div className="flex items-center gap-2">
        <label className="flex items-center gap-1 text-[10px] text-gray-500 whitespace-nowrap">
          最多等
          <input value={timeoutSec} onChange={e => setTimeoutSec(e.target.value)}
            className="w-14 text-[11px] px-1.5 py-1 rounded border border-gray-300 text-right" />
          秒
        </label>
        <label className="flex-1 flex items-center gap-1 text-[10px] text-gray-500 min-w-0 whitespace-nowrap">
          路徑存變數
          <input value={saveAs} onChange={e => setSaveAs(e.target.value)}
            placeholder="例：下載檔"
            className="flex-1 min-w-0 text-[11px] px-1.5 py-1 rounded border border-gray-300 font-mono" />
        </label>
      </div>
      <div className="flex gap-1 pt-0.5">
        <button type="button" onClick={submit}
          className="flex-1 text-[10px] bg-sky-600 text-white px-2 py-1 rounded hover:bg-sky-700">插入這個動作</button>
        <button type="button" onClick={closeMenu}
          className="text-[10px] text-gray-500 hover:text-gray-800 px-2 py-1 rounded border border-gray-200">取消</button>
      </div>
      <p className="text-[9px] text-gray-500 leading-snug">
        只認「這個步驟開始後新出現、且寫完」的檔案（排除 .crdownload 半成品、大小穩定才算完成）。
        下載可能不發生（例如查無資料）時，把它放進「❓ 條件分歧」的否則分支，不要單獨用。
      </p>
    </div>
  )
}
