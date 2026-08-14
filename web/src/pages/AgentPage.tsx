import { api } from "@/lib/api"
import { useCohort } from "@/lib/cohort"
import { eventBody, KIND_META } from "@/lib/agentEvents"
import { usePolling } from "@/lib/usePolling"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import SetupPanel from "@/components/SetupPanel"

/** 调参会话的 agent 事件流（当前分区）。 */
function TuningAgentEvents() {
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

export default function AgentPage() {
  return (
    <Tabs defaultValue="tuning">
      <TabsList>
        <TabsTrigger value="tuning">调参会话</TabsTrigger>
        <TabsTrigger value="setup">配置 agent（setup）</TabsTrigger>
      </TabsList>
      <TabsContent value="tuning" className="mt-4"><TuningAgentEvents /></TabsContent>
      <TabsContent value="setup" className="mt-4"><SetupPanel /></TabsContent>
    </Tabs>
  )
}
