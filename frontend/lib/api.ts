import type { OutputFormat, StepEvent, ScheduledTask, FileItem, OpenCLICategory, OpenCLIStatus, AgentMode, PipelineRun } from './types'

const BASE = '/api/backend'

/**
 * 長時間請求（如 LLM chat）專用的後端 URL：繞過 Next.js dev 的 rewrite proxy
 * 原因：Next.js rewrite 走 http-proxy，預設 socket timeout ~30s，超時就回 500
 * 只在瀏覽器端且後端在 localhost 時啟用；後端已配置 CORS 允許前端 origin
 */
const DIRECT_BASE = (() => {
  if (typeof window === 'undefined') return BASE
  const { hostname } = window.location
  // 後端埠:由 build-time 注入的 NEXT_PUBLIC_BACKEND_PORT 決定(預設 8014)
  const port = process.env.NEXT_PUBLIC_BACKEND_PORT || '8014'
  if (hostname === 'localhost' || hostname === '127.0.0.1') return `http://localhost:${port}`
  return BASE
})()

/**
 * fetch wrapper：對 5xx / network 錯誤做指數退避重試
 * 延遲序列 400ms → 1200ms → 2500ms（總等候 ~4s），覆蓋典型 uvicorn 熱重載空窗
 * 原因：Next.js dev proxy 在後端 .py 編輯觸發 uvicorn reload 的 2-5 秒內會回 500/502
 * 只對 idempotent 操作使用；4xx 不重試（客戶端錯誤）
 */
export async function fetchWithRetry(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const delays = [400, 1200, 2500]
  const tryOnce = async () => {
    try { return await fetch(input, init) } catch { return null }
  }
  let res = await tryOnce()
  for (const delay of delays) {
    if (res && res.ok) return res
    const status = res?.status ?? 0
    const shouldRetry = !res || (status >= 500 && status < 600)
    if (!shouldRetry) return res!
    await new Promise(r => setTimeout(r, delay))
    res = await tryOnce()
  }
  if (res) return res
  throw new Error('後端連線失敗（請確認 uvicorn 是否在運行）')
}

// ── Chat / Run ──────────────────────────────────────────────
export async function* streamTask(
  task: string,
  format: OutputFormat = 'md',
  savePath?: string,
  mode: AgentMode = 'auto'
): AsyncGenerator<StepEvent> {
  const res = await fetch(`${BASE}/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task, output_format: format, save_path: savePath ?? null, stream: true, mode }),
  })

  if (!res.ok) {
    throw new Error(`API 錯誤：${res.status}`)
  }

  const reader = res.body?.getReader()
  if (!reader) throw new Error('無法讀取串流')

  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''

    let event = ''
    let data = ''

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        event = line.slice(7).trim()
      } else if (line.startsWith('data: ')) {
        data = line.slice(6).trim()
      } else if (line === '' && event && data) {
        try {
          const parsed = JSON.parse(data)
          yield { type: event as StepEvent['type'], ...parsed }
        } catch { /* ignore malformed */ }
        event = ''
        data = ''
      }
    }
  }
}

// ── Tasks ───────────────────────────────────────────────────
export async function getTasks(): Promise<ScheduledTask[]> {
  const res = await fetch(`${BASE}/tasks`)
  const data = await res.json()
  return data.tasks ?? []
}

export async function createTask(task: {
  name: string
  task_prompt: string
  output_format: OutputFormat
  save_path?: string
  schedule_type: string
  schedule_expr: string
}): Promise<ScheduledTask> {
  const res = await fetch(`${BASE}/tasks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(task),
  })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.detail ?? 'Create task failed')
  }
  const data = await res.json()
  return data.task
}

