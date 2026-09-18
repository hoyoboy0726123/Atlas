'use client'
import { X, UserCheck } from 'lucide-react'
import type { HumanConfirmData, HumanConfirmNode } from './_helpers'
import { VariableInput } from './_variablePicker'

const CONFIRM_COLOR = '#10b981'

interface Props {
  node: HumanConfirmNode
  onUpdate: (data: Partial<HumanConfirmData>) => void
  onClose: () => void
  onDelete: () => void
  workflowId?: string
}

export default function HumanConfirmPanel({ node, onUpdate, onClose, onDelete, workflowId }: Props) {
  const data = node.data
  const inputCls = 'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-emerald-400 focus:ring-1 focus:ring-emerald-400/20 bg-white'

  return (
    <div className="absolute top-0 right-0 h-full w-[380px] bg-white shadow-2xl border-l border-gray-100 flex flex-col z-30 overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b" style={{ borderTopColor: CONFIRM_COLOR, borderTopWidth: 3 }}>
        <span className="w-8 h-8 rounded-full flex items-center justify-center text-white shrink-0"
          style={{ background: CONFIRM_COLOR }}><UserCheck className="w-4 h-4" strokeWidth={2.4} /></span>
        <div className="flex-1 min-w-0">
          <span className="font-semibold text-gray-800 text-sm block truncate">人工確認節點</span>
          <span className="text-xs text-gray-400">暫停 Pipeline 等待人工確認，可透過 Telegram 或網頁操作</span>
        </div>
        <button onClick={onDelete} title="刪除" className="text-gray-300 hover:text-red-400 transition-colors p-1">🗑</button>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors"><X className="w-4 h-4" /></button>
      </div>

      {/* Fields */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Name */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">節點名稱</label>
          <input value={data.name} onChange={e => onUpdate({ name: e.target.value })} className={`${inputCls} font-mono`} placeholder="人工確認" />
        </div>

        {/* Message */}
        <div>
          <div className="flex items-end justify-between mb-2">
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide">確認訊息</label>
          </div>
          <VariableInput
            value={data.message || ''}
            onChange={(v) => onUpdate({ message: v })}
            workflowId={workflowId}
            multiline
            rows={6}
            placeholder={'(選填)自訂確認提示…\n例如:請確認 {{ steps.crawl.output.path }} 是否正確'}
            showHint={false}
          />
          <p className="text-xs text-gray-400 mt-1.5">支援 <code className="bg-gray-100 px-1 rounded font-mono">{`{{ steps.X.output.Y }}`}</code> 等變數;留空 = 預設「請確認上一步結果是否正確」</p>
        </div>

        {/* Telegram toggle */}
        <div className="flex items-center justify-between p-3 rounded-xl border border-gray-200 bg-gray-50/50">
          <div className="flex-1 min-w-0 mr-3">
            <div className="text-sm font-medium text-gray-700">📱 Telegram 通知</div>
            <p className="text-xs text-gray-400 mt-0.5">
              {data.notifyTelegram
                ? '已啟用：暫停時送 Telegram 摘要 + 決策按鈕（繼續 / 中止 / 截圖 / 查看 Log；上一步若為 AI 技能節點還會多出「💬 補充指示」）'
                : '已關閉：僅透過網頁 UI 等待確認'}
            </p>
          </div>
          <button
            onClick={() => onUpdate({ notifyTelegram: !data.notifyTelegram })}
            className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${
              data.notifyTelegram ? 'bg-emerald-500' : 'bg-gray-300'
            }`}
          >
            <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
              data.notifyTelegram ? 'translate-x-5' : 'translate-x-0'
            }`} />
          </button>
        </div>

        {/* Screenshot toggle */}
        <div className="flex items-center justify-between p-3 rounded-xl border border-gray-200 bg-gray-50/50">
          <div className="flex-1 min-w-0 mr-3">
            <div className="text-sm font-medium text-gray-700">📸 自動截圖</div>
            <p className="text-xs text-gray-400 mt-0.5">
              {data.screenshot
                ? '已啟用：暫停時每個螢幕各截一張（多螢幕分開傳），自動壓縮後發到 Telegram'
                : '已關閉：Telegram 通知裡只有文字決策訊息，可手動按「📸 截圖」取得畫面'}
            </p>
          </div>
          <button
            onClick={() => onUpdate({ screenshot: !data.screenshot })}
            className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${
              data.screenshot ? 'bg-emerald-500' : 'bg-gray-300'
            }`}
          >
            <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
              data.screenshot ? 'translate-x-5' : 'translate-x-0'
            }`} />
          </button>
        </div>

        {/* Preview previous output toggle */}
        <div className="flex items-center justify-between p-3 rounded-xl border border-gray-200 bg-gray-50/50">
          <div className="flex-1 min-w-0 mr-3">
            <div className="text-sm font-medium text-gray-700">📄 附上一步驟輸出檔案預覽</div>
            <p className="text-xs text-gray-400 mt-0.5">
              {data.previewPrevOutput
                ? '已啟用：自動 render 上一步驟 output.path 的檔案成 PNG 傳 TG（xlsx/csv/docx/pptx/pdf/圖片）'
                : '已關閉'}
            </p>
            {data.previewPrevOutput && (
              <p className="text-[11px] text-amber-600 mt-1 leading-relaxed">
                ℹ️ B1 模式（pandas/PIL）僅保留資料結構，不保留顏色/合併/圖表等樣式；
                若需原版式請安裝 LibreOffice（系統會自動 fallback）
              </p>
            )}
          </div>
          <button
            onClick={() => onUpdate({ previewPrevOutput: !data.previewPrevOutput })}
            className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${
              data.previewPrevOutput ? 'bg-emerald-500' : 'bg-gray-300'
            }`}
          >
            <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
              data.previewPrevOutput ? 'translate-x-5' : 'translate-x-0'
            }`} />
          </button>
        </div>

        {/* Send previous output as Telegram document */}
        <div className="flex items-center justify-between p-3 rounded-xl border border-gray-200 bg-gray-50/50">
          <div className="flex-1 min-w-0 mr-3">
            <div className="text-sm font-medium text-gray-700">📎 自動傳送上一步輸出檔到 Telegram</div>
            <p className="text-xs text-gray-400 mt-0.5">
              {data.sendPrevOutput
                ? '已啟用：抵達節點時把上一步 output.path 當文件傳到 TG（手機可直接下載）'
                : '已關閉（仍可在 TG 點「📎 上一步輸出」按鈕手動取）'}
            </p>
            <p className="text-[11px] text-gray-500 mt-1 leading-relaxed">
              ℹ️ 任何工作流的輸出檔都適用（markdown / xlsx / pdf / 圖片 / zip 等）。
              資料夾會自動打包成 zip。檔案 &gt; 50 MB 會跳過、TG 訊息提示去 host 取。
              不勾的話、Telegram 上人工確認的訊息**永遠**有「📎 上一步輸出 / 📂 任一步輸出」按鈕、
              要時點即可。
            </p>
          </div>
          <button
            onClick={() => onUpdate({ sendPrevOutput: !data.sendPrevOutput })}
            className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${
              data.sendPrevOutput ? 'bg-emerald-500' : 'bg-gray-300'
            }`}
          >
            <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
              data.sendPrevOutput ? 'translate-x-5' : 'translate-x-0'
            }`} />
          </button>
        </div>

        {/* Timeout 行為 */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">超時後的行動</label>
          <select
            value={(data as { hcOnTimeout?: string }).hcOnTimeout || 'wait'}
            onChange={e => onUpdate({ hcOnTimeout: e.target.value } as Partial<typeof data>)}
            className={`${inputCls} mb-2`}
          >
            <option value="wait">⏳ 永遠等(預設、超時欄位忽略)</option>
            <option value="pass">✅ 當作通過、繼續下一步</option>
            <option value="reject">↩️ 駁回、上一步重做</option>
            <option value="abort">⛔ 中止整條 workflow</option>
          </select>
          <p className="text-[11px] text-gray-400 mb-3">
            選「永遠等」時、下方秒數欄位完全忽略;選其他三個則依秒數倒數
          </p>
        </div>

        {/* Timeout 秒數 */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">超時秒數</label>
          <div className="grid grid-cols-4 gap-2 mb-2">
            {[
              { v: 600, label: '10 分鐘' },
              { v: 1800, label: '30 分鐘' },
              { v: 3600, label: '1 小時' },
              { v: 86400, label: '24 小時' },
            ].map(opt => (
              <button
                key={opt.v}
                onClick={() => onUpdate({ timeout: opt.v })}
                className={`py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                  data.timeout === opt.v
                    ? 'border-emerald-500 bg-emerald-50 text-emerald-700'
                    : 'border-gray-200 text-gray-600 hover:border-emerald-300'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <input
            type="number"
            min={60}
            max={259200}
            value={data.timeout}
            onChange={e => onUpdate({ timeout: parseInt(e.target.value) || 3600 })}
            className={`${inputCls} font-mono`}
          />
          <p className="text-xs text-gray-400 mt-1">
            「永遠等」模式下此值無作用;其他模式倒數此秒數沒回應就執行上方行動
          </p>
        </div>
      </div>

      {/* Footer */}
      <div className="p-4 border-t bg-emerald-50">
        <div className="flex items-center justify-between text-xs text-gray-400">
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full" style={{ background: CONFIRM_COLOR }} />
            人工確認節點
          </span>
          <span className={`px-2 py-0.5 rounded-full font-medium ${
            data.status === 'success' ? 'bg-green-100 text-green-700' :
            data.status === 'failed'  ? 'bg-red-100 text-red-700' :
            data.status === 'running' ? 'bg-amber-100 text-amber-700' :
            'bg-gray-100 text-gray-500'
          }`}>
            {data.status === 'idle' ? '等待中' : data.status === 'running' ? '確認中' : data.status === 'success' ? '已確認' : '已中止'}
          </span>
        </div>
      </div>
    </div>
  )
}
