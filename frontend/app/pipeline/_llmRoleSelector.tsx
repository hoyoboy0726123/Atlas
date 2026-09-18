'use client'
/**
 * 主 / 副 LLM 選擇器 — 給每個用 LLM 的節點 panel 用。
 *
 * 進階設定區塊內,讓使用者選此節點用「主模型」(預設)還是「副模型」(設定頁掛的第二個)。
 * 副模型沒設時、所有節點實際都會 fallback 跑主模型(後端 _resolve_role_settings 處理)。
 */
import { useEffect, useState } from 'react'
import { getModelSettings, type ModelSettings } from '@/lib/api'

interface Props {
  value: 'primary' | 'secondary'
  onChange: (v: 'primary' | 'secondary') => void
  className?: string
}

export default function LlmRoleSelector({ value, onChange, className = '' }: Props) {
  const [settings, setSettings] = useState<ModelSettings | null>(null)

  useEffect(() => {
    getModelSettings().then(setSettings).catch(() => {})
  }, [])

  const sec = settings?.secondary_provider ? `${settings.secondary_provider} / ${settings.secondary_model}` : null
  const pri = settings ? `${settings.provider} / ${settings.model}` : null

  return (
    <div className={className}>
      <label className="text-xs text-gray-500 block mb-1">使用模型</label>
      <div className="flex gap-1.5">
        <button
          type="button"
          onClick={() => onChange('primary')}
          className={`flex-1 px-2 py-1.5 text-xs rounded border transition-colors ${
            value === 'primary'
              ? 'bg-indigo-600 text-white border-indigo-600'
              : 'bg-white text-gray-700 border-gray-200 hover:border-indigo-400'
          }`}
        >
          🅟 主模型
          {pri && value === 'primary' && (
            <span className="block text-[10px] opacity-80 truncate font-mono">{pri}</span>
          )}
        </button>
        <button
          type="button"
          onClick={() => onChange('secondary')}
          disabled={!sec}
          title={sec ? '使用設定頁掛的副模型' : '副模型尚未設定(到設定頁啟用)、選了會 fallback 主模型'}
          className={`flex-1 px-2 py-1.5 text-xs rounded border transition-colors ${
            !sec
              ? 'bg-gray-50 text-gray-400 border-gray-200 cursor-not-allowed'
              : value === 'secondary'
                ? 'bg-amber-600 text-white border-amber-600'
                : 'bg-white text-gray-700 border-gray-200 hover:border-amber-400'
          }`}
        >
          🅢 副模型
          {sec && value === 'secondary' && (
            <span className="block text-[10px] opacity-80 truncate font-mono">{sec}</span>
          )}
          {!sec && (
            <span className="block text-[10px] opacity-80">(未設定)</span>
          )}
        </button>
      </div>
      <p className="text-[11px] text-gray-400 mt-1">
        {value === 'secondary' && !sec
          ? '⚠ 副模型未設定、實際會 fallback 跑主模型。到設定頁啟用副模型才會生效。'
          : '副模型在設定頁掛、不同節點可選不同模型搭配。'}
      </p>
    </div>
  )
}
