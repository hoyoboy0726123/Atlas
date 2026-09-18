'use client'

import { useEffect, useState } from 'react'
import { Settings as SettingsIcon, Save, RefreshCw, AlertCircle, CheckCircle2, Cloud, HardDrive, ArrowLeft, Brain, Package, Plus, Trash2, Loader2, Sparkles, MessageSquare, Bell, Search, Shield, Users, Edit2, X as XIcon } from 'lucide-react'
import Link from 'next/link'
import { toast, Toaster } from 'sonner'
import {
  getModelSettings, saveModelSettings, getAvailableModels,
  getSkillPackages, addSkillPackage, removeSkillPackage,
  getNotificationSettings, saveNotificationSettings,
  getWebSearchSettings, saveWebSearchSettings,
  analyzeRecentLogs,
  listAvailableSkills, scanSkillDependencies,
  listSubagentRoles, createSubagentRole, updateSubagentRole, deleteSubagentRole,
  type SubagentRole, type SubagentRolePayload, type SelectableTool,
  scanUnlistedPackages, adoptExistingPackage,
  getNodeStatus,
  getHostTools,
  type HostToolsResponse,
  getSandboxStatus, setSandboxMode, getSkillsDir, setSkillsDir,
  type ModelSettings, type AvailableModels, type SkillPackage, type NotificationSettings,
  type WebSearchSettingsInput,
  type LogSuggestion, type AvailableSkill, type SkillDependencies,
  type UnlistedPackage, type NodeStatus, type SandboxStatus, type SkillsDirStatus,
  getMcpServers, getMcpCatalog, addMcpServer, refreshMcpServer, removeMcpServer,
  type McpServer, type McpCatalogEntry,
  listSecrets, setSecret, deleteSecret, type SecretMeta,
  getMcpServerConfig, type McpServerConfig,
} from '@/lib/api'
import { cn } from '@/lib/utils'

// ── Atlas 當 MCP server 的設定卡(config 一鍵複製)─────────────────────────
function McpServerModeCard() {
  const [cfg, setCfg] = useState<McpServerConfig | null>(null)
  useEffect(() => { getMcpServerConfig().then(setCfg).catch(() => {}) }, [])
  if (!cfg) return <div className="px-4 pb-4 text-xs text-gray-400">載入中…</div>
  const text = JSON.stringify(cfg.config, null, 2)
  return (
    <div className="px-4 pb-4 space-y-2">
      <p className="text-xs text-gray-500">
        外部 AI 會獲得工具:{cfg.tools.map(t => <code key={t} className="font-mono bg-gray-100 px-1 rounded mx-0.5">{t}</code>)}
        — 等於「Atlas 變成其他 AI 的手腳」。{cfg.note}
      </p>
      <div className="relative">
        <pre className="text-[11px] font-mono bg-gray-900 text-gray-100 rounded-lg p-3 overflow-x-auto">{text}</pre>
        <button onClick={() => { navigator.clipboard?.writeText(text); toast.success('已複製設定') }}
          className="absolute top-2 right-2 text-[11px] px-2 py-1 rounded bg-white/10 text-white hover:bg-white/20">複製</button>
      </div>
    </div>
  )
}

// ── Secrets Vault Section(憑證加密存放;值永不回傳)────────────────────────
function SecretsSection() {
  const [secrets, setSecrets] = useState<SecretMeta[]>([])
  const [loading, setLoading] = useState(false)
  const [nName, setNName] = useState('')
  const [nValue, setNValue] = useState('')
  const [saving, setSaving] = useState(false)

  const load = async () => {
    setLoading(true)
    try { setSecrets(await listSecrets()) } catch { /* 後端未起 */ }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-rose-100 flex items-center justify-center">
          <Shield className="w-5 h-5 text-rose-700" />
        </div>
        <div className="flex-1">
          <h2 className="text-lg font-semibold text-gray-900">Secrets(憑證庫)</h2>
          <p className="text-sm text-gray-500">
            API key / 密碼加密存放(Fernet,金鑰只在本機)。工作流用 <code className="font-mono text-xs bg-gray-100 px-1 rounded">{'{{ secrets.名稱 }}'}</code> 引用,執行時才解密;值不會出現在 YAML、對話或 AI 提示詞。
          </p>
        </div>
        <button onClick={load} disabled={loading}
          className="p-2 text-gray-400 hover:text-rose-600 transition-colors disabled:opacity-50" title="重新整理">
          <RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} />
        </button>
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 divide-y divide-gray-100">
        {secrets.length === 0 && !loading && (
          <div className="p-5 text-sm text-gray-400 text-center">尚未儲存任何 secret。</div>
        )}
        {secrets.map(s => (
          <div key={s.name} className="p-4 flex items-center gap-3">
            <div className="flex-1 min-w-0">
              <span className="text-sm font-mono font-medium text-gray-900">{s.name}</span>
              <span className="text-xs text-gray-400 ml-2 font-mono">••••••••</span>
              <p className="text-[11px] text-gray-400 mt-0.5">
                引用:<code className="font-mono bg-gray-50 px-1 rounded">{'{{ secrets.' + s.name + ' }}'}</code>
                <span className="ml-2">更新於 {new Date(s.updated_at * 1000).toLocaleString()}</span>
              </p>
            </div>
            <button onClick={() => { setNName(s.name); setNValue(''); }}
              className="text-xs text-rose-600 hover:underline">更新值</button>
            <button onClick={async () => {
              if (!confirm(`刪除 secret「${s.name}」?引用它的工作流會在執行時明確報缺。`)) return
              try { await deleteSecret(s.name); toast.success('已刪除'); await load() }
              catch (e) { toast.error((e as Error).message) }
            }} className="p-1.5 text-gray-400 hover:text-red-500" title="刪除">
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        ))}
      </div>

      {/* 新增 / 更新 */}
      <div className="mt-3 bg-white border border-gray-200 rounded-xl p-4">
        <div className="flex gap-2">
          <input value={nName} onChange={e => setNName(e.target.value)} placeholder="名稱(如 OPENAI_KEY)"
            className="w-48 text-sm border border-gray-200 rounded-lg px-3 py-1.5 font-mono" />
          <input value={nValue} onChange={e => setNValue(e.target.value)} type="password"
            placeholder="值(送出後不再顯示)"
            className="flex-1 text-sm border border-gray-200 rounded-lg px-3 py-1.5 font-mono" />
          <button onClick={async () => {
            if (!nName.trim() || !nValue) { toast.error('名稱與值必填'); return }
            setSaving(true)
            try {
              const r = await setSecret(nName.trim(), nValue)
              toast.success(`已加密儲存,引用:${r.usage}`)
              setNName(''); setNValue(''); await load()
            } catch (e) { toast.error((e as Error).message) }
            finally { setSaving(false) }
          }} disabled={saving}
            className="text-xs px-3 py-1.5 rounded-lg bg-rose-600 text-white hover:bg-rose-700 disabled:opacity-50 whitespace-nowrap">
            {saving ? '儲存中…' : '加密儲存'}
          </button>
        </div>
        <p className="text-[11px] text-gray-400 mt-2">
          ⚠ 這是本機層級加密(擋 DB 檔外流 / 匯出誤帶):金鑰檔在輸出目錄 .vault_key,請勿把它和資料庫一起分享。
        </p>
      </div>
    </div>
  )
}

