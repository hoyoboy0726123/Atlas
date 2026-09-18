'use client'
/**
 * OCR 取值插入器 —— 在動作列表的插入點加一個 ocr_get_text 動作。
 *
 * 為什麼一定要有「試抓」按鈕:
 *   OCR 的標籤比對是模糊的 —— 模型會把繁體讀成簡體(總計金額 → 總計金额)、會漏字
 *   (賣方統編 → 賣方統)。使用者填完標籤如果不能當場驗證,就要跑完整條流程才知道抓錯。
 *   試抓會直接回報「抓到什麼值」,失敗時還回報「畫面上有哪些相近文字」,照著改才有依據。
 *
 * 使用前提:目標畫面要開著。這跟 UIA Inspector 一樣是「對著實際畫面設定」的工具。
 */
import { useState } from 'react'
import { Plus, ScanText, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { ocrProbe, type OcrProbeResult } from '@/lib/api'
import type { ComputerUseAction } from './_helpers'

interface Props {
  index: number
  isOpen: boolean
  openMenu: () => void
  closeMenu: () => void
  onAdd: (index: number, action: ComputerUseAction) => void
}

type Dir = 'right' | 'below'
type Kind = 'amount' | 'ident' | 'taxid' | 'any'

const KIND_HINT: Record<Kind, string> = {
  amount: '金額（38,500 / NT$1,925）',
  ident: '單號（AB-12345678）',
  taxid: '統一編號 —— 會用檢查碼驗證，OCR 讀錯會當場抓到',
  any: '任何含數字的文字',
}

export default function OcrFieldInserter({ index, isOpen, openMenu, closeMenu, onAdd }: Props) {
  const [label, setLabel] = useState('')
  const [saveAs, setSaveAs] = useState('')
  const [dir, setDir] = useState<Dir>('right')
  const [kind, setKind] = useState<Kind>('amount')
  const [probing, setProbing] = useState(false)
  const [probe, setProbe] = useState<OcrProbeResult | null>(null)

  const reset = () => { setLabel(''); setSaveAs(''); setProbe(null); setDir('right'); setKind('amount') }

  const runProbe = async () => {
    if (!label.trim()) { toast.error('請先填標籤文字'); return }
    setProbing(true)
    setProbe(null)
    try {
      setProbe(await ocrProbe({ label: label.trim(), direction: dir, kind }))
    } catch (e: any) {
      toast.error(e?.message || 'OCR 試抓失敗')
    } finally {
      setProbing(false)
    }
  }

  const submit = () => {
    if (!label.trim()) { toast.error('請填標籤文字'); return }
    if (!saveAs.trim()) { toast.error('請填變數名稱'); return }
    onAdd(index, {
      type: 'ocr_get_text',
      label: label.trim(),
      direction: dir,
      kind,
      save_as: saveAs.trim(),
      description: `OCR 讀「${label.trim()}」→ {{${saveAs.trim()}}}`,
    })
    toast.success(`已插入 OCR 取值：${label.trim()} → {{${saveAs.trim()}}}`)
    reset()
    closeMenu()
  }

  if (!isOpen) {
    return (
      <div className="flex justify-center -my-0.5">
        <button
          data-vlm-insert-trigger
          type="button"
          onClick={openMenu}
          title="在此位置插入 OCR 取值（讀畫面上某個標籤旁邊的值、存成變數）"
          className="opacity-30 hover:opacity-100 transition-opacity flex items-center gap-0.5 px-2 py-0.5 rounded-full whitespace-nowrap text-[9px] text-teal-600 border border-dashed border-teal-300 hover:bg-teal-50"
        >
          <Plus className="w-2.5 h-2.5" /> OCR 取值
        </button>
      </div>
    )
  }

  return (
    // data-vlm-insert-popover 是必要的:面板的「點外面關閉」handler 只認這個屬性,
    // 沒有的話點自己的輸入框就會 mousedown → setInsertOpenAt(null) → 整張表單消失,
    // 「插入這個動作」按鈕更慘 —— DOM 先被移除、onClick 永遠不會觸發。
    <div data-vlm-insert-popover
         className="my-1 rounded-lg border border-teal-200 bg-teal-50/60 p-2 space-y-1.5">
      <div className="flex items-center gap-1 text-[11px] font-semibold text-teal-800">
        <ScanText className="w-3 h-3 shrink-0" />
        <span className="min-w-0">OCR 取值：讀「標籤旁邊」的值</span>
      </div>

      {/* ⚠ 兩個輸入框不要並排:面板只有 420px、扣掉 padding 每格剩約 180px,
          placeholder「標籤文字（例：總計金額）」會被裁掉一半,使用者看不出要填什麼。
          改成上下堆疊、各自帶標題。 */}
      <div className="space-y-1">
        <label className="block">
          <span className="text-[10px] text-gray-500">要找的標籤</span>
          <input
            value={label}
            onChange={e => { setLabel(e.target.value); setProbe(null) }}
            placeholder="例：總計金額"
            className="w-full text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-teal-500"
          />
        </label>
        <label className="block">
          <span className="text-[10px] text-gray-500">值存成變數</span>
          <input
            value={saveAs}
            onChange={e => setSaveAs(e.target.value)}
            placeholder="例：發票金額"
            className="w-full text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-teal-500 font-mono"
          />
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-gray-600">
        <span className="flex items-center gap-1">
          值在
          <select
            value={dir}
            onChange={e => { setDir(e.target.value as Dir); setProbe(null) }}
            className="text-[10px] border border-gray-300 rounded px-1 py-0.5 bg-white"
          >
            <option value="right">標籤右邊（同一列）</option>
            <option value="below">標籤下方（表格欄位）</option>
          </select>
        </span>
        <span className="flex items-center gap-1">
          格式
          <select
            value={kind}
            onChange={e => { setKind(e.target.value as Kind); setProbe(null) }}
            className="text-[10px] border border-gray-300 rounded px-1 py-0.5 bg-white"
          >
            <option value="amount">金額</option>
            <option value="ident">單號</option>
            <option value="taxid">統一編號（會驗檢查碼）</option>
            <option value="any">不限</option>
          </select>
        </span>
      </div>
      {/* 提示文字獨立一行 —— 跟 select 擠在同一列時會把 select 壓到只剩箭頭 */}
      <div className="text-[10px] text-gray-400 pl-0.5">{KIND_HINT[kind]}</div>

      {/* 試抓 —— 目標畫面要開著 */}
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={runProbe}
          disabled={probing}
          title="立刻對現在的螢幕試抓一次，確認標籤對得上"
          className="text-[10px] px-2 py-1 rounded border border-teal-400 text-teal-700 hover:bg-teal-100 disabled:opacity-50 inline-flex items-center gap-1 whitespace-nowrap shrink-0"
        >
          {probing ? <Loader2 className="w-3 h-3 animate-spin shrink-0" /> : <ScanText className="w-3 h-3 shrink-0" />}
          {probing ? '辨識中…' : '試抓看看'}
        </button>
        <span className="text-[9px] text-gray-500 leading-tight">目標畫面要開著、不要被遮住</span>
      </div>

      {probe && (
        <div className={`text-[10px] rounded border px-2 py-1.5 leading-relaxed ${
          probe.ok === false ? 'bg-rose-50 border-rose-200'
            : probe.found ? 'bg-emerald-50 border-emerald-200'
              : 'bg-amber-50 border-amber-200'
        }`}>
          {probe.ok === false ? (
            <span className="text-rose-700">✗ {probe.error}</span>
          ) : probe.found ? (
            <div className="text-emerald-800">
              <div className="font-semibold">✓ 抓到「{probe.value}」</div>
              <div className="opacity-80">
                標籤被 OCR 讀成「{probe.label_read_as}」（相似度 {probe.label_score}）
                {probe.label_read_as !== label.trim() && ' — 不完全一樣是正常的，系統會做簡繁與漏字容錯'}
              </div>
            </div>
          ) : (
            <div className="text-amber-800">
              <div className="font-semibold">✗ 沒抓到</div>
              <div className="opacity-90">{probe.reason}</div>
              {probe.candidates && probe.candidates.length > 0 && (
                <div className="mt-1">
                  畫面上的相近文字（點一下直接帶入）：
                  <div className="flex flex-wrap gap-1 mt-0.5">
                    {probe.candidates.map((c, i) => (
                      <button
                        key={i}
                        type="button"
                        onClick={() => { setLabel(c); setProbe(null) }}
                        className="px-1.5 py-0.5 rounded bg-white border border-amber-300 hover:bg-amber-100 font-mono"
                      >{c}</button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      <div className="flex gap-1 pt-0.5">
        <button
          type="button"
          onClick={submit}
          className="flex-1 text-[10px] bg-teal-600 text-white px-2 py-1 rounded hover:bg-teal-700"
        >插入這個動作</button>
        <button
          type="button"
          onClick={() => { reset(); closeMenu() }}
          className="text-[10px] text-gray-500 hover:text-gray-800 px-2 py-1 rounded border border-gray-200"
        >取消</button>
      </div>

      <p className="text-[9px] text-gray-500 leading-snug">
        值來自<strong>上傳的檔案</strong>（憑證 PDF / 圖檔）時，用 script 節點呼叫檔案 OCR 更準；
        欄位能被 UIA 讀到時，用 UIA Inspector 的「讀文字」最穩。這裡是給只能從畫面讀的情況用的。
      </p>
    </div>
  )
}
