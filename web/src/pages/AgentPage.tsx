import { api, type AgentEvent } from "@/lib/api"
import { useCohort } from "@/lib/cohort"
import { usePolling } from "@/lib/usePolling"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"

const KIND_META: Record<string, { label: string; cls: string }> = {
  agent_wakeup: { label: "唤醒", cls: "bg-blue-600/15 text-blue-700 dark:text-blue-400" },
  agent_tool_call: { label: "工具调用", cls: "bg-violet-600/15 text-violet-700 dark:text-violet-400" },
  agent_permission: { label: "权限", cls: "bg-amber-600/15 text-amber-700 dark:text-amber-400" },
  agent_error: { label: "异常", cls: "bg-red-600/15 text-red-700 dark:text-red-400" },
}

function eventBody(e: AgentEvent): string {
  switch (e.kind) {
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
    default:
      return JSON.stringify(e)
  }
}

export default function AgentPage() {
  const cohort = useCohort()
  const { data, error } = usePolling(() => api.agentEvents(cohort), 8000)

  if (error) return <div className="text-red-600 py-8 text-center">加载失败：{error}</div>
  if (!data) return <div className="text-muted-foreground py-12 text-center">加载中…</div>

  const events = data.events
  const counts: Record<string, number> = {}
  for (const e of events) counts[e.kind] = (counts[e.kind] ?? 0) + 1

  return (
    <div className="space-y-4">
      <div className="text-muted-foreground flex flex-wrap gap-3 text-sm">
        共 {events.length} 条 agent 事件：
        {Object.entries(counts).map(([k, n]) => (
          <span key={k}>{KIND_META[k]?.label ?? k} ×{n}</span>
        ))}
        {data.tokens.total_tokens > 0 && (
          <span className="text-blue-700 dark:text-blue-400"
                title="本分区所有调参唤醒累计的大模型 token 用量（按 agent_wakeup 审计汇总）">
            累计 tokens：{data.tokens.total_tokens.toLocaleString()}
            （in {data.tokens.input_tokens.toLocaleString()} /
            out {data.tokens.output_tokens.toLocaleString()}）
          </span>
        )}
      </div>

      {events.length === 0 ? (
        <div className="text-muted-foreground py-8 text-center text-sm">
          还没有 agent 活动记录（未启用 agent 或尚未唤醒）。
        </div>
      ) : (
        <Card>
          <CardContent className="pt-6">
            <ol className="space-y-2">
              {events.map((e, i) => {
                const meta = KIND_META[e.kind] ?? { label: e.kind, cls: "" }
                return (
                  <li key={i} className="flex items-start gap-2 border-b pb-2 text-sm last:border-0">
                    <Badge variant="outline" className={`${meta.cls} shrink-0`}>{meta.label}</Badge>
                    <span className="text-muted-foreground shrink-0 font-mono text-xs leading-5">
                      {e.ts}
                    </span>
                    <span className="break-all leading-5">{eventBody(e)}</span>
                  </li>
                )
              })}
            </ol>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
