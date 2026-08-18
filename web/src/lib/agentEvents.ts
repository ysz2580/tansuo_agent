import type { AgentEvent } from "@/lib/api"

/** agent 事件渲染：调参会话与 setup 会话共用同一套 journal 事件 kind
 *  （session_start / agent_* / finish），样式与文案集中在此供两个面板复用。 */

/** 按唤醒轮次分组后的叙事段：一轮 = 「看到什么 → 判断什么 → 做了什么」。
 *  tune 模式每轮唤醒一段；setup 模式整个会话一段（事件流同构，round=1）。 */
export interface AgentSegment {
  mode: "tune" | "setup"
  round: number               // tune=唤醒轮次；setup 恒为 1
  startTs?: string
  endTs?: string
  context: {                  // 看到什么
    round?: number
    budgetLeft?: number
    spaceVersion?: number
    signals?: string[]        // 确定性护栏信号（失败警报/收敛提示）
    trainScript?: string      // setup：被配置阅读的训练脚本
  }
  judgment?: string           // 判断什么：wakeup.end.note（tune）或 finish.summary（setup）
  toolCalls: AgentEvent[]     // 做了什么：段内 tool_call / permission / error
  tokens?: { in: number; out: number }
}

function num(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined
}

const ACTION_KINDS = new Set(["agent_tool_call", "agent_permission", "agent_error"])

/** 把 journal 事件数组按时序边界分组成轮次段。
 *  注意：journal ts 只到秒级且无时区，**不能**用来排序——只能依赖数组顺序
 *  （journal.jsonl 行序即追加序）。边界规则：
 *  - tune：wakeup 无 phase=start 开新段；phase=end 闭段并附 note/tokens；
 *    phase=signals 的护栏信号附入当前段；段间 action 事件归入当前段。
 *  - setup：整条流一段——session_start 开段（带训练脚本），action 累入，
 *    wakeup(mode=setup, phase=end) 附 tokens，finish.summary 作 judgment。
 *  模式自动识别：流内出现 mode="setup" 事件即按 setup 处理（两端点各自
 *  返回同构纯流：/api/agent/events 仅 agent_*；/api/setup/events 全量）。 */
export function groupByRounds(events: AgentEvent[]): AgentSegment[] {
  return events.some((e) => e.mode === "setup") ? groupSetup(events) : groupTune(events)
}

function groupTune(events: AgentEvent[]): AgentSegment[] {
  const segs: AgentSegment[] = []
  let cur: AgentSegment | null = null
  const open = (round: number, ts?: string): AgentSegment => {
    cur = { mode: "tune", round, startTs: ts, context: {}, toolCalls: [] }
    segs.push(cur)
    return cur
  }
  // 防御：end/signals/action 出现在 wakeup start 之前时兜底开段（round=0 显示为「未编号」）
  const ensure = (roundHint: number, ts?: string): AgentSegment => cur ?? open(roundHint, ts)

  for (const e of events) {
    if (e.kind === "agent_wakeup") {
      const round = num(e.round) ?? 0
      if (e.phase === "end") {
        const seg = ensure(round, e.ts)
        seg.endTs = e.ts
        if (typeof e.note === "string" && e.note) seg.judgment = e.note
        const tin = num(e.input_tokens)
        const tout = num(e.output_tokens)
        if (tin !== undefined || tout !== undefined) seg.tokens = { in: tin ?? 0, out: tout ?? 0 }
        cur = null
      } else if (e.phase === "signals") {
        const seg = ensure(round, e.ts)
        const sigs = Array.isArray(e.signals) ? (e.signals as unknown[]).map(String) : []
        seg.context.signals = [...(seg.context.signals ?? []), ...sigs]
      } else {
        // 无 phase = 本轮开始（loop.py 只在 start 时省略 phase 字段）
        const seg = open(round, e.ts)
        seg.context.round = round
        seg.context.budgetLeft = num(e.budget_left)
        seg.context.spaceVersion = num(e.space_version)
      }
    } else if (ACTION_KINDS.has(e.kind)) {
      ensure(num(e.round) ?? 0, e.ts).toolCalls.push(e)
    }
  }
  return segs
}

