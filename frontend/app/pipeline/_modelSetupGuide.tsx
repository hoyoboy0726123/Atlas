'use client'
// 「還沒有可用的 AI 模型」時取代錯誤訊息的設定指引。
// 首頁與側邊欄的 AI 助手共用;設定完成後 onRetry 會重送使用者剛剛那句話。
import { useEffect, useState } from 'react'
import { Loader2, RotateCcw, ExternalLink, CheckCircle2 } from 'lucide-react'
import { toast } from 'sonner'
import { getLlmReadiness, saveLlmKey, switchLlmModel, type LlmReadiness } from '@/lib/api'

const RECOMMENDED_OLLAMA = ['qwen3.8:27b', 'gemma4:12b']

function pickOllamaModel(models: string[]): string {
  return RECOMMENDED_OLLAMA.find(m => models.includes(m)) ?? models[0] ?? ''
}

function Cmd({ children }: { children: string }) {
  return (
    <code className="select-all rounded bg-gray-100 px-1.5 py-0.5 font-mono text-[0.92em] text-gray-800">
      {children}
    </code>
  )
}

interface Props {
  /** 'llm_not_ready' = 還沒設定;'llm_auth' = 金鑰無效 */
  code: string
  detail: string
  onRetry: () => void
  compact?: boolean
}

