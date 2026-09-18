import { create } from 'zustand'
import type { Edge } from '@xyflow/react'
import type { AppNode, SkillData } from './_helpers'
import {
  listWorkflows, createWorkflowApi, updateWorkflowApi, deleteWorkflowApi,
  type WorkflowData,
} from '@/lib/api'

// ── 一個工作流的完整資料 ─────────────────────────────────────────────────────
export interface Workflow {
  id: string
  name: string
  nodes: AppNode[]
  edges: Edge[]
  validate: boolean
  updatedAt: number
  serverUpdatedAt?: number   // 伺服器版本戳（秒）—— PUT 帶上，被別處改過會 409
}

/** 遷移舊節點類型：pipelineStep → scriptStep / skillStep */
function migrateNodes(nodes: AppNode[]): AppNode[] {
  return nodes.map(n => {
    if (n.type === 'pipelineStep') {
      const d = n.data as Record<string, any>
      if (d.skillMode) {
        const skillData: SkillData = {
          name: d.name ?? '',
          taskDescription: d.batch ?? '',
          workingDir: d.workingDir ?? '',
          outputPath: d.outputPath ?? '',
          expectedOutput: d.expect ?? '',
          readonly: d.readonly ?? false,
          skill: d.skill ?? '',
          askMode: d.askMode ?? false,
          timeout: d.timeout ?? 300,
          retry: d.retry ?? 0,
          index: d.index ?? 0,
          status: 'idle',
          errorMsg: '',
        }
        return {
          ...n,
          type: 'skillStep' as const,
          data: skillData,
        }
      }
      return { ...n, type: 'scriptStep' as const }
    }
    // humanConfirmation 節點不需遷移，直接保留
    return n
  })
}

function apiToWorkflow(d: WorkflowData): Workflow {
  return {
    id: d.id,
    name: d.name,
    nodes: migrateNodes((d.canvas?.nodes ?? []) as AppNode[]),
    edges: (d.canvas?.edges ?? []) as Edge[],
    validate: d.validate,
    updatedAt: d.updated_at * 1000,  // backend uses seconds, frontend uses ms
    // 樂觀鎖用：只從伺服器回應更新（本地編輯會動 updatedAt，不能拿它當 base）
    serverUpdatedAt: d.updated_at,
  }
}

// ── Chat UI 狀態機 ────────────────────────────────────────────────────────────
// Hero UX 重塑(Phase 2):AI 助手呈現方式的四種模式
//   - 'hero':首頁中央大畫面(預設、每次進站都看)
//   - 'sidebar':嵌在左 sidebar 底部(原本 Phase 1 行為)
//   - 'mini':縮小成 floating button(Phase 4)
//   - 'drawer':從旁邊滑出的抽屜(Phase 4)
// hasInteracted:有過互動就 true(目前未使用、Phase 4/5 會根據它決定要不要回到 hero)
export type ChatUIState = 'hero' | 'sidebar' | 'mini' | 'drawer'

// ── Store ────────────────────────────────────────────────────────────────────
interface WorkflowStore {
  workflows: Workflow[]
  activeId:  string | null
  loaded:    boolean         // 是否已從 API 載入

  // CRUD (all async, hit backend API)
  fetchWorkflows: () => Promise<void>
  createWorkflow: (name?: string) => Promise<string>   // returns new id
  updateWorkflow: (id: string, patch: Partial<Omit<Workflow, 'id'>>) => void
  removeWorkflow: (id: string) => Promise<void>
  setActive:      (id: string) => void
  getActive:      () => Workflow | undefined

  // 儲存目前畫布狀態（debounced by caller）。
  // 同時帶上 yaml 一起存，讓 TG 遠端遙控等不經過前端 getYaml() 的入口
  // 也能直接讀到對應的 YAML（不再因為 yaml 欄位空而拒絕啟動）。
  saveCanvas: (id: string, nodes: AppNode[], edges: Edge[], yaml?: string) => void

  // ── AI 助手 UI 狀態(Hero UX 重塑)──────────────────────────────────────
  chatUIState: ChatUIState     // 預設 'hero'(每次進站都看)
  hasInteracted: boolean       // 有過互動就 true(Phase 4/5 用)
  setChatUIState: (s: ChatUIState) => void
  setHasInteracted: (b: boolean) => void

  // ── 「卡住就問 AI」──────────────────────────
  // 節點面板按「問 AI」時，把當下的設定狀態(節點型別 / 已加的動作 / 手上有哪些變數)
  // 寫進這裡；聊天視窗會自動打開，並把它當 extra_system 送出去，
  // AI 才知道使用者卡在哪一步、手上已經有什麼。用完清掉。
  askAiContext: string | null
  setAskAiContext: (c: string | null) => void
}

// 防抖佇列：合併多次快速 saveCanvas / updateWorkflow 呼叫
const _pendingUpdates = new Map<string, { timer: ReturnType<typeof setTimeout>; patch: Record<string, any> }>()

