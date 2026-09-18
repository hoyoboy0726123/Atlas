'use client'
import { useState, useRef, useEffect } from 'react'
import { Bot, ChevronUp, ChevronDown, Send, Loader2, X, Minus, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'
import ReactMarkdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
// GFM：表格 / 刪除線。LLM 很愛用表格回答，沒這個的話 Markdown 表格
// 會原字直出一排 | 符號（Lite 實測）。
import remarkGfm from 'remark-gfm'
import { useWorkflowStore } from './_store'
import ModelSetupGuide from './_modelSetupGuide'
import {
  pipelineChatStream,
  getEnvPaths, type EnvPaths,
  getWorkflowChat, appendWorkflowChat, clearWorkflowChat, setWorkflowChat,
} from '@/lib/api'

// 後端回報「沒有可用的 AI 模型 / 金鑰無效」時丟這個,讓對話改顯示設定指引而不是錯誤訊息
class LlmSetupError extends Error {
  code: string
  constructor(code: string, detail: string) {
    super(detail)
    this.code = code
  }
}

// ── AI Chat Message Type ─────────────────────────────────────────────────────
interface ToolBlock {
  name: string         // tool 名稱(list_workflows / get_run_log 等)
  args: Record<string, unknown>
  status: 'running' | 'done'
  preview?: string     // tool_end 時填、回傳前 200 字
}

export interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  hasYaml?: boolean
  yaml?: string | null
  yamlError?: string | null
  toolBlocks?: ToolBlock[]   // 串流時顯示的 tool 呼叫紀錄
  streaming?: boolean        // 串流中(顯示游標 / 還在打字)
  setupGuide?: { code: string; detail: string }   // 沒有可用模型 → 顯示設定指引卡片
}

// 根據實際專案路徑組 AI 助手的初始訊息：範例使用真實可執行的腳本路徑，
// 輸出用相對 `ai_output/<name>/` 慣例。使用者可直接把範例描述貼給 AI 產生 YAML。
export function buildWelcomeMessage(env: EnvPaths): string {
  // 歡迎詞精簡化:節點介紹改成「📖 節點介紹」按鈕(中央彈窗)、範例改在 Hero 首頁(🆕 新話題)。
  // 這個對話區主要用途 = 針對「當前綁定的工作流」做除錯 / 修改。
  void env
  return `你好!我是這個工作流的 AI 助手 🤖

直接描述你想自動化的事,或把目前畫布上的步驟交給我**除錯 / 修改** — 我會問幾個關鍵問題、提一份方案讓你點頭,再幫你產生或更新 YAML。

· 想知道有哪些節點、各自適合什麼 → 點上方 **📖 節點介紹**
· 想開新題目 / 找完整範例 → 點左上角 **✨ 新對話** 回首頁挑一張範例卡(全新、與目前工作流脫鉤)`
}

// ── 節點介紹(📖 按鈕 → 中央彈窗)─────────────────────────────────────────
// 取代原本塞在歡迎詞裡的長清單。內容可隨節點演進在這裡集中維護。
const NODE_GUIDE: { icon: string; name: string; tag: string; desc: string }[] = [
  { icon: '🖥️', name: '腳本節點', tag: '跑你已寫好的程式',
    desc: '已有 .py / .bat / shell 想直接執行,最快、最可控。未勾虛擬環境 → 用系統全域 Python;勾了 → 用該專案自帶的 venv。' },
  { icon: '🤖', name: 'AI 技能節點', tag: '白話描述,AI 自動寫程式跑',
    desc: '沒有現成腳本、邏輯固定可重複時用。可掛官方技能(Word / PPT / Excel / PDF、GUI→CLI 拆解)。第二次起走 Recipe 快取、幾乎零 token。' },
  { icon: '🧠', name: '多代理節點', tag: '探索 / 試錯,32 種專業角色',
    desc: '不確定怎麼做、要研究 / 深度分析 / debug 時用。每次重新推理、邊做邊根據中間結果調整。token 用量約是 AI 技能的 2-5 倍。' },
  { icon: '✋', name: '人工確認節點', tag: '暫停,等你 Telegram 點頭',
    desc: '寄信、刪改等不可逆動作前的把關。可把上一步的產出直接傳到你手機再決定要不要續跑。' },
  { icon: '🔀', name: '條件節點', tag: '依結果分支(if / switch)',
    desc: '依上一步的輸出值決定走哪條路;可做 if / 多分支 switch。在畫布拖線即分支。' },
  { icon: '🌐', name: '網頁爬蟲節點', tag: 'URL → 乾淨內容',
    desc: '抓網頁轉 markdown,支援 SPA、需登入、Cloudflare 防護的站。影片模式可下載 YouTube / Vimeo / Bilibili。' },
  { icon: '📧', name: 'Outlook 自動化節點', tag: '收發信、附件、行事曆',
    desc: '10 個內建模板:寄信、附件寄送、批次讀信、下載附件、行事曆等(需本機 Outlook)。' },
  { icon: '👁️', name: '視覺驗證節點', tag: '用 VLM 看圖驗收',
    desc: '產出後讓視覺模型看畫面、判斷符不符合預期。需要支援視覺輸入的模型(如 Llama 4 Scout / Gemini / GPT-4o)。' },
  { icon: '🖱️', name: '桌面自動化節點', tag: '錄製滑鼠鍵盤、穩定回放',
    desc: '操作沒有 API 的舊軟體 UI。在畫布錄製(F7 待命開錄 / F9 結束),以圖像錨點 + UIA 多層 fallback 穩定回放。' },
]

export function NodeGuideModal({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
      onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl max-h-[85vh] flex flex-col"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <div>
            <h2 className="text-base font-bold text-gray-800">📖 節點介紹</h2>
            <p className="text-xs text-gray-400 mt-0.5">這個工作流能用的積木 — 描述需求時 AI 會自動幫你挑、組合</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none px-2">×</button>
        </div>
        <div className="overflow-y-auto p-4 grid grid-cols-1 sm:grid-cols-2 gap-2.5">
          {NODE_GUIDE.map(n => (
            <div key={n.name} className="rounded-xl border border-gray-100 bg-gray-50/60 p-3 hover:border-indigo-200 hover:bg-indigo-50/40 transition-colors">
              <div className="flex items-center gap-2">
                <span className="text-lg">{n.icon}</span>
                <span className="font-semibold text-sm text-gray-800">{n.name}</span>
              </div>
              <p className="text-[11px] font-medium text-indigo-500 mt-1">{n.tag}</p>
              <p className="text-xs text-gray-500 leading-relaxed mt-1">{n.desc}</p>
            </div>
          ))}
        </div>
        <div className="px-5 py-3 border-t border-gray-100 text-center">
          <p className="text-[11px] text-gray-400">不用記這些 — 直接用白話描述你的需求,AI 會幫你選對節點並串起來。</p>
        </div>
      </div>
    </div>
  )
}

// ── LaTeX → 純文字 / Unicode 清洗 ───────────────────────────────────────────
// LLM 偶爾會用 LaTeX 數學語法（$\rightarrow$ / $N$ / $\Rightarrow$），這個聊天 UI
// 沒裝 KaTeX、ReactMarkdown 會原字顯示一坨 "$\rightarrow$" 很醜。
// 在渲染前用 regex 把常見 LaTeX 命令換成 Unicode 對應字元；不做完整 LaTeX 解析、
// 沒 cover 的 case 至少把錢字號去掉、字母 / 命令裸露出來、可讀。
const _LATEX_CMD_TO_UNICODE: Record<string, string> = {
  rightarrow: '→', leftarrow: '←', Rightarrow: '⇒', Leftarrow: '⇐',
  to: '→', gets: '←', leftrightarrow: '↔', Leftrightarrow: '⇔',
  uparrow: '↑', downarrow: '↓', updownarrow: '↕',
  times: '×', div: '÷', pm: '±', mp: '∓',
  cdot: '·', cdots: '⋯', ldots: '…', dots: '…',
  leq: '≤', le: '≤', geq: '≥', ge: '≥', neq: '≠', ne: '≠',
  approx: '≈', equiv: '≡',
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε',
  theta: 'θ', lambda: 'λ', mu: 'μ', pi: 'π', sigma: 'σ', tau: 'τ',
  phi: 'φ', omega: 'ω',
  infty: '∞', forall: '∀', exists: '∃', in: '∈', notin: '∉',
  subset: '⊂', supset: '⊃', cup: '∪', cap: '∩',
  text: '', mathrm: '', mathbf: '', mathit: '',
}

export function cleanLatexInChat(text: string): string {
  if (!text || (!text.includes('$') && !text.includes('\\'))) return text
  // 1. inline math $...$ → 內容（剝掉 $ + 解析 LaTeX 命令）
  let out = text.replace(/\$([^\$\n]+?)\$/g, (_m, body: string) => {
    return body.replace(/\\([a-zA-Z]+)/g, (_full, cmd: string) =>
      _LATEX_CMD_TO_UNICODE[cmd] !== undefined ? _LATEX_CMD_TO_UNICODE[cmd] : cmd
    ).trim()
  })
  // 2. 在 $ 外面也可能有裸的 \rightarrow（沒包 $）→ 一起處理
  out = out.replace(/\\([a-zA-Z]+)/g, (m, cmd: string) =>
    _LATEX_CMD_TO_UNICODE[cmd] !== undefined ? _LATEX_CMD_TO_UNICODE[cmd] : m
  )
  return out
}

// ── AtlasChat Props ─────────────────────────────────────────────────────────
export type AtlasChatMode = 'sidebar' | 'hero' | 'mini' | 'drawer'

export interface AtlasChatProps {
  /** UI 呈現模式。
   * - `sidebar`(預設,Phase 1 唯一實作):嵌在左側 sidebar 底部、可摺疊
   * - `hero`:Phase 3 將實作 — 首頁中央大畫面
   * - `mini`:Phase 4 將實作 — 縮小成 floating button
   * - `drawer`:Phase 4 將實作 — 從旁邊滑出的抽屜
   */
  mode?: AtlasChatMode
  /** 套用 LLM 產生的 YAML 到 canvas(建立新工作流 / 覆寫目前)。
   *  mode='new' 時回傳新工作流 id(Hero 靠它把對話灌進新工作流);其餘回 null。 */
  onYamlApply: (yaml: string, mode: 'new' | 'overwrite') => void | Promise<string | null>
}