// ── MCP Servers Section(像 Skill 掛載:一鍵安裝、live 驗證、乾淨移除)──────────
function McpServersSection() {
  const [servers, setServers] = useState<McpServer[]>([])
  const [catalog, setCatalog] = useState<McpCatalogEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [installing, setInstalling] = useState('')          // 正在安裝的 name(顯示 spinner)
  const [expanded, setExpanded] = useState<string | null>(null)  // 展開看 tools 的 server
  // 自訂新增表單
  const [showCustom, setShowCustom] = useState(false)
  const [cName, setCName] = useState(''); const [cCmd, setCCmd] = useState('npx')
  const [cArgs, setCArgs] = useState(''); const [cEnv, setCEnv] = useState('')
  const [cRemote, setCRemote] = useState(false)     // 遠端 transport 模式
  const [cUrl, setCUrl] = useState(''); const [cConsent, setCConsent] = useState(false)
  const [cAdminPw, setCAdminPw] = useState('')      // 遠端防呆閘密碼(.env 有設才要求)
  // catalog 佔位補值(如 filesystem 的資料夾路徑)
  const [placeholderFor, setPlaceholderFor] = useState<McpCatalogEntry | null>(null)
  const [placeholderVals, setPlaceholderVals] = useState<Record<number, string>>({})
  const [envVals, setEnvVals] = useState<Record<string, string>>({})

  const load = async () => {
    setLoading(true)
    try {
      const [s, c] = await Promise.all([getMcpServers(), getMcpCatalog()])
      setServers(s); setCatalog(c)
    } catch (e) { toast.error((e as Error).message) }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const installedNames = new Set(servers.map(s => s.name))

  const doInstall = async (payload: { name: string; command?: string; args?: string[]; env?: Record<string, string>; transport?: string; url?: string; remote_consent?: boolean; admin_password?: string }) => {
    setInstalling(payload.name)
    try {
      const r = await addMcpServer(payload)
      toast.success(`已安裝 ${payload.name}(${r.tool_count} 個 tools)— AI 助手已可使用`)
      setPlaceholderFor(null); setShowCustom(false)
      await load()
    } catch (e) { toast.error((e as Error).message) }
    finally { setInstalling('') }
  }

  const installFromCatalog = (entry: McpCatalogEntry) => {
    const hasPlaceholder = entry.args.some(a => a.includes('<')) || entry.env_keys.length > 0
    if (hasPlaceholder) { setPlaceholderFor(entry); setPlaceholderVals({}); setEnvVals({}) }
    else doInstall({ name: entry.name, command: entry.command, args: entry.args })
  }

  const confirmPlaceholderInstall = () => {
    if (!placeholderFor) return
    const args = placeholderFor.args.map((a, i) => a.includes('<') ? (placeholderVals[i] || '').trim() : a)
    if (args.some(a => !a)) { toast.error('請填齊所有必要欄位'); return }
    const env: Record<string, string> = {}
    for (const k of placeholderFor.env_keys) {
      if (!(envVals[k] || '').trim()) { toast.error(`請填 ${k}`); return }
      env[k] = envVals[k].trim()
    }
    doInstall({ name: placeholderFor.name, command: placeholderFor.command, args, env: Object.keys(env).length ? env : undefined })
  }

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-teal-100 flex items-center justify-center">
          <Package className="w-5 h-5 text-teal-700" />
        </div>
        <div className="flex-1">
          <h2 className="text-lg font-semibold text-gray-900">MCP Servers</h2>
          <p className="text-sm text-gray-500">掛載外部 MCP 工具(GitHub / 記憶 / 資料庫…)。安裝時會實際連線驗證,AI 助手只會使用這裡列出的 server。</p>
        </div>
        <button onClick={load} disabled={loading}
          className="p-2 text-gray-400 hover:text-teal-600 transition-colors disabled:opacity-50" title="重新整理">
          <RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} />
        </button>
      </div>

      {/* 已安裝清單 */}
      <div className="bg-white rounded-2xl border border-gray-200 divide-y divide-gray-100">
        {servers.length === 0 && !loading && (
          <div className="p-6 text-sm text-gray-400 text-center">尚未安裝任何 MCP server — 從下方精選目錄一鍵安裝,或自訂新增。</div>
        )}
        {servers.map(s => (
          <div key={s.name} className="p-4">
            <div className="flex items-center gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-mono font-medium text-gray-900">{s.name}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-teal-50 text-teal-600">{s.tools_cache.length} tools</span>
                  {!s.enabled && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-500">已停用</span>}
                </div>
                <p className="text-xs text-gray-400 font-mono mt-0.5 truncate">{s.command} {s.args.join(' ')}</p>
              </div>
              <button onClick={() => setExpanded(expanded === s.name ? null : s.name)}
                className="text-xs text-gray-500 hover:text-teal-600 px-2 py-1">
                {expanded === s.name ? '收起' : '看 tools'}
              </button>
              <button onClick={async () => {
                setInstalling(s.name)
                try { const r = await refreshMcpServer(s.name); toast.success(`已重掃(${r.tool_count} 個 tools)`); await load() }
                catch (e) { toast.error((e as Error).message) } finally { setInstalling('') }
              }} disabled={installing === s.name}
                className="p-1.5 text-gray-400 hover:text-teal-600 disabled:opacity-50" title="重新掃描 tools">
                <RefreshCw className={cn('w-3.5 h-3.5', installing === s.name && 'animate-spin')} />
              </button>
              <button onClick={async () => {
                if (!confirm(`確定移除 MCP server「${s.name}」?移除後 AI 助手將不再使用它;使用它的既有工作流會明確報錯提示重新安裝。`)) return
                try { await removeMcpServer(s.name); toast.success(`已移除 ${s.name}`); await load() }
                catch (e) { toast.error((e as Error).message) }
              }} className="p-1.5 text-gray-400 hover:text-red-500" title="乾淨移除">
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
            {expanded === s.name && (
              <div className="mt-3 pl-1 space-y-1 max-h-48 overflow-y-auto">
                {s.tools_cache.map(t => (
                  <div key={t.name} className="text-xs">
                    <span className="font-mono text-teal-700">{t.name}</span>
                    <span className="text-gray-400 ml-2">{(t.description || '').split('\n')[0].slice(0, 90)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* 精選目錄(一鍵安裝) */}
      <div className="mt-4">
        <p className="text-xs font-medium text-gray-500 mb-2">精選常用(一鍵安裝,首次會下載套件、需 30 秒~2 分鐘)</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {catalog.filter(c => !installedNames.has(c.name)).map(c => (
            <div key={c.id} className="flex items-center gap-2 bg-white rounded-xl border border-gray-200 p-3">
              <div className="flex-1 min-w-0">
                <span className="text-sm font-mono font-medium text-gray-800">{c.name}</span>
                <p className="text-xs text-gray-400 truncate">{c.description}</p>
              </div>
              <button onClick={() => installFromCatalog(c)} disabled={!!installing}
                className="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg bg-teal-600 text-white hover:bg-teal-700 disabled:opacity-50">
                {installing === c.name ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />}
                安裝
              </button>
            </div>
          ))}
        </div>
      </div>

      {/* 佔位補值小表單(filesystem 資料夾 / github token 等) */}
      {placeholderFor && (
        <div className="mt-3 bg-teal-50 border border-teal-200 rounded-xl p-4 space-y-2">
          <p className="text-sm font-medium text-teal-800">安裝 {placeholderFor.name} — 還需要填:</p>
          {placeholderFor.guidance && (
            <p className="text-xs text-teal-700 bg-teal-100/60 rounded-lg px-3 py-2 leading-relaxed whitespace-pre-line">
              🔑 {placeholderFor.guidance}
            </p>
          )}
          {placeholderFor.args.map((a, i) => a.includes('<') ? (
            <input key={i} value={placeholderVals[i] || ''} placeholder={a.replace(/[<>]/g, '')}
              onChange={e => setPlaceholderVals(v => ({ ...v, [i]: e.target.value }))}
              className="w-full text-sm border border-teal-200 rounded-lg px-3 py-1.5 font-mono" />
          ) : null)}
          {placeholderFor.env_keys.map(k => (
            <input key={k} value={envVals[k] || ''} placeholder={k} type="password"
              onChange={e => setEnvVals(v => ({ ...v, [k]: e.target.value }))}
              className="w-full text-sm border border-teal-200 rounded-lg px-3 py-1.5 font-mono" />
          ))}
          <div className="flex gap-2">
            <button onClick={confirmPlaceholderInstall} disabled={!!installing}
              className="text-xs px-3 py-1.5 rounded-lg bg-teal-600 text-white hover:bg-teal-700 disabled:opacity-50">
              {installing ? '連線驗證中…' : '安裝並驗證'}
            </button>
            <button onClick={() => setPlaceholderFor(null)} className="text-xs px-3 py-1.5 rounded-lg text-gray-500 hover:bg-gray-100">取消</button>
          </div>
        </div>
      )}

      {/* 反向:把 Atlas 當 MCP server 給其他 AI 用 */}
      <details className="mt-4 bg-white border border-gray-200 rounded-xl overflow-hidden">
        <summary className="cursor-pointer px-4 py-3 text-sm font-medium text-gray-700 hover:bg-gray-50">
          🔁 反向模式:讓 Claude Desktop 等外部 AI 呼叫 Atlas 工作流(MCP server)
        </summary>
        <McpServerModeCard />
      </details>

      {/* 自訂新增 */}
      <div className="mt-3">
        {!showCustom ? (
          <button onClick={() => setShowCustom(true)} className="text-xs text-teal-600 hover:underline">+ 自訂新增(stdio 或遠端 MCP server)</button>
        ) : (
          <div className="bg-white border border-gray-200 rounded-xl p-4 space-y-2">
            {/* transport 切換 */}
            <div className="flex gap-1 text-xs">
              <button onClick={() => setCRemote(false)}
                className={`px-3 py-1 rounded-lg font-medium ${!cRemote ? 'bg-teal-600 text-white' : 'bg-gray-100 text-gray-500'}`}>本機(stdio)</button>
              <button onClick={() => setCRemote(true)}
                className={`px-3 py-1 rounded-lg font-medium ${cRemote ? 'bg-teal-600 text-white' : 'bg-gray-100 text-gray-500'}`}>遠端(HTTP/SSE)</button>
            </div>
            <input value={cName} onChange={e => setCName(e.target.value)} placeholder="名稱(如 slack)"
              className="w-full text-sm border border-gray-200 rounded-lg px-3 py-1.5 font-mono" />
            {!cRemote ? (
              <>
                <input value={cCmd} onChange={e => setCCmd(e.target.value)} placeholder="指令(npx / uvx / python)"
                  className="w-full text-sm border border-gray-200 rounded-lg px-3 py-1.5 font-mono" />
                <input value={cArgs} onChange={e => setCArgs(e.target.value)} placeholder="參數(空白分隔,如 -y slack-mcp-server)"
                  className="w-full text-sm border border-gray-200 rounded-lg px-3 py-1.5 font-mono" />
                <input value={cEnv} onChange={e => setCEnv(e.target.value)} placeholder="環境變數(選填,KEY=VALUE 空白分隔)"
                  className="w-full text-sm border border-gray-200 rounded-lg px-3 py-1.5 font-mono" />
              </>
            ) : (
              <>
                <input value={cUrl} onChange={e => setCUrl(e.target.value)} placeholder="Server URL(https://.../mcp)"
                  className="w-full text-sm border border-gray-200 rounded-lg px-3 py-1.5 font-mono" />
                <input value={cEnv} onChange={e => setCEnv(e.target.value)} placeholder="Bearer token(選填,填 Authorization=你的token)"
                  className="w-full text-sm border border-gray-200 rounded-lg px-3 py-1.5 font-mono" />
                <input type="password" value={cAdminPw} onChange={e => setCAdminPw(e.target.value)}
                  placeholder="管理者密碼(未在 .env 設定則留空)"
                  className="w-full text-sm border border-gray-200 rounded-lg px-3 py-1.5 font-mono" />
                <label className="flex items-start gap-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 cursor-pointer">
                  <input type="checkbox" checked={cConsent} onChange={e => setCConsent(e.target.checked)} className="mt-0.5" />
                  <span>我了解遠端 MCP server 會把工作流的資料送到這個外部網址,且它回傳的內容會進入我的工作流(可能含指令)。我信任此 server。</span>
                </label>
              </>
            )}
            <div className="flex gap-2">
              <button onClick={() => {
                const name = cName.trim(); if (!name) { toast.error('名稱必填'); return }
                const env: Record<string, string> = {}
                for (const kv of cEnv.trim().split(/\s+/).filter(Boolean)) {
                  const i = kv.indexOf('='); if (i > 0) env[kv.slice(0, i)] = kv.slice(i + 1)
                }
                if (cRemote) {
                  if (!cUrl.trim()) { toast.error('遠端需填 URL'); return }
                  if (!cConsent) { toast.error('遠端 server 需勾選信任確認'); return }
                  doInstall({ name, transport: 'streamable-http', url: cUrl.trim(), remote_consent: true, admin_password: cAdminPw || undefined, env: Object.keys(env).length ? env : undefined })
                } else {
                  if (!cCmd.trim()) { toast.error('指令必填'); return }
                  doInstall({ name, command: cCmd.trim(), args: cArgs.trim().split(/\s+/).filter(Boolean), env: Object.keys(env).length ? env : undefined })
                }
              }} disabled={!!installing}
                className="text-xs px-3 py-1.5 rounded-lg bg-teal-600 text-white hover:bg-teal-700 disabled:opacity-50">
                {installing ? '連線驗證中…' : '安裝並驗證'}
              </button>
              <button onClick={() => setShowCustom(false)} className="text-xs px-3 py-1.5 rounded-lg text-gray-500 hover:bg-gray-100">取消</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Claude Code Skills Section ────────────────────────────────────────────────
function InstalledSkillsSection({ onInstallRequest }: { onInstallRequest: (pkg: string) => Promise<void> }) {
  const [skills, setSkills] = useState<AvailableSkill[]>([])
  const [skillsRoot, setSkillsRoot] = useState('')
  const [rootExists, setRootExists] = useState(true)
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [depsCache, setDepsCache] = useState<Record<string, SkillDependencies>>({})
  const [scanning, setScanning] = useState<string | null>(null)
  // Skill 檔案目錄設定（可自訂路徑）
  const [dirCfg, setDirCfg] = useState<SkillsDirStatus | null>(null)
  const [dirInput, setDirInput] = useState('')
  const [savingDir, setSavingDir] = useState(false)

  const loadSkills = async () => {
    setLoading(true)
    try {
      const r = await listAvailableSkills()
      setSkills(r.skills || [])
      setSkillsRoot(r.skills_root)
      setRootExists(r.exists)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadSkills() }, [])

  const loadDir = async () => {
    try {
      const d = await getSkillsDir()
      setDirCfg(d)
      setDirInput(d.skills_dir)
    } catch { /* ignore */ }
  }
  useEffect(() => { loadDir() }, [])

  const applyDir = async () => {
    setSavingDir(true)
    try {
      const d = await setSkillsDir(dirInput.trim())
      setDirCfg(prev => (prev ? { ...prev, ...d } : d))
      toast.success(
        (d.skills_dir ? '已套用 Skill 目錄' : '已還原預設目錄') + `，找到 ${d.skill_count} 個 Skill`,
      )
      await loadSkills()
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setSavingDir(false)
    }
  }

  // 目前的 sandbox 模式（host / sandbox）— 決定顯示什麼安裝指令給使用者
  const [currentMode, setCurrentMode] = useState<'host' | 'sandbox'>('host')
  const refreshMode = async () => {
    try {
      const s = await getSandboxStatus()
      setCurrentMode(s.mode === 'wsl_docker' ? 'sandbox' : 'host')
    } catch { /* ignore */ }
  }
  useEffect(() => { refreshMode() }, [])

  // 沙盒模式切換時清掉 deps 快取（不同 mode 的「已安裝」結果不同）+ 收起展開的卡片 + 重新讀 mode
  // 不然使用者切到容器模式還會看到 host venv 掃出來的舊結果
  useEffect(() => {
    const handler = () => {
      setDepsCache({})
      setExpanded(null)
      refreshMode()
    }
    window.addEventListener('sandbox-mode-changed', handler)
    return () => window.removeEventListener('sandbox-mode-changed', handler)
  }, [])

  // 系統工具顯示名 → apt 套件名（沙盒容器是 Debian 12、用 apt）
  const TOOL_TO_APT: Record<string, string> = {
    'LibreOffice': 'libreoffice',
    'Poppler': 'poppler-utils',
    'FFmpeg': 'ffmpeg',
    'ImageMagick': 'imagemagick',
    'Tesseract OCR': 'tesseract-ocr',
    'wkhtmltopdf': 'wkhtmltopdf',
    'Pandoc': 'pandoc',
    'Git': 'git',
    'Node.js': 'nodejs',
    'Node.js/npm': 'nodejs npm',
    'Docker': 'docker.io',
  }
  const npmInstallCmd = (pkgs: string[]) =>
    currentMode === 'sandbox'
      ? `wsl docker exec atlas-sandbox npm install -g ${pkgs.join(' ')}`
      : `npm install -g ${pkgs.join(' ')}`
  const aptInstallCmd = (tools: string[]) => {
    const aptPkgs = tools.map(t => TOOL_TO_APT[t] || t.toLowerCase()).join(' ')
    return `wsl docker exec atlas-sandbox bash -c "apt-get update && apt-get install -y ${aptPkgs}"`
  }

  // 重抓目前展開的 skill 的依賴掃描（讓「尚未安裝」即時變「已安裝」）
  const refreshExpandedDeps = async () => {
    if (!expanded) return
    setScanning(expanded)
    try {
      const deps = await scanSkillDependencies(expanded)
      setDepsCache(prev => ({ ...prev, [expanded as string]: deps }))
    } catch { /* ignore */ }
    finally { setScanning(null) }
  }

  // 包一層、安裝後自動 refresh、不用 user 自己 collapse + expand
  const handleInstall = async (pkg: string) => {
    await onInstallRequest(pkg)
    await refreshExpandedDeps()
  }

  const handleToggleExpand = async (displayName: string) => {
    if (expanded === displayName) {
      setExpanded(null)
      return
    }
    setExpanded(displayName)
    if (!depsCache[displayName]) {
      setScanning(displayName)
      try {
        const deps = await scanSkillDependencies(displayName)
        setDepsCache(prev => ({ ...prev, [displayName]: deps }))
      } catch (e) {
        toast.error((e as Error).message)
      } finally {
        setScanning(null)
      }
    }
  }

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-purple-100 flex items-center justify-center">
          <Sparkles className="w-5 h-5 text-purple-700" />
        </div>
        <div className="flex-1">
          <h2 className="text-lg font-semibold text-gray-900">Agent Skills</h2>
          <p className="text-sm text-gray-500">已識別的 Skill 清單（來自 <code className="font-mono text-xs bg-gray-100 px-1 rounded">{skillsRoot}</code>）</p>
        </div>
        <button onClick={loadSkills} disabled={loading}
          className="p-2 text-gray-400 hover:text-purple-600 transition-colors disabled:opacity-50" title="重新掃描">
          <RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} />
        </button>
      </div>

      {/* Skill 檔案目錄設定 — 讓使用者自選 skill 檔放在哪 */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 mb-3">
        <label className="text-sm font-medium text-gray-700 block mb-1">Skill 檔案目錄</label>
        <p className="text-xs text-gray-400 mb-2">
          留空 = 用預設{' '}
          <code className="font-mono bg-gray-100 px-1 rounded">{dirCfg?.default || '~/.agents/skills/'}</code>
          。可改成你自己存放 Skill 的資料夾。
        </p>
        <div className="flex gap-2">
          <input
            value={dirInput}
            onChange={e => setDirInput(e.target.value)}
            placeholder={dirCfg?.default || '~/.agents/skills/（留空 = 用預設）'}
            disabled={savingDir || dirCfg?.env_override}
            className="flex-1 border border-gray-200 rounded-lg px-3 py-1.5 text-sm font-mono outline-none focus:border-purple-400 disabled:bg-gray-100"
          />
          <button onClick={applyDir} disabled={savingDir || dirCfg?.env_override}
            className="px-4 py-1.5 text-sm rounded-lg bg-purple-600 text-white hover:bg-purple-700 disabled:bg-gray-300 disabled:cursor-not-allowed">
            {savingDir ? '套用中…' : '套用'}
          </button>
        </div>
        {dirCfg?.env_override ? (
          <p className="text-xs text-amber-600 mt-1.5">⚠ 目前被環境變數 SKILLS_DIR 覆蓋,此設定不生效。</p>
        ) : dirCfg && !dirCfg.exists ? (
          <p className="text-xs text-red-500 mt-1.5">⚠ 目前的目錄不存在:{dirCfg.resolved}</p>
        ) : dirCfg ? (
          <p className="text-xs text-gray-400 mt-1.5">
            目前使用:<code className="font-mono">{dirCfg.resolved}</code>(找到 {dirCfg.skill_count} 個 Skill)
          </p>
        ) : null}
      </div>

      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {loading ? (
          <div className="p-6 text-center text-gray-400 text-sm">
            <RefreshCw className="w-4 h-4 animate-spin inline-block mr-2" />載入中...
          </div>
        ) : !rootExists ? (
          <div className="p-6 text-center text-gray-400 text-sm">
            <p>Skill 目錄不存在</p>
            <p className="mt-1 text-xs">用 <code className="font-mono bg-gray-100 px-1 py-0.5 rounded">npx skills add anthropics/skills</code> 安裝</p>
          </div>
        ) : skills.length === 0 ? (
          <div className="p-6 text-center text-gray-400 text-sm">目錄存在但沒有任何 Skill</div>
        ) : (
          <div className="divide-y divide-gray-100">
            {skills.map(s => {
              const deps = depsCache[s.display_name]
              const isExpanded = expanded === s.display_name
              const isScanning = scanning === s.display_name
              return (
                <div key={s.display_name} className="transition-colors">
                  <button onClick={() => handleToggleExpand(s.display_name)}
                    className="w-full flex items-start gap-3 px-4 py-3 hover:bg-gray-50 text-left">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-mono font-medium text-gray-900">{s.display_name}</span>
                        {s.has_scripts && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-blue-50 text-blue-600">scripts</span>}
                        {s.has_references && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-green-50 text-green-600">refs</span>}
                        {s.has_assets && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-50 text-amber-600">assets</span>}
                        {s.has_package_json && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-red-50 text-red-600">npm</span>}
                        {s.has_requirements && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-purple-50 text-purple-600">requirements.txt</span>}
                      </div>
                      {s.description && <p className="text-xs text-gray-500 mt-1 line-clamp-2">{s.description}</p>}
                    </div>
                    <span className="text-xs text-gray-400 shrink-0 mt-0.5">{isExpanded ? '收合 ▲' : '依賴 ▼'}</span>
                  </button>

                  {isExpanded && (
                    <div className="px-4 pb-3 bg-gray-50 border-t border-gray-100">
                      {isScanning && <p className="py-3 text-xs text-gray-400"><Loader2 className="w-3 h-3 animate-spin inline mr-1.5" />掃描依賴中…</p>}
                      {!isScanning && deps && (
                        <div className="space-y-3 pt-3 text-xs">
                          {/* Python 依賴 */}
                          <div>
                            <div className="font-medium text-gray-700 mb-1.5">🐍 Python 依賴</div>
                            {deps.python?.requirements_txt && deps.python.requirements_txt.length > 0 && (
                              <div className="mb-2">
                                <span className="text-gray-400">requirements.txt：</span>
                                <div className="mt-1 flex flex-wrap gap-1">
                                  {deps.python.requirements_txt.map(r => (
                                    <code key={r} className="px-1.5 py-0.5 bg-white border border-gray-200 rounded text-gray-700">{r}</code>
                                  ))}
                                </div>
                              </div>
                            )}
                            {deps.python?.missing && deps.python.missing.length > 0 ? (
                              <div>
                                <span className="text-amber-600">尚未安裝：</span>
                                <div className="mt-1 flex flex-wrap gap-1.5">
                                  {deps.python.missing.map(pkg => (
                                    <div key={pkg} className="flex items-center gap-1 bg-amber-50 border border-amber-200 rounded px-2 py-1">
                                      <code className="text-amber-800">{pkg}</code>
                                      <button onClick={() => handleInstall(pkg)}
                                        className="text-purple-600 hover:text-purple-800 text-[11px] font-medium ml-1">安裝</button>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            ) : (
                              <p className="text-gray-400">
                                {deps.python && deps.python.suggested_pip.length === 0
                                  ? (deps.python.has_manifest === false
                                      ? '此 skill 未用 requirements.txt 宣告依賴;缺套件會在執行時自動偵測補裝'
                                      : '無外部依賴（只用標準庫）')
                                  : '✓ 所有依賴已安裝'}
                              </p>
                            )}
                            {deps.python && deps.python.installed.length > 0 && (
                              <p className="mt-1 text-green-600">已安裝：{deps.python.installed.join(', ')}</p>
                            )}
                            {deps.python && deps.python.undeclared_imports && deps.python.undeclared_imports.length > 0 && (
                              <p className="mt-1 text-gray-400 text-[11px] leading-relaxed">
                                偵測到程式 import(未在 requirements.txt 宣告、<b>不提供一鍵安裝</b>,可能含本地模組/誤判,僅供參考):
                                {' '}{deps.python.undeclared_imports.join('、')}
                              </p>
                            )}
                          </div>

                          {/* Node 依賴 */}
                          <div>
                            <div className="font-medium text-gray-700 mb-1.5">📦 Node.js 依賴</div>
                            {deps.node?.package_json ? (
                              <div className="bg-red-50 border border-red-200 rounded px-2 py-2">
                                <p className="text-red-700 font-medium mb-1">此 Skill 有 package.json，需要手動執行：</p>
                                <code className="block bg-white px-2 py-1 rounded text-red-800">cd "{deps.path}" && npm install</code>
                                {deps.node.package_json.dependencies && (
                                  <div className="mt-2">
                                    <span className="text-gray-500">dependencies：</span>
                                    <div className="mt-0.5 flex flex-wrap gap-1">
                                      {Object.entries(deps.node.package_json.dependencies).map(([k, v]) => (
                                        <code key={k} className="px-1.5 py-0.5 bg-white border border-gray-200 rounded text-gray-700">{k}@{v}</code>
                                      ))}
                                    </div>
                                  </div>
                                )}
                              </div>
                            ) : deps.node?.suggested_npm && deps.node.suggested_npm.length > 0 ? (
                              <div className="space-y-1.5">
                                {deps.node.npm_available === false ? (
                                  <div className="bg-gray-50 border border-gray-200 rounded px-2 py-2 text-gray-600">
                                    <p className="mb-1">SKILL.md 提及需要以下 npm 套件，但系統找不到 <code className="bg-white px-1 rounded">npm</code>，無法判斷是否已安裝：</p>
                                    <div className="flex flex-wrap gap-1">
                                      {deps.node.suggested_npm.map(pkg => (
                                        <code key={pkg} className="px-1.5 py-0.5 bg-white border border-gray-200 rounded">{pkg}</code>
                                      ))}
                                    </div>
                                  </div>
                                ) : (
                                  <>
                                    {deps.node.missing_npm && deps.node.missing_npm.length > 0 && (
                                      <div className="bg-amber-50 border border-amber-200 rounded px-2 py-2">
                                        <p className="text-amber-800 mb-1">
                                          尚未安裝
                                          <span className="ml-1 text-[11px] text-amber-700">
                                            （目前 {currentMode === 'sandbox' ? '🛡 沙盒容器' : '💻 本機'} 模式、複製此指令到終端執行）：
                                          </span>
                                        </p>
                                        <code className="block bg-white px-2 py-1 rounded text-amber-900 text-[11px] mb-1.5 break-all">
                                          {npmInstallCmd(deps.node.missing_npm)}
                                        </code>
                                        <div className="flex flex-wrap gap-1">
                                          {deps.node.missing_npm.map(pkg => (
                                            <code key={pkg} className="px-1.5 py-0.5 bg-white border border-amber-200 rounded text-amber-900">{pkg}</code>
                                          ))}
                                        </div>
                                      </div>
                                    )}
                                    {deps.node.installed_npm && deps.node.installed_npm.length > 0 && (
                                      <p className="text-green-600">✓ 已安裝（npm -g）：{deps.node.installed_npm.join(', ')}</p>
                                    )}
                                    {(!deps.node.missing_npm || deps.node.missing_npm.length === 0) &&
                                     (!deps.node.installed_npm || deps.node.installed_npm.length === 0) && (
                                      <p className="text-gray-400">無 Node.js 依賴</p>
                                    )}
                                  </>
                                )}
                              </div>
                            ) : (
                              <p className="text-gray-400">無 Node.js 依賴</p>
                            )}
                          </div>

                          {/* 系統工具 */}
                          {deps.system_tools && deps.system_tools.length > 0 && (
                            <div>
                              <div className="font-medium text-gray-700 mb-1.5">🛠 系統工具</div>
                              <div className="flex flex-wrap gap-1.5 mb-2">
                                {deps.system_tools.map(tool => (
                                  <span key={tool} className="px-2 py-0.5 bg-slate-100 border border-slate-300 rounded text-slate-700 text-[11px]">
                                    {tool}
                                  </span>
                                ))}
                              </div>
                              {currentMode === 'sandbox' ? (
                                <div className="bg-slate-50 border border-slate-200 rounded px-2 py-2 text-[11px]">
                                  <p className="text-slate-700 mb-1">
                                    🛡 沙盒模式：可在容器內用 <code className="bg-white px-1 rounded">apt-get</code> 安裝。複製此指令執行：
                                  </p>
                                  <code className="block bg-white px-2 py-1 rounded text-slate-800 break-all">
                                    {aptInstallCmd(deps.system_tools)}
                                  </code>
                                </div>
                              ) : (
                                <div className="text-[11px] text-gray-500 leading-relaxed">
                                  💻 本機模式：這些工具無法透過 pip / npm 安裝，需到官網或用系統套件管理器（winget / brew / apt）取得。
                                  <br />
                                  例：<code className="bg-gray-100 px-1 rounded">winget install LibreOffice.LibreOffice</code> /
                                  <code className="bg-gray-100 px-1 rounded ml-1">brew install poppler</code> /
                                  <code className="bg-gray-100 px-1 rounded ml-1">apt install pandoc</code>
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}


// ── Skill Packages Section ────────────────────────────────────────────────────
// ── Subagent 角色管理 ───────────────────────────────────────────────────────
// 內建 5 role 不可改不可刪、自訂可 CRUD
// 工具用 checkbox 多選 (done 永遠隱式 ON)
const ROLE_PROMPT_TEMPLATE = `你是 [角色職能描述]、負責 [主要工作]。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 最高優先級違規(連 2 輪犯規系統會強制終止 step):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 每一輪 reply 必須含 <tool>...</tool> 標籤(或 <tool>done</tool>)
2. reply 必須短(< 500 字),分析 / 結論 / 報告寫進 run_python 的 Path.write_text(),不要寫在 reply 裡
3. 產物必須寫到指定的 output_path、不寫檔 = step fail
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

任務原則:
- [這個 role 的核心邏輯]
- [工作流程順序]

工作流(每步要 tool):
1. <tool>...</tool> — [第一步做什麼]
2. <tool>...</tool> — [第二步做什麼]
3. <tool>done</tool> — \`{"success": true, "summary": "..."}\`

停止條件:
- [何時 done]
- 達 max_iter 上限

回繁體中文、reply 永遠短。`

interface RoleFormState {
  role_id: string
  label: string
  description: string
  tools: string[]
  system_prompt: string
}

function emptyRoleForm(): RoleFormState {
  return { role_id: '', label: '', description: '', tools: [], system_prompt: '' }
}

function SubagentRolesSection() {
  const [roles, setRoles] = useState<SubagentRole[]>([])
  const [selectableTools, setSelectableTools] = useState<SelectableTool[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null) // null = 新增、有值 = 編輯該 id
  const [form, setForm] = useState<RoleFormState>(emptyRoleForm())
  const [saving, setSaving] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [expandedSP, setExpandedSP] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const r = await listSubagentRoles()
      setRoles(r.roles)
      setSelectableTools(r.selectable_tools)
    } catch (e) {
      toast.error('載入 subagent role 失敗:' + (e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const startCreate = () => {
    setEditingId(null)
    setForm(emptyRoleForm())
    setShowForm(true)
  }

  const startEdit = (r: SubagentRole) => {
    if (r.is_builtin) return
    setEditingId(r.role_id)
    setForm({
      role_id: r.role_id,
      label: r.label,
      description: r.description,
      tools: r.tools.filter(t => t !== 'done'),
      system_prompt: r.system_prompt,
    })
    setShowForm(true)
  }

  const cancelForm = () => {
    setShowForm(false)
    setEditingId(null)
    setForm(emptyRoleForm())
  }

  const toggleTool = (t: string) => {
    setForm(prev => ({
      ...prev,
      tools: prev.tools.includes(t)
        ? prev.tools.filter(x => x !== t)
        : [...prev.tools, t],
    }))
  }

  const applyTemplate = () => {
    if (form.system_prompt.trim() && !confirm('已有內容、要替換成範本?')) return
    setForm(prev => ({ ...prev, system_prompt: ROLE_PROMPT_TEMPLATE }))
  }

  const submit = async () => {
    if (saving) return
    setSaving(true)
    try {
      const payload: SubagentRolePayload = {
        role_id: form.role_id.trim(),
        label: form.label.trim(),
        description: form.description.trim(),
        tools: form.tools,
        system_prompt: form.system_prompt,
      }
      if (editingId) {
        await updateSubagentRole(editingId, payload)
        toast.success(`已更新自訂角色 '${editingId}'`)
      } else {
        await createSubagentRole(payload)
        toast.success(`已新增自訂角色 '${payload.role_id}'`)
      }
      cancelForm()
      await load()
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const onDelete = async (r: SubagentRole) => {
    if (r.is_builtin) return
    if (!confirm(`確定刪除自訂角色 '${r.role_id}' (${r.label})?`)) return
    setDeletingId(r.role_id)
    try {
      await deleteSubagentRole(r.role_id)
      toast.success(`已刪除 '${r.role_id}'`)
      await load()
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setDeletingId(null)
    }
  }

  const builtins = roles.filter(r => r.is_builtin)
  const customs = roles.filter(r => !r.is_builtin)

  return (
    <div className="bg-white rounded-2xl p-6 shadow-sm border border-gray-200 mb-4">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Users className="w-4 h-4 text-indigo-600" />
          <h2 className="text-base font-semibold text-gray-900">Subagent 角色管理</h2>
        </div>
        {!showForm && (
          <button
            onClick={startCreate}
            className="inline-flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg bg-indigo-600 text-white hover:bg-indigo-700"
          >
            <Plus className="w-3.5 h-3.5" /> 新增角色
          </button>
        )}
      </div>

      <p className="text-xs text-gray-500 mb-3">
        Subagent 節點用「角色」決定 system prompt + 可用工具。
        內建 5 個不可改;自訂角色 AI 助手呼叫 <code className="bg-gray-100 px-1 rounded">create_subagent_role</code> 也會出現在這。
      </p>

      {/* 新增 / 編輯表單 */}
      {showForm && (
        <div className="mb-4 p-4 rounded-xl border-2 border-indigo-300 bg-indigo-50/30">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-indigo-900">
              {editingId ? `編輯角色 '${editingId}'` : '新增自訂角色'}
            </h3>
            <button onClick={cancelForm} className="text-gray-400 hover:text-gray-700">
              <XIcon className="w-4 h-4" />
            </button>
          </div>
          <div className="space-y-3">
            <div>
              <label className="text-xs font-medium text-gray-600 mb-1 block">
                Role ID <span className="text-red-500">*</span>
                <span className="text-gray-400 ml-1">(英文 snake_case、不可改、yaml 鍵)</span>
              </label>
              <input
                value={form.role_id}
                onChange={(e) => setForm({ ...form, role_id: e.target.value })}
                placeholder="例: boss, legal_reviewer, supervisor"
                disabled={!!editingId}
                className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm font-mono disabled:bg-gray-100"
              />
            </div>
            <div>
              <label className="text-xs font-medium text-gray-600 mb-1 block">
                中文顯示名 <span className="text-red-500">*</span>
                <span className="text-gray-400 ml-1">(畫布上看到的)</span>
              </label>
              <input
                value={form.label}
                onChange={(e) => setForm({ ...form, label: e.target.value })}
                placeholder="例: 主管, 法務審稿員"
                className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm"
              />
            </div>
            <div>
              <label className="text-xs font-medium text-gray-600 mb-1 block">
                用途描述 <span className="text-red-500">*</span>
                <span className="text-gray-400 ml-1">(一句話、出現在工具提示)</span>
              </label>
              <input
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="例: 審視員工方案、做最終決策"
                className="w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm"
              />
            </div>
            <div>
              <label className="text-xs font-medium text-gray-600 mb-1 block">
                可用工具 <span className="text-gray-400 ml-1">(done 永遠 ON、不顯示)</span>
              </label>
              <div className="space-y-1.5 bg-white rounded-lg border border-gray-200 p-3">
                {selectableTools.map(t => (
                  <label key={t.id} className="flex items-start gap-2 cursor-pointer hover:bg-gray-50 -mx-1 px-1 py-0.5 rounded">
                    <input
                      type="checkbox"
                      checked={form.tools.includes(t.id)}
                      onChange={() => toggleTool(t.id)}
                      className="mt-0.5"
                    />
                    <div className="flex-1 min-w-0">
                      <div className="text-xs font-mono text-gray-800">{t.id}</div>
                      <div className="text-[11px] text-gray-500">{t.description}</div>
                    </div>
                  </label>
                ))}
              </div>
            </div>
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs font-medium text-gray-600">
                  System Prompt <span className="text-red-500">*</span>
                  <span className="text-gray-400 ml-1">(role 看到的第一條指令、至少 30 字)</span>
                </label>
                <button
                  type="button"
                  onClick={applyTemplate}
                  className="text-[11px] px-2 py-0.5 rounded border border-indigo-200 text-indigo-700 hover:bg-indigo-50"
                >
                  使用範本
                </button>
              </div>
              <textarea
                value={form.system_prompt}
                onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
                placeholder="點「使用範本」可填入含『最高優先級違規規則』的起手範本、然後改成你要的角色"
                rows={12}
                className="w-full border border-gray-200 rounded-lg px-2.5 py-2 text-xs font-mono outline-none focus:border-indigo-400"
              />
              <p className="text-[11px] text-gray-500 mt-1">
                💡 強烈建議含「最高優先級違規規則」段落(避免 LLM 寫長 prose 不呼工具的死循環)。
              </p>
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <button
                onClick={cancelForm}
                className="px-3 py-1.5 text-xs rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50"
              >
                取消
              </button>
              <button
                onClick={submit}
                disabled={saving}
                className="inline-flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                {saving && <Loader2 className="w-3 h-3 animate-spin" />}
                {editingId ? '儲存' : '建立'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 內建 role */}
      <div className="mb-3">
        <div className="text-[11px] uppercase font-semibold text-gray-400 mb-1.5 tracking-wider">內建角色(5 個、不可改不可刪)</div>
        {loading ? (
          <div className="text-xs text-gray-400 italic">載入中...</div>
        ) : (
          <div className="space-y-1.5">
            {builtins.map(r => (
              <div key={r.role_id} className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-mono text-gray-800">{r.role_id}</span>
                      <span className="text-xs text-gray-600">— {r.label}</span>
                    </div>
                    <div className="text-[11px] text-gray-500 mt-0.5">{r.description}</div>
                    <div className="text-[11px] text-gray-400 mt-0.5 font-mono truncate">tools: {r.tools.join(', ')}</div>
                  </div>
                  <button
                    onClick={() => setExpandedSP(expandedSP === r.role_id ? null : r.role_id)}
                    className="text-[11px] px-2 py-0.5 rounded border border-gray-200 text-gray-600 hover:bg-white"
                  >
                    {expandedSP === r.role_id ? '收起' : '看 prompt'}
                  </button>
                </div>
                {expandedSP === r.role_id && (
                  <pre className="mt-2 text-[10px] font-mono bg-white border border-gray-200 rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-60 overflow-y-auto">
                    {r.system_prompt}
                  </pre>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 自訂 role */}
      <div>
        <div className="text-[11px] uppercase font-semibold text-gray-400 mb-1.5 tracking-wider">
          自訂角色({customs.length} 個)
        </div>
        {customs.length === 0 ? (
          <div className="text-xs text-gray-400 italic py-2">
            尚無自訂角色。點上方「新增角色」、或讓 AI 助手用 <code className="bg-gray-100 px-1 rounded">create_subagent_role</code> 工具幫你建。
          </div>
        ) : (
          <div className="space-y-1.5">
            {customs.map(r => (
              <div key={r.role_id} className="rounded-lg border border-indigo-200 bg-indigo-50/30 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-mono text-indigo-900">{r.role_id}</span>
                      <span className="text-xs text-indigo-700">— {r.label}</span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700">自訂</span>
                    </div>
                    <div className="text-[11px] text-gray-600 mt-0.5">{r.description}</div>
                    <div className="text-[11px] text-gray-400 mt-0.5 font-mono truncate">tools: {r.tools.join(', ')}</div>
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => setExpandedSP(expandedSP === r.role_id ? null : r.role_id)}
                      className="text-[11px] px-2 py-0.5 rounded border border-gray-200 text-gray-600 hover:bg-white"
                    >
                      {expandedSP === r.role_id ? '收起' : '看 prompt'}
                    </button>
                    <button
                      onClick={() => startEdit(r)}
                      className="p-1 text-indigo-600 hover:bg-indigo-100 rounded"
                      title="編輯"
                    >
                      <Edit2 className="w-3.5 h-3.5" />
                    </button>
                    <button
                      onClick={() => onDelete(r)}
                      disabled={deletingId === r.role_id}
                      className="p-1 text-red-600 hover:bg-red-100 rounded disabled:opacity-50"
                      title="刪除"
                    >
                      {deletingId === r.role_id
                        ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        : <Trash2 className="w-3.5 h-3.5" />}
                    </button>
                  </div>
                </div>
                {expandedSP === r.role_id && (
                  <pre className="mt-2 text-[10px] font-mono bg-white border border-gray-200 rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-60 overflow-y-auto">
                    {r.system_prompt}
                  </pre>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}


function SkillPackagesSection() {
  const [packages, setPackages] = useState<SkillPackage[]>([])
  const [loading, setLoading] = useState(true)
  const [newPkg, setNewPkg] = useState('')
  const [installing, setInstalling] = useState(false)
  const [removingPkg, setRemovingPkg] = useState<string | null>(null)
  const [analyzing, setAnalyzing] = useState(false)
  const [suggestions, setSuggestions] = useState<LogSuggestion[]>([])
  const [analyzedCount, setAnalyzedCount] = useState(0)
  const [logCount, setLogCount] = useState(5)
  const [scanningVenv, setScanningVenv] = useState(false)
  const [unlisted, setUnlisted] = useState<UnlistedPackage[]>([])
  const [adopting, setAdopting] = useState<string | null>(null)

  // 套件管理跟著當前 skill_sandbox_mode 走（host / sandbox）
  // 這個 target 是後端 resolve 後告訴我們實際操作的是哪邊，供 UI 顯示
  const [currentTarget, setCurrentTarget] = useState<'host' | 'sandbox'>('host')

  const loadPkgs = async () => {
    setLoading(true)
    try {
      const resp = await getSkillPackages('auto')
      setPackages(resp.packages)
      setCurrentTarget(resp.target)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadPkgs() }, [])

  // 監聽沙盒 toggle 改變事件，自動 reload（由 SandboxSection 觸發）
  useEffect(() => {
    const handler = () => { loadPkgs() }
    window.addEventListener('sandbox-mode-changed', handler)
    return () => window.removeEventListener('sandbox-mode-changed', handler)
  }, [])

  const handleAdd = async () => {
    const name = newPkg.trim()
    if (!name) return
    setInstalling(true)
    try {
      const { message } = await addSkillPackage(name, 'auto')
      toast.success(message)
      setNewPkg('')
      await loadPkgs()
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setInstalling(false)
    }
  }

  const handleRemove = async (name: string) => {
    setRemovingPkg(name)
    try {
      const { message } = await removeSkillPackage(name, 'auto')
      toast.success(message)
      await loadPkgs()
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setRemovingPkg(null)
    }
  }

  const handleAnalyze = async () => {
    setAnalyzing(true)
    try {
      const result = await analyzeRecentLogs(logCount)
      setSuggestions(result.suggestions)
      setAnalyzedCount(result.analyzed)
      if (result.suggestions.length === 0) toast.info(`已分析 ${result.analyzed} 筆 log，未發現缺少的套件`)
      else toast.success(`發現 ${result.suggestions.length} 個建議安裝的套件`)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setAnalyzing(false)
    }
  }

  const handleInstallSuggestion = async (pipName: string) => {
    setInstalling(true)
    try {
      const { message } = await addSkillPackage(pipName, 'auto')
      toast.success(message)
      setSuggestions(prev => prev.filter(s => s.pip_name !== pipName))
      await loadPkgs()
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setInstalling(false)
    }
  }

  const handleScanVenv = async () => {
    setScanningVenv(true)
    try {
      const pkgs = await scanUnlistedPackages()
      setUnlisted(pkgs)
      if (pkgs.length === 0) toast.info('venv 已完全同步，無未納管套件')
      else toast.success(`發現 ${pkgs.length} 個已裝但未納管的套件`)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setScanningVenv(false)
    }
  }

  const handleAdopt = async (name: string) => {
    setAdopting(name)
    try {
      const msg = await adoptExistingPackage(name)
      toast.success(msg)
      setUnlisted(prev => prev.filter(p => p.name !== name))
      await loadPkgs()
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setAdopting(null)
    }
  }

  const handleAdoptAll = async () => {
    let ok = 0, fail = 0
    for (const p of unlisted) {
      try {
        await adoptExistingPackage(p.name)
        ok++
      } catch { fail++ }
    }
    toast.success(`已納管 ${ok} 個套件${fail ? `（${fail} 個失敗）` : ''}`)
    setUnlisted([])
    await loadPkgs()
  }

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-purple-100 flex items-center justify-center">
          <Package className="w-5 h-5 text-purple-700" />
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
            AI技能套件
            <span className={cn(
              'text-xs px-2 py-0.5 rounded-full font-medium',
              currentTarget === 'sandbox'
                ? 'bg-indigo-50 text-indigo-700 border border-indigo-200'
                : 'bg-gray-100 text-gray-600 border border-gray-200'
            )}>
              {currentTarget === 'sandbox' ? '🛡 沙盒容器' : '💻 本機 venv'}
            </span>
          </h2>
          <p className="text-sm text-gray-500">
            {currentTarget === 'sandbox'
              ? '目前管理：atlas-sandbox 容器（切換到本機模式會改管 Host venv）'
              : '目前管理：Host Python venv（切換到沙盒模式會改管容器）'}
          </p>
        </div>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {/* 新增套件 */}
        <div className="p-4 border-b border-gray-100">
          <div className="flex gap-2">
            <input
              value={newPkg}
              onChange={e => setNewPkg(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleAdd()}
              placeholder="輸入套件名稱（如 selenium、numpy）"
              disabled={installing}
              className="flex-1 px-3 py-2 border border-gray-200 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-purple-500 focus:border-transparent"
            />
            <button
              onClick={handleAdd}
              disabled={installing || !newPkg.trim()}
              className={cn(
                'px-4 py-2 rounded-lg text-sm font-medium flex items-center gap-1.5 transition-all',
                installing || !newPkg.trim()
                  ? 'bg-gray-200 text-gray-400 cursor-not-allowed'
                  : 'bg-purple-600 text-white hover:bg-purple-700'
              )}
            >
              {installing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
              安裝
            </button>
          </div>
          <p className="text-xs text-gray-400 mt-2">套件會安裝到後端的 Python 環境中，AI技能節點執行時可直接 import 使用</p>
        </div>

        {/* 套件清單 */}
        <div className="divide-y divide-gray-100">
          {loading ? (
            <div className="p-6 text-center text-gray-400 text-sm">
              <RefreshCw className="w-4 h-4 animate-spin inline-block mr-2" />
              載入中...
            </div>
          ) : packages.length === 0 ? (
            <div className="p-6 text-center text-gray-400 text-sm">尚無套件</div>
          ) : (
            packages.map(pkg => (
              <div key={pkg.name} className="flex items-center gap-3 px-4 py-3 hover:bg-gray-50 transition-colors">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-mono font-medium text-gray-900">{pkg.name}</span>
                    {pkg.installed ? (
                      <span className="text-xs px-1.5 py-0.5 rounded-full bg-green-100 text-green-700 font-medium">
                        {pkg.version || '已安裝'}
                      </span>
                    ) : (
                      <span className="text-xs px-1.5 py-0.5 rounded-full bg-red-100 text-red-600 font-medium">
                        未安裝
                      </span>
                    )}
                  </div>
                </div>
                <button
                  onClick={() => handleRemove(pkg.name)}
                  disabled={removingPkg === pkg.name}
                  className="p-1.5 text-gray-300 hover:text-red-500 transition-colors disabled:opacity-50"
                  title="移除套件"
                >
                  {removingPkg === pkg.name
                    ? <Loader2 className="w-4 h-4 animate-spin" />
                    : <Trash2 className="w-4 h-4" />}
                </button>
              </div>
            ))
          )}
        </div>

        {/* Log 分析 */}
        <div className="p-4 border-t border-gray-100">
          <div className="flex items-center gap-2 mb-2">
            <Search className="w-4 h-4 text-purple-500" />
            <span className="text-sm font-medium text-gray-700">從執行紀錄分析缺少的套件</span>
          </div>
          <div className="flex items-center gap-2">
            <select value={logCount} onChange={e => setLogCount(Number(e.target.value))}
              className="border border-gray-200 rounded-lg px-2 py-1.5 text-sm bg-white text-gray-700 outline-none focus:border-purple-400 cursor-pointer">
              <option value={3}>最近 3 筆</option>
              <option value={5}>最近 5 筆</option>
              <option value={10}>最近 10 筆</option>
            </select>
            <button onClick={handleAnalyze} disabled={analyzing}
              className={cn(
                'px-3 py-1.5 rounded-lg text-sm font-medium flex items-center gap-1.5 transition-all',
                analyzing ? 'bg-gray-200 text-gray-400 cursor-not-allowed' : 'bg-purple-600 text-white hover:bg-purple-700'
              )}>
              {analyzing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Search className="w-3.5 h-3.5" />}
              分析 Log
            </button>
          </div>

          {suggestions.length > 0 && (
            <div className="mt-3 space-y-2">
              <p className="text-xs text-gray-500">已分析 {analyzedCount} 筆紀錄，建議安裝以下套件：</p>
              {suggestions.map(s => (
                <div key={s.module} className="flex items-center gap-2 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                  <AlertCircle className="w-4 h-4 text-amber-500 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <span className="text-sm font-mono font-medium text-gray-800">{s.pip_name}</span>
                    {s.pip_name !== s.module && <span className="text-xs text-gray-400 ml-1.5">（import {s.module}）</span>}
                    <p className="text-xs text-gray-400 truncate">出現在：{s.found_in.join(', ')}</p>
                  </div>
                  <button onClick={() => handleInstallSuggestion(s.pip_name)} disabled={installing}
                    className="px-2.5 py-1 bg-purple-600 text-white rounded-lg text-xs font-medium hover:bg-purple-700 disabled:opacity-50 whitespace-nowrap">
                    安裝
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Venv 同步：納管已手動裝但不在清單的套件 */}
        <div className="p-4 border-t border-gray-100">
          <div className="flex items-center gap-2 mb-2">
            <RefreshCw className="w-4 h-4 text-purple-500" />
            <span className="text-sm font-medium text-gray-700">同步 venv：納管終端機手動安裝的套件</span>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={handleScanVenv} disabled={scanningVenv}
              className={cn(
                'px-3 py-1.5 rounded-lg text-sm font-medium flex items-center gap-1.5 transition-all',
                scanningVenv ? 'bg-gray-200 text-gray-400 cursor-not-allowed' : 'bg-purple-600 text-white hover:bg-purple-700'
              )}>
              {scanningVenv ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Search className="w-3.5 h-3.5" />}
              掃描 venv
            </button>
            {unlisted.length > 0 && (
              <button onClick={handleAdoptAll}
                className="px-3 py-1.5 rounded-lg text-sm font-medium bg-emerald-600 text-white hover:bg-emerald-700">
                全部納管（{unlisted.length}）
              </button>
            )}
          </div>

          {unlisted.length > 0 && (
            <div className="mt-3 space-y-1.5">
              <p className="text-xs text-gray-500">以下是已安裝但未納管的頂層套件（排除後端 requirements.txt）：</p>
              {unlisted.map(p => (
                <div key={p.name} className="flex items-center gap-2 bg-blue-50 border border-blue-200 rounded-lg px-3 py-2">
                  <Package className="w-4 h-4 text-blue-500 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <span className="text-sm font-mono font-medium text-gray-800">{p.name}</span>
                    {p.version && <span className="text-xs text-gray-400 ml-2">{p.version}</span>}
                  </div>
                  <button onClick={() => handleAdopt(p.name)} disabled={adopting === p.name}
                    className="px-2.5 py-1 bg-purple-600 text-white rounded-lg text-xs font-medium hover:bg-purple-700 disabled:opacity-50 whitespace-nowrap">
                    {adopting === p.name ? <Loader2 className="w-3 h-3 animate-spin" /> : '納管'}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 底部說明 */}
        <div className="px-4 py-3 bg-gray-50 border-t border-gray-100">
          <p className="text-xs text-gray-500">
            套件清單儲存在 <code className="font-mono bg-gray-100 px-1 py-0.5 rounded">backend/skill_packages.txt</code>，後端啟動時自動安裝缺少的套件
          </p>
        </div>
      </div>
    </div>
  )
}

// ── Notification Settings Section ────────────────────────────────────────────
function NotificationSection() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [tgToken, setTgToken] = useState('')
  const [tgChatId, setTgChatId] = useState('')
  // 遠端遙控：開啟後 bot 接受 /menu 指令、可從 TG 啟動工作流。預設 OFF（安全）
  const [tgRemoteControl, setTgRemoteControl] = useState(false)
  // .env fallback 旗標 — UI 沒填但 .env 有值時，仍應允許啟用遠端遙控
  const [tgTokenEnv, setTgTokenEnv] = useState(false)
  const [tgChatIdEnv, setTgChatIdEnv] = useState(false)
  const [lineToken, setLineToken] = useState('')
  const [original, setOriginal] = useState<NotificationSettings | null>(null)

  useEffect(() => {
    (async () => {
      setLoading(true)
      try {
        const s = await getNotificationSettings()
        setTgToken(s.telegram_bot_token)
        setTgChatId(s.telegram_chat_id)
        setTgRemoteControl(!!s.telegram_remote_control)
        setTgTokenEnv(!!s.telegram_bot_token_env_present)
        setTgChatIdEnv(!!s.telegram_chat_id_env_present)
        setLineToken(s.line_notify_token)
        setOriginal(s)
      } catch (e) { toast.error((e as Error).message) }
      finally { setLoading(false) }
    })()
  }, [])

  const dirty = original && (
    tgToken !== original.telegram_bot_token ||
    tgChatId !== original.telegram_chat_id ||
    tgRemoteControl !== !!original.telegram_remote_control ||
    lineToken !== original.line_notify_token
  )

  const handleSave = async () => {
    setSaving(true)
    try {
      const saved = await saveNotificationSettings({
        telegram_bot_token: tgToken,
        telegram_chat_id: tgChatId,
        telegram_remote_control: tgRemoteControl,
        line_notify_token: lineToken,
      })
      setOriginal(saved)
      toast.success('通知設定已儲存')
    } catch (e) { toast.error((e as Error).message) }
    finally { setSaving(false) }
  }

  const inputCls = 'flex-1 px-3 py-2 border border-gray-200 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-transparent'

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-emerald-100 flex items-center justify-center">
          <Bell className="w-5 h-5 text-emerald-700" />
        </div>
        <div>
          <h2 className="text-lg font-semibold text-gray-900">通知設定</h2>
          <p className="text-sm text-gray-500">設定 Pipeline 通知管道（人工確認節點、失敗通知）</p>
        </div>
      </div>

      {loading ? (
        <div className="bg-white rounded-xl border border-gray-200 p-8 text-center text-gray-400">
          <RefreshCw className="w-5 h-5 animate-spin inline-block mr-2" />載入中...
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
          {/* Telegram */}
          <div className="p-5 border-b border-gray-100">
            <div className="flex items-center gap-2 mb-3">
              <MessageSquare className="w-4 h-4 text-blue-500" />
              <span className="text-sm font-semibold text-gray-800">Telegram Bot</span>
            </div>
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-gray-500 mb-1">Bot Token</label>
                <input
                  type="password"
                  value={tgToken}
                  onChange={e => setTgToken(e.target.value)}
                  placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
                  className={inputCls + ' w-full'}
                />
                <p className="text-xs text-gray-400 mt-1">從 <code className="bg-gray-100 px-1 py-0.5 rounded">@BotFather</code> 取得</p>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-500 mb-1">Chat ID</label>
                <input
                  value={tgChatId}
                  onChange={e => setTgChatId(e.target.value)}
                  placeholder="123456789"
                  className={inputCls + ' w-full'}
                />
                <p className="text-xs text-gray-400 mt-1">你的 Telegram 用戶 ID 或群組 ID，可透過 <code className="bg-gray-100 px-1 py-0.5 rounded">@userinfobot</code> 取得</p>
              </div>

              {/* 遠端遙控 toggle — 預設 OFF。開啟後 bot 接受 /menu 指令啟動工作流 */}
              {/* 啟用條件：UI 有填 OR .env 有 fallback 值，兩種來源任一即可 */}
              {(() => {
                const tokenReady = !!tgToken || tgTokenEnv
                const chatReady = !!tgChatId || tgChatIdEnv
                const canEnable = tokenReady && chatReady
                const usingEnvOnly = (!tgToken && tgTokenEnv) || (!tgChatId && tgChatIdEnv)
                return (
                  <div className="pt-3 border-t border-gray-100">
                    <label className="flex items-start gap-2 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={tgRemoteControl}
                        onChange={e => setTgRemoteControl(e.target.checked)}
                        disabled={!canEnable}
                        className="mt-1"
                      />
                      <div className="flex-1">
                        <div className="text-sm font-medium text-gray-800">📲 啟用 Telegram 遠端遙控</div>
                        <p className="text-xs text-gray-500 mt-1 leading-relaxed">
                          開啟後從 Telegram 傳 <code className="bg-gray-100 px-1 py-0.5 rounded font-mono">/menu</code> 給 bot、會回工作流清單、點按鈕即可啟動並接收進度通知。<br />
                          支援指令：<code className="bg-gray-100 px-1 py-0.5 rounded font-mono">/menu</code>（列工作流）、<code className="bg-gray-100 px-1 py-0.5 rounded font-mono">/status</code>（查執行中 run）、<code className="bg-gray-100 px-1 py-0.5 rounded font-mono">/help</code>（指令表）。<br />
                          <span className="text-amber-700">⚠ 安全考量：只接受授權 Chat ID 的訊息，其他人 DM 會被忽略。</span>
                          {!canEnable && <span className="text-red-500 block mt-1">需先填好 Bot Token + Chat ID（UI 或 .env 任一來源）才能開啟。</span>}
                          {canEnable && usingEnvOnly && <span className="text-emerald-700 block mt-1">✓ 偵測到 .env 已設定 Bot Token / Chat ID，可直接勾選啟用。</span>}
                        </p>
                      </div>
                    </label>
                  </div>
                )
              })()}
            </div>
          </div>

          {/* LINE（預留） */}
          <div className="p-5 border-b border-gray-100 opacity-60">
            <div className="flex items-center gap-2 mb-3">
              <span className="text-lg leading-none">🟢</span>
              <span className="text-sm font-semibold text-gray-800">LINE Notify</span>
              <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 font-medium">即將推出</span>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Access Token</label>
              <input
                value={lineToken}
                onChange={e => setLineToken(e.target.value)}
                placeholder="尚未支援，敬請期待"
                disabled
                className={inputCls + ' w-full bg-gray-50 cursor-not-allowed'}
              />
              <p className="text-xs text-gray-400 mt-1">LINE Notify 整合開發中</p>
            </div>
          </div>

          {/* 儲存按鈕 */}
          <div className="px-5 py-4 bg-gray-50/50 flex items-center justify-between">
            <div className="text-xs text-gray-500">
              {dirty ? '有未儲存的變更' : '尚無變更'}
            </div>
            <button
              onClick={handleSave}
              disabled={saving || !dirty}
              className={cn(
                'px-4 py-2 rounded-lg text-sm font-medium flex items-center gap-2 transition-all',
                dirty && !saving
                  ? 'bg-emerald-600 text-white hover:bg-emerald-700'
                  : 'bg-gray-200 text-gray-400 cursor-not-allowed'
              )}
            >
              {saving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              儲存通知設定
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Web Search Section (Tavily) ───────────────────────────────────────────────
// 跟 NotificationSection 一樣 pattern：password input + toggle + save
// 後端不回 key 明文只回 has_key flag；使用者要改必須重新輸入（避免無意義的刷新）
function WebSearchSection() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [apiKey, setApiKey] = useState('')           // 空 = 未動（不會送到後端）
  const [hasKey, setHasKey] = useState(false)
  const [enabled, setEnabled] = useState(false)
  const [verbose, setVerbose] = useState(false)
  const [origHasKey, setOrigHasKey] = useState(false)
  const [origEnabled, setOrigEnabled] = useState(false)
  const [origVerbose, setOrigVerbose] = useState(false)
  // 搜尋深度預設(advanced vs basic);Atlas 預設 true(深度模式)。
  const [deepMode, setDeepMode] = useState(true)
  const [origDeepMode, setOrigDeepMode] = useState(true)

  useEffect(() => {
    (async () => {
      setLoading(true)
      try {
        const s = await getWebSearchSettings()
        setHasKey(s.has_key); setOrigHasKey(s.has_key)
        setEnabled(s.web_search_enabled); setOrigEnabled(s.web_search_enabled)
        setVerbose(s.web_search_full_content_default); setOrigVerbose(s.web_search_full_content_default)
        setDeepMode(s.web_search_deep_default); setOrigDeepMode(s.web_search_deep_default)
      } catch (e) { toast.error((e as Error).message) }
      finally { setLoading(false) }
    })()
  }, [])

  const dirty =
    apiKey.length > 0 ||            // 使用者輸入了新 key
    enabled !== origEnabled ||
    verbose !== origVerbose ||
    deepMode !== origDeepMode

  const handleSave = async () => {
    setSaving(true)
    try {
      const patch: WebSearchSettingsInput = {
        web_search_enabled: enabled,
        web_search_full_content_default: verbose,
        web_search_deep_default: deepMode,
      }
      if (apiKey.trim()) patch.tavily_api_key = apiKey.trim()
      const saved = await saveWebSearchSettings(patch)
      setHasKey(saved.has_key); setOrigHasKey(saved.has_key)
      setOrigEnabled(saved.web_search_enabled)
      setOrigVerbose(saved.web_search_full_content_default)
      setOrigDeepMode(saved.web_search_deep_default)
      setApiKey('')  // 儲存完清空輸入，避免使用者以為要重填
      toast.success('網路搜尋設定已儲存')
    } catch (e) { toast.error((e as Error).message) }
    finally { setSaving(false) }
  }

  const handleClearKey = async () => {
    if (!confirm('確定要移除 Tavily API Key？移除後搜尋功能會停用。')) return
    setSaving(true)
    try {
      const saved = await saveWebSearchSettings({ tavily_api_key: '', web_search_enabled: false })
      setHasKey(saved.has_key); setOrigHasKey(saved.has_key)
      setEnabled(saved.web_search_enabled); setOrigEnabled(saved.web_search_enabled)
      setApiKey('')
      toast.success('已移除 API Key')
    } catch (e) { toast.error((e as Error).message) }
    finally { setSaving(false) }
  }

  const inputCls = 'flex-1 px-3 py-2 border border-gray-200 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-transparent'

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-cyan-100 flex items-center justify-center">
          <Search className="w-5 h-5 text-cyan-700" />
        </div>
        <div>
          <h2 className="text-lg font-semibold text-gray-900">網路搜尋（Tavily）</h2>
          <p className="text-sm text-gray-500">啟用後 AI 技能節點會多一個 <code className="font-mono bg-gray-100 px-1 py-0.5 rounded">web_search</code> 工具，可在需要時查網</p>
        </div>
      </div>
      {loading ? (
        <div className="bg-white rounded-xl border border-gray-200 p-8 text-center text-gray-400">
          <RefreshCw className="w-5 h-5 animate-spin inline-block mr-2" />載入中...
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
          <div className="p-5 space-y-4">
            {/* API Key */}
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">
                Tavily API Key
                {hasKey && <span className="ml-2 text-emerald-600 text-[11px] font-normal">● 已設定（留空=不動）</span>}
              </label>
              <div className="flex gap-2">
                <input
                  type="password"
                  value={apiKey}
                  onChange={e => setApiKey(e.target.value)}
                  placeholder={hasKey ? '已設定，留空不動；要改請輸入新 key' : 'tvly-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'}
                  className={inputCls}
                />
                {hasKey && (
                  <button
                    onClick={handleClearKey}
                    disabled={saving}
                    className="px-3 py-2 text-xs text-red-600 border border-red-200 rounded-lg hover:bg-red-50 disabled:opacity-50"
                  >清除</button>
                )}
              </div>
              <p className="text-xs text-gray-400 mt-1">
                在 <a href="https://tavily.com/" target="_blank" rel="noopener noreferrer" className="text-cyan-600 hover:underline">tavily.com</a> 申請免費 key（每月有免費額度）
              </p>
            </div>

            {/* 啟用 toggle */}
            <div className="flex items-center justify-between pt-3 border-t border-gray-100">
              <div>
                <div className="text-sm font-medium text-gray-800">啟用網路搜尋</div>
                <div className="text-xs text-gray-500 mt-0.5">
                  {enabled ? '已啟用 — AI 可視需要呼叫 web_search' : '未啟用 — AI 看不到 web_search 工具'}
                </div>
              </div>
              <button
                onClick={() => setEnabled(!enabled)}
                disabled={!hasKey && !apiKey.trim()}
                className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${
                  enabled ? 'bg-cyan-500' : 'bg-gray-300'
                } ${(!hasKey && !apiKey.trim()) ? 'opacity-40 cursor-not-allowed' : ''}`}
                title={(!hasKey && !apiKey.trim()) ? '請先設定 API Key' : ''}
              >
                <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
                  enabled ? 'translate-x-5' : 'translate-x-0'
                }`} />
              </button>
            </div>

            {/* 完整內容模式 toggle */}
            <div className="flex items-center justify-between pt-3 border-t border-gray-100">
              <div>
                <div className="text-sm font-medium text-gray-800">預設回傳完整內容</div>
                <div className="text-xs text-gray-500 mt-0.5">
                  {verbose
                    ? '已啟用 — 搜尋時由 Tavily 直接回傳每則文章完整原文（~15000 字元）'
                    : '未啟用 — 只回 answer + URL 清單（~500 字元），需要完整內容時再寫程式抓'}
                </div>
                <div className="text-[11px] text-amber-600 mt-1 font-medium">
                  ⚠️ 需要雲端大 context 模型（Gemini/GPT/Claude）；<br/>
                  本地 Ollama 小 context（8B 以下）不建議開，會塞爆 context
                </div>
                <div className="text-[11px] text-gray-500 mt-0.5">
                  💡 好處：AI 不用自己寫爬蟲，避免被 Cloudflare / 反爬封鎖
                </div>
              </div>
              <button
                onClick={() => setVerbose(!verbose)}
                className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${
                  verbose ? 'bg-cyan-500' : 'bg-gray-300'
                }`}
              >
                <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
                  verbose ? 'translate-x-5' : 'translate-x-0'
                }`} />
              </button>
            </div>

            {/* 深度搜尋模式 toggle */}
            <div className="flex items-center justify-between pt-3 border-t border-gray-100">
              <div>
                <div className="text-sm font-medium text-gray-800">深度搜尋模式</div>
                <div className="text-xs text-gray-500 mt-0.5">
                  {deepMode
                    ? '已啟用 — Tavily advanced mode、品質好 5-10x、AI 助手 / Skill / Subagent 全部受惠'
                    : '未啟用 — Tavily basic mode、簡單查詢用、研究類任務不建議'}
                </div>
                <div className="text-[11px] text-amber-600 mt-1 font-medium">
                  ⚠️ Tavily credit 用量 2x、但深度研究品質提升大;
                  Atlas 定位深度研究、預設 ON。
                </div>
                <div className="text-[11px] text-gray-500 mt-0.5">
                  💡 影響範圍:全域 — AI 助手 chat、TG bot、Skill 節點、Subagent 節點都會用此預設
                </div>
              </div>
              <button
                onClick={() => setDeepMode(!deepMode)}
                className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${
                  deepMode ? 'bg-cyan-500' : 'bg-gray-300'
                }`}
              >
                <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
                  deepMode ? 'translate-x-5' : 'translate-x-0'
                }`} />
              </button>
            </div>
          </div>

          {dirty && (
            <div className="px-5 py-3 bg-gray-50 border-t border-gray-100 flex justify-end">
              <button
                onClick={handleSave}
                disabled={saving}
                className="flex items-center gap-2 px-4 py-2 bg-cyan-600 text-white rounded-lg text-sm font-medium hover:bg-cyan-700 disabled:opacity-50"
              >
                <Save className="w-4 h-4" />
                {saving ? '儲存中...' : '儲存變更'}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}


// ── computer_use 自動縮視窗設定 ────────────────────────────────────────────────
function ComputerUseAutoMinimizeSection() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [toggling, setToggling] = useState(false)

  useEffect(() => {
    fetch('/api/backend/settings/auto-minimize-for-computer-use')
      .then(r => r.json())
      .then(d => setEnabled(!!d.enabled))
      .catch(() => setEnabled(false))
  }, [])

  const toggle = async () => {
    if (enabled === null || toggling) return
    setToggling(true)
    const next = !enabled
    try {
      const res = await fetch('/api/backend/settings/auto-minimize-for-computer-use', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setEnabled(!!data.enabled)
      toast.success(next ? '已啟用 — workflow 開跑會自動縮小視窗' : '已關閉')
    } catch (e) {
      toast.error('設定切換失敗')
    } finally {
      setToggling(false)
    }
  }

  if (enabled === null) return null

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-amber-100 flex items-center justify-center">
          <Shield className="w-5 h-5 text-amber-700" />
        </div>
        <div>
          <h2 className="text-lg font-semibold text-gray-900">桌面自動化執行體驗</h2>
          <p className="text-sm text-gray-500">含 computer_use 節點的工作流啟動時的視窗行為</p>
        </div>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <div className="p-5 flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-sm font-semibold text-gray-800">
                工作流啟動時自動縮小前景視窗
              </span>
            </div>
            <p className="text-xs text-gray-500 leading-relaxed">
              當你按執行 / 排程觸發一個含 <code className="bg-gray-100 px-1 rounded">computer_use</code> 節點的工作流時、
              自動把當下的前景視窗(通常就是 Atlas 瀏覽器)縮到最小、避免擋住自動化的目標 app。
              工作流結束(成功 / 失敗)後會自動還原。並發多個工作流時用 ref-count 處理、不會反覆 minimize/restore。
            </p>
          </div>
          <button
            onClick={toggle}
            disabled={toggling}
            className={cn(
              'relative w-12 h-7 rounded-full transition-colors shrink-0',
              enabled ? 'bg-amber-500' : 'bg-gray-300',
              toggling && 'opacity-50 cursor-not-allowed'
            )}
          >
            <span className={cn(
              'absolute top-0.5 left-0.5 w-6 h-6 rounded-full bg-white shadow transition-transform',
              enabled ? 'translate-x-5' : 'translate-x-0'
            )} />
          </button>
        </div>
        <div className="px-5 py-3 bg-gray-50/50 text-xs text-gray-500 space-y-1 border-t border-gray-100">
          <p>• 預設關閉、要用再開,純 Windows 平台有效(Mac/Linux backend 會自動跳過)</p>
          <p>• 只縮「當下前景視窗」、不會強制關使用者其他工作中的視窗</p>
          <p>• 失敗(找不到前景或 API 例外)會 log warning 不擋 workflow 執行</p>
        </div>
      </div>
    </div>
  )
}


// ── AI 助手長期記憶 Section ───────────────────────────────────────────────────
function MemorySection() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [aggressive, setAggressive] = useState(false)
  const [facts, setFacts] = useState<{ key: string; value: string; category: string; source: string }[]>([])
  const [busy, setBusy] = useState(false)

  const loadFacts = () => {
    fetch('/api/backend/memory/facts')
      .then(r => r.json()).then(d => setFacts(d.facts || [])).catch(() => {})
  }
  useEffect(() => {
    fetch('/api/backend/settings/memory')
      .then(r => r.json())
      .then(d => { setEnabled(!!d.enabled); setAggressive(!!d.aggressive) })
      .catch(() => setEnabled(true))
    loadFacts()
  }, [])

  const put = async (payload: { enabled?: boolean; aggressive?: boolean }) => {
    setBusy(true)
    try {
      const res = await fetch('/api/backend/settings/memory', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      })
      if (!res.ok) throw new Error()
      const d = await res.json()
      setEnabled(!!d.enabled); setAggressive(!!d.aggressive)
      toast.success('記憶設定已更新')
    } catch { toast.error('設定切換失敗') } finally { setBusy(false) }
  }

  const delFact = async (key: string) => {
    try {
      await fetch(`/api/backend/memory/facts/${encodeURIComponent(key)}`, { method: 'DELETE' })
      setFacts(f => f.filter(x => x.key !== key)); toast.success(`已忘記「${key}」`)
    } catch { toast.error('刪除失敗') }
  }

  if (enabled === null) return null
  const Toggle = ({ on, onClick, color = 'bg-violet-500', disabled = false }: { on: boolean; onClick: () => void; color?: string; disabled?: boolean }) => (
    <button onClick={onClick} disabled={disabled}
      className={cn('relative w-12 h-7 rounded-full transition-colors shrink-0', on ? color : 'bg-gray-300', disabled && 'opacity-50 cursor-not-allowed')}>
      <span className={cn('absolute top-0.5 left-0.5 w-6 h-6 rounded-full bg-white shadow transition-transform', on ? 'translate-x-5' : 'translate-x-0')} />
    </button>
  )

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-violet-100 flex items-center justify-center text-xl">🧠</div>
        <div>
          <h2 className="text-lg font-semibold text-gray-900">AI 助手長期記憶</h2>
          <p className="text-sm text-gray-500">記得你的偏好 / 事實,跨對話「越用越懂你」</p>
        </div>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {/* 主開關 */}
        <div className="p-5 flex items-start justify-between gap-4 border-b border-gray-100">
          <div className="flex-1 min-w-0">
            <span className="text-sm font-semibold text-gray-800">開啟長期記憶</span>
            <p className="text-xs text-gray-500 leading-relaxed mt-1">
              開 → AI 助手能記住你明確要它記的偏好 / 事實(走確認),每次對話自動參考。
              關 → 不載入記憶工具、不注入記憶(已存的不刪、之後再開仍在)。敏感資料(密碼 / 金鑰)一律拒記。
            </p>
          </div>
          <Toggle on={enabled} onClick={() => put({ enabled: !enabled })} disabled={busy} />
        </div>

        {/* 激進萃取開關 */}
        <div className={cn('p-5 flex items-start justify-between gap-4 border-b border-gray-100', !enabled && 'opacity-40 pointer-events-none')}>
          <div className="flex-1 min-w-0">
            <span className="text-sm font-semibold text-gray-800">激進學習(自動推斷偏好)</span>
            <p className="text-xs text-gray-500 leading-relaxed mt-1">
              開 → 每次對話結束自動「推斷」你的偏好存成<b>低信心(推測)</b>記憶、不必你明說「記下」,越用越懂最快;
              但 AI 可能過度推斷 / 記錯(都標(推測)、你可隨時刪)。關 → 只記你明確說「記下」的事(保守、零誤記)。
            </p>
            <p className="text-[11px] text-amber-600 mt-1">⚠️ 預設關閉。要 AI 更積極學你再開。</p>
          </div>
          <Toggle on={aggressive} onClick={() => put({ aggressive: !aggressive })} color="bg-amber-500" disabled={busy || !enabled} />
        </div>

        {/* 記憶清單 */}
        <div className="px-5 py-4 bg-gray-50/50">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold text-gray-600">目前記得 {facts.length} 筆</span>
            <button onClick={loadFacts} className="text-xs text-violet-600 hover:underline">重新整理</button>
          </div>
          {facts.length === 0 ? (
            <p className="text-xs text-gray-400">還沒記任何事。對話中跟 AI 說「記住我…」就會出現在這。</p>
          ) : (
            <div className="space-y-1.5">
              {facts.map(f => (
                <div key={f.key} className="flex items-center gap-2 text-xs bg-white rounded-lg border border-gray-100 px-3 py-2">
                  <span className="px-1.5 py-0.5 rounded bg-violet-50 text-violet-600 shrink-0">{f.category}</span>
                  <span className="font-medium text-gray-700 shrink-0">{f.key}</span>
                  <span className="text-gray-500 truncate flex-1">= {f.value}{f.source === 'inferred' && <span className="text-amber-500">(推測)</span>}</span>
                  <button onClick={() => delFact(f.key)} className="text-gray-300 hover:text-red-500 shrink-0" title="忘記這條">🗑</button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}


function SelfHealSection() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [maxAttempts, setMaxAttempts] = useState(2)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    fetch('/api/backend/settings/self-heal')
      .then(r => r.json())
      .then(d => { setEnabled(!!d.enabled); setMaxAttempts(d.max_attempts || 2) })
      .catch(() => setEnabled(false))
  }, [])

  const put = async (payload: { enabled?: boolean; max_attempts?: number }) => {
    setBusy(true)
    try {
      const res = await fetch('/api/backend/settings/self-heal', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      })
      if (!res.ok) throw new Error()
      const d = await res.json()
      setEnabled(!!d.enabled); setMaxAttempts(d.max_attempts || 2)
      toast.success('自我修復設定已更新')
    } catch { toast.error('設定切換失敗') } finally { setBusy(false) }
  }

  if (enabled === null) return null
  const Toggle = ({ on, onClick, disabled = false }: { on: boolean; onClick: () => void; disabled?: boolean }) => (
    <button onClick={onClick} disabled={disabled}
      className={cn('relative w-12 h-7 rounded-full transition-colors shrink-0', on ? 'bg-teal-500' : 'bg-gray-300', disabled && 'opacity-50 cursor-not-allowed')}>
      <span className={cn('absolute top-0.5 left-0.5 w-6 h-6 rounded-full bg-white shadow transition-transform', on ? 'translate-x-5' : 'translate-x-0')} />
    </button>
  )

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-teal-100 flex items-center justify-center text-xl">🔧</div>
        <div>
          <h2 className="text-lg font-semibold text-gray-900">工作流自我修復</h2>
          <p className="text-sm text-gray-500">步驟失敗時,讓 AI 讀 log + 改 YAML + 重跑,自動修到好</p>
        </div>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {/* 主開關 */}
        <div className="p-5 flex items-start justify-between gap-4 border-b border-gray-100">
          <div className="flex-1 min-w-0">
            <span className="text-sm font-semibold text-gray-800">開啟自我修復</span>
            <p className="text-xs text-gray-500 leading-relaxed mt-1">
              開 → 某步驟失敗(重試耗盡)時,AI 助手自動讀執行 log、比對自己寫的 YAML、找出錯誤、改好、從失敗步重跑;
              到上限仍沒修好才轉人工決策。關(預設)→ 失敗即停下等你決定。
            </p>
            <p className="text-[11px] text-amber-600 mt-1">⚠️ 預設關閉。自動改 YAML 有風險,確認要無人值守再開。</p>
          </div>
          <Toggle on={enabled} onClick={() => put({ enabled: !enabled })} disabled={busy} />
        </div>

        {/* 次數上限 */}
        <div className={cn('p-5 flex items-center justify-between gap-4', !enabled && 'opacity-40 pointer-events-none')}>
          <div className="flex-1 min-w-0">
            <span className="text-sm font-semibold text-gray-800">最多自動修復次數</span>
            <p className="text-xs text-gray-500 leading-relaxed mt-1">
              單次執行最多自動嘗試幾輪「診斷 → 改 YAML → 重跑」。到上限仍失敗就轉人工。範圍 1~5。
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button onClick={() => put({ max_attempts: Math.max(1, maxAttempts - 1) })} disabled={busy || maxAttempts <= 1}
              className="w-8 h-8 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40">−</button>
            <span className="w-8 text-center text-sm font-semibold text-gray-800">{maxAttempts}</span>
            <button onClick={() => put({ max_attempts: Math.min(5, maxAttempts + 1) })} disabled={busy || maxAttempts >= 5}
              className="w-8 h-8 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40">＋</button>
          </div>
        </div>
      </div>
    </div>
  )
}


// ── Skill Sandbox Section ────────────────────────────────────────────────
function SandboxSection() {
  const [status, setStatus] = useState<SandboxStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [toggling, setToggling] = useState(false)

  const reload = async (refresh = false) => {
    try {
      const s = await getSandboxStatus(refresh)
      setStatus(s)
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  useEffect(() => {
    (async () => {
      await reload(false)
      setLoading(false)
    })()
  }, [])

  const toggle = async () => {
    if (!status) return
    const next = status.mode === 'host' ? 'wsl_docker' : 'host'
    // 切到 sandbox 前先提醒沒就緒
    if (next === 'wsl_docker' && !status.ready) {
      if (!confirm(`沙盒目前尚未就緒：\n\n${status.reasons.join('\n') || status.hint}\n\n仍然要切過去嗎？（Skill 執行會暫時 fallback 到 host）`)) {
        return
      }
    }
    setToggling(true)
    try {
      const updated = await setSandboxMode(next)
      setStatus(updated)
      toast.success(`已切換到${next === 'wsl_docker' ? '沙盒模式' : '本機模式'}`)
      // 通知 SkillPackagesSection 重新載入清單（以免使用者看到的是舊環境套件）
      window.dispatchEvent(new Event('sandbox-mode-changed'))
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setToggling(false)
    }
  }

  const badge = (ok: boolean, label: string) => (
    <span className={cn(
      'inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium',
      ok ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
         : 'bg-red-50 text-red-700 border border-red-200'
    )}>
      <span className={cn('w-1.5 h-1.5 rounded-full', ok ? 'bg-emerald-500' : 'bg-red-500')} />
      {label}
    </span>
  )

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-indigo-100 flex items-center justify-center">
          <Shield className="w-5 h-5 text-indigo-700" />
        </div>
        <div>
          <h2 className="text-lg font-semibold text-gray-900">Skill 沙盒</h2>
          <p className="text-sm text-gray-500">LLM 生成的 Python / Shell 程式碼要在哪裡執行</p>
        </div>
      </div>

      {loading ? (
        <div className="bg-white rounded-xl border border-gray-200 p-8 text-center text-gray-400">
          <RefreshCw className="w-5 h-5 animate-spin inline-block mr-2" />偵測中…
        </div>
      ) : !status ? null : (
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
          {/* Toggle row */}
          <div className="p-5 border-b border-gray-100 flex items-start justify-between gap-4">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-sm font-semibold text-gray-800">
                  {status.mode === 'wsl_docker' ? '🛡 沙盒模式（WSL + Docker）' : '💻 本機模式（Host subprocess）'}
                </span>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed">
                {status.mode === 'wsl_docker'
                  ? 'Skill 節點 run_python / run_shell 送進 atlas-sandbox 容器執行。computer_use / script 節點仍在 host 跑（需要桌面權限）。'
                  : 'Skill 節點 run_python / run_shell 直接在 Windows 跑（速度快）。LLM 能完整存取你的檔案系統。'}
              </p>
            </div>
            <button
              onClick={toggle}
              disabled={toggling}
              className={cn(
                'relative w-12 h-7 rounded-full transition-colors shrink-0',
                status.mode === 'wsl_docker' ? 'bg-indigo-500' : 'bg-gray-300',
                toggling && 'opacity-50 cursor-not-allowed'
              )}
            >
              <span className={cn(
                'absolute top-0.5 left-0.5 w-6 h-6 rounded-full bg-white shadow transition-transform',
                status.mode === 'wsl_docker' ? 'translate-x-5' : 'translate-x-0'
              )} />
            </button>
          </div>

          {/* Health */}
          <div className="p-5 border-b border-gray-100">
            <div className="flex items-center gap-2 mb-3">
              <span className="text-sm font-semibold text-gray-800">沙盒健康狀態</span>
              <button
                onClick={() => reload(true)}
                className="text-xs text-indigo-600 hover:text-indigo-700 flex items-center gap-1"
              >
                <RefreshCw className="w-3 h-3" />重新偵測
              </button>
            </div>
            <div className="flex flex-wrap gap-2 mb-3">
              {badge(status.wsl_ok, `WSL ${status.wsl_ok ? 'OK' : 'N/A'}`)}
              {badge(status.docker_ok, `Docker ${status.docker_ok ? 'OK' : 'N/A'}`)}
              {badge(status.container_running, `容器 ${status.container_running ? '執行中' : (status.container_exists ? '已停止' : '不存在')}`)}
            </div>
            {status.docker_version && (
              <p className="text-xs text-gray-400 font-mono mb-2">{status.docker_version}</p>
            )}
            {status.reasons.length > 0 && (
              <div className="mt-3 space-y-1">
                {status.reasons.map((r, i) => (
                  <div key={i} className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                    ⚠ {r}
                  </div>
                ))}
              </div>
            )}
            {status.hint && !status.ready && (
              <div className="mt-2 text-xs text-indigo-700 bg-indigo-50 border border-indigo-200 rounded-lg px-3 py-2">
                💡 {status.hint}
              </div>
            )}
          </div>

          {/* Footer info */}
          <div className="px-5 py-4 bg-gray-50/50 text-xs text-gray-500 space-y-1">
            <p>• 首次使用沙盒需執行 <code className="font-mono bg-white px-1.5 py-0.5 rounded border">sandbox/setup_sandbox.bat</code>（約 3-5 分鐘）</p>
            <p>• 模式切換即時生效，不用重啟後端</p>
            <p>• 沙盒關閉/壞掉時自動 fallback 到 host 確保工作流能跑</p>
            <p>• computer_use 節點永遠在 host 執行（需要桌面權限）</p>
          </div>
        </div>
      )}
    </div>
  )
}


export default function SettingsPage() {
  const [current, setCurrent] = useState<ModelSettings | null>(null)
  const [available, setAvailable] = useState<AvailableModels | null>(null)
  const [provider, setProvider] = useState<'groq' | 'ollama' | 'gemini' | 'openai' | 'anthropic' | 'claude_cli'>('groq')
  const [model, setModel] = useState('')
  const [ollamaUrl, setOllamaUrl] = useState('http://localhost:11434')
  const [thinking, setThinking] = useState<'auto' | 'on' | 'off'>('off')
  const [numCtx, setNumCtx] = useState<number>(65536)
  const [geminiThinking, setGeminiThinking] = useState<'off' | 'auto' | 'low' | 'medium' | 'high'>('off')
  const [antThinking, setAntThinking] = useState<'off' | 'on'>('off')
  // ── 副模型 state(空 provider = 不啟用)──
  const [secEnabled, setSecEnabled] = useState(false)
  const [secProvider, setSecProvider] = useState<'groq' | 'ollama' | 'gemini' | 'openai' | 'anthropic' | 'claude_cli'>('gemini')
  const [secModel, setSecModel] = useState('')
  const [secThinking, setSecThinking] = useState<'auto' | 'on' | 'off'>('off')
  const [secNumCtx, setSecNumCtx] = useState<number>(16384)
  const [secGeminiThinking, setSecGeminiThinking] = useState<'off' | 'auto' | 'low' | 'medium' | 'high'>('off')
  const [secAntThinking, setSecAntThinking] = useState<'off' | 'on'>('off')
  const [loading, setLoading] = useState(true)
  const [availableError, setAvailableError] = useState<string | null>(null)
  const [reloadingModels, setReloadingModels] = useState(false)
  const [saving, setSaving] = useState(false)
  const [nodeStatus, setNodeStatus] = useState<NodeStatus | null>(null)
  const [hostTools, setHostTools] = useState<HostToolsResponse | null>(null)

  useEffect(() => {
    getNodeStatus().then(setNodeStatus).catch(() => {})
    getHostTools().then(setHostTools).catch(() => {})
  }, [])

  // 拆開兩個請求：current 是本地 JSON 很快、必要；available 要打 4 個外部 API 可能失敗
  // available 失敗時仍讓使用者看到當前設定與 provider 切換，只把「載入失敗」侷限在模型下拉區
  const loadAvailable = async () => {
    setReloadingModels(true)
    setAvailableError(null)
    try {
      const avail = await getAvailableModels()
      setAvailable(avail)
    } catch (e) {
      setAvailableError((e as Error).message)
    } finally {
      setReloadingModels(false)
    }
  }

  const load = async () => {
    setLoading(true)
    try {
      const cur = await getModelSettings()
      setCurrent(cur)
      setProvider(cur.provider)
      setModel(cur.model)
      setOllamaUrl(cur.ollama_base_url || 'http://localhost:11434')
      setThinking(cur.ollama_thinking || 'off')
      setNumCtx(cur.ollama_num_ctx || 65536)
      setGeminiThinking(cur.gemini_thinking || 'off')
      setAntThinking(cur.anthropic_thinking || 'off')
      // 載入副模型(provider 有值 = 已啟用)
      const sp = (cur.secondary_provider || '') as string
      if (sp) {
        setSecEnabled(true)
        setSecProvider(sp as any)
        setSecModel(cur.secondary_model || '')
        setSecThinking(cur.secondary_ollama_thinking || 'off')
        setSecNumCtx(cur.secondary_ollama_num_ctx || 16384)
        setSecGeminiThinking(cur.secondary_gemini_thinking || 'off')
        setSecAntThinking(cur.secondary_anthropic_thinking || 'off')
      }
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setLoading(false)
    }
    // 背景載入可用模型清單；失敗不影響設定頁其他部分
    loadAvailable()
  }

  useEffect(() => { load() }, [])

  const handleSave = async () => {
    if (!model) {
      toast.error('請選擇主要模型')
      return
    }
    if (secEnabled && !secModel) {
      toast.error('副模型已啟用、請選擇模型(或取消啟用)')
      return
    }
    setSaving(true)
    try {
      const saved = await saveModelSettings({
        provider, model,
        ollama_base_url: ollamaUrl, ollama_thinking: thinking, ollama_num_ctx: numCtx,
        gemini_thinking: geminiThinking, anthropic_thinking: antThinking,
        // 副模型:secEnabled=false 時送空字串 → 後端清掉副模型設定
        secondary_provider: secEnabled ? secProvider : '',
        secondary_model: secEnabled ? secModel : '',
        secondary_ollama_thinking: secEnabled ? secThinking : 'off',
        secondary_ollama_num_ctx: secEnabled ? secNumCtx : 16384,
        secondary_gemini_thinking: secEnabled ? secGeminiThinking : 'off',
        secondary_anthropic_thinking: secEnabled ? secAntThinking : 'off',
      })
      setCurrent(saved)
      const secMsg = secEnabled ? `;副 ${saved.secondary_provider}/${saved.secondary_model}` : ''
      toast.success(`已儲存:主 ${saved.provider}/${saved.model}${secMsg}`)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  // 依 provider 取該 provider 的可用模型清單(主/副模型共用)
  const modelsFor = (prov: string) =>
    prov === 'groq' ? (available?.groq ?? [])
    : prov === 'gemini' ? (available?.gemini ?? [])
    : prov === 'openai' ? (available?.openai ?? [])
    : prov === 'anthropic' ? (available?.anthropic ?? [])
    : prov === 'claude_cli' ? (available?.claude_cli ?? [])
    : (available?.ollama ?? [])
  const options = modelsFor(provider)
  const secOptions = modelsFor(secProvider)
  const providerError = provider === 'groq' ? available?.groq_error
    : provider === 'gemini' ? available?.gemini_error
    : provider === 'openai' ? available?.openai_error
    : provider === 'anthropic' ? available?.anthropic_error
    : provider === 'claude_cli' ? available?.claude_cli_error
    : available?.ollama_error
  const _curSecProv = (current?.secondary_provider || '') as string
  const _curSecEnabled = !!_curSecProv
  const dirty = current && (
    provider !== current.provider ||
    model !== current.model ||
    ollamaUrl !== current.ollama_base_url ||
    thinking !== current.ollama_thinking ||
    numCtx !== current.ollama_num_ctx ||
    geminiThinking !== (current.gemini_thinking || 'off') ||
    antThinking !== (current.anthropic_thinking || 'off') ||
    // 副模型 dirty 偵測
    secEnabled !== _curSecEnabled ||
    (secEnabled && (
      secProvider !== _curSecProv ||
      secModel !== (current.secondary_model || '') ||
      secThinking !== (current.secondary_ollama_thinking || 'off') ||
      secNumCtx !== (current.secondary_ollama_num_ctx || 16384) ||
      secGeminiThinking !== (current.secondary_gemini_thinking || 'off') ||
      secAntThinking !== (current.secondary_anthropic_thinking || 'off')
    ))
  )

  return (
    <div className="flex-1 overflow-auto bg-gray-50">
      <Toaster position="top-right" richColors />
      <div className="max-w-3xl mx-auto p-8">
        {/* Header */}
        <div className="flex items-center gap-3 mb-6">
          <Link
            href="/pipeline"
            className="p-2 rounded-lg text-gray-500 hover:text-gray-900 hover:bg-white transition-colors"
            title="回到 Pipeline"
          >
            <ArrowLeft className="w-5 h-5" />
          </Link>
          <div className="w-10 h-10 rounded-xl bg-brand-100 flex items-center justify-center">
            <SettingsIcon className="w-5 h-5 text-brand-700" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-gray-900">設定</h1>
            <p className="text-sm text-gray-500">調整 Pipeline 執行與驗證時使用的 LLM 模型</p>
          </div>
        </div>

        {/* Node.js 環境警示（優先顯示，很多 skill 靠 npm 套件） */}
        {nodeStatus && !nodeStatus.node_installed && (
          <div className="mb-6 bg-red-50 border border-red-200 rounded-xl p-4">
            <div className="flex items-start gap-3">
              <AlertCircle className="w-5 h-5 text-red-600 shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-semibold text-red-800 mb-1">系統找不到 Node.js</div>
                <p className="text-xs text-red-700 leading-relaxed mb-2">
                  部分 Agent Skill（例如 <code className="bg-white px-1 rounded">pptx</code>）需要 <code className="bg-white px-1 rounded">npm</code> 套件（<code className="bg-white px-1 rounded">pptxgenjs</code> 等）才能完整運作。沒有 Node.js 會導致這些 Skill 執行失敗。
                </p>
                <p className="text-xs text-red-700">
                  安裝方式：<span className="break-all">{nodeStatus.install_hint}</span>
                </p>
              </div>
            </div>
          </div>
        )}
        {nodeStatus && nodeStatus.node_installed && !nodeStatus.npm_installed && (
          <div className="mb-6 bg-amber-50 border border-amber-200 rounded-xl p-4">
            <div className="flex items-start gap-3">
              <AlertCircle className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-semibold text-amber-800 mb-1">Node.js 已安裝但找不到 npm</div>
                <p className="text-xs text-amber-700">
                  Node.js ({nodeStatus.node_version}) 已偵測到,但無法找到 <code className="bg-white px-1 rounded">npm</code> 指令。請重新安裝 Node.js LTS 版本或確認 npm 已加入 PATH。
                </p>
              </div>
            </div>
          </div>
        )}

        {/* 主機系統工具(LibreOffice 等)健康檢查 ── 必要工具沒裝才顯示橘色警告區 */}
        {hostTools && (() => {
          const platKey = hostTools.platform === 'Windows' ? 'windows' :
                          hostTools.platform === 'Darwin' ? 'macos' : 'linux'
          const missingRequired = hostTools.tools.filter(t => t.required && !t.installed)
          const allInstalled = hostTools.tools.every(t => t.installed)
          if (missingRequired.length === 0 && allInstalled) return null
          return (
            <div className={`mb-6 rounded-xl p-4 border ${
              missingRequired.length > 0
                ? 'bg-amber-50 border-amber-200'
                : 'bg-blue-50 border-blue-200'
            }`}>
              <div className="flex items-start gap-3">
                <AlertCircle className={`w-5 h-5 shrink-0 mt-0.5 ${
                  missingRequired.length > 0 ? 'text-amber-600' : 'text-blue-600'
                }`} />
                <div className="flex-1 min-w-0">
                  <div className={`text-sm font-semibold mb-2 ${
                    missingRequired.length > 0 ? 'text-amber-800' : 'text-blue-800'
                  }`}>
                    🛠 主機系統工具狀態 ({hostTools.platform})
                  </div>
                  <div className="space-y-2">
                    {hostTools.tools.map(tool => (
                      <div key={tool.name} className="text-xs leading-relaxed">
                        <div className="flex items-center gap-2 mb-0.5">
                          <span className={tool.installed ? 'text-green-700' : (tool.required ? 'text-red-700' : 'text-gray-500')}>
                            {tool.installed ? '✅' : (tool.required ? '❌' : '⚪')}
                          </span>
                          <span className="font-semibold text-gray-800">{tool.name}</span>
                          {tool.required && !tool.installed && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-100 text-red-700">必要</span>
                          )}
                          {tool.installed && tool.found_at && (
                            <span className="text-[10px] text-gray-500 font-mono truncate">{tool.found_at}</span>
                          )}
                        </div>
                        <p className="text-gray-600 ml-6">{tool.why}</p>
                        {!tool.installed && (
                          <div className="ml-6 mt-1 bg-white border border-gray-200 rounded px-2 py-1 font-mono text-[10px] break-all">
                            {tool.install_cmd[platKey as 'windows' | 'macos' | 'linux']}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                  {missingRequired.length > 0 && (
                    <p className="text-[11px] text-amber-700 mt-3 leading-relaxed">
                      💡 必要工具沒裝會影響 <b>視覺驗證 / 人工確認預覽</b>(VLM 看到的不是真實 PPT/DOCX 版面、永遠評不過)。裝完<b>重啟 backend</b> 才生效。
                    </p>
                  )}
                </div>
              </div>
            </div>
          )
        })()}

        {loading ? (
          <div className="bg-white rounded-xl border border-gray-200 p-8 text-center text-gray-400">
            <RefreshCw className="w-5 h-5 animate-spin inline-block mr-2" />
            載入中...
          </div>
        ) : (
          <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
            {/* Current */}
            <div className="px-6 py-4 border-b border-gray-100 bg-gradient-to-r from-brand-50 to-purple-50">
              <div className="text-xs text-gray-500 uppercase tracking-wider font-semibold mb-1">目前使用的模型</div>
              <div className="flex items-center gap-2">
                <span className="px-2 py-0.5 rounded text-xs font-semibold uppercase bg-white border border-gray-200 text-gray-700">
                  {current?.provider}
                </span>
                <span className="font-mono text-sm font-medium text-gray-900">
                  {current?.model}
                </span>
              </div>
            </div>

            {/* Provider 選擇 */}
            <div className="p-6 border-b border-gray-100">
              <label className="block text-sm font-medium text-gray-700 mb-3">提供者</label>
              <div className="grid grid-cols-2 gap-3">
                {([
                  { v: 'groq' as const, icon: Cloud, name: 'Groq Cloud', desc: '雲端 API，速度快', fallbackModel: '' },
                  { v: 'gemini' as const, icon: Sparkles, name: 'Google Gemini', desc: '支援思考模式', fallbackModel: 'gemma-4-31b-it' },
                  { v: 'openai' as const, icon: Cloud, name: 'OpenAI', desc: 'GPT 系列、官方 API', fallbackModel: 'gpt-4o-mini' },
                  { v: 'anthropic' as const, icon: Cloud, name: 'Anthropic', desc: 'Claude 系列、支援延伸思考', fallbackModel: 'claude-sonnet-4-6-20250929' },
                  { v: 'claude_cli' as const, icon: Sparkles, name: 'Claude 訂閱', desc: '用 Pro/Max 訂閱當大腦，免 API 費', fallbackModel: 'opus' },
                  { v: 'ollama' as const, icon: HardDrive, name: 'Ollama 本地', desc: '離線運行，無配額', fallbackModel: '' },
                ]).map(p => (
                  <button
                    key={p.v}
                    onClick={() => {
                      setProvider(p.v)
                      const list = p.v === 'groq' ? available?.groq
                        : p.v === 'gemini' ? available?.gemini
                        : p.v === 'openai' ? available?.openai
                        : p.v === 'anthropic' ? available?.anthropic
                        : available?.ollama
                      setModel(list?.[0]?.id ?? p.fallbackModel)
                    }}
                    className={cn(
                      'flex items-center gap-3 p-4 rounded-lg border-2 transition-all text-left',
                      provider === p.v
                        ? 'border-brand-600 bg-brand-50'
                        : 'border-gray-200 hover:border-gray-300'
                    )}
                  >
                    <p.icon className={cn('w-5 h-5 shrink-0', provider === p.v ? 'text-brand-700' : 'text-gray-400')} />
                    <div className="min-w-0">
                      <div className="font-medium text-sm text-gray-900">{p.name}</div>
                      <div className="text-xs text-gray-500">{p.desc}</div>
                    </div>
                  </button>
                ))}
              </div>
            </div>

            {/* Ollama URL */}
            {provider === 'ollama' && (
              <div className="p-6 border-b border-gray-100">
                <label className="block text-sm font-medium text-gray-700 mb-2">Ollama Base URL</label>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={ollamaUrl}
                    onChange={(e) => setOllamaUrl(e.target.value)}
                    placeholder="http://localhost:11434"
                    className="flex-1 px-3 py-2 border border-gray-200 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                  />
                  <button
                    onClick={load}
                    className="px-3 py-2 border border-gray-200 rounded-lg text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-1.5"
                  >
                    <RefreshCw className="w-4 h-4" />
                    重新讀取
                  </button>
                </div>
              </div>
            )}

            {/* Provider 錯誤提示 */}
            {providerError && (
              <div className="mx-6 mt-4 p-3 bg-amber-50 border border-amber-200 rounded-lg text-xs text-amber-800 flex items-start gap-2">
                <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                <span>{providerError}</span>
              </div>
            )}

            {/* 思考模式 — Ollama */}
            {provider === 'ollama' && (
              <div className="p-6 border-b border-gray-100">
                <label className="block text-sm font-medium text-gray-700 mb-1 flex items-center gap-2">
                  <Brain className="w-4 h-4" />
                  思考模式
                </label>
                <p className="text-xs text-gray-500 mb-3">控制 qwen3 等支援思考的模型是否輸出推理過程（關閉可大幅加快速度）</p>
                <div className="grid grid-cols-3 gap-2">
                  {([
                    { v: 'auto', label: '預設', desc: '依模型設定' },
                    { v: 'off',  label: '關閉思考', desc: '最快，省時間' },
                    { v: 'on',   label: '開啟思考', desc: '更準確，較慢' },
                  ] as const).map(opt => (
                    <button
                      key={opt.v}
                      onClick={() => setThinking(opt.v)}
                      className={cn(
                        'p-3 rounded-lg border-2 transition-all text-left',
                        thinking === opt.v
                          ? 'border-brand-600 bg-brand-50'
                          : 'border-gray-200 hover:border-gray-300'
                      )}
                    >
                      <div className="text-sm font-medium text-gray-900">{opt.label}</div>
                      <div className="text-xs text-gray-500 mt-0.5">{opt.desc}</div>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* 思考模式 — Gemini */}
            {provider === 'gemini' && (
              <div className="p-6 border-b border-gray-100">
                <label className="block text-sm font-medium text-gray-700 mb-1 flex items-center gap-2">
                  <Brain className="w-4 h-4" />
                  思考模式
                </label>
                <p className="text-xs text-gray-500 mb-3">
                  Gemini 2.5+ / 3.x 支援思考模式，會在回答前先進行推理。選擇不支援的模型時自動忽略此設定。
                </p>
                <div className="grid grid-cols-5 gap-2">
                  {([
                    { v: 'off',    label: '關閉',   desc: '不思考' },
                    { v: 'auto',   label: '自動',   desc: '模型決定' },
                    { v: 'low',    label: '低',     desc: '快速' },
                    { v: 'medium', label: '中等',   desc: '均衡' },
                    { v: 'high',   label: '高',     desc: '最深入' },
                  ] as const).map(opt => (
                    <button
                      key={opt.v}
                      onClick={() => setGeminiThinking(opt.v)}
                      className={cn(
                        'p-3 rounded-lg border-2 transition-all text-left',
                        geminiThinking === opt.v
                          ? 'border-brand-600 bg-brand-50'
                          : 'border-gray-200 hover:border-gray-300'
                      )}
                    >
                      <div className="text-sm font-medium text-gray-900">{opt.label}</div>
                      <div className="text-xs text-gray-500 mt-0.5">{opt.desc}</div>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* 延伸思考 — Anthropic Claude Opus 4 系列 */}
            {provider === 'anthropic' && (
              <div className="p-6 border-b border-gray-100">
                <label className="block text-sm font-medium text-gray-700 mb-1 flex items-center gap-2">
                  <Brain className="w-4 h-4" />
                  延伸思考（Extended Thinking）
                </label>
                <p className="text-xs text-gray-500 mb-3">
                  Claude Opus 4 系列在回答前會輸出思考過程、適合複雜推理。Sonnet / Haiku 開了沒效果（自動忽略）。
                </p>
                <div className="grid grid-cols-2 gap-2">
                  {([
                    { v: 'off', label: '關閉思考', desc: '直接回答' },
                    { v: 'on',  label: '開啟思考', desc: 'Opus 會先推理再答' },
                  ] as const).map(opt => (
                    <button
                      key={opt.v}
                      onClick={() => setAntThinking(opt.v)}
                      className={cn(
                        'p-3 rounded-lg border-2 transition-all text-left',
                        antThinking === opt.v
                          ? 'border-brand-600 bg-brand-50'
                          : 'border-gray-200 hover:border-gray-300'
                      )}
                    >
                      <div className="text-sm font-medium text-gray-900">{opt.label}</div>
                      <div className="text-xs text-gray-500 mt-0.5">{opt.desc}</div>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* Context window（僅 Ollama）*/}
            {provider === 'ollama' && (
              <div className="p-6 border-b border-gray-100">
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Context 長度 (num_ctx)
                </label>
                <p className="text-xs text-gray-500 mb-3">
                  模型一次能處理的 token 數。越大越不容易截斷，但吃更多 VRAM 且變慢。AI 助手一輪要送提示詞 1.4～4 萬 tokens + 工具定義 5.5k + 對話與回答，建議 65536(24 GB 顯示卡可跑 27B 模型)；32768 只夠短對話，再低會被 Ollama 靜默截掉提示詞前半。
                </p>
                <div className="grid grid-cols-4 gap-2 mb-3">
                  {[8192, 16384, 32768, 65536].map((v) => (
                    <button
                      key={v}
                      onClick={() => setNumCtx(v)}
                      className={cn(
                        'p-2 rounded-lg border-2 transition-all text-sm',
                        numCtx === v
                          ? 'border-brand-600 bg-brand-50 text-brand-700 font-medium'
                          : 'border-gray-200 hover:border-gray-300 text-gray-700'
                      )}
                    >
                      {v >= 1024 ? `${v / 1024}K` : v}
                    </button>
                  ))}
                </div>
                <input
                  type="number"
                  value={numCtx}
                  onChange={(e) => setNumCtx(Math.max(2048, Math.min(262144, parseInt(e.target.value) || 65536)))}
                  min={2048}
                  max={262144}
                  step={2048}
                  className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
            )}

            {/* 模型選擇 */}
            <div className="p-6 border-b border-gray-100">
              <label className="block text-sm font-medium text-gray-700 mb-3">
                模型
                {provider === 'ollama' && <span className="text-xs text-gray-400 ml-2">（讀取自 ollama list）</span>}
                {provider === 'groq' && <span className="text-xs text-gray-400 ml-2">（從 Groq API 動態取得）</span>}
                {provider === 'gemini' && <span className="text-xs text-gray-400 ml-2">（從 Google API 動態取得）</span>}
                {provider === 'openai' && <span className="text-xs text-gray-400 ml-2">（從 OpenAI API 動態取得）</span>}
                {provider === 'anthropic' && <span className="text-xs text-gray-400 ml-2">（從 Anthropic API 動態取得）</span>}
                {provider === 'claude_cli' && <span className="text-xs text-gray-400 ml-2">（訂閱 CLI · 吃 Pro/Max 額度、免 API 費）</span>}
                {reloadingModels && <span className="text-xs text-indigo-500 ml-2"><Loader2 className="w-3 h-3 animate-spin inline mr-1" />載入中…</span>}
              </label>
              {availableError ? (
                <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
                  <div className="font-medium mb-1">模型清單載入失敗</div>
                  <div className="text-xs mb-2 break-all">{availableError}</div>
                  <button onClick={loadAvailable} disabled={reloadingModels}
                    className="text-xs px-3 py-1 bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50">
                    {reloadingModels ? '重試中…' : '重試'}
                  </button>
                </div>
              ) : options.length === 0 ? (
                <div className="p-4 bg-gray-50 rounded-lg text-sm text-gray-500 text-center">
                  {provider === 'ollama'
                    ? (available?.ollama_error ? `尚未發現 Ollama 模型：${available.ollama_error}` : '尚未發現任何 Ollama 本地模型')
                    : reloadingModels ? '載入中…' : '無可用模型'}
                </div>
              ) : (
                <div className="space-y-2 max-h-96 overflow-y-auto">
                  {options.map((opt) => (
                    <label
                      key={opt.id}
                      className={cn(
                        'flex items-center gap-3 p-3 rounded-lg border cursor-pointer transition-all',
                        model === opt.id
                          ? 'border-brand-600 bg-brand-50'
                          : 'border-gray-200 hover:border-gray-300 hover:bg-gray-50'
                      )}
                    >
                      <input
                        type="radio"
                        name="model"
                        value={opt.id}
                        checked={model === opt.id}
                        onChange={() => setModel(opt.id)}
                        className="accent-brand-600"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium text-gray-900 truncate">{opt.label}</div>
                        <div className="text-xs text-gray-500 font-mono truncate">{opt.id}</div>
                      </div>
                      {model === opt.id && current?.model === opt.id && current?.provider === provider && (
                        <CheckCircle2 className="w-4 h-4 text-brand-600 shrink-0" />
                      )}
                    </label>
                  ))}
                </div>
              )}
            </div>

            {/* ── 副模型 Secondary 區塊 ── */}
            <div className="px-6 py-4 border-t border-gray-200 bg-amber-50/30">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <Sparkles className="w-4 h-4 text-amber-600" />
                  <h3 className="text-sm font-semibold text-gray-900">副模型(可選)</h3>
                  <span className="text-xs text-gray-500">— 每個節點可在 panel 選用「副模型」</span>
                </div>
                <label className="inline-flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={secEnabled}
                    onChange={(e) => setSecEnabled(e.target.checked)}
                    className="accent-amber-600"
                  />
                  <span className="text-xs text-gray-700">{secEnabled ? '已啟用' : '未啟用'}</span>
                </label>
              </div>

              {secEnabled && (
                <div className="space-y-3">
                  {/* Provider 切換 */}
                  <div>
                    <label className="text-xs font-medium text-gray-600 mb-1.5 block">Provider</label>
                    <div className="grid grid-cols-3 gap-1.5">
                      {(['groq', 'gemini', 'openai', 'anthropic', 'ollama', 'claude_cli'] as const).map(p => (
                        <button
                          key={p}
                          type="button"
                          onClick={() => {
                            setSecProvider(p)
                            // 切 provider 自動選第一個可用模型(免手填)
                            setSecModel(modelsFor(p)?.[0]?.id ?? '')
                          }}
                          className={cn(
                            'px-2 py-1.5 text-xs rounded border transition-colors',
                            secProvider === p
                              ? 'bg-amber-600 text-white border-amber-600'
                              : 'bg-white text-gray-700 border-gray-200 hover:border-amber-400'
                          )}
                        >{p === 'claude_cli' ? 'Claude 訂閱' : p}</button>
                      ))}
                    </div>
                  </div>

                  {/* Model 清單選擇(跟主模型一致、免手填)*/}
                  <div>
                    <label className="text-xs font-medium text-gray-600 mb-1.5 block">Model</label>
                    {secOptions.length === 0 ? (
                      <div className="p-3 bg-white rounded-lg border border-gray-200 text-xs text-gray-500 text-center">
                        {secProvider === 'ollama'
                          ? (available?.ollama_error ? `尚未發現 Ollama 模型:${available.ollama_error}` : '尚未發現任何 Ollama 本地模型')
                          : (secProvider === 'claude_cli' && available?.claude_cli_error) ? available.claude_cli_error
                          : reloadingModels ? '載入中…' : '無可用模型(可到主模型區按重新整理)'}
                      </div>
                    ) : (
                      <div className="space-y-1.5 max-h-56 overflow-y-auto">
                        {secOptions.map((opt) => (
                          <label
                            key={opt.id}
                            className={cn(
                              'flex items-center gap-2.5 p-2 rounded-lg border cursor-pointer transition-all',
                              secModel === opt.id
                                ? 'border-amber-500 bg-amber-50'
                                : 'border-gray-200 hover:border-gray-300 hover:bg-gray-50'
                            )}
                          >
                            <input
                              type="radio"
                              name="secModel"
                              value={opt.id}
                              checked={secModel === opt.id}
                              onChange={() => setSecModel(opt.id)}
                              className="accent-amber-600"
                            />
                            <div className="flex-1 min-w-0">
                              <div className="text-xs font-medium text-gray-900 truncate">{opt.label}</div>
                              <div className="text-[11px] text-gray-500 font-mono truncate">{opt.id}</div>
                            </div>
                          </label>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Thinking(視 provider 切換)*/}
                  {secProvider === 'gemini' && (
                    <div>
                      <label className="text-xs font-medium text-gray-600 mb-1.5 block">Thinking</label>
                      <select
                        value={secGeminiThinking}
                        onChange={(e) => setSecGeminiThinking(e.target.value as any)}
                        className="border border-gray-200 rounded-lg px-2 py-1 text-xs"
                      >
                        {['off', 'auto', 'low', 'medium', 'high'].map(v => <option key={v} value={v}>{v}</option>)}
                      </select>
                    </div>
                  )}
                  {secProvider === 'anthropic' && (
                    <div>
                      <label className="text-xs font-medium text-gray-600 mb-1.5 block">Extended Thinking</label>
                      <select
                        value={secAntThinking}
                        onChange={(e) => setSecAntThinking(e.target.value as any)}
                        className="border border-gray-200 rounded-lg px-2 py-1 text-xs"
                      >
                        <option value="off">off</option>
                        <option value="on">on</option>
                      </select>
                    </div>
                  )}
                  {secProvider === 'ollama' && (
                    <>
                      <div>
                        <label className="text-xs font-medium text-gray-600 mb-1.5 block">Thinking</label>
                        <select
                          value={secThinking}
                          onChange={(e) => setSecThinking(e.target.value as any)}
                          className="border border-gray-200 rounded-lg px-2 py-1 text-xs"
                        >
                          {['off', 'auto', 'on'].map(v => <option key={v} value={v}>{v}</option>)}
                        </select>
                      </div>
                      <div>
                        <label className="text-xs font-medium text-gray-600 mb-1.5 block">num_ctx</label>
                        <input
                          type="number" min={2048} max={262144} step={2048}
                          value={secNumCtx}
                          onChange={(e) => setSecNumCtx(parseInt(e.target.value) || 16384)}
                          className="w-32 border border-gray-200 rounded-lg px-2 py-1 text-xs font-mono"
                        />
                      </div>
                    </>
                  )}

                  <p className="text-[11px] text-gray-500 leading-relaxed bg-white border border-amber-200 rounded px-2 py-1.5">
                    💡 用途:在節點 panel 選「llm_role: secondary」就會用這個模型。例:主用便宜的 Gemma 跑大部分節點、副掛 Claude Sonnet 給 PPT 產出 / 視覺驗證等對品質敏感的節點。
                  </p>
                </div>
              )}
              {!secEnabled && (
                <p className="text-[11px] text-gray-500">
                  未啟用 — 所有節點都用「主要模型」。打勾啟用後、節點可在 panel 選用副模型。
                </p>
              )}
            </div>

            {/* 儲存 */}
            <div className="px-6 py-4 bg-gray-50/50 flex items-center justify-between">
              <div className="text-xs text-gray-500">
                {dirty ? '有未儲存的變更' : '尚無變更'}
              </div>
              <button
                onClick={handleSave}
                disabled={saving || !dirty || !model}
                className={cn(
                  'px-4 py-2 rounded-lg text-sm font-medium flex items-center gap-2 transition-all',
                  dirty && model && !saving
                    ? 'bg-brand-600 text-white hover:bg-brand-700'
                    : 'bg-gray-200 text-gray-400 cursor-not-allowed'
                )}
              >
                {saving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                儲存設定
              </button>
            </div>
          </div>
        )}

        {/* Installed Skills (Claude Code skills from ~/.agents/skills/) */}
        <InstalledSkillsSection onInstallRequest={async (pkg) => {
          try {
            const { message } = await addSkillPackage(pkg, 'auto')
            toast.success(message)
          } catch (e) {
            toast.error((e as Error).message)
          }
        }} />

        {/* MCP Servers(掛載外部 MCP 工具、AI 助手即時感知) */}
        <McpServersSection />

        {/* Secrets Vault(憑證加密存放) */}
        <SecretsSection />

        {/* Subagent 角色管理 */}
        <SubagentRolesSection />

        {/* Skill Packages */}
        <SkillPackagesSection />

        {/* Notifications */}
        <NotificationSection />

        {/* Web Search (Tavily) */}
        <WebSearchSection />

        {/* Skill Sandbox */}
        <SandboxSection />

        {/* computer_use 自動縮視窗 */}
        <ComputerUseAutoMinimizeSection />

        {/* AI 助手長期記憶 */}
        <MemorySection />

        <SelfHealSection />

        {/* 提示 */}
        <div className="mt-4 text-xs text-gray-500 space-y-1">
          <p>• 設定會立即生效（新 pipeline 執行會使用新模型）</p>
          <p>• 設定儲存在 <code className="font-mono bg-gray-100 px-1.5 py-0.5 rounded">~/ai_output/pipeline_settings.json</code></p>
          <p>• 模型列表從各 API 動態取得，OpenRouter 僅顯示免費模型</p>
          <p>• OpenRouter API Key 請在 <code className="font-mono bg-gray-100 px-1.5 py-0.5 rounded">backend/.env</code> 設定 OPENROUTER_API_KEY（免費模型不需要 key 也可列出清單）</p>
        </div>
      </div>
    </div>
  )
}