export async function deleteTask(id: string): Promise<void> {
  const res = await fetch(`${BASE}/tasks/${id}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Delete task failed')
}

// ── FS Browser ──────────────────────────────────────────────
export async function fsBrowse(path = ''): Promise<{
  path: string
  parent: string | null
  items: { name: string; path: string; is_dir: boolean; ext: string }[]
}> {
  const res = await fetch(`${BASE}/fs/browse?path=${encodeURIComponent(path)}`)
  return res.json()
}

export async function fsCheckVenv(dir: string): Promise<{ has_venv: boolean; python_path: string | null; venv_dir_name: string | null }> {
  const res = await fetch(`${BASE}/fs/check-venv?dir=${encodeURIComponent(dir)}`)
  if (!res.ok) throw new Error('檢查失敗')
  return res.json()
}

// 在本機檔案總管開啟某工作流的輸出資料夾(OUTPUT_BASE_PATH/<工作流名稱>/)。
// 後端與使用者同機才有意義;找不到該資料夾 → 後端退回開 ai_output 根並回 existed=false。
export async function openOutputFolder(name: string): Promise<{ opened: string; existed: boolean }> {
  const res = await fetch(`${BASE}/fs/open-output?name=${encodeURIComponent(name)}`)
  if (!res.ok) {
    const e = await res.json().catch(() => ({} as { detail?: string }))
    throw new Error(e.detail || '開啟輸出資料夾失敗')
  }
  return res.json()
}

// 開 OS 原生檔案對話框(本機部署、後端與使用者同一台)。mode: open/save/dir。
// 回 { path }(取消或無法開啟 → path=null,呼叫端可 fallback 到內建瀏覽 modal)。
export async function fsNativePick(opts: {
  mode?: 'open' | 'save' | 'dir'
  initial_dir?: string
  default_name?: string
  py_only?: boolean
} = {}): Promise<{ path: string | null; error?: string }> {
  const res = await fetch(`${BASE}/fs/native-pick`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opts),
  })
  if (!res.ok) return { path: null, error: `HTTP ${res.status}` }
  return res.json()
}

// ── Claude Code Skills ──────────────────────────────────────
export interface AvailableSkill {
  name: string
  display_name: string
  description: string
  path: string
  has_scripts: boolean
  has_references: boolean
  has_assets: boolean
  has_package_json?: boolean
  has_requirements?: boolean
}
export async function listAvailableSkills(): Promise<{ skills_root: string; exists: boolean; skills: AvailableSkill[] }> {
  const res = await fetchWithRetry(`${BASE}/skills/available`)
  if (!res.ok) throw new Error('載入 Skill 清單失敗')
  return res.json()
}

export interface SkillDependencies {
  skill_name: string
  found: boolean
  path?: string
  python?: {
    requirements_txt: string[]
    imports_detected: string[]
    undeclared_imports?: string[]
    suggested_pip: string[]
    installed: string[]
    missing: string[]
    has_manifest?: boolean
  }
  node?: {
    package_json: { dependencies?: Record<string, string>; devDependencies?: Record<string, string> } | null
    needs_npm_install: boolean
    suggested_npm?: string[]
    installed_npm?: string[]
    missing_npm?: string[]
    npm_available?: boolean
  }
  system_tools?: string[]
}
export async function scanSkillDependencies(displayName: string): Promise<SkillDependencies> {
  const res = await fetchWithRetry(`${BASE}/skills/${encodeURIComponent(displayName)}/dependencies`)
  if (!res.ok) throw new Error('掃描依賴失敗')
  return res.json()
}

// ── Subagent role CRUD ────────────────────────────────────────────
export interface SubagentRole {
  role_id: string
  label: string
  description: string
  tools: string[]
  system_prompt: string
  source: 'builtin' | 'custom'
  is_builtin: boolean
}
export interface SelectableTool {
  id: string
  description: string
}
export interface SubagentRolesResponse {
  roles: SubagentRole[]
  selectable_tools: SelectableTool[]
  builtin_ids: string[]
}
export interface SubagentRolePayload {
  role_id: string
  label: string
  description: string
  tools: string[]
  system_prompt: string
}

export async function listSubagentRoles(): Promise<SubagentRolesResponse> {
  const res = await fetchWithRetry(`${BASE}/subagent/roles`)
  if (!res.ok) throw new Error('載入 subagent role 清單失敗')
  return res.json()
}

async function _readErrorDetail(res: Response): Promise<string> {
  try {
    const data = await res.json()
    return data?.detail || `HTTP ${res.status}`
  } catch {
    return `HTTP ${res.status}`
  }
}

export async function createSubagentRole(payload: SubagentRolePayload): Promise<{ ok: boolean; role_id: string; total_custom: number }> {
  const res = await fetchWithRetry(`${BASE}/subagent/roles`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new Error(await _readErrorDetail(res))
  return res.json()
}

export async function updateSubagentRole(roleId: string, payload: SubagentRolePayload): Promise<{ ok: boolean; role_id: string }> {
  const res = await fetchWithRetry(`${BASE}/subagent/roles/${encodeURIComponent(roleId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new Error(await _readErrorDetail(res))
  return res.json()
}

export async function deleteSubagentRole(roleId: string): Promise<{ ok: boolean; deleted: string; remaining_custom: number }> {
  const res = await fetchWithRetry(`${BASE}/subagent/roles/${encodeURIComponent(roleId)}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error(await _readErrorDetail(res))
  return res.json()
}

// ── Log Analysis ────────────────────────────────────────────
export interface LogSuggestion { module: string; pip_name: string; found_in: string[] }
export interface LogAnalysis { analyzed: number; files: { name: string; size: number; has_errors: boolean }[]; suggestions: LogSuggestion[] }
export async function analyzeRecentLogs(count: number = 5): Promise<LogAnalysis> {
  const res = await fetch(`${BASE}/pipeline/logs/analyze?count=${count}`)
  if (!res.ok) throw new Error('分析失敗')
  return res.json()
}

// ── Files ───────────────────────────────────────────────────
export async function listFiles(path = ''): Promise<FileItem[]> {
  const res = await fetch(`${BASE}/files?path=${encodeURIComponent(path)}`)
  const data = await res.json()
  return data.files ?? []
}

export async function readFile(path: string): Promise<{ content: string; name: string }> {
  const res = await fetch(`${BASE}/files/content?path=${encodeURIComponent(path)}`)
  if (!res.ok) throw new Error('Read file failed')
  return res.json()
}

// ── Health ──────────────────────────────────────────────────
export async function getHealth(): Promise<{ status: string; warnings: string[] }> {
  const res = await fetch(`${BASE}/health`)
  return res.json()
}

// ── OpenCLI ─────────────────────────────────────────────────
export async function getOpenCLISites(): Promise<OpenCLICategory[]> {
  const res = await fetch(`${BASE}/opencli/sites`)
  const data = await res.json()
  return data.sites ?? []
}

export async function getOpenCLIStatus(): Promise<OpenCLIStatus> {
  const res = await fetch(`${BASE}/opencli/status`)
  return res.json()
}

// ── Pipeline ─────────────────────────────────────────────────
export async function getPipelineRuns(): Promise<PipelineRun[]> {
  const res = await fetchWithRetry(`${BASE}/pipeline/runs`)
  if (!res.ok) return []
  const data = await res.json()
  return data.runs ?? []
}

export async function getPipelineRun(runId: string): Promise<PipelineRun> {
  const res = await fetchWithRetry(`${BASE}/pipeline/runs/${runId}`)
  if (!res.ok) throw new Error('找不到 pipeline run')
  return res.json()
}

export async function startPipeline(yamlContent: string, validate = true, useRecipe = false, workflowId?: string, noSaveRecipe = false, inputParams?: Record<string, string>): Promise<{ run_id: string }> {
  const res = await fetch(`${BASE}/pipeline/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      yaml_content: yamlContent,
      validate,
      use_recipe: useRecipe,
      workflow_id: workflowId ?? null,
      no_save_recipe: noSaveRecipe,
      input_params: inputParams || {},
    }),
  })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.detail ?? 'Pipeline 啟動失敗')
  }
  return res.json()
}

