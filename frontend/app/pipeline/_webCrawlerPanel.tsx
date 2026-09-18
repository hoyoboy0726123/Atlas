'use client'
import { useState } from 'react'
import { X, Plus, Trash2, GripVertical, Info, ChevronDown, ChevronRight, Globe, Film } from 'lucide-react'
import type { WebCrawlerData, WebCrawlerNode, WebCrawlerAction } from './_helpers'
import { VariableButton } from './_variablePicker'

const NODE_COLOR = '#0d9488'

interface Props {
  node: WebCrawlerNode
  pipelineName: string
  onUpdate: (data: Partial<WebCrawlerData>) => void
  onClose: () => void
  onDelete: () => void
  workflowId?: string
}

const ACTION_LABEL: Record<WebCrawlerAction['type'], string> = {
  click: '🖱 點擊選擇器',
  scroll: '↕ 滾動',
  wait: '⏳ 等待秒數',
  wait_for: '👀 等選擇器出現',
  type: '⌨ 輸入文字',
}

export default function WebCrawlerPanel({ node, onUpdate, onClose, onDelete, workflowId }: Props) {
  const data = node.data
  const inputCls = 'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-teal-500 focus:ring-1 focus:ring-teal-500/20 bg-white'

  const [newType, setNewType] = useState<WebCrawlerAction['type']>('click')
  // 進階設定預設收起；裡面是「不懂的人別動、懂的人才會動」的東西
  const [showAdvanced, setShowAdvanced] = useState(false)
  // Cookies 教學步驟（panel 空間有限、預設收起）
  const [showCookieHowto, setShowCookieHowto] = useState(false)

  const updateInteractions = (next: WebCrawlerAction[]) => onUpdate({ interactions: next })

  const addInteraction = () => {
    const tpl: Record<WebCrawlerAction['type'], WebCrawlerAction> = {
      click: { type: 'click', selector: '' },
      scroll: { type: 'scroll', to: 'bottom' },
      wait: { type: 'wait', seconds: 1 },
      wait_for: { type: 'wait_for', selector: '' },
      type: { type: 'type', selector: '', text: '' },
    }
    updateInteractions([...(data.interactions || []), { ...tpl[newType] }])
  }

  const updateAction = (idx: number, patch: Partial<WebCrawlerAction>) => {
    const next = [...(data.interactions || [])]
    next[idx] = { ...next[idx], ...patch }
    updateInteractions(next)
  }

  const removeAction = (idx: number) => {
    const next = [...(data.interactions || [])]
    next.splice(idx, 1)
    updateInteractions(next)
  }

  const moveAction = (idx: number, dir: -1 | 1) => {
    const next = [...(data.interactions || [])]
    const j = idx + dir
    if (j < 0 || j >= next.length) return
    ;[next[idx], next[j]] = [next[j], next[idx]]
    updateInteractions(next)
  }

  const isVideo = data.mode === 'video'

  return (
    <div className="absolute top-0 right-0 h-full w-[420px] bg-white shadow-2xl border-l border-gray-100 flex flex-col z-30 overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b" style={{ borderTopColor: NODE_COLOR, borderTopWidth: 3 }}>
        <span className="w-8 h-8 rounded-full flex items-center justify-center text-white shrink-0"
          style={{ background: NODE_COLOR }}>
          {isVideo
            ? <Film className="w-4 h-4" strokeWidth={2.4} />
            : <Globe className="w-4 h-4" strokeWidth={2.4} />}
        </span>
        <div className="flex-1 min-w-0">
          <span className="font-semibold text-gray-800 text-sm block truncate">
            {isVideo ? '影片下載節點' : '網頁爬蟲節點'}
          </span>
          <span className="text-xs text-gray-400">
            {isVideo
              ? '輸出 mp4 + 字幕 + .info.json + 摘要 .md，可接 skill 節點分析'
              : '輸出 markdown + frontmatter，可接 skill 節點分析'}
          </span>
        </div>
        <button onClick={onDelete} title="刪除" className="text-gray-300 hover:text-red-400 transition-colors p-1">🗑</button>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors"><X className="w-4 h-4" /></button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* ── 模式切換（全 panel 第一格、最重要） ────────────────── */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">抓取類型</label>
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => onUpdate({ mode: 'web' })}
              className={`px-3 py-2.5 rounded-lg border text-sm font-medium transition-colors ${
                !isVideo
                  ? 'border-teal-500 bg-teal-50 text-teal-700 ring-1 ring-teal-500/30'
                  : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300'
              }`}
            >
              🌐 抓網頁<br />
              <span className="text-[10px] font-normal opacity-80">Crawl4AI → markdown</span>
            </button>
            <button
              type="button"
              onClick={() => onUpdate({ mode: 'video' })}
              className={`px-3 py-2.5 rounded-lg border text-sm font-medium transition-colors ${
                isVideo
                  ? 'border-rose-500 bg-rose-50 text-rose-700 ring-1 ring-rose-500/30'
                  : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300'
              }`}
            >
              🎬 抓影片<br />
              <span className="text-[10px] font-normal opacity-80">yt-dlp → mp4 + 字幕</span>
            </button>
          </div>
        </div>

        {/* 模式說明 */}
        {!isVideo ? (
          <div className="p-3 rounded-lg border border-teal-200 bg-teal-50/50 space-y-1.5">
            <p className="text-xs text-teal-900 font-medium">執行流程：</p>
            <p className="text-[11px] text-teal-800/90 leading-relaxed">
              <span className="inline-block w-2 h-2 rounded-full bg-emerald-500 mr-1.5 align-middle" />
              <b>Tier 1</b>：沙盒內 Crawl4AI（Playwright + Chromium）抓取，95% 網站 OK
            </p>
            <p className="text-[11px] text-teal-800/90 leading-relaxed">
              <span className="inline-block w-2 h-2 rounded-full bg-sky-500 mr-1.5 align-middle" />
              <b>SPA 智慧重試</b>：第一輪內容偏少（疑似 React/Vue SPA 沒 hydrate 完）→ 自動套通用 selector + 滾動再抓一次
            </p>
            <p className="text-[11px] text-teal-800/90 leading-relaxed">
              <span className="inline-block w-2 h-2 rounded-full bg-amber-500 mr-1.5 align-middle" />
              <b>Tier 2</b>（CF 偵測到才走）：FlareSolverr 解 Cloudflare → 回 HTML → 轉 Markdown
            </p>
          </div>
        ) : (
          <div className="p-3 rounded-lg border border-rose-200 bg-rose-50/50 space-y-1.5">
            <p className="text-xs text-rose-900 font-medium">執行流程：</p>
            <p className="text-[11px] text-rose-800/90 leading-relaxed">
              沙盒內 <code className="font-mono bg-rose-100 px-1 rounded">yt-dlp</code> + <code className="font-mono bg-rose-100 px-1 rounded">ffmpeg</code> →
              下載 mp4 / 字幕 / metadata。支援 <b>YouTube / Vimeo / Bilibili / TikTok / Twitter</b> 等 1700+ 站。
            </p>
            <p className="text-[10px] text-rose-700/80 leading-relaxed">
              ⚠ 影片過長 / 過大會自動跳過（避免吃光硬碟）；下載前看 metadata 自動 reject 不符條件的影片。
            </p>
          </div>
        )}

        {/* 節點名稱（共用）*/}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">節點名稱</label>
          <input value={data.name} onChange={e => onUpdate({ name: e.target.value })} className={`${inputCls} font-mono`} />
        </div>

        {/* ============================================================ */}
        {/* 網頁模式專屬區塊                                             */}
        {/* ============================================================ */}
        {!isVideo && (() => {
          // URLs 來源：優先用 urls 陣列（保留 raw 排版含空行）、向後相容單欄位 url
          const urlsText = (data.urls && data.urls.length > 0)
            ? data.urls.join('\n')
            : (data.url || '')
          // 顯示用的「有效 URL」count（去空行 / 去 # 註解）
          const validUrls = urlsText
            .split('\n')
            .map(s => s.trim())
            .filter(s => s && !s.startsWith('#'))
          const isMulti = validUrls.length > 1
          const handleChange = (raw: string) => {
            // 把整段 raw text（含空行 / 註解）存進 urls 陣列保留排版；
            // 否則 React rerender 會吃掉使用者剛按下的 Enter（換行字元 round-trip 遺失）
            // backend / 驗證時才 filter
            if (raw === '') {
              onUpdate({ url: '', urls: [] })
            } else {
              onUpdate({ url: '', urls: raw.split('\n') })
            }
          }
          return (
            <div>
              <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
                目標 URL
                <span className="ml-2 text-[10px] font-normal text-gray-400">
                  {validUrls.length === 0 ? '一行一個 URL' : isMulti ? `${validUrls.length} 個 URL（多檔模式）` : '1 個 URL'}
                </span>
              </label>
              <textarea
                className={`${inputCls} font-mono text-xs resize-y min-h-[60px]`}
                rows={isMulti ? 7 : 4}
                placeholder={'https://example.com/article/{{ input.slug }}\n# 多 URL 一行一個(# 開頭視為註解、跳過)\nhttps://another-site.com/page'}
                value={urlsText}
                onChange={e => handleChange(e.target.value)}
                onWheel={(e) => {
                  const el = e.currentTarget
                  const atTop = el.scrollTop === 0
                  const atBot = el.scrollTop + el.clientHeight >= el.scrollHeight - 1
                  if ((!atTop && e.deltaY < 0) || (!atBot && e.deltaY > 0)) e.stopPropagation()
                }}
              />
              <div className="flex justify-end mt-1">
                <VariableButton
                  workflowId={workflowId}
                  onPick={(p) => handleChange(`${urlsText}{{ ${p} }}`)}
                />
              </div>
              <p className="text-[10px] text-gray-400 mt-0.5">
                {isMulti ? (
                  <>
                    📁 多 URL 模式：輸出檔路徑會被視為<b>資料夾</b>，每個 URL 一個 .md
                    + 一份 <code className="font-mono bg-gray-100 px-1 rounded">index.json</code> manifest。
                    檔名從 URL 衍生（不撞名、可預測）。
                  </>
                ) : (
                  <>💡 不用調設定就能跑。Dcard / Reddit / Medium 等動態載入的站，<b>SPA 智慧重試</b>會自動處理。多 URL 一行貼一個。</>
                )}
              </p>
            </div>
          )
        })()}

        {/* ============================================================ */}
        {/* 影片模式專屬區塊                                             */}
        {/* ============================================================ */}
        {isVideo && (
          <>
            {/* 影片 URL */}
            <div>
              <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">YouTube / Vimeo / Bilibili URL</label>
              <input
                className={`${inputCls} font-mono`}
                placeholder="https://www.youtube.com/watch?v=... 或 https://youtu.be/..."
                value={data.videoUrl}
                onChange={e => onUpdate({ videoUrl: e.target.value })}
              />
              <div className="flex justify-end mt-1">
                <VariableButton
                  workflowId={workflowId}
                  onPick={(p) => onUpdate({ videoUrl: `${data.videoUrl || ''}{{ ${p} }}` })}
                />
              </div>
              <p className="text-[10px] text-gray-400 mt-0.5">
                <Info className="w-3 h-3 inline align-text-top mr-0.5" />
                yt-dlp 支援的所有站都可以(playlist 只抓第一個影片)
              </p>
            </div>

            {/* 影片設定 */}
            <div className="p-3 rounded-lg border border-gray-200 bg-gray-50/40 space-y-2.5">
              <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide">影片設定</p>

              {/* 解析度 */}
              <div>
                <label className="text-xs text-gray-600 block mb-1">解析度上限</label>
                <select
                  className={inputCls}
                  value={data.videoQuality}
                  onChange={e => onUpdate({ videoQuality: e.target.value })}
                >
                  <option value="best">最佳（不限）</option>
                  <option value="1080p">1080p</option>
                  <option value="720p">720p（推薦、省空間）</option>
                  <option value="480p">480p</option>
                  <option value="360p">360p</option>
                </select>
              </div>

              {/* 大小 / 長度上限 */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-600 block mb-1">檔案大小上限</label>
                  <div className="flex items-center gap-1.5">
                    <input
                      type="number" min={50} max={5000} step={50}
                      className={inputCls}
                      value={data.videoMaxFilesizeMb}
                      onChange={e => onUpdate({ videoMaxFilesizeMb: Number(e.target.value) || 500 })}
                    />
                    <span className="text-xs text-gray-500 shrink-0">MB</span>
                  </div>
                </div>
                <div>
                  <label className="text-xs text-gray-600 block mb-1">長度上限</label>
                  <div className="flex items-center gap-1.5">
                    <input
                      type="number" min={0} max={1440}
                      className={inputCls}
                      value={data.videoMaxDurationMin}
                      onChange={e => onUpdate({ videoMaxDurationMin: Number(e.target.value) || 0 })}
                    />
                    <span className="text-xs text-gray-500 shrink-0">分</span>
                  </div>
                  <p className="text-[10px] text-gray-400 mt-0.5">0 = 不限</p>
                </div>
              </div>

              {/* 字幕 */}
              <div className="border-t border-gray-200 pt-2.5 space-y-1.5">
                <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                  <input type="checkbox" checked={data.videoSubs}
                         onChange={e => onUpdate({ videoSubs: e.target.checked })} />
                  <span>下載字幕（如果有的話；含 YouTube auto-generated 字幕）</span>
                </label>
                {data.videoSubs && (
                  <div>
                    <label className="text-[11px] text-gray-600 block mb-1">字幕語言偏好（逗號分隔，依序找）</label>
                    <input
                      className={inputCls}
                      placeholder="預設：zh-TW,zh-Hant,zh-CN,zh-Hans,en"
                      value={data.videoSubsLangs}
                      onChange={e => onUpdate({ videoSubsLangs: e.target.value })}
                    />
                    <p className="text-[10px] text-gray-400 mt-0.5">
                      留空用預設（繁中→簡中→英文）；輸出格式統一為 .srt 給後續 skill 節點直接讀
                    </p>
                  </div>
                )}
              </div>

            </div>
          </>
        )}

        {/* ============================================================ */}
        {/* 進階設定（預設收起；含教學提示）                             */}
        {/* ============================================================ */}
        <div className="border border-gray-200 rounded-lg overflow-hidden">
          <button
            type="button"
            onClick={() => setShowAdvanced(s => !s)}
            className="w-full flex items-center gap-2 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 transition-colors"
          >
            {showAdvanced ? <ChevronDown className="w-4 h-4 shrink-0" /> : <ChevronRight className="w-4 h-4 shrink-0" />}
            進階設定
            <span className="text-[10px] font-normal text-gray-400 ml-auto truncate">
              {isVideo ? 'Cookies / metadata JSON' : 'JS 渲染 / 等待選擇器 / 互動 / Cookies / 附件 / CF'}
            </span>
          </button>
          {showAdvanced && (
            <div className="border-t border-gray-200 p-3 space-y-3 bg-gray-50/40">
              {/* 教學提示 */}
              <div className="text-[11px] text-gray-700 leading-relaxed bg-blue-50 border border-blue-200 rounded p-2.5">
                💡 <b>大部分網站不需要動這些</b>。<b>SPA 智慧重試</b>已自動處理 Dcard / Reddit / Medium 等動態載入的站。
                只有遇到下面情況才需要進來：
                <ul className="list-disc list-inside mt-1 space-y-0.5 text-gray-600">
                  <li>要登入才看得到的內容（→ Cookies）</li>
                  <li>自動重試還是抓不到、想自己指定 selector 跟互動</li>
                  <li>純靜態站想關 JS 渲染省時間</li>
                  <li>{isVideo ? '想拿章節 / 縮圖等完整 metadata' : '想連同附件下載到本機'}</li>
                </ul>
              </div>

              {/* === 網頁模式進階 === */}
              {!isVideo && (
                <>
                  {/* JS 渲染 toggle */}
                  <div className="bg-white border border-gray-200 rounded-lg p-2.5 space-y-1">
                    <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                      <input type="checkbox" checked={data.jsRender} onChange={e => onUpdate({ jsRender: e.target.checked })} />
                      <span className="font-medium">啟用 JS 渲染</span>
                    </label>
                    <p className="text-[11px] text-gray-500 leading-relaxed pl-6">
                      開：跑 React/Vue/Angular 的 SPA。<b>關</b>：只下載原始 HTML、不跑 JS（速度快 5-10 倍，但 SPA 內容會抓不到）。
                      不確定就保持<b>開</b>。
                    </p>
                  </div>

                  {/* CF fallback toggle */}
                  <div className="bg-white border border-gray-200 rounded-lg p-2.5 space-y-1">
                    <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                      <input type="checkbox" checked={data.cloudflareFallback}
                             onChange={e => onUpdate({ cloudflareFallback: e.target.checked })} />
                      <span className="font-medium">Cloudflare 自動 fallback</span>
                    </label>
                    <p className="text-[11px] text-gray-500 leading-relaxed pl-6">
                      第一輪被 CF 擋（403 / 5xx 或挑戰頁面）→ 自動轉送 FlareSolverr 解。沒影響到一般站、保持<b>開</b>就好。
                    </p>
                  </div>

                  {/* 等待選擇器 */}
                  <div className="bg-white border border-gray-200 rounded-lg p-2.5 space-y-1">
                    <label className="text-sm font-medium text-gray-700 block">等待選擇器（CSS selector）</label>
                    <input
                      className={inputCls}
                      placeholder="留空 = 自動偵測；填了 = 直接照你給的"
                      value={data.waitForSelector}
                      onChange={e => onUpdate({ waitForSelector: e.target.value })}
                    />
                    <p className="text-[11px] text-gray-500 leading-relaxed">
                      指定 selector 出現後才開始抓；對 SPA 重要。<b>留空</b>會啟用智慧重試自動套常見模式
                      (<code className="font-mono text-[10px] bg-gray-100 px-1 rounded">a[href*=&quot;/p/&quot;]</code> /
                      <code className="font-mono text-[10px] bg-gray-100 px-1 rounded">a[href*=&quot;/comments/&quot;]</code> 等 11 種)。
                      <br />
                      填具體值範例：<code className="font-mono text-[10px] bg-gray-100 px-1 rounded">.article-content</code> /
                      <code className="font-mono text-[10px] bg-gray-100 px-1 rounded">#main</code> /
                      <code className="font-mono text-[10px] bg-gray-100 px-1 rounded">[data-loaded=true]</code>
                    </p>
                  </div>

                  {/* 下載附件 */}
                  <div className="bg-white border border-gray-200 rounded-lg p-2.5 space-y-1">
                    <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                      <input type="checkbox" checked={data.downloadAssets}
                             onChange={e => onUpdate({ downloadAssets: e.target.checked })} />
                      <span className="font-medium">下載附件到 assets/</span>
                    </label>
                    <p className="text-[11px] text-gray-500 leading-relaxed pl-6">
                      勾起來會把 markdown 裡圖片連結 + PDF / Office / 壓縮檔下載到 <code className="font-mono text-[10px] bg-gray-100 px-1 rounded">{'<output>'}/assets/</code>，
                      並把 markdown 裡的 URL 換成本機相對路徑。<b>不勾</b>：URL 留在 markdown、之後 skill 節點要自己抓。
                    </p>
                  </div>

                  {/* 智慧滾動 — 預設「達到貼文數 10」，避免拖太久或燒太多下游 token */}
                  <div className="bg-white border border-gray-200 rounded-lg p-2.5 space-y-2">
                    <label className="text-sm font-medium text-gray-700 block">智慧滾動（infinite scroll）</label>
                    <p className="text-[11px] text-gray-500 leading-relaxed">
                      <b>預設</b>：「達到 10 篇貼文就停」（適合多數「抓清單做摘要」場景；下游 LLM 處理量也合理）。
                      要調整就用下面兩格：
                      <br />
                      <b>固定次數</b>：強制滾 N 次後停（夾到 10 次上限）；不論貼文多寡。
                      <br />
                      <b>達到貼文數</b>：偵測貼文連結（Reddit <code className="font-mono text-[10px] bg-gray-100 px-1 rounded">/comments/</code> / Dcard <code className="font-mono text-[10px] bg-gray-100 px-1 rounded">/p/</code> / 新聞 <code className="font-mono text-[10px] bg-gray-100 px-1 rounded">/article/</code> 等）達標就停 — <b>不設輪數上限</b>，受底層 110 秒 deadline 約束。要撈幾百篇就填幾百。
                      <br />
                      兩格都清空（都 0）→ 退回「滾 2 次」基本模式。兩格同時填：<b>固定次數優先</b>。已填了下方「JS 互動序列」時這兩格會被忽略。
                    </p>
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label className="text-xs text-gray-600 block mb-1">固定滾動次數（上限 10）</label>
                        <input
                          type="number" min={0} max={10}
                          className={inputCls}
                          // value 為 0 時顯示空字串、placeholder 提示 — 讓使用者可以清空後直接打字
                          // 否則 React controlled input 會強制值為 "0"、清不掉
                          value={data.scrollCount === 0 ? '' : data.scrollCount}
                          placeholder="0 = 自動"
                          onChange={e => {
                            const raw = e.target.value
                            const v = raw === '' ? 0 : parseInt(raw)
                            if (isNaN(v)) return
                            onUpdate({ scrollCount: Math.max(0, Math.min(10, v)) })
                          }}
                        />
                      </div>
                      <div>
                        <label className="text-xs text-gray-600 block mb-1">達到貼文數就停</label>
                        <input
                          type="number" min={0}
                          className={inputCls}
                          value={data.targetPostCount === 0 ? '' : data.targetPostCount}
                          placeholder="0 = 不設目標"
                          onChange={e => {
                            const raw = e.target.value
                            const v = raw === '' ? 0 : parseInt(raw)
                            if (isNaN(v)) return
                            onUpdate({ targetPostCount: Math.max(0, v) })
                          }}
                        />
                      </div>
                    </div>
                  </div>

                  {/* 論壇 / 列表模式（with_children）：自動抓子頁、合併單一 markdown */}
                  <div className="bg-white border border-gray-200 rounded-lg p-2.5 space-y-2">
                    <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                      <input type="checkbox" checked={!!data.withChildren}
                             onChange={e => onUpdate({ withChildren: e.target.checked })} />
                      <span className="font-medium">同時抓討論串子頁（論壇 / 列表模式）</span>
                    </label>
                    <p className="text-[11px] text-gray-500 leading-relaxed pl-6">
                      開啟後：抓列表頁 → 自動抽前 N 個子頁連結 → 並行抓子頁 → <b>合併成單一 markdown</b>。
                      下游 skill 節點直接讀此檔做摘要、不用自己寫爬蟲。適合 Reddit / PTT / Dcard / 新聞站等「列表 → 詳細頁」結構。
                    </p>
                    {data.withChildren && (
                      <div className="grid grid-cols-2 gap-2 pl-6">
                        <div>
                          <label className="text-xs text-gray-600 block mb-1">最多抓幾個子頁</label>
                          <input
                            type="number" min={1} max={50}
                            className={inputCls}
                            value={data.maxChildren ?? 10}
                            placeholder="10"
                            onChange={e => {
                              const raw = e.target.value
                              const v = raw === '' ? 10 : parseInt(raw)
                              if (isNaN(v)) return
                              onUpdate({ maxChildren: Math.max(1, Math.min(50, v)) })
                            }}
                          />
                        </div>
                        <div>
                          <label className="text-xs text-gray-600 block mb-1">子頁 URL pattern（空=自動）</label>
                          <input
                            type="text"
                            className={inputCls}
                            value={data.childLinkPattern ?? ''}
                            placeholder="留空 = 自動辨識"
                            onChange={e => onUpdate({ childLinkPattern: e.target.value })}
                          />
                        </div>
                        <p className="text-[10px] text-gray-400 col-span-2 leading-relaxed">
                          自動辨識涵蓋：Reddit <code className="font-mono bg-gray-100 px-1 rounded">/comments/</code>、Dcard <code className="font-mono bg-gray-100 px-1 rounded">/p/</code>、
                          PTT <code className="font-mono bg-gray-100 px-1 rounded">/M.xxx.A.xxx</code>、HN <code className="font-mono bg-gray-100 px-1 rounded">/item?id=</code>、
                          新聞 <code className="font-mono bg-gray-100 px-1 rounded">/article/</code> 等 12 種；
                          其他站填 regex 例如 <code className="font-mono bg-gray-100 px-1 rounded">{'/posts/[a-z0-9]+'}</code>。
                          <br />
                          自動會跳過釘選 / 公告貼文（含「公告 / Pinned / 超級討論串 / 社群精選」字眼的區塊）。
                        </p>
                      </div>
                    )}
                  </div>

                  {/* JS 互動序列 */}
                  <div className="bg-white border border-gray-200 rounded-lg p-2.5 space-y-2">
                    <label className="text-sm font-medium text-gray-700 block">
                      JS 互動序列 {data.interactions?.length ? `（${data.interactions.length} 個動作）` : ''}
                    </label>
                    <p className="text-[11px] text-gray-500 leading-relaxed">
                      載入頁面後依序執行的動作。常用：點「載入更多」按鈕、等指定元素出現、輸入文字到搜尋框。
                      <b>填了這裡 → 上面的智慧滾動會被忽略</b>，完全照你給的序列跑。一般滾動需求請優先用上面智慧滾動，不要自己組。
                    </p>

                    {/* 序列清單 */}
                    {(data.interactions || []).length > 0 && (
                      <div className="space-y-1.5">
                        {(data.interactions || []).map((a, idx) => (
                          <div key={idx} className="border border-gray-200 rounded p-2 bg-gray-50 space-y-1.5">
                            <div className="flex items-center gap-1.5">
                              <button
                                type="button"
                                title="上移"
                                onClick={() => moveAction(idx, -1)}
                                disabled={idx === 0}
                                className="text-gray-300 hover:text-gray-600 disabled:opacity-30"
                              >
                                <GripVertical className="w-3.5 h-3.5" />
                              </button>
                              <span className="text-xs font-medium text-gray-700 flex-1">
                                {idx + 1}. {ACTION_LABEL[a.type]}
                              </span>
                              <button
                                type="button"
                                onClick={() => removeAction(idx)}
                                title="刪除"
                                className="text-gray-300 hover:text-red-400 transition-colors"
                              >
                                <Trash2 className="w-3.5 h-3.5" />
                              </button>
                            </div>

                            {a.type === 'click' && (
                              <input
                                className={`${inputCls} text-xs`}
                                placeholder="CSS selector，例：.load-more / button[type=submit]"
                                value={a.selector || ''}
                                onChange={e => updateAction(idx, { selector: e.target.value })}
                              />
                            )}
                            {a.type === 'wait_for' && (
                              <input
                                className={`${inputCls} text-xs`}
                                placeholder="CSS selector，例：.content / #result"
                                value={a.selector || ''}
                                onChange={e => updateAction(idx, { selector: e.target.value })}
                              />
                            )}
                            {a.type === 'wait' && (
                              <input
                                type="number"
                                min={0.1} step={0.1}
                                className={`${inputCls} text-xs`}
                                placeholder="秒數"
                                value={a.seconds ?? 1}
                                onChange={e => updateAction(idx, { seconds: Number(e.target.value) || 1 })}
                              />
                            )}
                            {a.type === 'scroll' && (
                              <div className="space-y-1.5">
                                <select
                                  className={`${inputCls} text-xs`}
                                  value={a.to || 'bottom'}
                                  onChange={e => updateAction(idx, { to: e.target.value as 'top' | 'bottom' | 'pixels' })}
                                >
                                  <option value="bottom">滾到底</option>
                                  <option value="top">滾到頂</option>
                                  <option value="pixels">向下滾 N 像素</option>
                                </select>
                                {a.to === 'pixels' && (
                                  <input
                                    type="number"
                                    min={1}
                                    className={`${inputCls} text-xs`}
                                    placeholder="像素數，例：1000"
                                    value={a.pixels ?? 1000}
                                    onChange={e => updateAction(idx, { pixels: Number(e.target.value) || 1000 })}
                                  />
                                )}
                              </div>
                            )}
                            {a.type === 'type' && (
                              <div className="space-y-1.5">
                                <input
                                  className={`${inputCls} text-xs`}
                                  placeholder="CSS selector，例：input[name=q]"
                                  value={a.selector || ''}
                                  onChange={e => updateAction(idx, { selector: e.target.value })}
                                />
                                <input
                                  className={`${inputCls} text-xs`}
                                  placeholder="要輸入的文字"
                                  value={a.text || ''}
                                  onChange={e => updateAction(idx, { text: e.target.value })}
                                />
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}

                    <div className="flex gap-1.5">
                      <select
                        className={`${inputCls} text-xs flex-1`}
                        value={newType}
                        onChange={e => setNewType(e.target.value as WebCrawlerAction['type'])}
                      >
                        <option value="click">{ACTION_LABEL.click}</option>
                        <option value="scroll">{ACTION_LABEL.scroll}</option>
                        <option value="wait">{ACTION_LABEL.wait}</option>
                        <option value="wait_for">{ACTION_LABEL.wait_for}</option>
                        <option value="type">{ACTION_LABEL.type}</option>
                      </select>
                      <button
                        type="button"
                        onClick={addInteraction}
                        className="px-3 py-1.5 bg-teal-500 hover:bg-teal-600 text-white rounded-lg text-xs font-medium flex items-center gap-1"
                      >
                        <Plus className="w-3 h-3" /> 新增
                      </button>
                    </div>
                  </div>
                </>
              )}

              {/* === 影片模式進階 === */}
              {isVideo && (
                <div className="bg-white border border-gray-200 rounded-lg p-2.5 space-y-1">
                  <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                    <input type="checkbox" checked={data.videoSaveInfoJson}
                           onChange={e => onUpdate({ videoSaveInfoJson: e.target.checked })} />
                    <span className="font-medium">保留完整 metadata JSON</span>
                    <code className="font-mono text-[10px] bg-gray-100 px-1 rounded">video.info.json</code>
                  </label>
                  <p className="text-[11px] text-gray-500 leading-relaxed pl-6">
                    yt-dlp 完整 metadata dump（~50KB）。預設不存因為 90% 是過期 URL / 格式列表 / HTTP headers。
                    勾起來才有 — <b>chapters 章節時間軸</b> / 全長描述 / tags / 縮圖連結都在這。
                  </p>
                </div>
              )}

              {/* === 共用：Cookies === */}
              <div className="bg-white border border-gray-200 rounded-lg p-2.5 space-y-2">
                <label className="text-sm font-medium text-gray-700 block">
                  登入 Cookies
                  <span className="ml-1 text-[10px] font-normal text-gray-400">
                    {isVideo ? '會員 / 付費 / 年齡限制影片用' : '會員區 / 私密內容用'}
                  </span>
                </label>
                <textarea
                  className={`${inputCls} font-mono text-xs resize-y min-h-[80px]`}
                  rows={5}
                  placeholder={'三種格式都接受：\n  key=value（一行一個）\n  key=v1; k2=v2（整串 Cookie 標頭）\n  [{"name":"k","value":"v"}]（JSON 陣列）\n\n或填 ${MY_COOKIE} 參照 backend/.env（推薦、不明文存）'}
                  value={data.cookies}
                  onChange={e => onUpdate({ cookies: e.target.value })}
                  onWheel={(e) => {
                    const el = e.currentTarget
                    const atTop = el.scrollTop === 0
                    const atBot = el.scrollTop + el.clientHeight >= el.scrollHeight - 1
                    if ((!atTop && e.deltaY < 0) || (!atBot && e.deltaY > 0)) e.stopPropagation()
                  }}
                />
                <p className="text-[10px] text-amber-600 leading-snug">
                  🔒 Cookie 等同登入憑證。直接貼會明文存進 workflow 設定 —
                  建議在 <code className="font-mono bg-amber-50 px-1 rounded">backend/.env</code> 設
                  <code className="font-mono bg-amber-50 px-1 rounded">MY_COOKIE=…</code>、
                  這裡只填 <code className="font-mono bg-amber-50 px-1 rounded">{'${MY_COOKIE}'}</code> 參照,
                  workflow 就只會存佔位符、真值留在不進版控的 .env。
                </p>

                {/* 教學：怎麼從瀏覽器抓 cookies */}
                <button
                  type="button"
                  onClick={() => setShowCookieHowto(s => !s)}
                  className="w-full flex items-center gap-1.5 text-[11px] text-blue-600 hover:text-blue-800 transition-colors"
                >
                  {showCookieHowto ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                  教學：怎麼從瀏覽器抓 cookies？
                </button>
                {showCookieHowto && (
                  <div className="text-[11px] text-gray-700 leading-relaxed bg-blue-50 border border-blue-200 rounded p-2.5 space-y-2">
                    <div>
                      <p className="font-semibold mb-1">🟦 方法 A：用瀏覽器擴充套件（最簡單、推薦）</p>
                      <ol className="list-decimal list-inside space-y-0.5 pl-1">
                        <li>裝 Chrome 擴充套件「<b>Cookie-Editor</b>」或「<b>EditThisCookie</b>」（Firefox 也有相同套件）</li>
                        <li>用瀏覽器登入目標網站（例：<code className="font-mono bg-white px-1 rounded">medium.com</code>）</li>
                        <li>點擴充套件圖示 → <b>Export</b> → 選 <b>Header String</b>（或 JSON 也可以）</li>
                        <li>整串複製、貼到上面的 textarea</li>
                      </ol>
                    </div>

                    <div className="border-t border-blue-200 pt-2">
                      <p className="font-semibold mb-1">🟦 方法 B：DevTools 手動抓（不裝套件）</p>
                      <ol className="list-decimal list-inside space-y-0.5 pl-1">
                        <li>用瀏覽器登入目標網站</li>
                        <li>按 <kbd className="font-mono bg-white px-1 rounded border border-gray-300">F12</kbd> 開 DevTools</li>
                        <li>切到 <b>Network</b> 分頁 → 隨便重新整理 → 點任一個請求</li>
                        <li>右側 <b>Headers</b> → 找 <b>Request Headers</b> 區 → 找 <code className="font-mono bg-white px-1 rounded">Cookie:</code> 那一行</li>
                        <li>整串複製（從 <code className="font-mono bg-white px-1 rounded">key=value; ...</code> 開始）→ 貼到上面</li>
                      </ol>
                    </div>

                    <div className="border-t border-blue-200 pt-2">
                      <p className="font-semibold mb-1">⚠️ 注意事項</p>
                      <ul className="list-disc list-inside space-y-0.5 pl-1 text-gray-600">
                        <li>Cookies 等於你的登入 session — <b>別貼進公開的 git / share 連結</b></li>
                        <li>通常 <b>1-7 天</b>會過期、抓不到就重貼一次</li>
                        <li>部分網站有「裝置綁定 / IP 綁定」，貼了也沒用（對這類站爬蟲會卡住、得用其他方式）</li>
                        <li>YouTube 一般影片不需要 cookies；只有<b>會員 / 付費 / 18+</b>才需要</li>
                      </ul>
                    </div>

                    <div className="border-t border-blue-200 pt-2">
                      <p className="font-semibold mb-1">📝 實際貼上長相</p>
                      <pre className="font-mono text-[10px] bg-white border border-gray-200 rounded p-1.5 overflow-x-auto">
{`# 方法 A 的 Header String 格式（一行內全部 cookies）
session_id=abc123; user_id=42; csrf_token=xyz; ...

# 或一行一個（也接受）
session_id=abc123
user_id=42
csrf_token=xyz`}
                      </pre>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* ============================================================ */}
        {/* 共用：輸出 + Timeout / Retry                                 */}
        {/* ============================================================ */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
            輸出檔路徑{isVideo && '（.md 摘要；實際 mp4 / 字幕擺同層）'}
          </label>
          <input
            className={`${inputCls} font-mono`}
            placeholder={isVideo ? 'ai_output/{name}/summary.md（留空自動命名）' : 'ai_output/{name}/result.md（留空自動命名）'}
            value={data.outputPath}
            onChange={e => onUpdate({ outputPath: e.target.value })}
          />
          <p className="text-[10px] text-gray-400 mt-0.5">
            {isVideo
              ? '下個 skill 節點可從 .md 讀摘要，或直接讀同層的 video.mp4 / video.*.srt / video.info.json'
              : '下個 skill 節點可從這個路徑讀回 markdown 做分析'}
          </p>
        </div>

        {/* 驗證閘：expect（對應 YAML output.expect；抓外部資料建議必填） */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">預期輸出描述（AI 驗證、建議填）</label>
          <textarea
            rows={3}
            value={data.expectText || ''}
            onChange={e => onUpdate({ expectText: e.target.value })}
            placeholder={'描述「怎樣才算真的抓到目標資料」…\n例：確實抓到商品列表(多筆名稱+價格)，非 404/反爬錯頁/cookie 同意頁'}
            className={`${inputCls} resize-y text-xs leading-relaxed min-h-[60px]`}
          />
          <p className="text-[10px] text-gray-400 mt-0.5">
            抓外部資料常拿到「看似成功的錯頁」（403 / 空頁 / 反爬頁），填了才能在這一步就攔下、不會污染下游。
          </p>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">Timeout（秒）</label>
            <input
              type="number" min={30} max={3600} step={30}
              className={inputCls}
              value={data.timeout}
              onChange={e => onUpdate({ timeout: Number(e.target.value) || (isVideo ? 600 : 180) })}
            />
            <p className="text-[10px] text-gray-400 mt-0.5">
              {isVideo ? '影片建議 600+ 秒' : '網頁建議 180 秒'}
            </p>
          </div>
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">失敗重試次數</label>
            <input
              type="number" min={0} max={5}
              className={inputCls}
              value={data.retry}
              onChange={e => onUpdate({ retry: Number(e.target.value) || 0 })}
            />
          </div>
        </div>
      </div>
    </div>
  )
}