// ── AtlasChat Component ─────────────────────────────────────────────────────
// AI 助手聊天面板。原本內嵌於 _sidebar.tsx,Phase 1 抽出獨立元件、行為 100% 不變。
// Phase 2-5 會在這基礎上加 hero / mini / drawer 三種 mode。
export default function AtlasChat({ mode = 'sidebar', onYamlApply }: AtlasChatProps) {
  // 從 zustand store 拿目前綁定工作流的資訊(顯示「對話綁定:xxx」用)
  const { workflows, activeId } = useWorkflowStore()

  const [showChat, setShowChat] = useState(false)
  // 節點面板按「卡住了？問 AI」帶進來的當下設定狀態。
  // 有值就自動展開聊天,並在下一次送出時當 extra_system 附上去 —— AI 因此知道
  // 使用者卡在哪個節點、已經加了哪些動作、手上有哪些變數。附一次就清掉。
  const askAiContext = useWorkflowStore(s => s.askAiContext)
  const setAskAiContext = useWorkflowStore(s => s.setAskAiContext)
  useEffect(() => {
    if (askAiContext) setShowChat(true)
  }, [askAiContext])
  const [messages, setMessages] = useState<ChatMsg[]>([
    { role: 'assistant', content: '你好！請告訴我你想自動化的工作流程，我會幫你產生 Pipeline YAML 設定。\n\n（正在載入專案路徑資訊…）' }
  ])

  // 環境路徑（用來動態組 welcome message）— 跨多個 effect 共用
  const [envPaths, setEnvPaths] = useState<EnvPaths | null>(null)
  // 「新話題」後暫時解綁工作流：下次發訊息不帶 workflow_id（AI 看不到當前 yaml/canvas）
  // 解綁狀態會在使用者切換 activeId 時自動清掉、重新綁定
  const [chatUnbound, setChatUnbound] = useState(false)
  useEffect(() => {
    // activeId 變了（使用者切了 workflow） → 解除暫時解綁、回到正常綁定
    setChatUnbound(false)
  }, [activeId])
  useEffect(() => {
    getEnvPaths().then(setEnvPaths).catch(() => {/* ignore — 沿用預設訊息 */})
  }, [])

  // 對話歷史持久化：
  //   有 activeId（選了工作流）→ 後端 per-workflow chat
  //   沒 activeId（剛開 app）→ localStorage scratch 暫存
  // 切換 activeId 時重新載入對應的歷史；welcome 訊息只在歷史空時顯示
  const SCRATCH_LS_KEY = 'pipeline-ai-chat-scratch-v1'
  // 防止 initial load 把自己又 persist 回去（會造成無限循環 / 覆蓋 race）
  const loadingRef = useRef(false)
  // 「建立新工作流」時暫存目前對話 → 下次 activeId 切到新流時把它「複製」過去
  //（目前工作流的對話原樣保留、不清空 = both）。
  const carryOverRef = useRef<ChatMsg[] | null>(null)

  useEffect(() => {
    loadingRef.current = true
    const loadWelcome = (): ChatMsg => ({
      role: 'assistant',
      content: envPaths ? buildWelcomeMessage(envPaths)
        : '你好！請告訴我你想自動化的工作流程，我會幫你產生 Pipeline YAML 設定。',
    })
    const applyLoaded = (loaded: ChatMsg[]) => {
      setMessages(loaded.length > 0 ? loaded : [loadWelcome()])
      // 讓 React render 完再釋放 loading flag，避免緊接著的 setMessages 被誤判為使用者輸入
      setTimeout(() => { loadingRef.current = false }, 0)
    }
    // carry-over:剛按「建立新工作流」→ activeId 已切到新流 → 把暫存對話「複製」過去
    //（原工作流那邊的對話不動 = both）。略過抓新流空對話、改顯示帶過來的、並持久化到新流。
    if (carryOverRef.current && activeId) {
      const carried = carryOverRef.current
      carryOverRef.current = null
      applyLoaded(carried)
      ;(async () => {
        for (const m of carried) {
          if (!m.content) continue
          try { await appendWorkflowChat(activeId, m.role, m.content) } catch {/* ignore */}
        }
      })()
      toast.success('已把這段對話帶到新工作流（原本的對話也保留著）')
      return
    }
    if (activeId) {
      getWorkflowChat(activeId)
        .then(msgs => applyLoaded(msgs as ChatMsg[]))
        .catch(() => applyLoaded([]))
    } else {
      try {
        const raw = localStorage.getItem(SCRATCH_LS_KEY)
        const parsed = raw ? JSON.parse(raw) : []
        applyLoaded(Array.isArray(parsed) ? parsed : [])
      } catch {
        applyLoaded([])
      }
    }
  }, [activeId, envPaths])

  // 輔助：判斷目前顯示的是「歡迎訊息」單條還是使用者真的有對話
  // welcome 不該被寫進 DB 或 localStorage，避免每次載入都把 welcome 當歷史又寫回
  const isWelcomeOnly = (msgs: ChatMsg[]) =>
    msgs.length === 1 && msgs[0].role === 'assistant' && !msgs[0].hasYaml
  const [input, setInput]     = useState('')
  const [loading, setLoading] = useState(false)
  // 設定指引卡片按「送出」:移除卡片與它前面那句使用者訊息,再把那句重送一次
  const retryFromGuide = (i: number) => {
    const prevUser = messages[i - 1]
    if (!prevUser || prevUser.role !== 'user') return
    const base = messages.slice(0, i - 1)
    setMessages(base)
    handleSend({ text: prevUser.content, base, persistUser: false })
  }
  const chatEndRef = useRef<HTMLDivElement>(null)

  // 自動滾到底部
  useEffect(() => {
    if (showChat) chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, showChat])

  // opts 給「設定好模型後重送」用:text/base 指定要重送的訊息與它之前的對話,
  // persistUser=false 避免那句使用者訊息被第二次寫進對話紀錄
  const handleSend = async (opts?: { text?: string; base?: ChatMsg[]; persistUser?: boolean }) => {
    const text = (opts?.text ?? input).trim()
    if (!text || loading) return
    const userMsg: ChatMsg = { role: 'user', content: text }
    // 若當前只有 welcome 訊息，送出使用者訊息時把 welcome 丟掉（不存進歷史）
    const baseMsgs = opts?.base ?? (isWelcomeOnly(messages) ? [] : messages)
    const newMsgs = [...baseMsgs, userMsg]
    // 立即加入 user msg + 一個空的 assistant streaming bubble、後續 token 邊長邊填
    const assistantBubble: ChatMsg = {
      role: 'assistant',
      content: '',
      streaming: true,
      toolBlocks: [],
    }
    setMessages([...newMsgs, assistantBubble])
    setInput('')
    setLoading(true)
    if (opts?.persistUser !== false) persistAppend(userMsg).catch(() => {/* 落地失敗不擋 UI */})

    let accumulated = ''
    let finalHasYaml = false
    let finalYaml: string | null = null
    let finalYamlError: string | null = null
    try {
      const _askCtx = useWorkflowStore.getState().askAiContext
      if (_askCtx) setAskAiContext(null)   // 只附一次,別每輪重複塞進 system
      await pipelineChatStream(
        newMsgs.map(m => ({ role: m.role, content: m.content })),
        chatUnbound ? undefined : (activeId ?? null),
        (ev) => {
          if (ev.type === 'token') {
            accumulated += ev.text
            setMessages(prev => {
              const copy = [...prev]
              const last = copy[copy.length - 1]
              if (last && last.role === 'assistant' && last.streaming) {
                copy[copy.length - 1] = { ...last, content: accumulated }
              }
              return copy
            })
          } else if (ev.type === 'tool_start') {
            setMessages(prev => {
              const copy = [...prev]
              const last = copy[copy.length - 1]
              if (last && last.role === 'assistant' && last.streaming) {
                const blocks = [...(last.toolBlocks || [])]
                blocks.push({ name: ev.name, args: ev.args || {}, status: 'running' })
                copy[copy.length - 1] = { ...last, toolBlocks: blocks }
              }
              return copy
            })
          } else if (ev.type === 'tool_end') {
            setMessages(prev => {
              const copy = [...prev]
              const last = copy[copy.length - 1]
              if (last && last.role === 'assistant' && last.streaming && last.toolBlocks?.length) {
                const blocks = [...last.toolBlocks]
                // 找最近一個 running 的 tool block(同名)
                for (let i = blocks.length - 1; i >= 0; i--) {
                  if (blocks[i].name === ev.name && blocks[i].status === 'running') {
                    blocks[i] = { ...blocks[i], status: 'done', preview: ev.result_preview }
                    break
                  }
                }
                copy[copy.length - 1] = { ...last, toolBlocks: blocks }
              }
              return copy
            })
          } else if (ev.type === 'done') {
            finalHasYaml = ev.has_yaml
            finalYaml = ev.yaml_content
            finalYamlError = ev.yaml_error
            // 用 done 事件帶的 reply 覆寫(_clean_latex 處理過的版本、比累積 token 更乾淨)
            accumulated = ev.reply || accumulated
          } else if (ev.type === 'error') {
            if (ev.code === 'llm_not_ready' || ev.code === 'llm_auth') throw new LlmSetupError(ev.code, ev.detail)
            throw new Error(ev.detail || '串流錯誤')
          }
        },
        undefined,
        _askCtx,
      )
      // 串流結束、finalize bubble
      setMessages(prev => {
        const copy = [...prev]
        const last = copy[copy.length - 1]
        if (last && last.role === 'assistant' && last.streaming) {
          const finalized: ChatMsg = {
            role: 'assistant',
            content: accumulated,
            hasYaml: finalHasYaml,
            yaml: finalYaml,
            yamlError: finalYamlError,
            toolBlocks: last.toolBlocks,
            streaming: false,
          }
          copy[copy.length - 1] = finalized
          // 只 persist 不含 streaming flag、不含 toolBlocks(toolBlocks 是 ephemeral)
          persistAppend({
            role: 'assistant',
            content: accumulated,
            hasYaml: finalHasYaml,
            yaml: finalYaml,
            yamlError: finalYamlError,
          }).catch(() => {/* 同上 */})
        }
        return copy
      })
      if (finalYamlError) {
        const errStr: string = finalYamlError
        toast.error(`產生的 YAML 有語法問題：${errStr.slice(0, 120)}`)
      }
    } catch (e) {
      if (e instanceof LlmSetupError) {
        const guide: ChatMsg = { role: 'assistant', content: '', setupGuide: { code: e.code, detail: e.message } }
        setMessages(prev => {
          const copy = [...prev]
          const last = copy[copy.length - 1]
          if (last && last.role === 'assistant' && last.streaming) copy[copy.length - 1] = guide
          else copy.push(guide)
          return copy
        })
        return
      }
      const errMsg = e instanceof Error ? e.message : '未知錯誤'
      toast.error(`AI 回應失敗:${errMsg.slice(0, 220)}`)
      // 把錯誤替換到 streaming bubble 上
      setMessages(prev => {
        const copy = [...prev]
        const last = copy[copy.length - 1]
        if (last && last.role === 'assistant' && last.streaming) {
          copy[copy.length - 1] = {
            role: 'assistant',
            content: `❌ ${errMsg}`,
            streaming: false,
          }
        } else {
          copy.push({ role: 'assistant', content: `❌ ${errMsg}` })
        }
        return copy
      })
    } finally {
      setLoading(false)
    }
  }

  // 將一則訊息寫入持久層（backend 或 localStorage）
  const persistAppend = async (msg: ChatMsg) => {
    if (loadingRef.current) return  // 初次載入中不 persist 避免 race
    if (activeId) {
      try {
        await appendWorkflowChat(activeId, msg.role, msg.content)
      } catch {/* ignore — 下次進來 DB 讀不到最新一則，但不影響目前 UI */}
    } else {
      // scratch 模式：整個 messages 陣列寫 localStorage（最簡單、讀 side 也統一）
      try {
        // 用 setTimeout 0 確保拿到最新的 setMessages 後的 state
        setTimeout(() => {
          setMessages(curr => {
            try {
              const toSave = curr.filter(m => !m.yamlError)  // 不把 error marker 存進去
              localStorage.setItem(SCRATCH_LS_KEY, JSON.stringify(toSave))
            } catch {/* quota */}
            return curr
          })
        }, 0)
      } catch {/* ignore */}
    }
  }

  // 清除「這個工作流」的對話紀錄 —— 只洗掉聊天歷史,仍綁定當前工作流。
  // 跟左上角「新對話」(完全脫鉤、開新題目)不同:這裡保留 activeId 綁定,AI 下一輪仍會
  // 透過 _workflow_state_block 拿到當前工作流的 canvas/YAML、只是不再看到先前的對話。
  // 用途:聊天歷史被污染 / 太長 / 想重新開始討論「同一條工作流」時。
  const handleClearChat = async () => {
    if (loading) return
    if (!confirm('清除這個工作流的對話紀錄?\n(不影響工作流本身;AI 仍記得當前工作流、只會忘記先前聊過的內容)')) return
    const welcome: ChatMsg = {
      role: 'assistant',
      content: envPaths ? buildWelcomeMessage(envPaths)
        : '你好!請告訴我你想自動化的工作流程,我會幫你產生 Pipeline YAML 設定。',
    }
    setMessages([welcome])
    if (activeId) {
      try { await clearWorkflowChat(activeId) } catch {/* ignore — UI 已清、DB 清失敗不擋 */}
    } else {
      try { localStorage.removeItem(SCRATCH_LS_KEY) } catch {/* ignore */}
    }
    toast.success('已清除對話(仍綁定當前工作流)')
  }

  // 「新對話 / 開 Hero」入口已移到左上角側欄工具列(_sidebar.tsx,「新增」與「匯入」之間)。
  // 側欄按鈕直接呼叫 store 的 setChatUIState('hero');page.tsx 僅在 chatUIState==='hero'
  // 時掛載 <HeroMode>、離開即卸載 → 每次進 Hero 的 heroMessages 都是全新 []、保證乾淨狀態。

  // ── Hero 畫面(Phase 3)──────────────────────────────────────────────────
  // 首頁中央大畫面、玻璃感(可透出 canvas)、4 個範例卡片(送第一則後收起)、
  // 內嵌 chat history、輸入框、底部 2 個 CTA。
  // 點範例 → 文字塞進輸入框(不立即送出);送出 → 訊息在 hero 內展開、不切 sidebar;
  // 只有 ESC / 右上 X / CTA / YAML 套用才會切到 sidebar。
  //
  // Hero 是「全新對話」介面:不繼承 parent 的 messages、不讀 localStorage 歷史、
  // 不把訊息 persist 進 localStorage / backend。reload 後就消失,sidebar 模式
  // 的歷史照常持久化(workflow-bound)。
  if (mode === 'hero') {
    return <HeroMode
      envPaths={envPaths}
      onYamlApply={onYamlApply}
    />
  }
  if (mode === 'mini') {
    // TODO Phase 4 實作:縮小 floating button(右下角 / 右上角)、點開展 drawer
    return null
  }
  if (mode === 'drawer') {
    // TODO Phase 4 實作:從旁邊滑出的抽屜、含過渡動畫
    return null
  }

  // ── mode === 'sidebar'(預設、Phase 1 唯一實作的 mode) ─────────────────
  // 展開時以 absolute 覆蓋在 sidebar 下緣,佔 75% 高度(約蓋住工作流列表),收合時回到底部單列按鈕
  return (
    <div
      className={
        showChat
          ? 'absolute inset-x-0 bottom-0 top-1/4 bg-white border-t border-gray-100 flex flex-col z-20 shadow-lg'
          : 'border-t border-gray-100 flex flex-col'
      }
    >
      {/* Toggle button — 放大強調,讓使用者一眼看到左下角可開 AI 助手求助 */}
      <button
        onClick={() => setShowChat(!showChat)}
        title="點開 AI 助手,綁定目前工作流、用白話描述就能修改 / 診斷它(新增請用左上角「新對話」)"
        className={`w-full flex items-center gap-2.5 px-4 py-3 transition-colors ${
          showChat
            ? 'text-indigo-700 bg-indigo-50'
            : 'text-indigo-700 bg-gradient-to-r from-indigo-50 to-purple-50 hover:from-indigo-100 hover:to-purple-100'
        }`}
      >
        <span className={`flex items-center justify-center w-8 h-8 rounded-lg shrink-0 ${showChat ? 'bg-indigo-100 text-indigo-600' : 'bg-indigo-600 text-white shadow-sm'}`}>
          <Bot className="w-5 h-5" />
        </span>
        <span className="flex-1 text-left min-w-0">
          <span className="block text-[15px] font-bold leading-tight">AI 助手</span>
          {!showChat && <span className="block text-[11px] text-indigo-500/90 leading-tight">需要幫忙?點我用 AI 修改 / 診斷你的工作流</span>}
        </span>
        {loading && <Loader2 className="w-4 h-4 animate-spin text-indigo-500 shrink-0" />}
        {!loading && (showChat ? <ChevronDown className="w-4 h-4 shrink-0" /> : <ChevronUp className="w-4 h-4 shrink-0" />)}
      </button>

      {/* Chat panel */}
      {showChat && (
        <div className="flex flex-col flex-1 min-h-0 border-t border-gray-100">
          {/* Sub-toolbar：顯示目前綁定的工作流 + 新話題按鈕 */}
          <div className="flex flex-col gap-1 px-2.5 py-1.5 bg-gray-50/50 border-b border-gray-100 text-[11px] text-gray-500">
            {/* 第一行：綁定指示(左、長名換行不截斷)+ 清除對話鈕(右、同一行)*/}
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 break-words leading-snug flex-1">
                {chatUnbound ? (
                  <>🆕 新話題（未綁工作流；切換 / 重選工作流即重新綁定）</>
                ) : activeId ? (
                  <span className="flex flex-col gap-0.5">
                    <span className="text-[13px] text-gray-600">💾 對話綁定工作流</span>
                    <span className="text-[14px] font-bold text-blue-700 break-all">{workflows.find(w => w.id === activeId)?.name || activeId}</span>
                  </span>
                ) : (
                  <>📝 暫存模式（未選工作流；建立 / 選取後才會持久保存）</>
                )}
              </div>
              <button
                onClick={handleClearChat}
                disabled={loading}
                title="清除這個工作流的對話紀錄(只洗掉聊天歷史;AI 仍記得當前工作流、可繼續討論)"
                className="shrink-0 flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] text-gray-400 hover:text-red-600 hover:bg-red-50 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                🗑 清除對話
              </button>
            </div>
            {/* 「新對話」入口已移到左上角側欄工具列(「新增」與「匯入」之間)、此處不再放按鈕 */}
          </div>
          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-2.5 space-y-2.5">
            {messages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                {msg.role === 'assistant' && (
                  <div className="w-5 h-5 rounded-full bg-indigo-100 flex items-center justify-center shrink-0 mt-0.5 mr-1.5">
                    <Bot className="w-3 h-3 text-indigo-600" />
                  </div>
                )}
                <div className={`max-w-[88%] min-w-0 rounded-xl px-2.5 py-1.5 text-xs leading-relaxed break-words overflow-hidden ${
                  msg.role === 'user'
                    ? 'bg-indigo-600 text-white rounded-br-sm'
                    : 'bg-gray-100 text-gray-700 rounded-bl-sm'
                }`} style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
                  {/* Tool blocks（串流時顯示工具呼叫進度）*/}
                  {msg.role === 'assistant' && msg.toolBlocks && msg.toolBlocks.length > 0 && (
                    <div className="mb-1.5 space-y-1">
                      {msg.toolBlocks.map((tb, ti) => (
                        <div
                          key={ti}
                          className={`text-[11px] px-2 py-1 rounded border ${
                            tb.status === 'running'
                              ? 'bg-indigo-50 border-indigo-200 text-indigo-700'
                              : 'bg-gray-50 border-gray-200 text-gray-600'
                          }`}
                        >
                          {tb.status === 'running' ? (
                            <span className="flex items-center gap-1.5">
                              <Loader2 className="w-3 h-3 animate-spin shrink-0" />
                              <span className="font-mono">{tb.name}</span>
                              <span className="text-[10px] text-indigo-500/70 truncate">
                                {Object.entries(tb.args).slice(0, 2).map(([k, v]) =>
                                  `${k}=${typeof v === 'string' ? `"${v.slice(0, 30)}"` : JSON.stringify(v).slice(0, 30)}`
                                ).join(', ')}
                              </span>
                            </span>
                          ) : (
                            <span className="flex items-center gap-1.5">
                              <span className="text-emerald-500 shrink-0">✓</span>
                              <span className="font-mono">{tb.name}</span>
                              {tb.preview && (
                                <span className="text-[10px] text-gray-500 truncate" title={tb.preview}>
                                  {tb.preview.slice(0, 60)}
                                </span>
                              )}
                            </span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {msg.setupGuide ? (
                    <ModelSetupGuide compact code={msg.setupGuide.code} detail={msg.setupGuide.detail}
                      onRetry={() => retryFromGuide(i)} />
                  ) : msg.role === 'assistant' ? (
                    <div className="prose prose-xs max-w-none prose-p:my-0.5 prose-pre:text-xs prose-pre:whitespace-pre-wrap prose-code:break-all">
                      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]}>{cleanLatexInChat(msg.content.replace(/YAML_READY\n/g, ''))}</ReactMarkdown>
                      {msg.streaming && (
                        <span className="inline-block w-1.5 h-3 ml-0.5 bg-indigo-500 animate-pulse align-middle" />
                      )}
                    </div>
                  ) : (
                    <span className="whitespace-pre-wrap">{msg.content}</span>
                  )}
                  {msg.hasYaml && msg.yamlError && (
                    <div className="mt-1.5 p-2 rounded-lg bg-red-50 border border-red-200 text-[11px] text-red-700 leading-relaxed">
                      ⚠️ YAML 有問題，建議請 AI 修正後再套用：<br/>
                      <code className="break-all">{msg.yamlError}</code>
                    </div>
                  )}
                  {msg.hasYaml && msg.yaml && (
                    <div className="mt-1.5 grid grid-cols-2 gap-1">
                      <button
                        onClick={() => {
                          // 把目前對話複製一份帶去新工作流(目前的保留不動 = both)
                          carryOverRef.current = (isWelcomeOnly(messages) ? [] : messages)
                            .filter(m => !m.yamlError)
                          onYamlApply(msg.yaml!, 'new')
                        }}
                        disabled={!!msg.yamlError}
                        title={msg.yamlError
                          ? 'YAML 有解析錯誤、無法套用、請請 AI 重新產出完整 YAML'
                          : '建立一個新的工作流來放這份 YAML，不碰目前的'}
                        className={`flex items-center justify-center gap-1 py-1 rounded-lg text-xs font-medium transition-colors ${
                          msg.yamlError
                            ? 'bg-gray-200 text-gray-400 cursor-not-allowed'
                            : 'bg-emerald-500 hover:bg-emerald-400 text-white'
                        }`}
                      >
                        ＋ 建立新工作流
                      </button>
                      <button
                        onClick={() => {
                          if (!confirm('這會覆蓋目前工作流的內容（無法還原）。確定要繼續嗎？')) return
                          onYamlApply(msg.yaml!, 'overwrite')
                        }}
                        disabled={!!msg.yamlError}
                        title={msg.yamlError
                          ? 'YAML 有解析錯誤、無法套用、請請 AI 重新產出完整 YAML'
                          : '用這份 YAML 覆蓋目前工作流（會彈確認）'}
                        className={`flex items-center justify-center gap-1 py-1 rounded-lg text-xs font-medium transition-colors ${
                          msg.yamlError
                            ? 'bg-gray-200 text-gray-400 cursor-not-allowed border border-gray-300'
                            : 'border border-gray-300 bg-white hover:bg-gray-50 text-gray-600'
                        }`}
                      >
                        ⚠ 覆蓋目前
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}
            {loading && (
              <div className="flex items-center gap-2 text-xs text-gray-400 pl-7">
                <Loader2 className="w-3 h-3 animate-spin" /> 思考中…
              </div>
            )}
            <div ref={chatEndRef} />
          </div>

          {/* Input */}
          <div className="p-2 border-t border-gray-100 flex gap-1.5 items-end">
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => {
                // Shift+Enter 送出;Enter 純換行(輸入框小、避免誤送)
                if (e.key === 'Enter' && e.shiftKey) {
                  e.preventDefault()
                  if (input.trim() && !loading) handleSend()
                }
              }}
              placeholder="描述你的工作流…(Shift+Enter 送出 · Enter 換行)"
              disabled={loading}
              rows={2}
              className="flex-1 border border-gray-200 rounded-xl px-2.5 py-1.5 text-xs outline-none focus:border-indigo-400 transition-colors disabled:bg-gray-50 resize-none"
            />
            <button
              onClick={() => handleSend()}
              disabled={!input.trim() || loading}
              className="w-7 h-7 flex items-center justify-center bg-indigo-600 text-white rounded-xl hover:bg-indigo-700 disabled:opacity-40 transition-colors shrink-0"
            >
              <Send className="w-3 h-3" />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── HeroMode 子元件(Phase 3 — Soft Architectural Grid 風格重做)─────────
// Atlas 首頁中央大畫面。獨立子元件、由父 AtlasChat 在 mode==='hero' 時 render。
// 視覺定位:Soft Architectural Grid — 米色 + 32px 規則網格 + 純白卡片 + 4px hard
// shadow + brutalist 邊框、字型混 Bricolage Grotesque / JetBrains Mono / Plus
// Jakarta Sans。無圓角、無漸層、無 emoji(取代舊 dark glass overlay + sky 配色)。
//
// 8 個 ACTIONS 卡片對齊 Atlas 真實功能(對應 buildWelcomeMessage 內的 ex1-ex8):
//   chain        Python 腳本串接
//   scrape-ai    爬蟲 + AI + Outlook
//   existing     啟動既有 Python 專案
//   multiagent   多代理探索分析
//   schedule     排程定時任務
//   notify       人工確認與通知串
//   db-report    資料分析 → AI 報表
//   monitor      網頁變化偵測
interface HeroModeProps {
  envPaths: EnvPaths | null
  onYamlApply: (yaml: string, mode: 'new' | 'overwrite') => void
}

// ── Atlas Soft Architectural Grid — design tokens ───────────────────────
// 直接使用 design handoff README 的色票 / 字級 / 間距、不抽 CSS variable
// (避免污染 globals.css、HeroMode 是相對隔離的 overlay,單檔內聚就好)
const ATLAS_PAL = {
  bg:       '#F6F4EE',                    // 主背景(米色)
  bgCard:   '#FFFFFF',                    // 卡片底色
  ink:      '#16170F',                    // 主文字(近黑)
  inkSoft:  '#67685E',                    // 次要文字(60% 暖灰)
  rule:     'rgba(22,23,15,0.10)',        // 邊框與網格線
  forest:   '#3E5C4B',                    // 主色 — 強調連結、hover 邊框、accent
  brick:    '#B85A2E',
  sand:     '#D6B16D',
  dusk:     '#5470A1',
} as const

const FONT_DISPLAY = "'Bricolage Grotesque', sans-serif"
const FONT_BODY    = "'Plus Jakarta Sans', system-ui, sans-serif"
const FONT_MONO    = "'JetBrains Mono', monospace"

// ── ActionGlyph — 8 個幾何 SVG icon(逐字抄 reference HTML) ─────────────
type GlyphPalette = { fg: string; a1: string; a2: string; a3: string; a4: string }

function ActionGlyph({ id, size = 28, palette: c }: { id: ActionId; size?: number; palette: GlyphPalette }) {
  const props = { width: size, height: size, viewBox: '0 0 32 32', fill: 'none' as const, 'aria-hidden': true }
  switch (id) {
    case 'chain':
      return (
        <svg {...props}>
          <rect x="3" y="13" width="9" height="6" rx="2" fill={c.a1} />
          <rect x="11.5" y="13" width="9" height="6" rx="2" fill={c.a2} />
          <rect x="20" y="13" width="9" height="6" rx="2" fill={c.a3} />
        </svg>
      )
    case 'scrape-ai':
      return (
        <svg {...props}>
          <circle cx="10" cy="10" r="6" fill={c.a2} />
          <circle cx="22" cy="16" r="6" fill={c.a1} />
          <circle cx="14" cy="22" r="6" fill={c.a3} />
        </svg>
      )
    case 'existing':
      return (
        <svg {...props}>
          <rect x="5" y="5" width="22" height="22" rx="3" fill="none" stroke={c.fg} strokeWidth="1.5" />
          <rect x="9" y="9" width="14" height="3" rx="1" fill={c.a2} />
          <rect x="9" y="14" width="9" height="3" rx="1" fill={c.a3} />
          <rect x="9" y="19" width="11" height="3" rx="1" fill={c.a1} />
        </svg>
      )
    case 'multiagent':
      return (
        <svg {...props}>
          <circle cx="9" cy="9" r="4.5" fill={c.a1} />
          <circle cx="23" cy="9" r="4.5" fill={c.a3} />
          <circle cx="16" cy="22" r="4.5" fill={c.a2} />
          <path d="M9 9 L23 9 M9 9 L16 22 M23 9 L16 22" stroke={c.fg} strokeWidth="1" opacity="0.4" />
        </svg>
      )
    case 'research':
      // 放大鏡 + 書本(研究)
      return (
        <svg {...props}>
          <rect x="6" y="6" width="14" height="18" rx="1.5" fill={c.a2} />
          <path d="M10 11 L17 11 M10 14 L17 14 M10 17 L15 17" stroke={c.fg} strokeWidth="1.2" strokeLinecap="round" opacity="0.6" />
          <circle cx="22" cy="22" r="5" fill="none" stroke={c.a1} strokeWidth="2.4" />
          <path d="M26 26 L29 29" stroke={c.a1} strokeWidth="2.4" strokeLinecap="round" />
        </svg>
      )
    case 'compete':
      // 矩陣 / 對比表(競品)
      return (
        <svg {...props}>
          <rect x="5" y="5" width="9" height="9" rx="1" fill={c.a1} />
          <rect x="18" y="5" width="9" height="9" rx="1" fill={c.a2} />
          <rect x="5" y="18" width="9" height="9" rx="1" fill={c.a3} />
          <rect x="18" y="18" width="9" height="9" rx="1" fill="none" stroke={c.fg} strokeWidth="1.5" />
        </svg>
      )
    case 'db-report':
      return (
        <svg {...props}>
          <ellipse cx="16" cy="8" rx="9" ry="3" fill={c.a2} />
          <path d="M7 8 V15 C7 17 11 18 16 18 C21 18 25 17 25 15 V8" fill={c.a3} />
          <path d="M7 15 V22 C7 24 11 25 16 25 C21 25 25 24 25 22 V15" fill={c.a1} />
        </svg>
      )
    case 'monitor':
      return (
        <svg {...props}>
          <circle cx="14" cy="14" r="8" fill="none" stroke={c.fg} strokeWidth="1.5" />
          <circle cx="14" cy="14" r="3" fill={c.a1} />
          <path d="M20 20 L26 26" stroke={c.fg} strokeWidth="2.4" strokeLinecap="round" />
        </svg>
      )
    case 'outlook-todo':
      return (
        <svg {...props}>
          <rect x="5" y="8" width="18" height="13" rx="1.5" fill="none" stroke={c.fg} strokeWidth="1.5" />
          <path d="M5.5 9 L14 15.5 L22.5 9" fill="none" stroke={c.a1} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )
  }
}

// ── AtlasMark — 跟 sidebar 一致的 indigo gradient + 山峰 SVG ──────────
// (之前用 brutalist 幾何版本、跟畫布 sidebar 樣式不一致、改成跟 sidebar 同款)
function AtlasMark({ size = 32 }: { size?: number }) {
  return (
    <div
      className="rounded-lg bg-gradient-to-br from-indigo-500 via-indigo-600 to-purple-600 flex items-center justify-center shrink-0 shadow-md"
      style={{ width: size, height: size }}
    >
      <svg
        viewBox="0 0 24 24"
        width={size * 0.75}
        height={size * 0.75}
        fill="none"
        stroke="white"
        strokeWidth="2.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-label="Atlas"
      >
        <path d="M4 21 L12 3 L20 21" />
        <path d="M7.8 14 L16.2 14" />
      </svg>
    </div>
  )
}

// ── 8 個 ACTIONS(對齊 Atlas 真實功能)─────────────────────────────────────
type ActionId =
  | 'chain' | 'scrape-ai' | 'existing' | 'multiagent'
  | 'research' | 'compete' | 'db-report' | 'monitor' | 'outlook-todo'

interface AtlasAction {
  id: ActionId
  title: string
  desc: string
  tag: string
  examples: string[]
}

// 卡片內容對齊 Atlas 真實節點集合(網頁爬蟲 / AI 技能 / 多代理 / 人工確認 / Outlook
// / 視覺驗證 / 啟動既有 Python 專案 等)、與 buildWelcomeMessage 的範例呼應。
//
// ⚠ 所有 example 都設計成「點下去 AI 助手能寫出可直接跑通的 YAML」、不依賴 user
// 提供額外檔案。需要資料的 case(資料分析、多代理)由 AI 助手在第一步生假資料 demo。
const ATLAS_ACTIONS: AtlasAction[] = [
  {
    id: 'chain', title: '啟動 Python 專案 / 腳本', desc: '跑你現成的專案、或把 .py 串成工作流', tag: 'Workflow',
    examples: [
      '我有幾支依序執行的 .py(給資料夾路徑)→ 串成工作流,上一步的輸出當下一步的輸入',
      '我有既有 GUI / CLI 專案(給資料夾路徑)→ AI 讀源碼、ask_user 收參數再跑',
    ],
  },
  {
    id: 'db-report', title: '資料分析 → 視覺化', desc: 'pandas 分析 + 圖表 / 儀表板', tag: 'Insight',
    examples: [
      '先產一份示範銷售資料 → pandas 分析 → 趨勢圖 + python-docx 產出 Word 報告',
      '我有自己的 csv / xlsx → 整合成含原生圖表的 Excel 儀表板',
    ],
  },
  {
    id: 'compete', title: '自然語言造工具（可重複用）', desc: '一句話 → AI 寫程式跑通 → 第二次起 0 成本 replay', tag: 'Skill',
    examples: [
      '把一份銷售 csv 依地區拆成多個檔 → AI 一句話寫好跑通(下次同檔 recipe 免費 replay、不再叫 LLM)',
      '描述一件重複雜事(合併多個 csv / 批次改檔名 / 抽 PDF 表格 / 用資料生 PPT)→ AI 寫好跑通,成為你的專屬工具',
    ],
  },
  {
    id: 'outlook-todo', title: 'Outlook 郵件自動化', desc: '讀信 → 分類 → 判優先 → TG 通知', tag: 'Inbox',
    examples: [
      '搜當日 Outlook 收件匣 → 依專案分類 → 四面向判前 3 優先 → 產 Word 待辦表 → human_confirm 發 TG',
      '抓收件匣未回覆超過 3 天的信 → 整理成 Word 待辦清單 → 提醒我回覆',
    ],
  },
  {
    id: 'scrape-ai', title: '爬蟲 + AI + Outlook', desc: '抓 → 摘要 → 確認 → 寄信', tag: 'Pipeline',
    examples: [
      '每天抓 PTT 科技業板熱門 → AI 摘要 → 產 Word 報告 → Telegram 確認 → Outlook 夾 Word 寄信',
      '抓 Hacker News top 10 → 中文翻譯 + 重點 → 產 Word → Outlook 夾 Word 草稿',
    ],
  },
  {
    id: 'multiagent', title: '多代理協作', desc: '多角色分工:分析 / 研究 / 審查 / 撰寫', tag: 'Agent',
    examples: [
      '先產一份示範客戶回饋 → 多代理:分析師找模式 + 研究員歸納主題 + 寫手產洞察報告 → 轉成 Word',
      '先產一份示範銷售資料 → data_analyst 探索式分析 → 轉成 Word 報告',
    ],
  },
  {
    id: 'monitor', title: '條件分流工作流', desc: '依結果走不同路（if / switch）', tag: 'Branch',
    examples: [
      '月營收達標 → 產獎勵 Word 報告;未達標 → 產改善 Word 警示(condition 分流,先產示範資料)',
      '我的流程「若 X 成立就做 A、否則做 B」→ 幫我接成條件分流工作流',
    ],
  },
  {
    id: 'research', title: '網路研究 → 報告', desc: '收料 → 分析 → 深度報告(建議搭強模型)', tag: 'Research',
    examples: [
      '深度研究某主題 → researcher 收料 → report_writer 寫報告 → docx 產 Word(沒網址→web_search;建議搭 Claude / GPT)',
      '三款筆電規格比較(web_search 收料、不爬蟲)→ Word 比較報告',
    ],
  },
]

// Hero 範例卡 hover:滑鼠停在範例區上半 → 自動往上捲、下半 → 往下捲(免滾輪)。
// 卡片本身固定大小不變、只在內部捲動。
function AutoScrollExamples({ examples, onPick }: { examples: readonly string[]; onPick: (k: number) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const dirRef = useRef(0)
  const rafRef = useRef(0)
  const tick = () => {
    const el = ref.current
    if (el && dirRef.current) el.scrollTop += dirRef.current * 2.5
    rafRef.current = dirRef.current ? requestAnimationFrame(tick) : 0
  }
  useEffect(() => () => { if (rafRef.current) cancelAnimationFrame(rafRef.current) }, [])
  return (
    <div
      ref={ref}
      className="[&::-webkit-scrollbar]:w-1 [&::-webkit-scrollbar-thumb]:bg-black/15 [&::-webkit-scrollbar-thumb]:rounded-full"
      style={{ flex: 1, minHeight: 0, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 4 }}
      onMouseMove={(e) => {
        const el = ref.current
        if (!el || el.scrollHeight <= el.clientHeight + 2) { dirRef.current = 0; return }
        const rect = el.getBoundingClientRect()
        const r = (e.clientY - rect.top) / rect.height
        dirRef.current = r > 0.62 ? 1 : r < 0.38 ? -1 : 0
        if (dirRef.current && !rafRef.current) rafRef.current = requestAnimationFrame(tick)
      }}
      onMouseLeave={() => { dirRef.current = 0 }}
    >
      {examples.map((ex, k) => (
        <div
          key={k}
          onClick={(e) => { e.stopPropagation(); onPick(k) }}
          onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = ATLAS_PAL.bg }}
          onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'transparent' }}
          style={{
            fontSize: 11.5, lineHeight: 1.35, color: ATLAS_PAL.ink,
            display: 'flex', gap: 6, padding: '4px 6px',
            marginLeft: -6, marginRight: -6,
            borderBottom: k === examples.length - 1 ? 'none' : `1px solid ${ATLAS_PAL.rule}`,
            cursor: 'pointer', transition: 'background 120ms', flexShrink: 0,
          }}
        >
          <span style={{ color: ATLAS_PAL.forest, fontFamily: FONT_MONO, fontSize: 10, flexShrink: 0 }}>+</span>
          <span>{ex}</span>
        </div>
      ))}
    </div>
  )
}

function HeroMode({ envPaths, onYamlApply }: HeroModeProps) {
  const setChatUIState = useWorkflowStore(s => s.setChatUIState)
  const setHasInteracted = useWorkflowStore(s => s.setHasInteracted)
  const activeId = useWorkflowStore(s => s.activeId)
  const inputRef = useRef<HTMLTextAreaElement>(null)   // Hero 首次輸入框(已改 textarea,支援 Shift+Enter 換行)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const chatEndRef = useRef<HTMLDivElement>(null)
  // 出場淡入(opacity 0 → 1,300ms)— 用 mounted flag + CSS transition
  const [mounted, setMounted] = useState(false)
  // 退場淡出(opacity → 0 + scale 0.98,200ms)— 觸發後等動畫結束才切 state
  const [closing, setClosing] = useState(false)

  // ── Hero 專屬 ephemeral state ───────────────────────────────────────────
  // Hero 是「全新對話」介面 — 不繼承 parent messages、不讀 localStorage 歷史、
  // 不 persist 出去。reload 後就消失,讓進站體驗永遠像第一次見面。
  // (sidebar 模式仍會走 parent AtlasChat 的 workflow-bound 歷史)
  const [heroMessages, setHeroMessages] = useState<ChatMsg[]>([])
  const [heroInput, setHeroInput] = useState('')
  const [heroLoading, setHeroLoading] = useState(false)
  // Soft Architectural Grid 卡片 hover 狀態 — 單一 hoveredId、同時只能有一張卡 active
  const [hoveredCard, setHoveredCard] = useState<ActionId | null>(null)

  useEffect(() => {
    // 進場:下一個 frame 設 mounted = true、讓 opacity 0 → 1 過渡
    const t = requestAnimationFrame(() => setMounted(true))
    return () => cancelAnimationFrame(t)
  }, [])

  // hasStarted:對話是否已展開(本次 session 有任何 user 訊息)
  // - 初始(heroMessages 空)→ false:顯示歡迎大標 + 4 卡片 + CTA
  // - 送出第一則訊息後 → true:卡片 / CTA 收起、改顯示 chat history scroll area
  const hasStarted = heroMessages.some(m => m.role === 'user')

  // hasStarted 切換時、自動滾到最新訊息
  useEffect(() => {
    if (hasStarted) chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [heroMessages, hasStarted])

  // 切到 sidebar mode 的統一 helper:含退場動畫
  const exitToSidebar = (markInteracted: boolean = false) => {
    if (closing) return
    setClosing(true)
    if (markInteracted) setHasInteracted(true)
    // 等 fade out 動畫結束才真正切 state
    setTimeout(() => setChatUIState('sidebar'), 200)
  }

  // ESC 鍵 → 切 sidebar(逃生口)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        exitToSidebar(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [closing]) // eslint-disable-line react-hooks/exhaustive-deps

  // 點範例卡片 → 把指定 example 塞進輸入框(不立即送出、focus 給使用者改)
  // exampleIdx 可指定要用哪個 example(預設 0、給「點 card 整體」用)
  // 點「具體某行 example」時、handler 會傳對應 idx、不再永遠用第一個
  const onCardClick = (action: AtlasAction, exampleIdx: number = 0) => {
    const text = action.examples[exampleIdx] ?? action.examples[0]
    setHeroInput(text)
    setTimeout(() => inputRef.current?.focus(), 0)
  }

  // Hero 輸入框 auto-grow:隨內容自動長高、最多 6 行(≈120px)、超過則框內捲動。
  // 兩個輸入框(歡迎大框 inputRef / 對話續打 textareaRef)共用 heroInput、同一時間只掛載一個。
  // 涵蓋:打字、點範例卡填入(setHeroInput)、送出後清空(setHeroInput('') → 縮回單行)。
  useEffect(() => {
    const MAX_H = 120  // ≈ 6 行
    for (const t of [inputRef.current, textareaRef.current]) {
      if (!t) continue
      t.style.height = 'auto'
      t.style.height = Math.min(t.scrollHeight, MAX_H) + 'px'
      t.style.overflowY = t.scrollHeight > MAX_H ? 'auto' : 'hidden'
    }
  }, [heroInput])

  // Hero-local handleSend:走 pipelineChatStream、但完全不 persist(不寫 localStorage、
  // 不呼 appendWorkflowChat)。訊息只活在這個 HeroMode component 的 state 裡、
  // reload 就消失。
  // ⚠️ workflow_id 一律不帶(送 null)—— Hero 是「全新對話 / 永遠建新工作流」入口,
  // 必須跟當前畫布工作流「完全脫鉤」:不帶歷史、也不帶 workflow_id。
  // 否則後端 _workflow_state_block 會把當前(如 PPT)工作流整份 YAML 灌進 system prompt、
  // 害 AI 把無關的新需求當成「對該工作流的增量編輯」(記憶汙染 bug 的真正來源)。
  const heroHandleSend = async (opts?: { text?: string; base?: ChatMsg[] }) => {
    const text = (opts?.text ?? heroInput).trim()
    if (!text || heroLoading) return
    const userMsg: ChatMsg = { role: 'user', content: text }
    const baseMsgs = opts?.base ?? heroMessages
    const newMsgs = [...baseMsgs, userMsg]
    const assistantBubble: ChatMsg = {
      role: 'assistant',
      content: '',
      streaming: true,
      toolBlocks: [],
    }
    setHeroMessages([...newMsgs, assistantBubble])
    setHeroInput('')
    setHeroLoading(true)

    let accumulated = ''
    let finalHasYaml = false
    let finalYaml: string | null = null
    let finalYamlError: string | null = null
    try {
      await pipelineChatStream(
        newMsgs.map(m => ({ role: m.role, content: m.content })),
        null,   // Hero = 全新對話、與當前畫布工作流完全脫鉤(不帶 workflow_id)
        (ev) => {
          if (ev.type === 'token') {
            accumulated += ev.text
            setHeroMessages(prev => {
              const copy = [...prev]
              const last = copy[copy.length - 1]
              if (last && last.role === 'assistant' && last.streaming) {
                copy[copy.length - 1] = { ...last, content: accumulated }
              }
              return copy
            })
          } else if (ev.type === 'tool_start') {
            setHeroMessages(prev => {
              const copy = [...prev]
              const last = copy[copy.length - 1]
              if (last && last.role === 'assistant' && last.streaming) {
                const blocks = [...(last.toolBlocks || [])]
                blocks.push({ name: ev.name, args: ev.args || {}, status: 'running' })
                copy[copy.length - 1] = { ...last, toolBlocks: blocks }
              }
              return copy
            })
          } else if (ev.type === 'tool_end') {
            setHeroMessages(prev => {
              const copy = [...prev]
              const last = copy[copy.length - 1]
              if (last && last.role === 'assistant' && last.streaming && last.toolBlocks?.length) {
                const blocks = [...last.toolBlocks]
                for (let i = blocks.length - 1; i >= 0; i--) {
                  if (blocks[i].name === ev.name && blocks[i].status === 'running') {
                    blocks[i] = { ...blocks[i], status: 'done', preview: ev.result_preview }
                    break
                  }
                }
                copy[copy.length - 1] = { ...last, toolBlocks: blocks }
              }
              return copy
            })
          } else if (ev.type === 'done') {
            finalHasYaml = ev.has_yaml
            finalYaml = ev.yaml_content
            finalYamlError = ev.yaml_error
            accumulated = ev.reply || accumulated
          } else if (ev.type === 'error') {
            if (ev.code === 'llm_not_ready' || ev.code === 'llm_auth') throw new LlmSetupError(ev.code, ev.detail)
            throw new Error(ev.detail || '串流錯誤')
          }
        },
      )
      setHeroMessages(prev => {
        const copy = [...prev]
        const last = copy[copy.length - 1]
        if (last && last.role === 'assistant' && last.streaming) {
          copy[copy.length - 1] = {
            role: 'assistant',
            content: accumulated,
            hasYaml: finalHasYaml,
            yaml: finalYaml,
            yamlError: finalYamlError,
            toolBlocks: last.toolBlocks,
            streaming: false,
          }
        }
        return copy
      })
      if (finalYamlError) {
        const errStr: string = finalYamlError
        toast.error(`產生的 YAML 有語法問題:${errStr.slice(0, 120)}`)
      }
    } catch (e) {
      if (e instanceof LlmSetupError) {
        const guide: ChatMsg = { role: 'assistant', content: '', setupGuide: { code: e.code, detail: e.message } }
        setHeroMessages(prev => {
          const copy = [...prev]
          const last = copy[copy.length - 1]
          if (last && last.role === 'assistant' && last.streaming) copy[copy.length - 1] = guide
          else copy.push(guide)
          return copy
        })
        return
      }
      const errMsg = e instanceof Error ? e.message : '未知錯誤'
      toast.error(`AI 回應失敗:${errMsg.slice(0, 220)}`)
      setHeroMessages(prev => {
        const copy = [...prev]
        const last = copy[copy.length - 1]
        if (last && last.role === 'assistant' && last.streaming) {
          copy[copy.length - 1] = {
            role: 'assistant',
            content: `❌ ${errMsg}`,
            streaming: false,
          }
        } else {
          copy.push({ role: 'assistant', content: `❌ ${errMsg}` })
        }
        return copy
      })
    } finally {
      setHeroLoading(false)
    }
  }

  // 設定指引卡片按「送出」:移除卡片與它前面那句使用者訊息,再把那句重送一次
  const heroRetryFromGuide = (i: number) => {
    const prevUser = heroMessages[i - 1]
    if (!prevUser || prevUser.role !== 'user') return
    const base = heroMessages.slice(0, i - 1)
    setHeroMessages(base)
    heroHandleSend({ text: prevUser.content, base })
  }

  // 送出 → 走 hero-local handleSend、訊息只更新 heroMessages、不 persist
  // 標記 hasInteracted,讓下次進站直接走 sidebar mode(不要再彈 hero)
  const onSubmit = async () => {
    if (!heroInput.trim() || heroLoading) return
    setHasInteracted(true)
    await heroHandleSend()
  }

  // Enter 送出 / Shift+Enter 換行
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSubmit()
    }
  }

  // 點 overlay 灰色邊緣不關閉(避免誤點、必須主動操作 CTA 才關)
  const onOverlayClick = (e: React.MouseEvent) => {
    // 不做任何事 — 留著當 click sink
    e.stopPropagation()
  }

  // 點「跑現有工作流」CTA — Phase 5 才接 modal、現在先 toast + 切 sidebar
  // (Soft Architectural Grid 設計不顯示此 CTA、保留 handler 給未來用)
  const _onRunExisting = () => {
    toast.info('Phase 5 將開啟「選擇現有工作流」對話框')
    exitToSidebar(true)
  }

  // Footer:[+] new canvas — 純切 sidebar、不發訊息(切到空白畫布)
  const onNewCanvas = () => {
    exitToSidebar(true)
  }

  // Footer:[⌥] import .py — Phase 5 才接、現在 placeholder
  const onImportPy = () => {
    // eslint-disable-next-line no-console
    console.log('[Hero] import .py — TODO: open file picker (Phase 5)')
    toast.info('import .py 功能即將上線(Phase 5)')
  }

  // Footer:[⌘K] command — Phase 5 才接 command palette
  const onCmdK = () => {
    // eslint-disable-next-line no-console
    console.log('[Hero] command palette — TODO (Phase 5)')
    toast.info('Command palette 即將上線(Phase 5)')
  }

  // 右上「最小化」按鈕:把 hero 收到 sidebar、保留對話內容
  const onMinimize = () => {
    exitToSidebar(true)
  }

  // YAML 套用包裝:套用後 hero 不關、繼續在 hero 內對話
  // (若想立刻去畫布看結果、使用者可按最小化按鈕)
  const handleYamlApplyInHero = async (yaml: string, mode: 'new' | 'overwrite') => {
    // onYamlApply 的回傳是 void | string | null(sidebar 版不回傳 id)→ 窄化成 string | null
    const applied = await onYamlApply(yaml, mode)
    const newId = typeof applied === 'string' ? applied : null
    // Hero 對話原本不 persist,套用後就永久消失 —— 但那段對話是「為什麼這樣設計」
    // 的唯一紀錄。工作流建好後把它灌進該工作流的對話,之後在側邊助手回溯得到。
    // Hero 只有「建立新工作流」一顆按鈕(overwrite 按鈕只在 sidebar),所以這裡
    // 一定是全新的空工作流 → 用 setWorkflowChat 整份寫入,不會蓋掉任何既有討論。
    if (newId) {
      const carry = heroMessages
        .filter(m => !m.yamlError && (m.content || '').trim() && !m.streaming)
        .map(m => ({ role: m.role as 'user' | 'assistant', content: m.content }))
      if (carry.length) {
        try {
          await setWorkflowChat(newId, [
            { role: 'assistant' as const,
              content: '── 以下為在首頁建立此工作流時的原始討論(自動保留、方便回溯需求來源)──' },
            ...carry,
          ])
        } catch {/* 灌入失敗不該擋住進畫布 —— 工作流本身已建立成功 */}
      }
    }
    // 套 YAML 是「進畫布」的訊號、自動切回 sidebar 讓使用者看到畫布
    exitToSidebar(true)
  }

  // 整體 fade in/out 樣式:mounted false 或 closing true → opacity 0
  const containerOpacity = (!mounted || closing) ? 'opacity-0' : 'opacity-100'
  const cardScale = closing ? 'scale-[0.98]' : 'scale-100'

  // ── Soft Architectural Grid 配色 / 字型 ────────────────────────────────
  // 這些 inline style 直接打在 element 上、不走 Tailwind。原因:
  // 1. brutalist 設計用很多自訂顏色與精確字級、用 Tailwind 反而要寫一堆 arbitrary value
  // 2. inline style 與 reference HTML 一比就能對齊、改 design tokens 直觀
  // 3. HeroMode 是相對隔離的 overlay、不影響 globals.css
  const glyphPal: GlyphPalette = {
    fg: ATLAS_PAL.ink, a1: ATLAS_PAL.forest, a2: ATLAS_PAL.brick, a3: ATLAS_PAL.sand, a4: ATLAS_PAL.dusk,
  }

  return (
    <div
      onClick={onOverlayClick}
      // Layer 1 — Backdrop:全螢幕半透明黑 + backdrop-blur,讓底下 canvas 模糊可見
      // (取代舊「整片米色蓋滿 viewport」設計、改回真正的 overlay 觀感)
      // 點 backdrop 不關 hero(避免誤關)— onOverlayClick 是 click sink
      className={`fixed inset-0 z-50 bg-slate-950/20 backdrop-blur-md flex items-center justify-center transition-opacity duration-300 ${containerOpacity}`}
      style={{ color: ATLAS_PAL.ink, fontFamily: FONT_BODY }}
    >
      {/* Layer 2 — Centered Hero Panel:
          - max-w-[1100px] w-[92vw] / max-h-[88vh]:不撐破 viewport、自然置中
          - 米色背景 + 32px 規則網格(設計風保留)
          - 圓角 0(brutalist)+ 1px 邊框 + shadow-2xl(深度感、像紙片浮著)
          - overflow-hidden + flex flex-col:內容超過 panel 不溢出、垂直排列 */}
      <div
        className={`relative w-[92vw] max-w-[1100px] max-h-[88vh] overflow-hidden flex flex-col rounded-none shadow-2xl shadow-black/30 transition-all duration-300 ${cardScale}`}
        style={{
          backgroundColor: ATLAS_PAL.bg,
          backgroundImage:
            `linear-gradient(${ATLAS_PAL.rule} 1px, transparent 1px),` +
            `linear-gradient(90deg, ${ATLAS_PAL.rule} 1px, transparent 1px)`,
          backgroundSize: '32px 32px',
          backgroundPosition: '-1px -1px',
          border: `1px solid ${ATLAS_PAL.rule}`,
          padding: '24px 36px',
        }}
        onClick={e => e.stopPropagation()}
      >
        {/* ================= Top bar ================= */}
        {/* 左:Atlas mark + wordmark;右:三個 stage chip (1·觸發 / 2·處理 / 3·輸出)
            外加最小化 / 關閉(取代舊 absolute 右上 icon)
            marginBottom 從 36 縮到 20、塞進 panel 上方 */}
        <div className="flex items-center justify-between shrink-0" style={{ marginBottom: 20, position: 'relative', zIndex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <AtlasMark size={32} />
            <span className="font-bold text-gray-900 text-base tracking-tight">Atlas</span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {['1·觸發', '2·處理', '3·輸出'].map((s, i) => (
              <div
                key={i}
                style={{
                  fontFamily: FONT_MONO, fontSize: 10.5, letterSpacing: '0.06em',
                  padding: '5px 10px', border: `1px solid ${ATLAS_PAL.rule}`,
                  background: ATLAS_PAL.bgCard, color: ATLAS_PAL.inkSoft,
                }}
              >{s}</div>
            ))}
            {/* 重新開始(清空對話):user 想開新對話時用、不關閉視窗 */}
            <button
              onClick={() => {
                if (heroMessages.length === 0) return
                if (!confirm('清空目前對話、重新開始?')) return
                setHeroMessages([])
              }}
              title="清空對話、重新開始"
              disabled={heroMessages.length === 0}
              style={{
                fontFamily: FONT_MONO, fontSize: 10.5, letterSpacing: '0.06em',
                padding: '5px 10px', border: `1px solid ${ATLAS_PAL.rule}`,
                background: ATLAS_PAL.bgCard, color: ATLAS_PAL.inkSoft,
                cursor: heroMessages.length === 0 ? 'not-allowed' : 'pointer',
                opacity: heroMessages.length === 0 ? 0.5 : 1,
                marginLeft: 4,
              }}
            >
              <RotateCcw className="w-3.5 h-3.5 inline-block" style={{ verticalAlign: 'middle' }} />
            </button>
            {/* 最小化 + 關閉:用 mono 文字而非 lucide icon、貼合 brutalist 風格 */}
            <button
              onClick={onMinimize}
              title="最小化到 sidebar(對話保留)"
              style={{
                fontFamily: FONT_MONO, fontSize: 10.5, letterSpacing: '0.06em',
                padding: '5px 10px', border: `1px solid ${ATLAS_PAL.rule}`,
                background: ATLAS_PAL.bgCard, color: ATLAS_PAL.inkSoft, cursor: 'pointer',
              }}
            >
              <Minus className="w-3.5 h-3.5 inline-block" style={{ verticalAlign: 'middle' }} />
            </button>
            <button
              onClick={() => exitToSidebar(false)}
              title="關閉(ESC)"
              style={{
                fontFamily: FONT_MONO, fontSize: 10.5, letterSpacing: '0.06em',
                padding: '5px 10px', border: `1px solid ${ATLAS_PAL.rule}`,
                background: ATLAS_PAL.bgCard, color: ATLAS_PAL.inkSoft, cursor: 'pointer',
              }}
            >
              <X className="w-3.5 h-3.5 inline-block" style={{ verticalAlign: 'middle' }} />
            </button>
          </div>
        </div>

        {/* ================= Initial state(!hasStarted):Hero + Cards grid + Input + Footer =================
            整個 !hasStarted 區塊用 flex flex-col flex-1 min-h-0、讓內容垂直排列、
            cards grid 可在 panel 內 scroll、input + footer 永遠停在 panel 底部 */}
        {!hasStarted && (
          <div className="flex flex-col flex-1 min-h-0">
            {/* Hero header:breadcrumb + h1(h1 從 56 縮到 42、給 input/cards 留空間)*/}
            <div className="shrink-0" style={{ marginBottom: 16, position: 'relative', zIndex: 1 }}>
              <div style={{
                display: 'flex', gap: 10, alignItems: 'center', marginBottom: 10,
                fontFamily: FONT_MONO, fontSize: 11, letterSpacing: '0.04em', color: ATLAS_PAL.inkSoft,
              }}>
                <span style={{ display: 'inline-block', width: 8, height: 8, background: ATLAS_PAL.forest }} />
                home / start a workflow
              </div>
              <h1 style={{
                fontFamily: FONT_DISPLAY, fontWeight: 500, fontSize: 38,
                lineHeight: 1.1, letterSpacing: '-0.03em', margin: 0, color: ATLAS_PAL.ink,
                whiteSpace: 'nowrap',
              }}>
                選一個方向開始,或<span style={{
                  color: ATLAS_PAL.forest,
                  textDecoration: 'underline',
                  textUnderlineOffset: 6,
                  textDecorationThickness: 2,
                }}>用一句話</span>描述你要的工作流。
              </h1>
            </div>

            {/* 4×2 cards grid(<1000px 小螢幕時自動降成 2 欄 4 列)
                用 min-h-0 + overflow-y-auto:cards 區若超過剩餘空間就 scroll、不擠到 input */}
            <div
              role="list"
              className="grid grid-cols-2 lg:grid-cols-4 flex-1 min-h-0 overflow-y-auto"
              style={{
                gap: 10,
                position: 'relative',
                zIndex: 1,
                marginBottom: 14,
                gridAutoRows: 'min-content',
              }}
            >
              {ATLAS_ACTIONS.map(a => {
                const isHover = hoveredCard === a.id
                return (
                  <button
                    key={a.id}
                    role="listitem"
                    aria-expanded={isHover}
                    onMouseEnter={() => setHoveredCard(a.id)}
                    onMouseLeave={() => setHoveredCard(null)}
                    onFocus={() => setHoveredCard(a.id)}
                    onBlur={() => setHoveredCard(null)}
                    onClick={() => onCardClick(a)}
                    style={{
                      background: ATLAS_PAL.bgCard,
                      border: `1px solid ${isHover ? ATLAS_PAL.ink : ATLAS_PAL.rule}`,
                      padding: 12, cursor: 'pointer',
                      transition: 'border-color 180ms, box-shadow 200ms, transform 200ms',
                      position: 'relative', minHeight: 160,
                      display: 'flex', flexDirection: 'column', gap: 10,
                      boxShadow: isHover ? `4px 4px 0 0 ${ATLAS_PAL.ink}` : 'none',
                      transform: isHover ? 'translate(-3px,-3px)' : 'translate(0,0)',
                      textAlign: 'left',
                      borderRadius: 0,
                      color: ATLAS_PAL.ink,
                      fontFamily: FONT_BODY,
                    }}
                  >
                    {/* Top row:glyph 左、tag 右 */}
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                      <ActionGlyph id={a.id} size={28} palette={glyphPal} />
                      <div style={{
                        fontFamily: FONT_MONO, fontSize: 9.5, letterSpacing: '0.06em',
                        color: ATLAS_PAL.inkSoft, padding: '2px 6px',
                        border: `1px solid ${ATLAS_PAL.rule}`,
                      }}>{a.tag}</div>
                    </div>

                    {/* Body — 卡片固定大小不變(不跳動);hover 時範例區在卡片「內部」捲動,
                        滑鼠移到下方被遮的範例項會自動 scrollIntoView 捲上來,不撐大卡片。 */}
                    <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
                      {/* Default layer:title + desc */}
                      <div style={{
                        opacity: isHover ? 0 : 1,
                        transition: 'opacity 160ms',
                        position: 'absolute', inset: 0,
                        pointerEvents: isHover ? 'none' : 'auto',
                      }}>
                        <div style={{
                          fontFamily: FONT_DISPLAY, fontWeight: 600, fontSize: 17,
                          lineHeight: 1.2, letterSpacing: '-0.01em', marginBottom: 6,
                        }}>{a.title}</div>
                        <div style={{
                          fontSize: 12.5, color: ATLAS_PAL.inkSoft, lineHeight: 1.45,
                        }}>{a.desc}</div>
                      </div>
                      {/* Hover layer:title↗(固定) + 可內部捲動的範例區 */}
                      <div style={{
                        opacity: isHover ? 1 : 0,
                        transition: 'opacity 200ms 40ms',
                        position: 'absolute', inset: 0,
                        display: 'flex', flexDirection: 'column', gap: 4,
                        pointerEvents: isHover ? 'auto' : 'none',
                      }}>
                        <div style={{
                          fontFamily: FONT_DISPLAY, fontWeight: 600, fontSize: 13.5,
                          lineHeight: 1.1, marginBottom: 2, flexShrink: 0,
                        }}>{a.title} ↗</div>
                        <AutoScrollExamples examples={a.examples} onPick={(k) => onCardClick(a, k)} />
                      </div>
                    </div>
                  </button>
                )
              })}
            </div>

            {/* OR / 描述一句 divider */}
            <div style={{ position: 'relative', zIndex: 1 }}>
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
                <div style={{ height: 1, background: ATLAS_PAL.ink, flex: 1 }} />
                <div style={{
                  fontFamily: FONT_MONO, fontSize: 10.5, letterSpacing: '0.14em',
                  color: ATLAS_PAL.inkSoft, padding: '0 12px', textTransform: 'uppercase',
                }}>OR / 描述一句</div>
                <div style={{ height: 1, background: ATLAS_PAL.ink, flex: 1 }} />
              </div>

              {/* Input row(brutalist:純黑邊框、無圓角)*/}
              <div style={{
                background: ATLAS_PAL.bgCard, border: `1px solid ${ATLAS_PAL.ink}`,
                display: 'flex', alignItems: 'flex-start', padding: '14px 16px', gap: 14,
              }}>
                <span style={{ fontFamily: FONT_MONO, fontSize: 12, color: ATLAS_PAL.forest, paddingTop: 3 }}>{'>'}</span>
                <textarea
                  ref={inputRef}
                  value={heroInput}
                  onChange={e => setHeroInput(e.target.value)}
                  onKeyDown={onKeyDown}
                  disabled={heroLoading}
                  rows={1}
                  placeholder="每天早上 9 點抓 PTT 科技業板熱門 → AI 摘要 → Telegram 通知(Enter 送出 / Shift+Enter 換行)"
                  style={{
                    flex: 1, border: 'none', outline: 'none', background: 'transparent',
                    fontFamily: FONT_MONO, fontSize: 13.5, color: ATLAS_PAL.ink, resize: 'none',
                    lineHeight: 1.45,
                  }}
                />
                <button
                  onClick={onSubmit}
                  disabled={!heroInput.trim() || heroLoading}
                  style={{
                    background: heroInput.trim() && !heroLoading ? ATLAS_PAL.ink : ATLAS_PAL.inkSoft,
                    color: ATLAS_PAL.bg, padding: '8px 16px',
                    fontFamily: FONT_MONO, fontSize: 11.5, letterSpacing: '0.04em',
                    cursor: heroInput.trim() && !heroLoading ? 'pointer' : 'not-allowed',
                    border: 'none', borderRadius: 0,
                    display: 'inline-flex', alignItems: 'center', gap: 6,
                  }}
                >
                  {heroLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                  run ↵
                </button>
              </div>
            </div>

            {/* Footer hints(panel 內底部、shrink-0 不被擠掉)*/}
            <div className="shrink-0" style={{
              paddingTop: 14,
              display: 'flex', justifyContent: 'space-between',
              fontFamily: FONT_MONO, fontSize: 10.5, letterSpacing: '0.06em',
              color: ATLAS_PAL.inkSoft, position: 'relative', zIndex: 1,
            }}>
              <div style={{ display: 'flex', gap: 20 }}>
                <button
                  onClick={onNewCanvas}
                  style={{ fontFamily: FONT_MONO, fontSize: 10.5, letterSpacing: '0.06em', color: ATLAS_PAL.inkSoft, background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
                >[+] new canvas</button>
                <button
                  onClick={onImportPy}
                  style={{ fontFamily: FONT_MONO, fontSize: 10.5, letterSpacing: '0.06em', color: ATLAS_PAL.inkSoft, background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
                >[⌥] import .py</button>
                <button
                  onClick={onCmdK}
                  style={{ fontFamily: FONT_MONO, fontSize: 10.5, letterSpacing: '0.06em', color: ATLAS_PAL.inkSoft, background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
                >[⌘K] command</button>
              </div>
              <span>esc · skip</span>
            </div>
          </div>
        )}

        {/* ================= Chat mode(hasStarted=true):chat history + input + footer ================= */}
        {/* 進入對話後、卡片區收起、改顯示 chat scroll;視覺保持 brutalist 風格(白卡 + 黑邊)
            外層 flex flex-col flex-1 min-h-0:讓 chat scroll area 真正能 flex-1、input 永遠在底 */}
        {hasStarted && (
          <div className="flex flex-col flex-1 min-h-0">
            {/* Breadcrumb-style header(對話模式縮小)*/}
            <div className="shrink-0" style={{
              display: 'flex', gap: 10, alignItems: 'center', marginBottom: 12,
              fontFamily: FONT_MONO, fontSize: 11, letterSpacing: '0.04em', color: ATLAS_PAL.inkSoft,
              position: 'relative', zIndex: 1,
            }}>
              <span style={{ display: 'inline-block', width: 8, height: 8, background: ATLAS_PAL.forest }} />
              home / talking to atlas
            </div>

            {/* Chat history scroll area */}
            <div
              className="flex-1 min-h-0 overflow-y-auto pr-1 space-y-3 [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:bg-black/10 [&::-webkit-scrollbar-thumb]:rounded-full"
              style={{ position: 'relative', zIndex: 1 }}
            >
              {heroMessages.map((msg, i) => (
                <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  {msg.role === 'assistant' && (
                    <div
                      className="shrink-0 mt-1 mr-2 flex items-center justify-center"
                      style={{
                        width: 22, height: 22,
                        background: ATLAS_PAL.bgCard,
                        border: `1px solid ${ATLAS_PAL.rule}`,
                      }}
                    >
                      <span style={{
                        display: 'inline-block', width: 8, height: 8, background: ATLAS_PAL.forest,
                      }} />
                    </div>
                  )}
                  <div
                    className="max-w-[85%] min-w-0 break-words overflow-hidden"
                    style={{
                      background: msg.role === 'user' ? ATLAS_PAL.ink : ATLAS_PAL.bgCard,
                      color: msg.role === 'user' ? ATLAS_PAL.bg : ATLAS_PAL.ink,
                      border: msg.role === 'user' ? 'none' : `1px solid ${ATLAS_PAL.rule}`,
                      padding: '10px 14px',
                      fontFamily: FONT_BODY,
                      fontSize: 13,
                      lineHeight: 1.5,
                      borderRadius: 0,
                      overflowWrap: 'anywhere', wordBreak: 'break-word',
                    }}
                  >
                    {msg.role === 'assistant' && msg.toolBlocks && msg.toolBlocks.length > 0 && (
                      <div className="mb-2 space-y-1">
                        {msg.toolBlocks.map((tb, ti) => (
                          <div
                            key={ti}
                            style={{
                              fontFamily: FONT_MONO, fontSize: 11,
                              padding: '4px 8px',
                              border: `1px solid ${tb.status === 'running' ? ATLAS_PAL.forest : ATLAS_PAL.rule}`,
                              background: tb.status === 'running' ? '#EEF2EC' : '#F7F6F2',
                              color: ATLAS_PAL.ink,
                            }}
                          >
                            {tb.status === 'running' ? (
                              <span className="flex items-center gap-1.5">
                                <Loader2 className="w-3 h-3 animate-spin shrink-0" />
                                <span>{tb.name}</span>
                                <span style={{ fontSize: 10, color: ATLAS_PAL.inkSoft }} className="truncate">
                                  {Object.entries(tb.args).slice(0, 2).map(([k, v]) =>
                                    `${k}=${typeof v === 'string' ? `"${v.slice(0, 30)}"` : JSON.stringify(v).slice(0, 30)}`
                                  ).join(', ')}
                                </span>
                              </span>
                            ) : (
                              <span className="flex items-center gap-1.5">
                                <span style={{ color: ATLAS_PAL.forest, flexShrink: 0 }}>+</span>
                                <span>{tb.name}</span>
                                {tb.preview && (
                                  <span style={{ fontSize: 10, color: ATLAS_PAL.inkSoft }} className="truncate" title={tb.preview}>
                                    {tb.preview.slice(0, 60)}
                                  </span>
                                )}
                              </span>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                    {msg.setupGuide ? (
                      <ModelSetupGuide code={msg.setupGuide.code} detail={msg.setupGuide.detail}
                        onRetry={() => heroRetryFromGuide(i)} />
                    ) : msg.role === 'assistant' ? (
                      <div className="prose prose-sm max-w-none prose-p:my-1 prose-pre:text-xs prose-pre:whitespace-pre-wrap prose-code:break-all">
                        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]}>{cleanLatexInChat(msg.content.replace(/YAML_READY\n/g, ''))}</ReactMarkdown>
                        {msg.streaming && (
                          <span
                            className="inline-block align-middle ml-0.5 animate-pulse"
                            style={{ width: 6, height: 12, background: ATLAS_PAL.forest }}
                          />
                        )}
                      </div>
                    ) : (
                      <span className="whitespace-pre-wrap">{msg.content}</span>
                    )}
                    {msg.hasYaml && msg.yamlError && (
                      <div
                        className="mt-2"
                        style={{
                          padding: 8,
                          border: `1px solid ${ATLAS_PAL.brick}`,
                          background: '#FBEFE7',
                          color: ATLAS_PAL.brick,
                          fontSize: 11,
                          lineHeight: 1.5,
                        }}
                      >
                        YAML 有問題,建議請 AI 修正後再套用:<br />
                        <code className="break-all">{msg.yamlError}</code>
                      </div>
                    )}
                    {msg.hasYaml && msg.yaml && (
                      // Hero 是新對話入口、永遠建新工作流、不該有「覆蓋」(沒目前 workflow 可覆蓋)
                      // overwrite 按鈕只放 sidebar mode 內、Hero 移除
                      <div className="mt-2">
                        <button
                          onClick={() => handleYamlApplyInHero(msg.yaml!, 'new')}
                          disabled={!!msg.yamlError}
                          title={msg.yamlError
                            ? 'YAML 有解析錯誤、無法套用、請請 AI 重新產出完整 YAML'
                            : '建立一個新的工作流來放這份 YAML'}
                          className="w-full"
                          style={{
                            background: msg.yamlError ? '#D5D2CC' : ATLAS_PAL.forest,
                            color: msg.yamlError ? '#8B8680' : ATLAS_PAL.bg,
                            fontFamily: FONT_MONO, fontSize: 11, letterSpacing: '0.04em',
                            padding: '10px 12px',
                            cursor: msg.yamlError ? 'not-allowed' : 'pointer',
                            borderRadius: 0, border: 'none',
                          }}
                        >+ new workflow</button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
              {heroLoading && (
                <div
                  className="flex items-center gap-2 pl-8"
                  style={{ fontFamily: FONT_MONO, fontSize: 11, color: ATLAS_PAL.inkSoft }}
                >
                  <Loader2 className="w-3 h-3 animate-spin" /> thinking…
                </div>
              )}
              <div ref={chatEndRef} />
            </div>

            {/* Chat-mode input(同 brutalist 樣式、shrink-0 永遠停在 panel 底部)*/}
            <div className="shrink-0" style={{
              background: ATLAS_PAL.bgCard, border: `1px solid ${ATLAS_PAL.ink}`,
              display: 'flex', alignItems: 'flex-start', padding: '12px 14px', gap: 12,
              marginTop: 12, position: 'relative', zIndex: 1,
            }}>
              <span style={{ fontFamily: FONT_MONO, fontSize: 12, color: ATLAS_PAL.forest, paddingTop: 2 }}>{'>'}</span>
              <textarea
                ref={textareaRef}
                value={heroInput}
                onChange={e => setHeroInput(e.target.value)}
                onKeyDown={onKeyDown}
                disabled={heroLoading}
                rows={2}
                placeholder="繼續對話…(Enter 送出 / Shift+Enter 換行)"
                style={{
                  flex: 1, border: 'none', outline: 'none', background: 'transparent',
                  fontFamily: FONT_MONO, fontSize: 13, color: ATLAS_PAL.ink, resize: 'none',
                  lineHeight: 1.45,
                }}
              />
              <button
                onClick={onSubmit}
                disabled={!heroInput.trim() || heroLoading}
                style={{
                  background: heroInput.trim() && !heroLoading ? ATLAS_PAL.ink : ATLAS_PAL.inkSoft,
                  color: ATLAS_PAL.bg, padding: '8px 16px',
                  fontFamily: FONT_MONO, fontSize: 11.5, letterSpacing: '0.04em',
                  cursor: heroInput.trim() && !heroLoading ? 'pointer' : 'not-allowed',
                  border: 'none', borderRadius: 0,
                  display: 'inline-flex', alignItems: 'center', gap: 6,
                }}
              >
                {heroLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                run ↵
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