export async function deletePipelineRun(runId: string): Promise<void> {
  const res = await fetch(`${BASE}/pipeline/runs/${runId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('刪除失敗')
}

export async function resumePipeline(runId: string, decision: 'retry' | 'skip' | 'abort' | 'continue' | 'retry_with_hint' | 'answer' | 'install_dep' | 'approve_command' | 'deny_command' | 'hint_command' | 'redo_prev' | 'self_heal_now', hint?: string): Promise<{ message: string }> {
  const body: Record<string, string> = { decision }
  if (hint) body.hint = hint
  const res = await fetch(`${BASE}/pipeline/runs/${runId}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error('Resume 失敗')
  return res.json()
}

export async function abortPipeline(runId: string): Promise<{ message: string }> {
  const res = await fetch(`${BASE}/pipeline/runs/${runId}/abort`, {
    method: 'POST',
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail ?? '中止失敗')
  }
  return res.json()
}

export async function savePendingRecipes(runId: string): Promise<{ saved: number }> {
  const res = await fetch(`${BASE}/pipeline/runs/${runId}/save-recipes`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('儲存 Recipe 失敗')
  return res.json()
}

export async function getPipelineLog(runId: string): Promise<{ log: string }> {
  const res = await fetch(`${BASE}/pipeline/runs/${runId}/log`)
  if (!res.ok) throw new Error('取得 log 失敗')
  return res.json()
}

export async function getPipelineScheduled(): Promise<ScheduledTask[]> {
  const res = await fetchWithRetry(`${BASE}/pipeline/scheduled`)
  if (!res.ok) return []
  const data = await res.json()
  return data.tasks ?? []
}

export async function cancelPipelineSchedule(name: string): Promise<void> {
  const res = await fetch(`${BASE}/pipeline/scheduled/cancel-by-name/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error('取消排程失敗')
}

export async function createPipelineSchedule(req: {
  name: string
  yaml_content: string
  schedule_type: string
  schedule_expr: string
  validate?: boolean
  use_recipe?: boolean
  workflow_id?: string
}): Promise<ScheduledTask> {
  const res = await fetch(`${BASE}/pipeline/scheduled`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.detail ?? '建立排程失敗')
  }
  const data = await res.json()
  return data.task
}

export async function deletePipelineSchedule(taskId: string): Promise<void> {
  const res = await fetch(`${BASE}/pipeline/scheduled/${taskId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('刪除排程失敗')
}

// ── Settings ─────────────────────────────────────────────────
export interface ModelSettings {
  provider: 'groq' | 'ollama' | 'gemini' | 'openai' | 'anthropic' | 'claude_cli'
  model: string
  ollama_base_url: string
  ollama_thinking: 'auto' | 'on' | 'off'
  ollama_num_ctx: number
  gemini_thinking: 'off' | 'auto' | 'low' | 'medium' | 'high'
  anthropic_thinking: 'off' | 'on'
  // 副模型(選填、空 = 不啟用)
  secondary_provider?: '' | 'groq' | 'ollama' | 'gemini' | 'openai' | 'anthropic' | 'claude_cli'
  secondary_model?: string
  secondary_ollama_thinking?: 'auto' | 'on' | 'off'
  secondary_ollama_num_ctx?: number
  secondary_gemini_thinking?: 'off' | 'auto' | 'low' | 'medium' | 'high'
  secondary_anthropic_thinking?: 'off' | 'on'
}

export interface ModelOption {
  id: string
  label: string
  supports_thinking?: boolean
  context_length?: number
}

export interface AvailableModels {
  groq: ModelOption[]
  groq_error: string | null
  gemini: ModelOption[]
  gemini_error: string | null
  openai: ModelOption[]
  openai_error: string | null
  anthropic: ModelOption[]
  anthropic_error: string | null
  ollama: ModelOption[]
  ollama_base_url: string
  ollama_error: string | null
  claude_cli?: ModelOption[]
  claude_cli_error?: string | null
}

export async function getModelSettings(): Promise<ModelSettings> {
  const res = await fetchWithRetry(`${BASE}/settings/model`)
  if (!res.ok) throw new Error('讀取設定失敗')
  return res.json()
}

export async function saveModelSettings(s: ModelSettings): Promise<ModelSettings> {
  const res = await fetch(`${BASE}/settings/model`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(s),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail ?? '儲存失敗')
  }
  return res.json()
}

export async function getAvailableModels(): Promise<AvailableModels> {
  // 用 DIRECT_BASE 繞過 Next.js proxy：此端點要打 4 個外部 API（Groq/Gemini/OpenRouter/Ollama）
  // 經常 10 秒以上，走 proxy 容易碰到 reload 空窗或 timeout
  const res = await fetchWithRetry(`${DIRECT_BASE}/settings/models/available`)
  if (!res.ok) throw new Error('讀取模型清單失敗')
  return res.json()
}

// ── Skill Packages ─────────────────────────────────────────
export interface SkillPackage {
  name: string
  installed: boolean
  version: string
}

export interface EnvPaths {
  project_root: string
  test_workflows_dir: string | null
  has_finance_example: boolean
  finance_example_dir: string | null
  path_sep: string
}

export async function getEnvPaths(): Promise<EnvPaths> {
  const res = await fetchWithRetry(`${BASE}/env/paths`)
  if (!res.ok) throw new Error('讀取專案路徑失敗')
  return res.json()
}

// ── Computer Use 錄製 API ──────────────────────────────────────
export interface RecordingStatus {
  recording: boolean
  session_id?: string
  output_dir?: string
  action_count?: number
  duration_sec?: number
  stopped?: boolean
  latest_actions?: any[]
}

export async function startComputerUseRecording(sessionId: string, outputDir: string): Promise<RecordingStatus> {
  const res = await fetch(`${BASE}/computer-use/recording/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, output_dir: outputDir }),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`開始錄製失敗：${detail || res.status}`)
  }
  return res.json()
}

export async function stopComputerUseRecording(): Promise<RecordingStatus> {
  const res = await fetch(`${BASE}/computer-use/recording/stop`, { method: 'POST' })
  if (!res.ok) throw new Error('停止錄製失敗')
  return res.json()
}

export async function armComputerUseRecordingHotkey(sessionId: string, outputDir: string): Promise<{ armed: boolean; key: string }> {
  const res = await fetch(`${BASE}/computer-use/recording/arm-hotkey`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, output_dir: outputDir }),
  })
  if (!res.ok) throw new Error('arm 熱鍵失敗')
  return res.json()
}

export async function disarmComputerUseRecordingHotkey(): Promise<{ armed: boolean }> {
  const res = await fetch(`${BASE}/computer-use/recording/disarm-hotkey`, { method: 'POST' })
  if (!res.ok) return { armed: false }
  return res.json()
}

export async function duplicateCanvasAssets(src: string, dest: string): Promise<{ ok: boolean; copied_files: number; error?: string }> {
  const res = await fetch(`${BASE}/canvas/duplicate-assets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ src, dest }),
  })
  if (!res.ok) return { ok: false, copied_files: 0, error: `HTTP ${res.status}` }
  return res.json()
}

export async function getComputerUseRecordingStatus(): Promise<RecordingStatus> {
  const res = await fetchWithRetry(`${BASE}/computer-use/recording/status`)
  if (!res.ok) throw new Error('查詢錄製狀態失敗')
  return res.json()
}

export async function loadComputerUseRecording(outputDir: string): Promise<{ actions: any[]; meta: any; output_dir: string }> {
  const res = await fetchWithRetry(`${BASE}/computer-use/recording/load?output_dir=${encodeURIComponent(outputDir)}`)
  if (!res.ok) throw new Error('載入錄製結果失敗')
  return res.json()
}

export interface GroundingStatus {
  available: boolean
  sandbox_ok: boolean
  model_present: boolean
  gpu_ok: boolean
  vram_gb: number
  precision: string
  reason: string
  install_hint: string
  note: string
}

/** 這台機器能不能用 vlm_mode='grounding'（後端有 60s 快取）。 */
export async function getGroundingStatus(): Promise<GroundingStatus> {
  const res = await fetchWithRetry(`${BASE}/computer-use/grounding/status`)
  if (!res.ok) throw new Error('查詢 GUI 定位狀態失敗')
  return res.json()
}

/** 替 vlm_mode='grounding' 產生「要點什麼」的描述草稿。
 *  使用者按 🪄 才呼叫（走一次雲端視覺 API），不在錄製時自動全跑。 */
