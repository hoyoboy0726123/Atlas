'use client'
/**
 * 插入選單收折器 —— 動作間隙的插入工具長到 7 顆,全部常駐太擠。
 * 收折時只顯示一顆「＋ 插入」,點開才展開整排;一次只展開一個間隙。
 */
import type { ReactNode } from 'react'

interface Props {
  expanded: boolean
  onExpand: () => void
  onCollapse: () => void
  children: ReactNode
}

export default function InsertHub({ expanded, onExpand, onCollapse, children }: Props) {
  if (!expanded) {
    return (
      <div className="flex justify-center -my-0.5">
        <button
          data-vlm-insert-trigger
          type="button"
          onClick={onExpand}
          title="展開插入選單（OCR 取值 / 等下載 / OCR 等待 / 逐筆迴圈 / 讀剪貼簿 / 喚醒視窗）"
          className="opacity-25 hover:opacity-100 transition-opacity px-2.5 py-0.5 rounded-full whitespace-nowrap text-[9px] text-gray-500 border border-dashed border-gray-300 hover:bg-gray-50 hover:text-gray-700"
        >＋ 插入</button>
      </div>
    )
  }
  return (
    // w-full + flex-wrap:展開的工具塞不進一行就往下排整齊(硬擠會被裁掉,使用者反饋)
    <div className="w-full flex flex-wrap items-center justify-center gap-x-1.5 gap-y-1">
      {children}
      <button
        data-vlm-insert-trigger
        type="button"
        onClick={onCollapse}
        title="收合插入選單"
        className="opacity-40 hover:opacity-100 transition-opacity px-1.5 py-0.5 rounded-full text-[9px] text-gray-400 border border-dashed border-gray-300 hover:bg-gray-50"
      >✕</button>
    </div>
  )
}
