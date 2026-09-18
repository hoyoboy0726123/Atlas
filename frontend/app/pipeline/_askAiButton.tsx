'use client'
/**
 * 「卡住就問 AI」按鈕 —— 把當下的設定狀態交給 AI 助手接話。
 *
 * 為什麼是「卡住才問」而不是「事前列待辦」:
 *   真實情況是邊做邊想 —— 使用者不知道整條流程長怎樣,是做到某一步才發現
 *   「我不知道怎麼把這個值傳到下一關」。事前產生的待辦清單幫不上這種忙。
 *
 * 按下去只做兩件事:組出當下狀態的摘要、打開聊天。使用者接著用白話問就好。
 */
import { ChevronRight, Sparkles } from 'lucide-react'
import { useWorkflowStore } from './_store'
import type { ComputerUseAction } from './_helpers'

/** 動作摘要:讓 AI 看得出「讀了什麼存到哪」「填了什麼進去」。 */
function summarizeActions(actions: ComputerUseAction[] | undefined): string {
  if (!actions || actions.length === 0) return '（還沒有任何動作）'
  return actions.map((a, i) => {
    const bits: string[] = [`#${i + 1} ${a.type}`]
    const c = a.control
    if (c) bits.push(`控制項=${c.name || c.auto_id || c.type || '?'}${c.auto_id ? `[${c.auto_id}]` : ''}`)
    if (a.label) bits.push(`標籤=${a.label}`)
    if (a.save_as) bits.push(`存成變數 {{${a.save_as}}}`)
    if (a.text) bits.push(`填入=${a.text}`)
    if (a.keys) bits.push(`按鍵=${Array.isArray(a.keys) ? a.keys.join('+') : a.keys}`)
    if (a.window) bits.push(`視窗=${a.window}`)
    return '  ' + bits.join('  ')
  }).join('\n')
}

/** 這個節點目前已經取到手的變數。 */
function collectVars(actions: ComputerUseAction[] | undefined): string[] {
  return (actions || []).map(a => a.save_as).filter((v): v is string => !!v && v.trim().length > 0)
}

interface Props {
  /** 節點顯示名稱，例如「桌面自動化節點」 */
  nodeLabel: string
  /** 節點在 YAML 裡的步驟名，AI 要用它組跨節點引用語法 */
  stepName?: string
  /** computer_use 專用：目標視窗 + 動作序列 */
  uiaWindow?: string
  actions?: ComputerUseAction[]
  /** 其他節點型別可以直接丟一段自己的狀態描述 */
  extraState?: string
  className?: string
}

export default function AskAiButton({
  nodeLabel, stepName, uiaWindow, actions, extraState, className = '',
}: Props) {
  const setAskAiContext = useWorkflowStore(s => s.setAskAiContext)
  const setChatUIState = useWorkflowStore(s => s.setChatUIState)

  const ask = () => {
    const vars = collectVars(actions)
    const lines: string[] = [
      '## 使用者正在編輯的節點（他按了「問 AI」，代表卡在這裡）',
      `節點：${nodeLabel}`,
    ]
    if (stepName) lines.push(`步驟名稱：${stepName}（跨節點引用時要用這個名字）`)
    if (uiaWindow !== undefined) lines.push(`目標視窗：${uiaWindow || '（留空＝當前最前面的視窗）'}`)
    if (actions !== undefined) {
      lines.push('', '目前的動作序列：', summarizeActions(actions))
      lines.push('', vars.length
        ? `這個節點已經取到手的變數：${vars.map(v => `{{${v}}}`).join('、')}`
        : '這個節點還沒有取到任何變數（沒有動作設了 save_as）')
    }
    if (extraState) lines.push('', extraState)

    lines.push(
      '',
      '## 回答方式',
      '- 直接接著他的進度講「下一步做什麼」，不要從頭複述整條流程。',
      '- 能直接給設定的（變數怎麼傳、運算式怎麼寫、條件怎麼分支、YAML 欄位填什麼）就直接給。',
      '- 需要他自己動手的（去畫面上挑控制項、錄製滑鼠點擊、開啟某個視窗），',
      '  就講清楚「在哪個面板、按哪個鈕、挑什麼」，不要只說「請自行設定」。',
      '- 跨節點引用變數的語法是 {{ steps.<步驟名>.output.<變數名> }}；',
      '  同一個節點內直接寫 {{<變數名>}} 就好。',
      '- 提醒他取值動作必須排在填值動作之前，否則變數是空的。',
    )
    setAskAiContext(lines.join('\n'))
    setChatUIState('sidebar')
  }

  return (
    <button
      type="button"
      onClick={ask}
      title="把目前這個節點的設定狀態交給 AI，讓它接著告訴你下一步"
      className={`inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] font-medium
                  whitespace-nowrap text-indigo-600 border border-indigo-200 bg-indigo-50/60
                  hover:bg-indigo-100 hover:border-indigo-300 transition-colors ${className}`}
    >
      {/* 圖示要 shrink-0,否則空間不夠時 flex 會把它壓成細線 */}
      <Sparkles className="w-3 h-3 shrink-0" />
      <span className="whitespace-nowrap">問 AI</span>
      <ChevronRight className="w-3 h-3 opacity-60 shrink-0" />
    </button>
  )
}