export async function describeForGrounding(
  assetsDir: string, action: Record<string, unknown>,
): Promise<{ description: string; verified: boolean; verify_px: number | null; verify_msg: string }> {
  const res = await fetch(`${BASE}/computer-use/grounding/describe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assets_dir: assetsDir, action }),
  })
  const j = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(j?.detail || '產生描述失敗')
  return j
}

export async function deleteComputerUseAssets(dir: string): Promise<{ deleted: boolean; path: string; reason?: string }> {
  const res = await fetch(`${BASE}/computer-use/assets?dir=${encodeURIComponent(dir)}`, { method: 'DELETE' })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `刪除錨點資料夾失敗 (${res.status})`)
  }
  return res.json()
}

export function computerUseAssetImageUrl(dir: string, name: string): string {
  return `${BASE}/computer-use/assets/image?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(name)}`
}

export interface MonitorRect {
  left: number
  top: number
  width: number
  height: number
}

export async function getComputerUseMonitors(): Promise<{ monitors: MonitorRect[] }> {
  // monitors[0] = 虛擬桌面全景；monitors[1..N] = 每台實體螢幕
  const res = await fetch(`${BASE}/computer-use/monitors`)
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `讀取 monitor 清單失敗 (${res.status})`)
  }
  return res.json()
}

export interface CropAnchorReq {
  dir: string
  full_image: string
  click_x: number
  click_y: number
  full_left?: number
  full_top?: number
  crop_left: number
  crop_top: number
  crop_width: number
  crop_height: number
  save_as: string
}

export async function cropAnchorFromFull(req: CropAnchorReq): Promise<{
  image: string
  anchor_off_x: number
  anchor_off_y: number
  width: number
  height: number
  variance: number
}> {
  const res = await fetch(`${BASE}/computer-use/assets/crop`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ full_left: 0, full_top: 0, ...req }),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `裁切錨點失敗 (${res.status})`)
  }
  return res.json()
}

/** UIA element tree 節點(遞迴)。給 frontend tree picker 用。 */
export interface UiaElement {
  type: string                 // ControlType 名(Button / Edit / DataGrid 等)
  name: string
  auto_id: string              // AutomationId(可能是空)
  rect: number[]               // [x, y, w, h] 絕對桌面座標
  enabled: boolean
  offscreen: boolean
  children: UiaElement[]
}

export interface UiaInspectResult {
  ok: boolean
  window: { name: string; class: string; rect: number[]; process_id: number }
  tree: UiaElement
  error?: string
  /** 掃描被截斷的情況;depth_cut>0 代表還有更深的元素沒讀到 */
  truncated?: { depth_cut: number; budget_hit: boolean; nodes: number; max_depth: number }
}

/** 檢視指定視窗的 UIA element tree(空 window = 當前 foreground)。 */
/** 螢幕 OCR 試抓結果。找不到時分兩種：標籤沒找到 / 標籤找到但旁邊沒有合格式的值。 */
export interface OcrProbeResult {
  ok: boolean
  error?: string
  word_count?: number
  found?: boolean
  value?: string
  label_read_as?: string
  label_score?: number
  direction?: string
  box?: number[]
  label_found?: boolean
  reason?: string
  candidates?: string[]
}

/** 立刻對當下螢幕試抓一次，讓使用者在設定時就看到會抓到什麼。 */
export async function ocrProbe(req: {
  label: string
  direction?: 'right' | 'below'
  kind?: 'amount' | 'ident' | 'taxid' | 'any'
  region?: number[]
}): Promise<OcrProbeResult> {
  const res = await fetch(`${BASE}/computer-use/ocr/probe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      label: req.label,
      direction: req.direction ?? 'right',
      kind: req.kind ?? 'amount',
      ...(req.region ? { region: req.region } : {}),
    }),
  })
  if (!res.ok) throw new Error(`OCR 試抓失敗（HTTP ${res.status}）`)
  return res.json()
}

export async function uiaInspect(req: {
  window?: string
  max_depth?: number
  max_children_per_node?: number
}): Promise<UiaInspectResult> {
  const res = await fetch(`${BASE}/computer-use/uia/inspect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      window: req.window ?? '',
      max_depth: req.max_depth ?? 6,
      max_children_per_node: req.max_children_per_node ?? 50,
    }),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `UIA inspect 失敗 (${res.status})`)
  }
  return res.json()
}

/** 桌面對應位置畫紅框 outline、ttl_ms 後自動消失。 */
export async function uiaHighlight(req: {
  x: number; y: number; width: number; height: number; ttl_ms?: number
}): Promise<void> {
  await fetch(`${BASE}/computer-use/uia/highlight`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...req, ttl_ms: req.ttl_ms ?? 1500 }),
  }).catch(() => {})  // hover 失敗不打擾使用者
}

/** 立即清除紅框 outline。 */
export async function uiaHighlightClear(): Promise<void> {
  await fetch(`${BASE}/computer-use/uia/highlight/clear`, {
    method: 'POST',
  }).catch(() => {})
}

export interface UiaWindowInfo {
  name: string
  class: string
  rect: number[]
  is_offscreen: boolean
}

/** 列當下所有可見的 top-level 視窗。 */
export async function uiaListWindows(): Promise<{ ok: boolean; windows: UiaWindowInfo[] }> {
  const res = await fetch(`${BASE}/computer-use/uia/windows`)
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `列視窗失敗 (${res.status})`)
  }
  return res.json()
}

/** UIA Live Picker — 滑鼠 hover 桌面選元素 + F8 確認、F9 取消 */
export interface UiaPickerStatus {
  running: boolean
  hovered: UiaElement | null
  confirmed: UiaElement | null
  error: string | null
}

export async function uiaPickerStart(): Promise<{ ok: boolean; started: boolean; running: boolean }> {
  const res = await fetch(`${BASE}/computer-use/uia/picker/start`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
export async function uiaPickerPoll(): Promise<UiaPickerStatus> {
  const res = await fetch(`${BASE}/computer-use/uia/picker/poll`)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
export async function uiaPickerConsume(): Promise<{ ok: boolean; element: UiaElement | null }> {
  const res = await fetch(`${BASE}/computer-use/uia/picker/consume`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
export async function uiaPickerStop(): Promise<{ ok: boolean; was_running: boolean }> {
  const res = await fetch(`${BASE}/computer-use/uia/picker/stop`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
export async function uiaPickerConfirm(): Promise<{ ok: boolean; element?: UiaElement; error?: string }> {
  const res = await fetch(`${BASE}/computer-use/uia/picker/confirm`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

/** 把 base64 PNG 直接存到 assets_dir(VLM 錨點立即截圖用、瀏覽器內裁切後上傳) */
export async function saveAnchorPng(req: {
  dir: string
  name: string
  png_b64: string
}): Promise<{ image: string; width: number; height: number; variance: number; size_bytes: number }> {
  const res = await fetch(`${BASE}/computer-use/assets/save-png`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `存錨點 PNG 失敗 (${res.status})`)
  }
  return res.json()
}

export interface NodeStatus {
  node_installed: boolean
  node_version: string
  npm_installed: boolean
  npm_version: string
  install_hint: string
}

export async function getNodeStatus(): Promise<NodeStatus> {
  const res = await fetchWithRetry(`${BASE}/settings/node-status`)
  if (!res.ok) throw new Error('讀取 Node.js 狀態失敗')
  return res.json()
}

// ── 主機系統工具(LibreOffice / FFmpeg / Tesseract...)健康檢查 ─────────
export interface HostTool {
  name: string
  bin: string
  installed: boolean
  found_at: string | null
  install_cmd: { windows: string; macos: string; linux: string }
  why: string
  required: boolean
}

export interface HostToolsResponse {
  platform: 'Windows' | 'Darwin' | 'Linux' | string
  tools: HostTool[]
}

export async function getHostTools(): Promise<HostToolsResponse> {
  const res = await fetchWithRetry(`${BASE}/system/host-tools`)
  if (!res.ok) throw new Error('讀取主機工具狀態失敗')
  return res.json()
}

// target = 'auto' 會跟著當前 sandbox toggle，'host' / 'sandbox' 可明確指定
export type SkillPackageTarget = 'auto' | 'host' | 'sandbox'

export interface SkillPackagesResponse {
  target: 'host' | 'sandbox'   // 後端 resolve 後實際是哪一邊
  packages: SkillPackage[]
}

export async function getSkillPackages(target: SkillPackageTarget = 'auto'): Promise<SkillPackagesResponse> {
  const qs = target === 'auto' ? '' : `?target=${target}`
  const res = await fetchWithRetry(`${BASE}/settings/skill-packages${qs}`)
  if (!res.ok) throw new Error('讀取套件清單失敗')
  return res.json()
}

export async function addSkillPackage(name: string, target: SkillPackageTarget = 'auto'): Promise<{ message: string; target: string }> {
  const res = await fetch(`${BASE}/settings/skill-packages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, target }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '安裝失敗')
  return data
}

