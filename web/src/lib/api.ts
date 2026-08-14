// 后端 REST API 封装（FastAPI，见 tansuo/web/app.py）

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  })
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`
    try {
      const body = await resp.json()
      if (body && typeof body.detail === "string") detail = body.detail
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new Error(detail)
  }
  return resp.json() as Promise<T>
}

/** 分区作用域查询串：cohort 为空表示后端默认（最新分区）。 */
function qs(cohort?: string | null): string {
  return cohort ? `?cohort=${encodeURIComponent(cohort)}` : ""
}

// ---------- 类型（与后端返回结构对应） ----------

export interface Summary {
  counts: { completed: number; pruned: number; failed: number; running: number }
  primary: string
  direction: "maximize" | "minimize"
  best: { trial: number; value: number; params: Record<string, unknown> } | null
  top_k: { trial: number; value: number; params: Record<string, unknown> }[]
  contrast: Record<string, unknown>
  importances: Record<string, number>
  convergence: string
  experiment: string
  budget_total: number
  space_version: number
  workers: number
  eta_s: number | null
  watch: { name: string; direction: string }[]
  cohort: string | null
  fingerprint_changed: boolean
}

export interface Trial {
  number: number
  state: "COMPLETE" | "PRUNED" | "FAIL" | "RUNNING" | "WAITING"
  value: number | null
  params: Record<string, number | string>
  attrs: Record<string, unknown>
  duration_s: number | null
  fail_reason: string | null
}

export interface TrialsResp {
  trials: Trial[]
  primary: string
  direction: string
}

export interface CurvePoint {
  epoch: number
  [metric: string]: number
}

export interface TrialCurve {
  trial: number
  value: number
  params: Record<string, unknown>
  curve: CurvePoint[]
}

export interface CurveResp {
  primary: string
  watch: string[]
  curve: TrialCurve
}

export interface CurvesResp {
  primary: string
  watch: string[]
  curves: TrialCurve[]
}

export interface SpaceParam {
  name: string
  type: "choice" | "float" | "int"
  description: string
  choices?: (string | number)[]
  env_choices?: (string | number)[]
  low?: number
  high?: number
  log?: boolean
  env_low?: number
  env_high?: number
  frozen?: string | number | null
  depends_on?: Record<string, unknown>
}

export interface SpacePatchEvent {
  ts: string
  version: number
  ops: { op: string; param: string; low?: number; high?: number; value?: unknown }[]
  rationale: string
}

export interface SpaceResp {
  version: number
  params: SpaceParam[]
  free_params: number
  patches: SpacePatchEvent[]
}

export interface AgentEvent {
  kind: string
  ts: string
  [key: string]: unknown
}

// 本分区调参会话的累计 token 用量（来自 agent_wakeup end 事件审计）
export interface AgentTokenSummary {
  rounds: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
}

export interface ReportResp {
  exists: boolean
  updated?: string
  content: string | null
  best: string | null
}

export interface RunStatus {
  running: boolean
  pid: number | null
  args: string[]
  log_path: string | null
  started_at: string | null
  exit_code: number | null
  stopped: boolean
  last_cohort: string | null
}

export interface RunLogResp extends RunStatus {
  text: string
}

// 记录分区（cohort）：一条不可删除的历史记录单元
export type RunComparable =
  | "match"
  | "code-changed"
  | "data-changed"
  | "code-data-changed"
  | "objective-changed"
  | "legacy"

export interface RunInfo {
  id: string
  created_at: string | null
  note: string
  objective_hash: string | null
  code_hash: string | null
  data_hash: string | null
  prompt_version: number
  prompt_changed: boolean
  primary_metric: { name: string; direction: string } | null
  completed: number
  best: number | null
  locked: boolean
  virtual: boolean
  incomplete: boolean
  comparable: RunComparable
}

export interface RunsResp {
  runs: RunInfo[]
  current: {
    objective_hash: string
    code_hash: string
    data_hash: string
    reliable: boolean
    data_reliable: boolean
  }
  default: string | null
}

// 跨分区对比（/api/runs/compare）：仅优化目标指纹相同的分区可比
export interface CompareBest {
  trial: number
  value: number
  params: Record<string, unknown>
}

export interface CompareCohort {
  id: string
  created_at: string | null
  note: string
  code_hash: string | null
  data_hash: string | null
  prompt_version: number
  completed: number
  locked: boolean
  best: CompareBest | null
  top_k: CompareBest[]
  curve: CurvePoint[]
}

export interface CompareResp {
  objective_hash: string
  primary: { name: string; direction: string }
  watch: string[]
  cohorts: CompareCohort[]
}

export interface AgentConfig {
  model: string
  base_url: string
  enabled: boolean
  auth_token_masked: string
  auth_token_source: string
  env_refs: { base_url: boolean; auth_token: boolean }
}

export interface AgentConfigBody {
  model?: string
  base_url?: string
  auth_token?: string
}

export interface ProbeResult {
  model: string
  base_url: string
  ok: boolean
  stage: string
  detail: string
}

export interface SaveResult {
  probe: ProbeResult
  write_back: { ok: boolean; changed: string[]; errors: string[] }
  warnings: string[]
}

// 提示词管理（/api/config/prompts）：全局配置，前后端同步、带版本与回滚
export interface PromptInfo {
  name: "tuning_system" | "tuning_wake_brief" | "setup_system"
  override: string      // 用户覆盖文本；空串=用出厂默认
  default: string       // 出厂默认模板
  effective: string     // override 或 default（供编辑器加载）
  vars: string[]        // 该提示词可用的 {{var}} 占位符
}

export interface PromptHistoryEntry {
  ts: string
  version: number
  which: string
  rationale: string
  source: string
  text: string          // 该版本生效的完整文本（载入此版本=用 text 覆盖）
  hash: string
}

export interface PromptsResp {
  version: number
  prompts: PromptInfo[]
  history: PromptHistoryEntry[]
}

export interface PromptPreviewBody {
  which: string
  text: string
}

export interface PromptPreviewResp {
  rendered: string
  missing_vars: string[]
}

export interface PromptSaveBody {
  which: string
  text: string
  rationale: string
}

export interface PromptSaveResp {
  version: number
  entry: PromptHistoryEntry
}

// 通知配置（/api/config/notify）：webhook 推送（会话结束 / agent 降级）
export interface NotifyConfig {
  enabled: boolean
  format: string            // generic / dingtalk / lark / slack
  events: string[]          // session_end / agent_degrade
  webhook_url_masked: string
  webhook_url_source: string
}

export interface NotifySaveBody {
  webhook_url?: string
  format?: string
  events?: string[]
  enabled?: boolean
}

export interface NotifySaveResp {
  write_back: { ok: boolean; changed: string[]; errors: string[] }
  warnings: string[]
}

export interface NotifyTestResp {
  ok: boolean
  detail: string
}

// 项目管理（/api/projects）：项目 = 一个目录（训练代码 + 数据集），
// tansuo 工作产物放 <项目>/.tansuo/；注册表在 ~/.tansuo_agent/projects.json
export interface ProjectInfo {
  id: string
  name: string
  dir: string                 // 项目目录（绝对路径）：一切相对路径的基准
  settings_path: string
  space_path: string
  train_script: string        // 可空：未登记则不能跑 setup agent
  created_at: string
  last_used: string
}

export interface ProjectsResp {
  projects: ProjectInfo[]
  active_id: string | null
}

export interface ProjectCreateBody {
  name?: string               // 缺省用目录名
  dir: string
  train_script?: string
}

export interface ProjectCreateResp extends ProjectInfo {
  scaffolded: boolean         // true=新生成 .tansuo/ 模板；false=打开既有项目
}

export interface BrowseDirEntry {
  name: string
  path: string
  has_children: boolean
}

export interface BrowseResp {
  path: string                // ""=盘符/home 根视图
  parent: string | null
  dirs: BrowseDirEntry[]
}

export interface FilesResp {
  path: string
  files: string[]
}

// setup agent（配置会话）：与搜索硬互斥，状态结构与 RunStatus 同构
export interface SetupStatus {
  running: boolean
  pid: number | null
  args: string[]
  log_path: string | null
  started_at: string | null
  exit_code: number | null
  stopped: boolean
  project_dir: string | null
  settings_path: string | null
  train_script: string | null
}

export interface SetupLogResp extends SetupStatus {
  text: string
}

export interface SetupEventsResp {
  events: AgentEvent[]
  tokens: AgentTokenSummary
}

// ---------- 端点 ----------

export const api = {
  // 分区作用域端点：cohort 缺省 → 后端取最新分区（无分区则扁平布局）
  summary: (cohort?: string | null) => http<Summary>(`/summary${qs(cohort)}`),
  trials: (cohort?: string | null) => http<TrialsResp>(`/trials${qs(cohort)}`),
  trialCurve: (n: number, cohort?: string | null) =>
    http<CurveResp>(`/trials/${n}/curve${qs(cohort)}`),
  curves: (cohort?: string | null) => http<CurvesResp>(`/curves${qs(cohort)}`),
  space: (cohort?: string | null) => http<SpaceResp>(`/space${qs(cohort)}`),
  agentEvents: (cohort?: string | null) =>
    http<{ events: AgentEvent[]; tokens: AgentTokenSummary }>(`/agent/events${qs(cohort)}`),
  report: (cohort?: string | null) => http<ReportResp>(`/report${qs(cohort)}`),
  reportGenerate: (cohort?: string | null) =>
    http<{ report: string; best: string }>(`/report/generate${qs(cohort)}`, { method: "POST" }),
  // 分区列表 + 当前三指纹 + 可比性
  runs: () => http<RunsResp>("/runs"),
  // 跨分区对比：cohorts 缺省 → 后端取与当前目标指纹相同的全部分区
  runsCompare: (cohorts?: string[]) =>
    http<CompareResp>(`/runs/compare${cohorts && cohorts.length
      ? `?cohorts=${encodeURIComponent(cohorts.join(","))}` : ""}`),
  runStatus: () => http<RunStatus>("/run/status"),
  runLog: (tail = 300) => http<RunLogResp>(`/run/log?tail=${tail}`),
  runStart: (body: { trials?: number; wake_every?: number; no_agent?: boolean; fresh?: boolean; new_cohort?: boolean; note?: string; workers?: number; max_duration_h?: number }) =>
    http<RunStatus>("/run/start", { method: "POST", body: JSON.stringify(body) }),
  runStop: () => http<RunStatus & { marked_failed?: number[] }>("/run/stop", { method: "POST", body: "{}" }),
  agentConfig: () => http<AgentConfig>("/config/agent"),
  probe: (body: AgentConfigBody) =>
    http<ProbeResult>("/config/agent/probe", { method: "POST", body: JSON.stringify(body) }),
  save: (body: AgentConfigBody) =>
    http<SaveResult>("/config/agent/save", { method: "POST", body: JSON.stringify(body) }),
  // 通知配置（全局）
  notifyConfig: () => http<NotifyConfig>("/config/notify"),
  notifySave: (body: NotifySaveBody) =>
    http<NotifySaveResp>("/config/notify/save", { method: "POST", body: JSON.stringify(body) }),
  notifyTest: () =>
    http<NotifyTestResp>("/config/notify/test", { method: "POST", body: "{}" }),
  // 提示词管理（全局，不分分区）
  prompts: () => http<PromptsResp>("/config/prompts"),
  promptsPreview: (body: PromptPreviewBody) =>
    http<PromptPreviewResp>("/config/prompts/preview", { method: "POST", body: JSON.stringify(body) }),
  promptsSave: (body: PromptSaveBody) =>
    http<PromptSaveResp>("/config/prompts/save", { method: "POST", body: JSON.stringify(body) }),
  // 项目管理：注册 / 激活 / 新建（自动脚手架 .tansuo/）/ 删除（仅移除注册）
  projects: () => http<ProjectsResp>("/projects"),
  activeProject: () => http<ProjectInfo>("/projects/active"),
  createProject: (body: ProjectCreateBody) =>
    http<ProjectCreateResp>("/projects", { method: "POST", body: JSON.stringify(body) }),
  activateProject: (id: string) =>
    http<ProjectInfo>(`/projects/${encodeURIComponent(id)}/activate`,
                      { method: "POST", body: "{}" }),
  deleteProject: (id: string) =>
    http<{ ok: boolean }>(`/projects/${encodeURIComponent(id)}`, { method: "DELETE" }),
  // 服务端目录浏览（浏览器无法枚举服务器文件夹）：path 空 → Windows 盘符 / 其他平台 home
  browseDir: (path = "") =>
    http<BrowseResp>(`/fs/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  browseFiles: (path: string, ext = ".py") =>
    http<FilesResp>(`/fs/files?path=${encodeURIComponent(path)}&ext=${encodeURIComponent(ext)}`),
  // setup agent（配置会话）：对指定项目跑配置 agent，与搜索硬互斥
  setupStart: (projectId: string) =>
    http<SetupStatus>(`/projects/${encodeURIComponent(projectId)}/setup`,
                      { method: "POST", body: "{}" }),
  setupStop: () => http<SetupStatus>("/setup/stop", { method: "POST", body: "{}" }),
  setupStatus: () => http<SetupStatus>("/setup/status"),
  setupLog: (tail = 300) => http<SetupLogResp>(`/setup/log?tail=${tail}`),
  setupEvents: () => http<SetupEventsResp>("/setup/events"),
}
