'use client'
import { useState, useEffect } from 'react'
import { X, MousePointerSquareDashed, Trash2, ScanEye } from 'lucide-react'
import type { VisualValidationData } from './_helpers'
import ScreenRegionPicker from './_screenRegionPicker'
import { VariableButton } from './_variablePicker'
import LlmRoleSelector from './_llmRoleSelector'
import { getHostTools } from '@/lib/api'
import { toast } from 'sonner'

interface Props {
  data: VisualValidationData
  onUpdate: (patch: Partial<VisualValidationData>) => void
  onClose: () => void
  onDelete: () => void
  workflowId?: string
}

const NODE_COLOR = '#6366f1'

export default function VisualValidationPanel({ data, onUpdate, onClose, onDelete, workflowId }: Props) {
  const inputCls = 'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400/20 bg-white'
  const [showPicker, setShowPicker] = useState(false)
  const hasRegion = data.searchRegion && data.searchRegion.length === 4 && data.searchRegion[2] > 0 && data.searchRegion[3] > 0

  // 偵測主機是否裝 LibreOffice — pptx/docx 走 prev_output 模式必須
  // 沒裝 → prev_output radio disable + 已選此模式時自動切到 current_screen
  const [libreofficeOk, setLibreofficeOk] = useState<boolean | null>(null)
  useEffect(() => {
    getHostTools()
      .then(r => {
        const lo = r.tools.find(t => t.name === 'LibreOffice')
        setLibreofficeOk(!!lo?.installed)
      })
      .catch(() => setLibreofficeOk(null))
  }, [])

  // 沒裝 LibreOffice 且當前是 prev_output → 自動切 current_screen + 提示
  useEffect(() => {
    if (libreofficeOk === false && data.source === 'prev_output') {
      onUpdate({ source: 'current_screen' })
      toast.warning('主機未裝 LibreOffice — 已自動切到「目前螢幕」模式;裝完 LibreOffice 並重啟 backend 才能用「上一步輸出檔」', {
        duration: 7000,
      })
    }
  }, [libreofficeOk, data.source, onUpdate])

  const prevOutputDisabled = libreofficeOk === false

  return (
    <div className="absolute top-0 right-0 h-full w-[420px] bg-white shadow-2xl border-l border-gray-100 flex flex-col z-30 overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b" style={{ borderTopColor: NODE_COLOR, borderTopWidth: 3 }}>
        <span
          className="w-8 h-8 rounded-full flex items-center justify-center text-white shrink-0"
          style={{ background: NODE_COLOR }}
        ><ScanEye className="w-4 h-4" strokeWidth={2.4} /></span>
        <div className="flex-1 min-w-0">
          <span className="font-semibold text-gray-800 text-sm block truncate">視覺驗證節點</span>
          <span className="text-xs text-gray-400">用 VLM 看畫面判斷是否符合預期（取代 AI 驗證的「文字檢查」）</span>
        </div>
        <button onClick={onDelete} title="刪除節點" className="text-gray-300 hover:text-red-400 transition-colors p-1">🗑</button>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors"><X className="w-4 h-4" /></button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Name */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">節點名稱</label>
          <input value={data.name}
                 onChange={e => onUpdate({ name: e.target.value })}
                 className={`${inputCls} font-mono`} />
        </div>

        {/* 來源選擇（2 個 radio：上一步輸出檔 / 目前螢幕畫面） */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">看什麼？</label>
          <div className="space-y-1.5">
            {/* 上一步輸出檔(沒裝 LibreOffice 時 disable、pptx/docx 視覺渲染必須) */}
            <label
              className={`block p-2.5 rounded-lg border transition-colors ${
                prevOutputDisabled
                  ? 'border-gray-200 bg-gray-50 opacity-60 cursor-not-allowed'
                  : data.source === 'prev_output'
                    ? 'border-indigo-400 bg-indigo-50 cursor-pointer'
                    : 'border-gray-200 hover:border-indigo-200 hover:bg-gray-50 cursor-pointer'
              }`}
              onClick={() => {
                if (prevOutputDisabled) {
                  toast.error('主機未裝 LibreOffice — 無法渲染 pptx/docx 真實版面;此模式停用。請先安裝再重啟 backend:winget install -e --id TheDocumentFoundation.LibreOffice --source winget', {
                    duration: 9000,
                  })
                }
              }}
            >
              <div className="flex items-start gap-2">
                <input type="radio"
                       name="vv-source"
                       checked={data.source === 'prev_output'}
                       onChange={() => !prevOutputDisabled && onUpdate({ source: 'prev_output' })}
                       disabled={prevOutputDisabled}
                       className="mt-0.5 accent-indigo-600 disabled:cursor-not-allowed" />
                <div className="flex-1">
                  <div className="text-sm font-medium text-gray-800 flex items-center gap-2">
                    上一步的輸出檔
                    {prevOutputDisabled && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-100 text-red-700 font-mono">需 LibreOffice</span>
                    )}
                  </div>
                  <div className="text-[11px] text-gray-500 leading-relaxed mt-0.5">
                    自動處理:圖檔(PNG/JPG)直送 VLM;非圖檔(xlsx/docx/pdf/pptx)自動 render 成 PNG 再送。
                    <br />
                    <span className="text-indigo-600">📄 用在:驗證上一步腳本 / Skill 產出的檔案是不是對的</span>
                    {prevOutputDisabled && (
                      <>
                        <br />
                        <span className="text-red-600">⚠ 此模式已停用 — pptx / docx 真實版面渲染需 LibreOffice。請改用「目前螢幕」、或安裝 LibreOffice 後重啟 backend。</span>
                      </>
                    )}
                  </div>
                </div>
              </div>
            </label>

            {/* 目前螢幕畫面 */}
            <label className={`block p-2.5 rounded-lg border cursor-pointer transition-colors ${
              data.source === 'current_screen'
                ? 'border-indigo-400 bg-indigo-50'
                : 'border-gray-200 hover:border-indigo-200 hover:bg-gray-50'
            }`}>
              <div className="flex items-start gap-2">
                <input type="radio"
                       name="vv-source"
                       checked={data.source === 'current_screen'}
                       onChange={() => onUpdate({ source: 'current_screen' })}
                       className="mt-0.5 accent-indigo-600" />
                <div className="flex-1">
                  <div className="text-sm font-medium text-gray-800">目前的螢幕畫面</div>
                  <div className="text-[11px] text-gray-500 leading-relaxed mt-0.5">
                    執行到這個節點時，即時抓桌面螢幕送 VLM 判斷。
                    <br />
                    <span className="text-indigo-600">🖥 用在：桌面自動化動作做完一段，先讓 VLM 看畫面有沒有達到預期，再決定要不要往下走</span>
                  </div>
                </div>
              </div>
            </label>
          </div>
        </div>

        {/* 判斷條件 prompt */}
        <div>
          <div className="flex items-end justify-between mb-1.5">
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
              判斷條件(給 VLM 的 prompt)
            </label>
            <VariableButton
              workflowId={workflowId}
              onPick={(p) => onUpdate({ prompt: `${data.prompt || ''}{{ ${p} }}` })}
            />
          </div>
          <textarea
            rows={7}
            value={data.prompt}
            onChange={e => onUpdate({ prompt: e.target.value })}
            onWheel={(e) => {
              const el = e.currentTarget
              const atTop = el.scrollTop === 0
              const atBot = el.scrollTop + el.clientHeight >= el.scrollHeight - 1
              if ((!atTop && e.deltaY < 0) || (!atBot && e.deltaY > 0)) e.stopPropagation()
            }}
            placeholder={
              data.source === 'current_screen'
                ? '例：登入後右上角是否出現綠色的「登入成功」訊息？沒有任何錯誤紅字？'
                : '例：圖表是否每個部門都用「不同的顏色」？柱狀條沒有重疊或缺漏？'
            }
            className={`${inputCls} resize-y font-mono text-xs leading-relaxed min-h-[120px]`}
          />
          <p className="text-[11px] text-gray-400 mt-1">VLM 會回 pass / fail + 原因。pass=false 步驟即失敗(會走 retry 邏輯)</p>
        </div>

        <LlmRoleSelector
          value={(data as any).llmRole || 'primary'}
          onChange={(v) => onUpdate({ llmRole: v } as any)}
        />

        {/* 螢幕區域選擇器(只在 current_screen 時顯示) */}
        {data.source === 'current_screen' && (
          <div className="rounded-xl border border-indigo-200 bg-indigo-50/40 p-3 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-indigo-700 uppercase tracking-wide">
                只看螢幕某塊區域（選填）
              </span>
              {hasRegion && (
                <button
                  onClick={() => onUpdate({ searchRegion: [] })}
                  title="清除區域 → 看整個螢幕"
                  className="text-[11px] text-red-500 hover:text-red-700 flex items-center gap-1"
                >
                  <Trash2 className="w-3 h-3" /> 清除
                </button>
              )}
            </div>
            <p className="text-[11px] text-gray-600 leading-relaxed">
              留空 = 整個螢幕都送 VLM。圈一塊 = VLM 只看這塊（省 token、避免被旁邊干擾元素影響判斷）。
              例如：只看右下角通知的位置、或只看對話框中央。
            </p>
            {hasRegion ? (
              <div className="flex items-center gap-2 px-2.5 py-1.5 bg-white rounded border border-indigo-200">
                <span className="text-[11px] font-mono text-gray-700 flex-1">
                  📐 {data.searchRegion[0]}, {data.searchRegion[1]} 起 {data.searchRegion[2]} × {data.searchRegion[3]} px
                </span>
                <button onClick={() => setShowPicker(true)}
                        className="text-[11px] text-indigo-600 hover:text-indigo-800 hover:bg-indigo-100 px-2 py-0.5 rounded">
                  重拉
                </button>
              </div>
            ) : (
              <button
                onClick={() => setShowPicker(true)}
                className="w-full flex items-center justify-center gap-1.5 px-3 py-2 bg-white border-2 border-dashed border-indigo-300 rounded-lg text-sm text-indigo-700 hover:bg-indigo-50 transition-colors"
              >
                <MousePointerSquareDashed className="w-4 h-4" />
                從螢幕上拉一塊區域
              </button>
            )}
          </div>
        )}

        {/* 模型相依提示 */}
        <div className="p-2.5 bg-amber-50 border border-amber-200 rounded-lg text-[11px] text-amber-800 leading-relaxed">
          <strong>⚠ 視覺模型必備</strong>：此節點用的是 Settings 主模型。模型不支援視覺（如純文字 LLM）會直接報錯，不會偷偷退化。建議模型：Gemini 2.5 Flash / GPT-4o / Claude 3.5+。
        </div>
      </div>

      {/* Footer */}
      <div className="p-4 border-t bg-indigo-50">
        <p className="text-xs text-indigo-700">取代「AI 驗證」的視覺增強版 — VLM 真的看圖判斷，不是只看 stdout 文字</p>
      </div>

      {/* 螢幕區域 Picker modal */}
      {showPicker && (
        <ScreenRegionPicker
          initialRegion={data.searchRegion || []}
          onApply={(region) => onUpdate({ searchRegion: region })}
          onClose={() => setShowPicker(false)}
        />
      )}
    </div>
  )
}