export async function removeSkillPackage(name: string, target: SkillPackageTarget = 'auto'): Promise<{ message: string; target: string }> {
  const qs = target === 'auto' ? '' : `?target=${target}`
  const res = await fetch(`${BASE}/settings/skill-packages/${encodeURIComponent(name)}${qs}`, {
    method: 'DELETE',
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '移除失敗')
  return data
}

// ── 觸發器:Webhook + 檔案夾監看(綁單一 workflow、事件驅動啟動)──────────
export interface WebhookInfo {
  token: string; workflow_id: string; enabled: boolean
  created_at: number; last_fired_at: number; fire_count: number
  url: string | null
}
export interface FolderWatchInfo {
  id: string; workflow_id: string; folder_path: string; pattern: string
  enabled: boolean; created_at: number; last_seen_mtime: number; trigger_count: number
}

export async function getWebhook(workflowId: string): Promise<WebhookInfo | null> {
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/webhook`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error('讀取 webhook 失敗')
  return res.json()
}

export async function createWebhook(workflowId: string): Promise<WebhookInfo> {
  const res = await fetch(`${BASE}/workflows/${workflowId}/webhook`, { method: 'POST' })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '建立 webhook 失敗')
  return data
}

export async function disableWebhook(workflowId: string): Promise<void> {
  const res = await fetch(`${BASE}/workflows/${workflowId}/webhook`, { method: 'DELETE' })
  if (!res.ok) throw new Error('停用 webhook 失敗')
}

export async function getFolderWatch(workflowId: string): Promise<FolderWatchInfo | null> {
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/folder-watch`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error('讀取檔案夾監看失敗')
  return res.json()
}

export async function createFolderWatch(workflowId: string, folderPath: string, pattern: string): Promise<FolderWatchInfo> {
  const res = await fetch(`${BASE}/workflows/${workflowId}/folder-watch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_path: folderPath, pattern }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '建立監看失敗')
  return data
}

export async function disableFolderWatch(workflowId: string): Promise<void> {
  const res = await fetch(`${BASE}/workflows/${workflowId}/folder-watch`, { method: 'DELETE' })
  if (!res.ok) throw new Error('停用監看失敗')
}

// ── Secrets Vault(值永不回傳;工作流以 {{ secrets.名稱 }} 引用)──────────
export interface SecretMeta { name: string; created_at: number; updated_at: number }

export async function listSecrets(): Promise<SecretMeta[]> {
  const res = await fetchWithRetry(`${BASE}/settings/secrets`)
  if (!res.ok) throw new Error('讀取 secrets 失敗')
  return (await res.json()).secrets
}

export async function setSecret(name: string, value: string): Promise<{ usage: string }> {
  const res = await fetch(`${BASE}/settings/secrets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, value }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '儲存失敗')
  return data
}

export async function deleteSecret(name: string): Promise<void> {
  const res = await fetch(`${BASE}/settings/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('刪除失敗')
}

// ── Run Diff:兩次執行的逐步輸出差異 ─────────────────────────────────────
export interface RunDiffOutput {
  kind: 'json' | 'text' | 'binary' | 'presence' | 'none' | 'error'
  identical?: boolean
  summary: string
  added?: { key: string; value: unknown }[]
  removed?: { key: string; value: unknown }[]
  changed?: { key: string; before: unknown; after: unknown; delta?: number }[]
  diff?: string
  plus?: number; minus?: number
  truncated?: boolean
  error?: string
}
export interface RunDiffStep {
  step_name: string
  in_a: boolean; in_b: boolean
  validation_a: string | null; validation_b: string | null
  output: RunDiffOutput
}
export interface RunDiffResult {
  run_a: { run_id: string; started_at?: string; status?: string }
  run_b: { run_id: string; started_at?: string; status?: string }
  steps: RunDiffStep[]
  changed_steps: number
  summary: string
}

export async function getRunDiff(workflowId: string, runA?: string, runB?: string): Promise<RunDiffResult> {
  const qs = runA && runB ? `?run_a=${encodeURIComponent(runA)}&run_b=${encodeURIComponent(runB)}` : ''
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/run-diff${qs}`)
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '比較失敗')
  return data
}

// ── MCP servers(像 Skill 掛載:安裝時 live 驗證、乾淨移除)────────────────
export interface McpTool { name: string; description: string; input_schema?: unknown }
export interface McpServer {
  name: string; command: string; args: string[]; env: Record<string, string>
  enabled: boolean; tools_cache: McpTool[]; created_at: number; last_scan_at: number
}
export interface McpCatalogEntry {
  id: string; name: string; description: string
  command: string; args: string[]; env_keys: string[]
  guidance?: string   // 去哪拿 token 的指引(SaaS server 才有)
}

export async function getMcpCatalog(): Promise<McpCatalogEntry[]> {
  const res = await fetchWithRetry(`${BASE}/mcp/catalog`)
  if (!res.ok) throw new Error('讀取 MCP 目錄失敗')
  return (await res.json()).catalog
}

export async function getMcpServers(): Promise<McpServer[]> {
  const res = await fetchWithRetry(`${BASE}/mcp/servers`)
  if (!res.ok) throw new Error('讀取 MCP server 清單失敗')
  return (await res.json()).servers
}

export async function addMcpServer(payload: { name: string; command?: string; args?: string[]; env?: Record<string, string>; transport?: string; url?: string; remote_consent?: boolean; admin_password?: string }): Promise<{ server: McpServer; tool_count: number }> {
  const res = await fetch(`${BASE}/mcp/servers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '安裝 MCP server 失敗')
  return data
}

export async function refreshMcpServer(name: string): Promise<{ server: McpServer; tool_count: number }> {
  const res = await fetch(`${BASE}/mcp/servers/${encodeURIComponent(name)}/refresh`, { method: 'POST' })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '重掃失敗')
  return data
}

export interface McpServerConfig { config: Record<string, unknown>; tools: string[]; note: string }
export async function getMcpServerConfig(): Promise<McpServerConfig> {
  const res = await fetchWithRetry(`${BASE}/mcp/server-config`)
  if (!res.ok) throw new Error('讀取 server 設定失敗')
  return res.json()
}

export async function removeMcpServer(name: string): Promise<{ removed: string }> {
  const res = await fetch(`${BASE}/mcp/servers/${encodeURIComponent(name)}`, { method: 'DELETE' })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '移除失敗')
  return data
}

export interface UnlistedPackage { name: string; version: string }
export async function scanUnlistedPackages(): Promise<UnlistedPackage[]> {
  const res = await fetchWithRetry(`${BASE}/settings/skill-packages/unlisted`)
  if (!res.ok) throw new Error('掃描 venv 失敗')
  const data = await res.json()
  return data.packages
}

export async function adoptExistingPackage(name: string): Promise<string> {
  const res = await fetch(`${BASE}/settings/skill-packages/adopt`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '加入失敗')
  return data.message
}

// ── Workflows ───────────────────────────────────────────────
export interface WorkflowData {
  id: string
  name: string
  yaml: string
  canvas: { nodes: any[]; edges: any[] }
  validate: boolean
  created_at: number
  updated_at: number
}

export async function listWorkflows(): Promise<WorkflowData[]> {
  const res = await fetchWithRetry(`${BASE}/workflows`)
  if (!res.ok) throw new Error('讀取工作流失敗')
  return res.json()
}

export async function createWorkflowApi(name: string = '新工作流', canvas?: { nodes: any[]; edges: any[] }, validate = false): Promise<WorkflowData> {
  const res = await fetchWithRetry(`${BASE}/workflows`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, canvas, validate }),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`建立工作流失敗 (${res.status}): ${detail || '後端暫時無回應，請稍後再試'}`)
  }
  return res.json()
}

export async function updateWorkflowApi(
  id: string,
  patch: { name?: string; canvas?: any; validate?: boolean; yaml?: string; base_updated_at?: number },
  // keepalive：關頁前 flush 防抖時用 —— 普通 fetch 會被瀏覽器取消
  opts?: { keepalive?: boolean },
): Promise<WorkflowData> {
  const res = await fetchWithRetry(`${BASE}/workflows/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
    ...(opts?.keepalive ? { keepalive: true } : {}),
  })
  if (res.status === 409) {
    // 樂觀鎖衝突：工作流被別處（AI 助手 / 其他分頁）改過了
    const detail = await res.json().then(d => d?.detail).catch(() => '')
    throw new Error(`CONFLICT:${detail || '工作流已被別處修改'}`)
  }
  if (!res.ok) throw new Error('更新工作流失敗')
  return res.json()
}

