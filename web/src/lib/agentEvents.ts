import type { AgentEvent } from "@/lib/api"

/** agent 事件渲染：调参会话与 setup 会话共用同一套 journal 事件 kind
 *  （session_start / agent_* / finish），样式与文案集中在此供两个面板复用。 */

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
        return `第 ${e.round} 轮结束${usage}${e.note ? `：${e.note}` : ""}`
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