export default function ModelSetupGuide({ code, detail, onRetry, compact = false }: Props) {
  const [r, setR] = useState<LlmReadiness | null>(null)
  const [checking, setChecking] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [switching, setSwitching] = useState(false)
  const [ollamaModel, setOllamaModel] = useState('')

  const recheck = async (silent = false) => {
    setChecking(true)
    try {
      const x = await getLlmReadiness()
      setR(x)
      setOllamaModel(m => m || pickOllamaModel(x.detected.ollama.models))
      if (!silent && x.ready) toast.success('AI 模型已可使用')
      else if (!silent) toast.message(x.problem || '還沒有可用的模型')
    } catch (e) {
      if (!silent) toast.error(`檢查失敗:${e instanceof Error ? e.message : e}`)
    } finally {
      setChecking(false)
    }
  }

  useEffect(() => { recheck(true) }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const saveKey = async () => {
    if (!apiKey.trim() || saving) return
    setSaving(true)
    try {
      const x = await saveLlmKey('gemini', apiKey.trim())
      setR(x)
      setApiKey('')
      toast.success(`已儲存,改用 Gemini(${x.model})`)
      onRetry()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const switchTo = async (provider: string, model: string, label: string) => {
    setSwitching(true)
    try {
      const x = await switchLlmModel(provider, model)
      setR(x)
      if (!x.ready) { toast.error(x.problem || '切換後仍無法使用'); return }
      toast.success(`已改用 ${label}`)
      onRetry()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setSwitching(false)
    }
  }

  const d = r?.detected
  const ollamaReady = !!d?.ollama.running && d.ollama.models.length > 0
  const claudeReady = !!d?.claude_cli.installed && d.claude_cli.logged_in
  const geminiKeyOnly = !!d?.keys.gemini && r?.provider !== 'gemini'
  const quick = (ollamaReady || claudeReady || geminiKeyOnly) && !r?.ready
  const text = compact ? 'text-[11.5px]' : 'text-[13px]'

  return (
    <div className={`space-y-3 ${text} leading-relaxed text-gray-700`}>
      <div>
        <div className={`font-semibold text-gray-900 ${compact ? 'text-[13px]' : 'text-[15px]'}`}>
          {code === 'llm_auth' ? 'AI 模型的 API Key 無法使用' : '先設定一個 AI 模型,就能開始'}
        </div>
        <p className="mt-1">
          {(r?.problem || detail) + ' '}
          Atlas 需要一個 AI 模型來幫你規劃工作流。選下面任一種方式設定好,就會自動送出你剛剛的訊息。
        </p>
      </div>

      {r?.ready && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-emerald-800">
          <CheckCircle2 className="h-4 w-4 shrink-0" />
          <span className="flex-1">模型已經可以使用了({r.provider_label} · {r.model})。</span>
          <button type="button" onClick={onRetry}
            className="rounded-md bg-emerald-600 px-3 py-1 font-medium text-white hover:bg-emerald-700">
            送出剛剛的訊息
          </button>
        </div>
      )}

      {quick && d && (
        <div className="space-y-2 rounded-lg border border-emerald-200 bg-emerald-50/70 px-3 py-2.5">
          <div className="font-medium text-emerald-900">這台電腦已經有可以直接用的模型</div>
          {ollamaReady && (
            <div className="flex flex-wrap items-center gap-2">
              <span>本機 Ollama:</span>
              <select id="setup-ollama-model" value={ollamaModel} onChange={e => setOllamaModel(e.target.value)}
                className="min-w-0 max-w-full rounded border border-gray-300 bg-white px-1.5 py-1 font-mono text-[0.92em]">
                {d.ollama.models.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
              <button type="button" disabled={switching || !ollamaModel}
                onClick={() => switchTo('ollama', ollamaModel, `Ollama ${ollamaModel}`)}
                className="rounded-md bg-emerald-600 px-3 py-1 font-medium text-white hover:bg-emerald-700 disabled:opacity-50">
                改用並送出
              </button>
            </div>
          )}
          {claudeReady && (
            <div className="flex flex-wrap items-center gap-2">
              <span>已登入 Claude Code(訂閱)</span>
              <button type="button" disabled={switching}
                onClick={() => switchTo('claude_cli', r?.defaults.claude_cli || 'sonnet', 'Claude 訂閱')}
                className="rounded-md bg-emerald-600 px-3 py-1 font-medium text-white hover:bg-emerald-700 disabled:opacity-50">
                改用並送出
              </button>
            </div>
          )}
          {geminiKeyOnly && (
            <div className="flex flex-wrap items-center gap-2">
              <span>backend/.env 已有 Gemini API Key</span>
              <button type="button" disabled={switching}
                onClick={() => switchTo('gemini', r?.defaults.gemini || 'gemini-3.5-flash-lite', 'Gemini')}
                className="rounded-md bg-emerald-600 px-3 py-1 font-medium text-white hover:bg-emerald-700 disabled:opacity-50">
                改用並送出
              </button>
            </div>
          )}
        </div>
      )}

      <div className="rounded-lg border border-indigo-200 bg-white px-3 py-2.5">
        <div className="font-medium text-gray-900">
          方法一:貼上 Gemini API Key <span className="font-normal text-indigo-600">(推薦,最快)</span>
        </div>
        <ol className="mt-1 list-decimal space-y-1 pl-5">
          <li>
            到{' '}
            <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-0.5 text-indigo-600 underline underline-offset-2">
              Google AI Studio <ExternalLink className="h-3 w-3" />
            </a>{' '}
            用 Google 帳號免費建立一把 API Key。
          </li>
          <li>
            貼在這裡:
            <div className="mt-1 flex gap-1.5">
              <input id="setup-gemini-key" type="password" value={apiKey} autoComplete="off"
                onChange={e => setApiKey(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') saveKey() }}
                placeholder="AIza…"
                className="min-w-0 flex-1 rounded border border-gray-300 px-2 py-1 font-mono text-[0.92em] outline-none focus:border-indigo-400" />
              <button type="button" onClick={saveKey} disabled={!apiKey.trim() || saving}
                className="flex shrink-0 items-center gap-1 rounded-md bg-indigo-600 px-3 py-1 font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
                {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}儲存並送出
              </button>
            </div>
          </li>
        </ol>
        <p className="mt-1.5 text-[0.9em] text-gray-500">
          會先向 Google 確認這把 Key 有效才儲存。Key 只存在你電腦的 backend/.env;模型預設用 gemini-3.5-flash-lite。
        </p>
      </div>

      <details className="rounded-lg border border-gray-200 bg-white px-3 py-2">
        <summary className="cursor-pointer font-medium text-gray-900">方法二:本機 Ollama(免費,資料不離開電腦)</summary>
        <ol className="mt-1.5 list-decimal space-y-1 pl-5">
          <li>
            到{' '}
            <a href="https://ollama.com/download" target="_blank" rel="noopener noreferrer"
              className="text-indigo-600 underline underline-offset-2">ollama.com</a>{' '}
            下載並安裝。
          </li>
          <li>開啟終端機,執行 <Cmd>ollama pull gemma4:12b</Cmd>(需要 8 GB 顯示記憶體;有 24 GB 可改用 <Cmd>qwen3.8:27b</Cmd>)。</li>
          <li>下載完按下面的「重新檢查」,就會出現「改用並送出」。</li>
        </ol>
      </details>

      <details className="rounded-lg border border-gray-200 bg-white px-3 py-2">
        <summary className="cursor-pointer font-medium text-gray-900">方法三:Claude 訂閱(已有 Pro / Max 方案)</summary>
        <ol className="mt-1.5 list-decimal space-y-1 pl-5">
          <li>安裝 Node.js 18 以上,再執行 <Cmd>npm install -g @anthropic-ai/claude-code</Cmd></li>
          <li>執行 <Cmd>claude</Cmd>,照畫面指示用 Claude 帳號登入。</li>
          <li>登入完按下面的「重新檢查」,就會出現「改用並送出」。</li>
        </ol>
      </details>

      <div className="flex flex-wrap gap-2">
        <button type="button" onClick={() => recheck()} disabled={checking}
          className="flex items-center gap-1 rounded-md border border-gray-300 bg-white px-3 py-1 font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">
          {checking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
          重新檢查
        </button>
        <a href="/settings" target="_blank" rel="noopener noreferrer"
          className="flex items-center gap-1 rounded-md border border-gray-300 bg-white px-3 py-1 font-medium text-gray-700 hover:bg-gray-50">
          前往設定頁 <ExternalLink className="h-3.5 w-3.5" />
        </a>
      </div>
    </div>
  )
}