export async function deleteWorkflowApi(id: string, cascade = true): Promise<void> {
  const res = await fetchWithRetry(`${BASE}/workflows/${id}?cascade=${cascade}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('刪除工作流失敗')
}

export function exportWorkflowUrl(id: string): string {
  return `${BASE}/workflows/${id}/export`
}

export async function importWorkflow(file: File): Promise<{
  workflow: WorkflowData
  recipe_count: number
  has_local_scripts: boolean
}> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/workflows/import`, { method: 'POST', body: form })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail ?? '匯入失敗')
  }
  return res.json()
}

// ── Recipe Book ─────────────────────────────────────────────
export interface Recipe {
  recipe_id: string
  workflow_id: string
  step_name: string
  task_hash: string
  input_fingerprints: Record<string, string>
  output_path: string | null
  code: string
  python_version: string
  success_count: number
  fail_count: number
  created_at: number
  last_success_at: number
  last_fail_at: number
  avg_runtime_sec: number
  disabled: boolean
  was_interactive?: boolean
}

export async function listRecipes(): Promise<Recipe[]> {
  const res = await fetch(`${BASE}/recipes`)
  if (!res.ok) throw new Error('讀取 recipes 失敗')
  return res.json()
}

export async function deleteRecipe(workflowId: string, stepName: string): Promise<void> {
  const res = await fetch(`${BASE}/recipes/${encodeURIComponent(workflowId)}/${encodeURIComponent(stepName)}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error('刪除 recipe 失敗')
}

export async function deleteWorkflowRecipes(workflowId: string): Promise<number> {
  const res = await fetch(`${BASE}/recipes/${encodeURIComponent(workflowId)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('刪除 workflow recipes 失敗')
  const data = await res.json()
  return data.deleted_count ?? 0
}

export interface RecipeStatus {
  has_recipes: boolean
  total_skill_steps: number
  covered_steps: number
  steps: Record<string, { has_recipe: boolean; success_count: number; avg_runtime_sec: number }>
}

export async function getRecipeStatus(pipelineName: string, stepNames: string[]): Promise<RecipeStatus> {
  const params = new URLSearchParams({ steps: stepNames.join(',') })
  const res = await fetch(`${BASE}/recipes/status/${encodeURIComponent(pipelineName)}?${params}`)
  if (!res.ok) throw new Error('查詢 recipe 狀態失敗')
  return res.json()
}

// ── Notification Settings ──────────────────────────────────
export interface NotificationSettings {
  telegram_bot_token: string
  telegram_chat_id: string
  telegram_remote_control: boolean
  line_notify_token: string
  // 後端額外旗標：對應的值是否存在於 .env（fallback 來源）
  // 後端不會回傳 env 明文，前端用旗標判斷「可否啟用遠端遙控」
  telegram_bot_token_env_present?: boolean
  telegram_chat_id_env_present?: boolean
}

export async function getNotificationSettings(): Promise<NotificationSettings> {
  const res = await fetchWithRetry(`${BASE}/settings/notifications`)
  if (!res.ok) throw new Error('讀取通知設定失敗')
  return res.json()
}

export async function saveNotificationSettings(s: Partial<NotificationSettings>): Promise<NotificationSettings> {
  const res = await fetch(`${BASE}/settings/notifications`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(s),
  })
  if (!res.ok) throw new Error('儲存通知設定失敗')
  return res.json()
}

// ── Web Search (Tavily) ────────────────────────────────────
// 後端不回 key 明文（只回 has_key flag 表示「已設定」）— 安全考量
export interface WebSearchSettingsStatus {
  has_key: boolean
  web_search_enabled: boolean
  web_search_full_content_default: boolean
  web_search_deep_default: boolean
}

export interface WebSearchSettingsInput {
  // null / undefined = 不動；空字串 = 清除 key
  tavily_api_key?: string
  web_search_enabled?: boolean
  web_search_full_content_default?: boolean
  web_search_deep_default?: boolean
}

export async function getWebSearchSettings(): Promise<WebSearchSettingsStatus> {
  const res = await fetchWithRetry(`${BASE}/settings/web-search`)
  if (!res.ok) throw new Error('讀取網路搜尋設定失敗')
  return res.json()
}

export async function saveWebSearchSettings(s: WebSearchSettingsInput): Promise<WebSearchSettingsStatus> {
  const res = await fetch(`${BASE}/settings/web-search`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(s),
  })
  if (!res.ok) throw new Error('儲存網路搜尋設定失敗')
  return res.json()
}

// ── Skill Sandbox ────────────────────────────────────
export interface SandboxStatus {
  mode: 'host' | 'wsl_docker'
  wsl_ok: boolean
  docker_ok: boolean
  docker_version: string
  container_exists: boolean
  container_running: boolean
  ready: boolean
  reasons: string[]
  hint: string
}

export async function getSandboxStatus(refresh = false): Promise<SandboxStatus> {
  const qs = refresh ? '?refresh=true' : ''
  const res = await fetchWithRetry(`${BASE}/settings/sandbox${qs}`)
  if (!res.ok) throw new Error('讀取沙盒狀態失敗')
  return res.json()
}

export async function setSandboxMode(mode: 'host' | 'wsl_docker'): Promise<SandboxStatus> {
  const res = await fetch(`${BASE}/settings/sandbox`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  })
  if (!res.ok) throw new Error('切換沙盒模式失敗')
  return res.json()
}

// ── AI 助手長期記憶 ───────────────────────────────────────
export interface MemorySettings { enabled: boolean; aggressive: boolean; fact_count?: number }
export interface MemoryFact { key: string; value: string; category: string; source: string; confidence: number; updated_at: number }