function _debouncedApiUpdate(id: string, patch: Record<string, any>) {
  const existing = _pendingUpdates.get(id)
  if (existing) {
    clearTimeout(existing.timer)
    Object.assign(existing.patch, patch)
  } else {
    _pendingUpdates.set(id, { timer: 0 as any, patch: { ...patch } })
  }
  const entry = _pendingUpdates.get(id)!
  entry.timer = setTimeout(() => {
    _pendingUpdates.delete(id)
    void _sendUpdate(id, entry.patch)
  }, 500)
}

/** 實際送 PUT。帶樂觀鎖 base；被別處改過（409）就重載最新版並明講。 */
async function _sendUpdate(id: string, patch: Record<string, any>, opts?: { keepalive?: boolean }) {
  const st = useWorkflowStore.getState()
  const base = (st.workflows.find(w => w.id === id) as any)?.serverUpdatedAt
  try {
    const fresh = await updateWorkflowApi(
      id, base !== undefined ? { ...patch, base_updated_at: base } : patch, opts)
    useWorkflowStore.setState(s => ({
      workflows: s.workflows.map(w =>
        w.id === id ? { ...w, serverUpdatedAt: (fresh as any).updated_at } as any : w),
    }))
  } catch (e) {
    if (e instanceof Error && e.message.startsWith('CONFLICT:')) {
      // ⚠ 不能靜默重試：AI 助手或另一個分頁剛改過，硬存會整份蓋掉那些修改。
      const { toast } = await import('sonner')
      toast.warning(e.message.slice('CONFLICT:'.length), { duration: 9000 })
      void useWorkflowStore.getState().fetchWorkflows()
      return
    }
    // 其他失敗維持原行為：本地已更新，下次 fetchWorkflows 會同步
  }
}

/** 立刻送出所有還在防抖等待的更新 —— 切換工作流 / 關頁前呼叫，
 *  否則 500ms 窗口內的修改會永遠消失（Lite 實測就是這樣掉資料的）。 */
function _flushPendingUpdates() {
  for (const [id, entry] of _pendingUpdates) {
    clearTimeout(entry.timer)
    _pendingUpdates.delete(id)
    void _sendUpdate(id, entry.patch, { keepalive: true })
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', _flushPendingUpdates)
  window.addEventListener('beforeunload', _flushPendingUpdates)
}

export const useWorkflowStore = create<WorkflowStore>()(
  (set, get) => ({
    workflows: [],
    activeId:  null,
    loaded:    false,

    fetchWorkflows: async () => {
      try {
        const data = await listWorkflows()
        const workflows = data.map(apiToWorkflow)
        const { activeId } = get()
        const active = activeId && workflows.find(w => w.id === activeId)
          ? activeId
          : (workflows[0]?.id ?? null)
        set({ workflows, activeId: active, loaded: true })
      } catch {
        set({ loaded: true })
      }
    },

    createWorkflow: async (name) => {
      const data = await createWorkflowApi(name ?? '新工作流')
      const wf = apiToWorkflow(data)
      set(s => ({ workflows: [...s.workflows, wf], activeId: wf.id }))
      return wf.id
    },

    updateWorkflow: (id, patch) => {
      // 立即更新本地狀態
      set(s => ({
        workflows: s.workflows.map(w =>
          w.id === id ? { ...w, ...patch, updatedAt: Date.now() } : w
        ),
      }))
      // 異步 debounced 更新後端
      const apiPatch: Record<string, any> = {}
      if (patch.name !== undefined) apiPatch.name = patch.name
      if (patch.validate !== undefined) apiPatch.validate = patch.validate
      if (Object.keys(apiPatch).length > 0) {
        _debouncedApiUpdate(id, apiPatch)
      }
    },

    removeWorkflow: async (id) => {
      set(s => {
        const ws = s.workflows.filter(w => w.id !== id)
        const activeId = s.activeId === id ? (ws[ws.length - 1]?.id ?? null) : s.activeId
        return { workflows: ws, activeId }
      })
      try {
        await deleteWorkflowApi(id)
      } catch {
        // 靜默
      }
    },

    setActive: (id) => {
      // 先 flush 上一條工作流還沒送出的修改再切，否則重抓會用舊資料蓋掉
      _flushPendingUpdates()
      set({ activeId: id })
    },

    getActive: () => {
      const { workflows, activeId } = get()
      return workflows.find(w => w.id === activeId)
    },

    saveCanvas: (id, nodes, edges, yaml) => {
      // 立即更新本地狀態
      set(s => ({
        workflows: s.workflows.map(w =>
          w.id === id ? { ...w, nodes, edges, updatedAt: Date.now() } : w
        ),
      }))
      // 異步 debounced 更新後端 — 帶 yaml 一起存（給 TG 遠端遙控用）
      const patch: Record<string, any> = { canvas: { nodes, edges } }
      if (typeof yaml === 'string') patch.yaml = yaml
      _debouncedApiUpdate(id, patch)
    },

    // ── AI 助手 UI 狀態 ──────────────────────────────────────────────────
    // 預設 'hero' — 每次進站都看到中央大畫面、需主動操作 CTA 才會切到 sidebar
    chatUIState: 'hero',
    hasInteracted: false,
    setChatUIState: (s) => set({ chatUIState: s }),
    setHasInteracted: (b) => set({ hasInteracted: b }),

    askAiContext: null,
    setAskAiContext: (c) => set({ askAiContext: c }),
  })
)