function groupSetup(events: AgentEvent[]): AgentSegment[] {
  const segs: AgentSegment[] = []
  let cur: AgentSegment | null = null
  const ensure = (): AgentSegment => {
    if (!cur) {
      cur = { mode: "setup", round: 1, context: {}, toolCalls: [] }
      segs.push(cur)
    }
    return cur
  }
  for (const e of events) {
    if (e.kind === "session_start") {
      const seg = ensure()
      seg.startTs = seg.startTs ?? e.ts
      if (typeof e.train === "string" && e.train) seg.context.trainScript = e.train
    } else if (e.kind === "agent_wakeup" && e.phase === "end") {
      const seg = ensure()
      seg.endTs = e.ts
      const tin = num(e.input_tokens)
      const tout = num(e.output_tokens)
      if (tin !== undefined || tout !== undefined) seg.tokens = { in: tin ?? 0, out: tout ?? 0 }
    } else if (e.kind === "finish") {
      const seg = ensure()
      seg.endTs = e.ts
      if (typeof e.summary === "string" && e.summary && !seg.judgment) seg.judgment = e.summary
    } else if (ACTION_KINDS.has(e.kind)) {
      ensure().toolCalls.push(e)
    }
  }
  return segs
}

export const KIND_META: Record<string, { label: string; cls: string }> = {
  session_start: { label: "会话开始", cls: "bg-emerald-600/15 text-emerald-700 dark:text-emerald-400" },
  agent_wakeup: { label: "唤醒", cls: "bg-blue-600/15 text-blue-700 dark:text-blue-400" },
  agent_tool_call: { label: "工具调用", cls: "bg-violet-600/15 text-violet-700 dark:text-violet-400" },
  agent_permission: { label: "权限", cls: "bg-amber-600/15 text-amber-700 dark:text-amber-400" },
  agent_error: { label: "异常", cls: "bg-red-600/15 text-red-700 dark:text-red-400" },
  finish: { label: "结束", cls: "bg-gray-600/15 text-gray-600 dark:text-gray-400" },
}

export function eventBody(e: AgentEvent): string {
  switch (e.kind) {
    case "session_start":
      return e.mode === "setup"
        ? `配置会话开始 ｜ 训练脚本：${e.train ?? "?"}`
        : `会话开始${e.cohort ? ` ｜ 分区 ${e.cohort}` : ""}`
    case "agent_wakeup": {
      if (e.phase === "end") {
        const usage = typeof e.input_tokens === "number"
          ? `（本轮 in ${e.input_tokens} / out ${e.output_tokens} tokens）`
          : ""
        return e.mode === "setup"
          ? `配置会话完成${usage}`
          : `第 ${e.round} 轮结束${usage}${e.note ? `：${e.note}` : ""}`
      }
      if (e.phase === "signals") {
        const sigs = Array.isArray(e.signals) ? (e.signals as string[]).join("；") : ""
        return `第 ${e.round} 轮护栏信号：${sigs}`
      }
      return `第 ${e.round} 轮开始 ｜ 剩余预算 ${e.budget_left} ｜ 空间 v${e.space_version}`
    }
    case "agent_tool_call": {
      const input = e.input ? JSON.stringify(e.input) : ""
      const brief = input.length > 120 ? input.slice(0, 120) + "…" : input
      return `${e.tool}${brief ? `(${brief})` : ""}${e.allowed === false ? " —— 被权限策略拦截" : ""}`
    }
    case "agent_permission":
      return `${e.tool}：策略 ${e.policy} → ${e.outcome}`
    case "agent_error":
      return String(e.error ?? "")
    case "finish":
      return `会话结束（${e.reason ?? "?"}）`
    default:
      return JSON.stringify(e)
  }
}