export async function getMemorySettings(): Promise<MemorySettings> {
  const res = await fetchWithRetry(`${BASE}/settings/memory`)
  if (!res.ok) throw new Error('讀取記憶設定失敗')
  return res.json()
}

export async function setMemorySettings(payload: { enabled?: boolean; aggressive?: boolean }): Promise<MemorySettings> {
  const res = await fetch(`${BASE}/settings/memory`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new Error('切換記憶設定失敗')
  return res.json()
}

export async function listMemoryFacts(): Promise<MemoryFact[]> {
  const res = await fetchWithRetry(`${BASE}/memory/facts`)
  if (!res.ok) throw new Error('讀取記憶清單失敗')
  return (await res.json()).facts || []
}

export async function deleteMemoryFact(key: string): Promise<void> {
  const res = await fetch(`${BASE}/memory/facts/${encodeURIComponent(key)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('刪除記憶失敗')
}

// ── Skill 檔案目錄 ────────────────────────────────────────
export interface SkillsDirStatus {
  skills_dir: string        // 使用者設定的自訂路徑（空 = 用預設）
  resolved: string          // 實際解析到的路徑
  default?: string          // 預設路徑 ~/.agents/skills/
  exists: boolean
  skill_count: number
  env_override?: boolean    // 是否被環境變數 SKILLS_DIR 覆蓋
}

export async function getSkillsDir(): Promise<SkillsDirStatus> {
  const res = await fetchWithRetry(`${BASE}/settings/skills-dir`)
  if (!res.ok) throw new Error('讀取 Skill 目錄設定失敗')
  return res.json()
}

export async function setSkillsDir(skillsDir: string): Promise<SkillsDirStatus> {
  const res = await fetch(`${BASE}/settings/skills-dir`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skills_dir: skillsDir }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '設定 Skill 目錄失敗')
  }
  return res.json()
}

export async function pipelineChat(
  messages: Array<{ role: 'user' | 'assistant'; content: string }>,
  workflowId?: string | null,
): Promise<{
  reply: string
  has_yaml: boolean
  yaml_content: string | null
  yaml_error?: string | null
}> {
  // 使用 DIRECT_BASE 繞過 Next.js dev rewrite proxy 的 ~30s socket timeout
  // （LLM 回應經常 30-60s；走 proxy 會被截斷回 500）
  // 不用 fetchWithRetry：觸發 LLM 有金流/速率成本，失敗讓使用者手動重送
  const body: Record<string, unknown> = { messages }
  if (workflowId) body.workflow_id = workflowId
  const res = await fetch(`${DIRECT_BASE}/pipeline/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    // 後端走 _friendly_llm_error 翻譯後、HTTPException detail 已是繁中友善訊息
    let detail = ''
    try {
      const j = await res.json()
      detail = typeof j?.detail === 'string' ? j.detail : JSON.stringify(j?.detail ?? j)
    } catch {
      detail = await res.text().catch(() => '')
    }
    throw new Error(detail || `AI 回應失敗(HTTP ${res.status})、請確認後端 uvicorn 在執行`)
  }
  return res.json()
}


// ── 串流版 chat:fetch streaming + NDJSON 解析 ──────────────────────
export type ChatStreamEvent =
  | { type: 'token'; text: string }
  | { type: 'tool_start'; name: string; args: Record<string, unknown> }
  | { type: 'tool_end'; name: string; result_preview: string }
  | { type: 'done'; reply: string; has_yaml: boolean; yaml_content: string | null; yaml_error: string | null }
  | { type: 'error'; status_code?: number; detail: string; code?: 'llm_not_ready' | 'llm_auth' | string; readiness?: LlmReadiness }

// ── 首次設定:AI 模型就緒檢查 ─────────────────────────────────────────
export interface LlmReadiness {
  ready: boolean
  provider: string
  provider_label: string
  model: string
  problem: string
  detected: {
    ollama: { running: boolean; models: string[] }
    claude_cli: { installed: boolean; logged_in: boolean }
    keys: Record<'gemini' | 'groq' | 'openai' | 'anthropic', boolean>
  }
  defaults: Record<string, string>
}

async function _llmSetupFetch(path: string, init?: RequestInit): Promise<LlmReadiness> {
  const res = await fetch(`${BASE}${path}`, init)
  if (!res.ok) {
    const j = await res.json().catch(() => null)
    throw new Error((j && typeof j.detail === 'string' && j.detail) || `HTTP ${res.status}`)
  }
  return res.json()
}

export function getLlmReadiness(): Promise<LlmReadiness> {
  return _llmSetupFetch('/settings/llm-readiness')
}

/** 存 API Key 到後端 .env(立即生效、不回傳金鑰),並切成該家的預設模型。 */
export function saveLlmKey(provider: string, apiKey: string): Promise<LlmReadiness> {
  return _llmSetupFetch('/settings/llm-key', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, api_key: apiKey, use_now: true }),
  })
}

/** 一鍵改用本機偵測到的模型(只動主模型)。 */
export function switchLlmModel(provider: string, model: string): Promise<LlmReadiness> {
  return _llmSetupFetch('/settings/llm-use', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, model }),
  })
}

/**
 * 串流呼叫 /pipeline/chat/stream、邊收事件邊 callback。
 * AbortSignal 可中斷請求(用 AbortController)。
 *
 * 行為:
 *   - token  事件:LLM 即時產出的文字片段、incremental render
 *   - tool_start / tool_end:tool 呼叫進度
 *   - done   事件:最終結果(含 has_yaml / yaml_content / yaml_error)
 *   - error  事件:任何階段失敗(會 throw)
 */
export async function pipelineChatStream(
  messages: Array<{ role: 'user' | 'assistant'; content: string }>,
  workflowId: string | null | undefined,
  onEvent: (ev: ChatStreamEvent) => void,
  signal?: AbortSignal,
  extraSystem?: string | null,
): Promise<void> {
  const body: Record<string, unknown> = { messages }
  if (workflowId) body.workflow_id = workflowId
  // 節點面板「問 AI」帶進來的當下設定狀態。後端 PipelineChatRequest.extra_system
  // 會 append 到 system prompt，AI 因此知道使用者卡在哪、手上有哪些變數。
  if (extraSystem) body.extra_system = extraSystem
  const res = await fetch(`${DIRECT_BASE}/pipeline/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) {
    let detail = ''
    try {
      const j = await res.json()
      detail = typeof j?.detail === 'string' ? j.detail : JSON.stringify(j?.detail ?? j)
    } catch {
      detail = await res.text().catch(() => '')
    }
    throw new Error(detail || `AI 串流失敗(HTTP ${res.status})`)
  }
  if (!res.body) throw new Error('AI 串流回應沒有 body')

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // NDJSON:一行一個 JSON
    let nl = buffer.indexOf('\n')
    while (nl >= 0) {
      const line = buffer.slice(0, nl).trim()
      buffer = buffer.slice(nl + 1)
      if (line) {
        try {
          const ev = JSON.parse(line) as ChatStreamEvent
          onEvent(ev)
          if (ev.type === 'error') {
            throw new Error(ev.detail || '串流回 error 事件')
          }
        } catch (e) {
          // 只吞「JSON.parse 失敗」(SyntaxError,通常是空行/非法事件行);
          // 其餘一律往上拋 —— 包含後端 error 事件與 onEvent() 拋出的例外,
          // 否則後端回錯時使用者只看到空泡泡、不跳任何錯誤。
          if (!(e instanceof SyntaxError)) throw e
        }
      }
      nl = buffer.indexOf('\n')
    }
  }
}

// ── Per-workflow chat history ────────────────────────────────────────────────
// 每個工作流保留獨立的 AI 助手對話紀錄，使用者日後可以接續討論加功能
// （未綁工作流的 scratch 對話由前端用 localStorage 暫存，不走這裡）

export interface WorkflowChatMessage {
  role: 'user' | 'assistant'
  content: string
  ts?: number
}

export async function getWorkflowChat(workflowId: string): Promise<WorkflowChatMessage[]> {
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/chat`)
  if (!res.ok) throw new Error('讀取工作流對話失敗')
  const data = await res.json()
  return data.messages || []
}

