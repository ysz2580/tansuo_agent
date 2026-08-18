import { api } from "@/lib/api"
import { useCohort } from "@/lib/cohort"
import { groupByRounds } from "@/lib/agentEvents"
import { usePolling } from "@/lib/usePolling"
import { AgentTimeline } from "@/components/AgentTimeline"
import SetupPanel from "@/components/SetupPanel"

/** Agent 主页（默认首页）：以 agent 活动为主轴讲叙事。
 *  上节「配置 agent」= setup 会话（一生一次，SetupPanel 自管控件/日志/时间线）；
 *  下节「监督会话」= 调参期每 N 次试验唤醒一轮，按轮次呈现
 *  「看到什么 → 判断什么 → 做了什么」。 */
export default function AgentPage() {
  return (
    <div className="space-y-8">
      <section className="space-y-3">
        <h2 className="text-lg font-semibold">配置 agent（setup）</h2>
        <SetupPanel />
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">监督会话（调参期唤醒）</h2>
        <TuningAgentTimeline />
      </section>
    </div>
  )
}

/** 调参监督时间线（当前分区）：每轮唤醒一张卡。 */
function TuningAgentTimeline() {
  const cohort = useCohort()
  const { data, error } = usePolling(() => api.agentEvents(cohort), 8000)

  if (error) return <div className="text-red-600 py-8 text-center">加载失败：{error}</div>
  if (!data) return <div className="text-muted-foreground py-12 text-center">加载中…</div>

  const events = data.events
  const segments = groupByRounds(events)

  return (
    <div className="space-y-3">
      {events.length > 0 && (
        <div className="text-muted-foreground flex flex-wrap items-center gap-3 text-sm">
          <span>共 {data.tokens.rounds} 轮唤醒 · {events.length} 条 agent 事件</span>
          {data.tokens.total_tokens > 0 && (
            <span className="text-blue-700 dark:text-blue-400"
                  title="本分区所有调参唤醒累计的大模型 token 用量（按 agent_wakeup 审计汇总）">
              累计 tokens：{data.tokens.total_tokens.toLocaleString()}
              （in {data.tokens.input_tokens.toLocaleString()} /
              out {data.tokens.output_tokens.toLocaleString()}）
            </span>
          )}
        </div>
      )}
      <AgentTimeline
        segments={segments}
        emptyHint={(
          <div className="text-muted-foreground rounded-md border border-dashed py-12 text-center text-sm">
            尚无监督记录：启动搜索后，agent 每若干次试验唤醒一轮，
            这里按轮次呈现它「看到什么 → 判断什么 → 做了什么」。
          </div>
        )}
      />
    </div>
  )
}
