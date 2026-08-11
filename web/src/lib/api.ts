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

// ---------- 类型（与后端返回结构对应） ----------

export interface Summary {
  counts: { completed: number; pruned: number; failed: number; running: number }
  primary: string
  direction: "maximize" | "minimize"
  best: { trial: number; value: number; params: Record<string, unknown> } | null
  top_k: { trial: number; value: number; params: Record<string, unknown> }[]
  contrast: Record<string, unknown>
  convergence: string
  experiment: string
  budget_total: number
  space_version: number
  watch: { name: string; direction: string }[]
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
}

export interface RunLogResp extends RunStatus {
  text: string
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

// ---------- 端点 ----------

export const api = {
  summary: () => http<Summary>("/summary"),
  trials: () => http<TrialsResp>("/trials"),
  trialCurve: (n: number) => http<CurveResp>(`/trials/${n}/curve`),
  curves: () => http<CurvesResp>("/curves"),
  space: () => http<SpaceResp>("/space"),
  agentEvents: () => http<{ events: AgentEvent[] }>("/agent/events"),
  report: () => http<ReportResp>("/report"),
  reportGenerate: () => http<{ report: string; best: string }>("/report/generate", { method: "POST" }),
  runStatus: () => http<RunStatus>("/run/status"),
  runLog: (tail = 300) => http<RunLogResp>(`/run/log?tail=${tail}`),
  runStart: (body: { trials?: number; wake_every?: number; no_agent?: boolean; fresh?: boolean }) =>
    http<RunStatus>("/run/start", { method: "POST", body: JSON.stringify(body) }),
  runStop: () => http<RunStatus & { marked_failed?: number[] }>("/run/stop", { method: "POST", body: "{}" }),
  agentConfig: () => http<AgentConfig>("/config/agent"),
  probe: (body: AgentConfigBody) =>
    http<ProbeResult>("/config/agent/probe", { method: "POST", body: JSON.stringify(body) }),
  save: (body: AgentConfigBody) =>
    http<SaveResult>("/config/agent/save", { method: "POST", body: JSON.stringify(body) }),
}