export async function appendWorkflowChat(
  workflowId: string,
  role: 'user' | 'assistant',
  content: string,
): Promise<WorkflowChatMessage[]> {
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role, content }),
  })
  if (!res.ok) throw new Error('追加對話訊息失敗')
  const data = await res.json()
  return data.messages || []
}

export async function setWorkflowChat(
  workflowId: string,
  messages: Array<{ role: 'user' | 'assistant'; content: string }>,
): Promise<WorkflowChatMessage[]> {
  // 覆寫整份（用於把 localStorage scratch 一次灌進新建立的工作流）
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/chat`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
  })
  if (!res.ok) throw new Error('覆寫對話訊息失敗')
  const data = await res.json()
  return data.messages || []
}

export async function clearWorkflowChat(workflowId: string): Promise<void> {
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/chat`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error('清空對話失敗')
}

// ── 螢幕擷取（視覺驗證節點：current_screen 來源時，給「拉一塊」picker 用）
export interface ScreenSnapshot {
  origin_x: number    // 虛擬桌面左上絕對座標 X（多螢幕配置可能負值）
  origin_y: number    // 虛擬桌面左上絕對座標 Y
  width: number       // 截圖寬（像素）
  height: number      // 截圖高（像素）
  image_b64: string   // PNG base64
}

export async function getScreenSnapshot(): Promise<ScreenSnapshot> {
  const res = await fetchWithRetry(`${BASE}/screen/snapshot`)
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`螢幕擷取失敗（${res.status}）：${detail}`)
  }
  return res.json()
}

// ── 列 assets_dir 內的錨點 PNG 檔（給 VLM 挑錨點 file picker 用）
export interface AssetFileEntry {
  name: string
  size: number
  mtime: number
}
export interface AnchorAnalysis {
  index: number
  checked: boolean
  /** 執行時「真的搆得到而且搶得走」的相似處數量（不是整張畫面的總數） */
  rivals: number
  nearest_rival_px: number
  /** 整張錄製畫面上掃到的相似處總數（含搆不到 / 分數差太多的） */
  scanned?: number
  /** 各階段各有幾個搆得到，決定要給什麼建議 */
  phases?: { box: number; near: number; fullscreen: number }
  /** 錨點幾乎沒有特徵（純色）→ CV 會亂命中，比有替身更嚴重 */
  flat?: boolean
  variance?: number
  target_score?: number
  best_rival_score?: number
  reason: string
}

/** 算每張錨點在錄製畫面上有幾個「執行時搶得走」的替身。 */
export async function analyzeAnchors(
  assetsDir: string,
  actions: Record<string, unknown>[],
  cv?: { cv_search_radius?: number; cv_threshold?: number; cv_search_only_near?: boolean },
): Promise<{ results: AnchorAnalysis[] }> {
  // 一定要把步驟層級的 CV 設定帶過去 —— 分析要用「執行時真的會用的那組值」，
  // 否則會報一堆執行時根本碰不到的假警報。
  const res = await fetch(`${BASE}/computer-use/assets/analyze-anchors`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assets_dir: assetsDir, actions, ...(cv || {}) }),
  })
  if (!res.ok) throw new Error(`錨點分析失敗（${res.status}）`)
  return res.json()
}

export async function listAssetFiles(dir: string): Promise<{ dir: string; files: AssetFileEntry[] }> {
  const url = `${BASE}/computer-use/assets/list?dir=${encodeURIComponent(dir)}`
  const res = await fetchWithRetry(url)
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`列檔案失敗（${res.status}）：${detail}`)
  }
  return res.json()
}

// 直接給縮圖 URL（讓 <img src=...> 載入；瀏覽器會自動 GET）
export function assetImageUrl(dir: string, name: string): string {
  return `${BASE}/computer-use/assets/image?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(name)}`
}

// ── Outlook 自動化節點：連線測試 ────────────────────────────────────────
export interface OutlookConnectionTest {
  ok: boolean
  version?: string
  inbox_count?: number
  sent_count?: number
  drafts_count?: number
  diagnosis: string
  error: string
}

export async function testOutlookConnection(): Promise<OutlookConnectionTest> {
  const res = await fetch(`${BASE}/outlook/test-connection`)
  if (!res.ok) {
    return {
      ok: false,
      diagnosis: `後端連線失敗（${res.status}）— 請確認後端 (uvicorn) 正在運行`,
      error: `HTTP ${res.status}`,
    }
  }
  return res.json()
}

// ── 變數系統(Ticket 1):dry-run + workflow variables ────────────────────────

export interface VariableField {
  key: string
  type: string
  last_value: string | number
  source?: string
}

export interface VariableStepInfo {
  name: string
  node_type: string
  fields: VariableField[]
}

export interface WorkflowVariablesResult {
  available: {
    steps: VariableStepInfo[]
    input: { key: string; last_value: string; required: boolean }[]
    env: { key: string; last_value: string; is_secret: boolean }[]
  }
  referenced: string[]
  last_run_id: string | null
}

export interface DryRunStepResult {
  index: number
  name: string
  node_type: string
  rendered: Record<string, string>
  referenced_vars: string[]
  errors: string[]
  /** 此 step 預期的工作目錄(subprocess CWD / skill agent working_dir 用) */
  working_dir?: string
  /** 非致命提醒(e.g. 引用的上游 step 沒設 output.path、runtime 可能是空) */
  warnings?: string[]
}

export interface DryRunResult {
  ok: boolean
  workflow_name: string
  input_params: Record<string, string>
  steps: DryRunStepResult[]
}

/** 列出 workflow 可用變數 + 上次跑出來的實際值(給「插入變數」modal 用)。 */
export async function getWorkflowVariables(wfId: string): Promise<WorkflowVariablesResult> {
  const res = await fetchWithRetry(`${BASE}/workflows/${wfId}/variables`)
  if (!res.ok) throw new Error(`getWorkflowVariables 失敗:${res.status}`)
  return res.json()
}

/** 不執行、只 render 各 step 的 {{ }} → 預覽渲染後的命令。 */
export async function dryRunPipeline(req: {
  yaml_content: string
  input_params?: Record<string, string>
  workflow_id?: string
}): Promise<DryRunResult> {
  const res = await fetch(`${BASE}/pipeline/dry-run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      yaml_content: req.yaml_content,
      input_params: req.input_params || {},
      workflow_id: req.workflow_id,
    }),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `dryRunPipeline 失敗 (${res.status})`)
  }
  return res.json()
}
